"""Check relationship membership using invented JSON, including awkward paths."""
from contextlib import closing
from copy import deepcopy
from dataclasses import FrozenInstanceError
from decimal import Decimal
import hashlib
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from fhir_cohort_synth.dependencies import DependencyError, iter_groups, load_rules
from fhir_cohort_synth.ingest import InputError, ingest
from fhir_cohort_synth.jsonio import dumps, loads
from fhir_cohort_synth.profiling import profile_index


REPO = Path(__file__).resolve().parents[1]


def path(*keys):
    return [["item", None] if key is None else ["key", key] for key in keys]


def select(*keys, origin="anchor"):
    return {"from": origin, "path": path(*keys)}


def rule(fields=None, *, name="example", resource_type="Widget", anchor=(), links=None):
    return {"name": name, "resource_type": resource_type, "anchor": path(*anchor),
            "fields": fields or {}, "links": links or {}}


def document(*rules):
    return {"schema_version": 1, "rules": list(rules)}


def resource(identity, kind="Widget", **fields):
    return {"resourceType": kind, "id": identity,
            "meta": {"profile": [f"http://example.invalid/{kind}"]}, **fields}


def bundle(*entries):
    return {"resourceType": "Bundle", "type": "collection", "entry": [
        {"resource": item, **({"fullUrl": url} if url else {})} for url, item in entries]}


def link(*keys, target="Widget", fields=None, origin="anchor"):
    return {"reference": select(*keys, origin=origin), "resource_type": target,
            "fields": fields or {"id": path("id")}}


def value(match):
    return loads(match["node"].scalar_json)


class DependencyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cohort = self.root / "ingested" / "cohort.sqlite"
        self.fields = self.root / "profile" / "field-occurrences.sqlite"

    def prepare(self, resources):
        source = self.root / "data.ndjson"
        source.write_text("\n".join(dumps(r) for r in resources) + "\n", encoding="utf-8")
        ingest([source], self.cohort.parent)
        profile_index(self.cohort, self.fields.parent)

    def groups(self, *rules):
        return list(iter_groups(self.cohort, self.fields, document(*rules)))

    def query(self, database, sql, parameters=()):
        with closing(sqlite3.connect(database)) as db:
            rows = db.execute(sql, parameters).fetchall()
            db.commit()
            return rows

    def test_siblings_precision_types_and_immutable_node_associations(self):
        self.prepare([resource("w", quantity={"value": Decimal("1.23000000000000000001"),
                                              "unit": "u", "flag": True, "text": "1"})])
        group, = self.groups(rule({key: select("quantity", key) for key in ("value", "unit", "flag", "text")}))
        nodes = [matches[0]["node"] for matches in group["fields"].values()]
        self.assertEqual(len({node.parent_id for node in nodes}), 1)
        self.assertEqual([node.kind for node in nodes], ["number", "string", "boolean", "string"])
        self.assertEqual(nodes[0].scalar_json, "1.23000000000000000001")
        self.assertEqual(nodes[0].resource_id, group["resource_id"])
        with self.assertRaises(FrozenInstanceError):
            nodes[0].kind = "string"

    def test_components_and_root_context_without_coding_multiplication(self):
        self.prepare([resource("o", "Observation", status="final", component=[
            {"code": {"coding": [{"system": "a", "code": "A"}, {"system": "b", "code": "AA"}]},
             "valueQuantity": {"value": 10, "unit": "u"}},
            {"code": {"coding": [{"system": "a", "code": "B"}]},
             "valueQuantity": {"value": 20, "unit": "v"}}])])
        groups = self.groups(rule({"coding": select("code", "coding", None),
                                   "value": select("valueQuantity", "value"),
                                   "unit": select("valueQuantity", "unit"),
                                   "status": select("status", origin="root")},
                                  resource_type="Observation", anchor=("component", None)))
        self.assertEqual(len(groups), 2)
        self.assertEqual([g["anchor"].array_index for g in groups], [0, 1])
        self.assertEqual([len(g["fields"]["coding"]) for g in groups], [2, 1])
        self.assertEqual([value(g["fields"]["value"][0]) for g in groups], [10, 20])
        self.assertEqual([value(g["fields"]["unit"][0]) for g in groups], ["u", "v"])
        for group in groups:
            self.assertEqual(value(group["fields"]["status"][0]), "final")
            for match in group["fields"]["coding"]:
                self.assertEqual(match["node"].path[:2], group["anchor"].path)

    def test_missing_null_and_empty_values_are_distinct(self):
        self.prepare([resource("w", null=None, text="", obj={}, array=[], scalar=False)])
        group, = self.groups(rule({key: select(key) for key in ("missing", "null", "text", "obj", "array")}))
        selected = {k: v[0] for k, v in group["fields"].items()}
        self.assertEqual(selected["missing"]["status"], "missing")
        self.assertIsNone(selected["missing"]["node"])
        self.assertEqual(selected["missing"]["context"], group["anchor"])
        self.assertEqual(selected["null"]["node"].scalar_json, "null")
        self.assertEqual(value(selected["text"]), "")
        self.assertEqual(selected["obj"]["node"].kind, "object")
        self.assertEqual(selected["array"]["node"].array_length, 0)
        self.assertTrue(all(selected[k]["status"] == "present" for k in ("null", "text", "obj", "array")))

    def test_wildcard_branches_retain_missing_empty_and_ineligible_context(self):
        self.prepare([resource("w", items=[{"x": 1}, {}, None, [], {"x": 3}], empty=[])])
        group, = self.groups(rule({"x": select("items", None, "x"), "empty": select("empty", None)}))
        matches = group["fields"]["x"]
        self.assertEqual([m["status"] for m in matches],
                         ["present", "missing", "not_applicable", "not_applicable", "present"])
        self.assertEqual([m["context"].array_index for m in matches], [0, 1, 2, 3, 4])
        self.assertEqual(matches[1]["remaining_path"], (("key", "x"),))
        self.assertEqual(matches[2]["context"].kind, "null")
        self.assertEqual(group["fields"]["empty"][0]["status"], "empty")

    def test_nested_arrays_keep_order_and_only_objects_become_anchors(self):
        self.prepare([resource("w", matrix=[[{"x": 1}, None], [], [{"x": 2}, {}]])])
        groups = self.groups(rule({"x": select("x")}, anchor=("matrix", None, None)))
        self.assertEqual(len(groups), 3)
        self.assertEqual([g["fields"]["x"][0]["status"] for g in groups], ["present", "present", "missing"])
        self.assertEqual([g["anchor"].path[1][1] for g in groups], [0, 2, 2])

    def test_literal_field_names_do_not_collide_with_path_syntax(self):
        self.prepare([resource("w", **{"a.b": 1, "a": {"b": 2}, "x[*]": 3, "x": [4], "": {"*": 5}})])
        group, = self.groups(rule({"literal": select("a.b"), "nested": select("a", "b"),
                                  "brackets": select("x[*]"), "items": select("x", None), "empty": select("", "*")}))
        self.assertEqual([value(matches[0]) for matches in group["fields"].values()], [1, 2, 3, 4, 5])

    def test_missing_anchor_or_resource_type_produces_no_groups(self):
        self.prepare([resource("w", obj=None)])
        self.assertEqual(self.groups(rule(anchor=("absent",))), [])
        self.assertEqual(self.groups(rule(anchor=("obj",))), [])
        self.assertEqual(self.groups(rule(resource_type="Unknown")), [])
        self.assertEqual(self.groups(), [])

    def test_direct_links_deduplicate_occurrences_and_repeated_target_slots(self):
        target = resource("target", extra={"class": "demo"})
        parent = resource("parent", items=[{"x": 1}], related=[{"reference": "Widget/target"}, {"reference": "Widget/target"}])
        self.prepare([parent, target, parent, target])
        groups = self.groups(rule({"x": select("x")}, anchor=("items", None),
                                 links={"related": link("related", None, origin="root", fields={"class": path("extra", "class")})}))
        self.assertEqual(len(groups), 1)
        related = groups[0]["links"]["related"]
        self.assertEqual([r["status"] for r in related["references"]], ["resolved", "resolved"])
        self.assertEqual(len(related["targets"]), 1)
        self.assertEqual(value(related["targets"][0]["fields"]["class"][0]), "demo")

    def test_missing_unresolved_wrong_type_and_logical_links(self):
        self.prepare([resource("w", links={"missingTarget": {"reference": "Widget/no"},
                                           "wrongType": {"reference": "Gadget/g"},
                                           "logical": {"identifier": {"system": "demo", "value": "x"}},
                                           "null": None, "empty": []}), resource("g", "Gadget")])
        group, = self.groups(rule(links={key: link("links", key) for key in ("absent", "missingTarget", "wrongType", "logical", "null")}))
        statuses = {key: data["references"][0]["status"] for key, data in group["links"].items()}
        self.assertEqual(statuses, {"absent": "missing", "missingTarget": "unresolved", "wrongType": "wrong_type",
                                    "logical": "unresolved", "null": "unresolved"})
        self.assertTrue(all(not data["targets"] for data in group["links"].values()))
        self.assertEqual(group["links"]["null"]["references"][0]["selection"]["node"].kind, "null")

    def test_reference_paths_with_literal_dots_match_the_correct_edge(self):
        self.prepare([resource("parent", **{"a.b": {"reference": "Gadget/one"}, "a": {"b": {"reference": "Gadget/two"}}}),
                      resource("one", "Gadget"), resource("two", "Gadget")])
        group, = self.groups(rule(links={"literal": link("a.b", target="Gadget"), "nested": link("a", "b", target="Gadget")}))
        self.assertEqual(value(group["links"]["literal"]["targets"][0]["fields"]["id"][0]), "one")
        self.assertEqual(value(group["links"]["nested"]["targets"][0]["fields"]["id"][0]), "two")

    def test_contained_targets_keep_root_node_identity_and_ownership(self):
        first = resource("first", contained=[resource("local", "Gadget", value=1)], related={"reference": "#local"})
        second = resource("second", contained=[resource("local", "Gadget", value=2)], related={"reference": "#local"})
        self.prepare([first, second, first])
        groups = self.groups(rule(links={"local": link("related", target="Gadget", fields={"value": path("value")})}))
        self.assertEqual(len(groups), 2)
        target_ids = []
        for expected, group in enumerate(groups, 1):
            target, = group["links"]["local"]["targets"]
            target_ids.append(target["resource_id"])
            self.assertEqual(value(target["fields"]["value"][0]), expected)
            self.assertEqual(target["node"].resource_id, group["resource_id"])
            self.assertEqual(target["node"].path, (("key", "contained"), ("index", 0)))
            self.assertNotEqual(target["resource_id"], target["node"].resource_id)
        self.assertEqual(len(set(target_ids)), 2)

    def test_references_from_contained_anchors_use_the_contained_owner(self):
        self.prepare([resource("w", label="outer", contained=[resource("child", "Gadget", related={"reference": "#"})])])
        group, = self.groups(rule({"root_label": select("label", origin="root")}, anchor=("contained", None),
                                 links={"owner": link("related", fields={"label": path("label")})}))
        target, = group["links"]["owner"]["targets"]
        self.assertEqual(target["resource_id"], group["resource_id"])
        self.assertEqual(value(target["fields"]["label"][0]), "outer")

    def test_duplicate_payload_with_different_bundle_targets_is_ambiguous(self):
        parent = resource("w", related={"reference": "Gadget/g"})
        # Each occurrence resolves uniquely in its Bundle. At the deduplicated
        # resource level the targets disagree, even though ingestion completed.
        self.prepare([bundle((None, parent), ("urn:uuid:one", resource("g", "Gadget"))),
                      bundle((None, parent), ("urn:uuid:two", resource("g", "Gadget")))])
        group, = self.groups(rule(links={"related": link("related", target="Gadget")}))
        self.assertEqual(group["links"]["related"]["references"][0]["status"], "ambiguous")
        self.assertEqual(group["links"]["related"]["targets"], [])

    def test_mixed_resolved_and_unresolved_occurrences_do_not_choose_a_target(self):
        parent = resource("w", related={"reference": "Gadget/g/_history/1"})
        one = resource("g", "Gadget")
        one["meta"]["versionId"] = "1"
        two = resource("g", "Gadget")
        two["meta"]["versionId"] = "2"
        self.prepare([bundle((None, parent), ("urn:uuid:one", one)),
                      bundle((None, parent), ("urn:uuid:two", two))])
        group, = self.groups(rule(links={"related": link("related", target="Gadget")}))
        self.assertEqual(group["links"]["related"]["references"][0]["status"], "unresolved")
        self.assertEqual(group["links"]["related"]["targets"], [])

    def test_direct_links_do_not_follow_the_targets_outgoing_references(self):
        self.prepare([resource("w", related={"reference": "Gadget/g"}),
                      resource("g", "Gadget", next={"reference": "Gadget/another"}), resource("another", "Gadget")])
        group, = self.groups(rule(links={"related": link("related", target="Gadget", fields={"next": path("next")})}))
        target, = group["links"]["related"]["targets"]
        self.assertEqual(target["fields"]["next"][0]["node"].kind, "object")
        self.assertNotIn("links", target)

    def test_wrong_database_pair_rejected_even_when_ids_and_counts_match(self):
        self.prepare([resource("w", value=1)])
        other = self.root / "other.ndjson"
        other.write_text(dumps(resource("w", value=2)), encoding="utf-8")
        other_out = self.root / "other"
        ingest([other], other_out)
        with self.assertRaisesRegex(DependencyError, "do not match"):
            list(iter_groups(other_out / "cohort.sqlite", self.fields, document(rule())))

    def test_occurrence_context_mismatch_is_rejected(self):
        self.prepare([resource("w")])
        self.query(self.cohort, "UPDATE occurrences SET context='different'")
        with self.assertRaisesRegex(DependencyError, "do not match"):
            self.groups(rule())

    def test_incomplete_unsupported_and_missing_inputs_are_rejected(self):
        self.prepare([resource("w")])
        for database, column, changed, original in [
            (self.fields, "status", "in_progress", "completed"),
            (self.fields, "schema_version", 99, 1),
            (self.cohort, "status", "interrupted", "completed"),
            (self.cohort, "schema_version", 99, 1),
        ]:
            old = self.query(database, f"SELECT {column} FROM run")[0][0]
            self.query(database, f"UPDATE run SET {column}=?", (changed,))
            with self.subTest(database=database.name, column=column), self.assertRaises(InputError):
                self.groups(rule())
            self.query(database, f"UPDATE run SET {column}=?", (old,))
        missing = self.root / "missing.sqlite"
        with self.assertRaises(InputError):
            list(iter_groups(self.cohort, missing, document(rule())))
        self.assertFalse(missing.exists())

    def test_input_copies_are_accepted_and_reads_do_not_modify_files(self):
        self.prepare([resource("w")])
        moved = self.root / "copied.sqlite"
        shutil.copyfile(self.cohort, moved)
        files = (moved, self.fields)
        before = [(hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in files]
        listing = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))
        with closing(iter_groups(moved, self.fields, document(rule()))) as groups:
            self.assertEqual(next(groups)["rule"], "example")
        after = [(hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in files]
        self.assertEqual(before, after)
        self.assertEqual(listing, sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*")))

    def test_rule_file_loader_and_invalid_configurations(self):
        config = document(rule({"value": select("value")}))
        rules_path = self.root / "rules.json"
        rules_path.write_text(dumps(config), encoding="utf-8")
        self.assertEqual(load_rules(rules_path), config)
        invalid = [None, [], {"schema_version": 2, "rules": []}, {"schema_version": True, "rules": []},
                   {"schema_version": 1, "rules": {}}, document(rule(), rule())]
        for change in ({"anchor": "a.b"}, {"anchor": [["index", 0]]}, {"anchor": [["item", 0]]},
                       {"resource_type": ""}, {"unexpected": True}, {"name": []},
                       {"fields": {"x": {"path": [], "from": "anything"}}},
                       {"fields": {"x": {"path": [["key", 3]]}}},
                       {"links": {"x": {"reference": {"path": []}, "resource_type": "Widget", "fields": {}, "links": {}}}}):
            bad = deepcopy(config)
            bad["rules"][0].update(change)
            invalid.append(bad)
        for bad in invalid:
            with self.subTest(config=bad), self.assertRaises(DependencyError):
                list(iter_groups("unused", "unused", bad))
        for text in ('{"schema_version":1,"schema_version":1,"rules":[]}', '{invalid', '{"schema_version":NaN,"rules":[]}'):
            rules_path.write_text(text, encoding="utf-8")
            with self.assertRaises(DependencyError):
                load_rules(rules_path)

    def test_example_rules_end_to_end_on_invented_fhir_bundle(self):
        ingest([REPO / "examples/mii-demo-bundle.json"], self.cohort.parent)
        profile_index(self.cohort, self.fields.parent)
        rules = load_rules(REPO / "examples/dependency-rules.json")
        groups = list(iter_groups(self.cohort, self.fields, rules))
        quantity = [g for g in groups if g["rule"] == "observation_quantity"]
        components = [g for g in groups if g["rule"] == "observation_component"]
        linked = [g for g in groups if g["rule"] == "observation_encounter"]
        self.assertEqual(len(quantity), self.query(self.cohort, "SELECT count(*) FROM resources WHERE resource_type='Observation'")[0][0])
        self.assertEqual(len(components), 2)
        self.assertEqual([value(g["fields"]["value"][0]) for g in components], [120, 80])
        self.assertTrue(linked)
        resolved = [g for g in linked if g["links"]["encounter"]["targets"]]
        self.assertTrue(resolved)
        for group in resolved:
            target, = group["links"]["encounter"]["targets"]
            self.assertEqual(target["fields"]["class"][0]["status"], "present")
            self.assertEqual(target["fields"]["period"][0]["status"], "present")


if __name__ == "__main__":
    unittest.main()
