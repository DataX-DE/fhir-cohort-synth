"""Hand-calculated conditional distributions on invented, local JSON only."""
from contextlib import closing, redirect_stdout, redirect_stderr
from copy import deepcopy
from decimal import Decimal
import hashlib
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fhir_cohort_synth import conditional_statistics as conditional
from fhir_cohort_synth.cli import main
from fhir_cohort_synth.dependencies import DependencyError, load_rules
from fhir_cohort_synth.ingest import InputError, ingest
from fhir_cohort_synth.jsonio import dumps, loads
from fhir_cohort_synth.profiling import profile_index


REPO = Path(__file__).resolve().parents[1]


def path(*keys):
    return [["item", None] if key is None else ["key", key] for key in keys]


def selector(*keys):
    return {"path": path(*keys)}


def resource(identity, kind="Observation", **fields):
    return {"resourceType": kind, "id": identity,
            "meta": {"profile": [f"https://example.invalid/{kind}"]}, **fields}


def bundle(*entries):
    return {"resourceType": "Bundle", "type": "collection", "entry": [
        {"resource": item, **({"fullUrl": url} if url else {})} for url, item in entries]}


def rules(*, fields=None, given=None, targets=None, anchor=(), links=None, resource_type="Observation"):
    return {"schema_version": 1, "rules": [{
        "name": "example", "resource_type": resource_type, "anchor": path(*anchor),
        "fields": fields if fields is not None else {"category": selector("category"), "x": selector("x")},
        "links": links or {},
        "statistics": {"given": given if given is not None else [{"field": "category"}],
                       "targets": targets if targets is not None else [{"field": "x"}]},
    }]}


def token(outcome, index=0):
    return loads(outcome)[index][0]["value_json"]


class ConditionalTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cohort = self.root / "ingested" / "cohort.sqlite"
        self.fields = self.root / "profile" / "field-occurrences.sqlite"
        self.output = self.root / "conditional"

    def prepare(self, resources):
        source = self.root / "resources.ndjson"
        source.write_text("\n".join(dumps(r) for r in resources) + "\n", encoding="utf-8")
        ingest([source], self.cohort.parent)
        profile_index(self.cohort, self.fields.parent)

    def run_stats(self, specification=None):
        header = conditional.profile_conditional(self.cohort, self.fields, specification or rules(), self.output)
        self.db = sqlite3.connect(self.output / "conditional-statistics.sqlite")
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.report = loads((self.output / "conditional-statistics.json").read_text())
        return header

    def test_exact_conditional_probabilities_and_second_context(self):
        self.prepare([resource(f"o{i}", category=category, x=x) for i, (category, x) in enumerate(
            [("A", 10), ("A", 10), ("A", 20), ("B", 90), ("B", 90)])])
        self.run_stats()
        rows = self.db.execute("SELECT c.context_json,p.* FROM conditional_probabilities p JOIN contexts c ON c.id=p.context_id")
        actual = {(loads(token(row["context_json"])), loads(token(row["outcome_json"]))):
                  (row["numerator"], row["denominator"], row["probability"]) for row in rows}
        self.assertEqual(actual["A", 10][:2], (2, 3))
        self.assertAlmostEqual(actual["A", 10][2], 2 / 3)
        self.assertAlmostEqual(actual["A", 20][2], 1 / 3)
        self.assertEqual(actual["B", 90], (2, 2, 1.0))
        self.assertEqual(self.report["counts"]["groups_seen"], 5)
        self.assertEqual(self.report["counts"]["contexts"], 2)
        numbers = next(c["numbers"][0] for c in self.report["contexts"] if c["group_count"] == 3)
        self.assertEqual(numbers["sample_count"], 3)
        self.assertEqual(numbers["minimum"], 10)
        self.assertEqual(numbers["maximum"], 20)
        self.assertEqual(numbers["quantiles"], {"p05": 10, "p25": 10, "p50": 10, "p75": 20, "p95": 20})

    def test_multiple_targets_are_joint_not_independent(self):
        self.prepare([resource("a", x=1, y="left"), resource("b", x=2, y="right")])
        self.run_stats(rules(fields={"x": selector("x"), "y": selector("y")}, given=[],
                             targets=[{"field": "x"}, {"field": "y"}]))
        outcomes = {(token(row[0]), token(row[0], 1)): row[1] for row in self.db.execute("SELECT outcome_json,frequency FROM outcome_frequencies")}
        self.assertEqual(outcomes, {("1", '"left"'): 1, ("2", '"right"'): 1})
        self.assertEqual(self.db.execute("SELECT context_json FROM contexts").fetchone()[0], "[]")

    def test_equal_selected_content_ignores_source_identifiers_and_unselected_data(self):
        self.prepare([resource("one", category="A", x=10, unused="first"),
                      resource("two", category="A", x=10, unused="second")])
        self.run_stats()
        self.assertEqual(self.db.execute("SELECT frequency FROM outcome_frequencies").fetchone()[0], 2)
        self.assertEqual(self.report["contexts"][0]["resource_count"], 2)
        context = self.db.execute("SELECT context_json FROM contexts").fetchone()[0]
        self.assertNotIn("resource_id", context)
        self.assertNotIn("node_id", context)

    def test_components_multiple_codings_and_changed_anchor_positions(self):
        a = {"coding": [{"system": "s", "code": "A"}, {"system": "t", "code": "AA"}], "x": 10}
        b = {"coding": [{"system": "s", "code": "B"}], "x": 20}
        self.prepare([resource("one", parts=[a, b]), resource("two", parts=[b, a])])
        self.run_stats(rules(anchor=("parts", None), fields={"category": selector("coding", None), "x": selector("x")}))
        self.assertEqual(self.report["counts"]["groups_included"], 4)
        self.assertEqual(self.report["counts"]["contexts"], 2)
        self.assertEqual([r[0] for r in self.db.execute("SELECT frequency FROM outcome_frequencies")], [2, 2])
        for context in self.report["contexts"]:
            self.assertEqual(context["group_count"], 2)
            self.assertEqual(context["numbers"][0]["sample_count"], 2)

    def test_nested_container_content_order_types_and_numeric_precision(self):
        nested = {"b": [Decimal("1.00000000000000000001"), True, "1", None, {}, []], "a.b[*]": {"": "é"}}
        reordered = {"a.b[*]": {"": "é"}, "b": list(nested["b"])}
        changed = {**nested, "b": list(reversed(nested["b"]))}
        self.prepare([resource("one", x=nested), resource("two", x=reordered), resource("three", x=changed)])
        self.run_stats(rules(given=[]))
        counts = {token(row[0]): row[1] for row in self.db.execute("SELECT outcome_json,frequency FROM outcome_frequencies")}
        self.assertEqual(counts, {dumps(nested): 2, dumps(changed): 1})
        self.assertEqual(self.report["contexts"][0]["numbers"], [])

    def test_nested_wildcards_do_not_flatten_array_boundaries(self):
        self.prepare([resource("one", matrix=[[1], [2]]), resource("two", matrix=[[1, 2]])])
        self.run_stats(rules(fields={"x": selector("matrix", None, None)}, given=[]))
        self.assertEqual(self.db.execute("SELECT count(*) FROM outcome_frequencies").fetchone()[0], 2)
        self.assertEqual(self.report["contexts"][0]["numbers"], [])

    def test_missing_null_empty_and_ineligible_selections_stay_distinct(self):
        self.prepare([resource("missing"), resource("null", x=None), resource("text", x=""),
                      resource("object", x={}), resource("array", x=[]), resource("false", x=False)])
        self.run_stats(rules(given=[]))
        matches = [loads(row[0])[0][0] for row in self.db.execute("SELECT outcome_json FROM outcome_frequencies")]
        self.assertEqual(len(matches), 6)
        self.assertEqual(sum(m["status"] == "missing" for m in matches), 1)
        self.assertEqual({m.get("value_json") for m in matches}, {None, "null", '""', "{}", "[]", "false"})

    def test_missing_parent_does_not_become_missing_child(self):
        self.prepare([resource("a"), resource("b", x={}), resource("c", x=None), resource("d", x=[])])
        self.run_stats(rules(fields={"x": selector("x", "child")}, given=[]))
        matches = [loads(row[0])[0][0] for row in self.db.execute("SELECT outcome_json FROM outcome_frequencies")]
        self.assertEqual(len(matches), 4)
        missing = [m for m in matches if m["status"] == "missing"]
        self.assertEqual(sorted(len(m["remaining_path"]) for m in missing), [1, 2])
        self.assertEqual({m["context_kind"] for m in matches if m["status"] == "not_applicable"}, {"null", "array"})

    def test_precision_boolean_string_and_number_tokens(self):
        values = [Decimal("0.123456789012345678901"), Decimal("0.123456789012345678902"), 1, Decimal("1.0"), True, "1"]
        self.prepare([resource(f"o{i}", x=x) for i, x in enumerate(values)])
        self.run_stats(rules(given=[]))
        self.assertEqual(self.db.execute("SELECT count(*) FROM outcome_frequencies").fetchone()[0], 6)
        numeric = self.db.execute("SELECT * FROM numeric_summaries").fetchone()
        self.assertEqual(numeric["sample_count"], 4)
        self.assertEqual(numeric["minimum_json"], "0.123456789012345678901")
        self.assertEqual(numeric["maximum_json"], "1.0")
        self.assertEqual(loads(numeric["quantiles_json"])["p50"], values[1])
        self.assertEqual({r[0] for r in self.db.execute("SELECT typeof(value_json) FROM numeric_frequencies")}, {"text"})

    def test_patient_resource_and_unassigned_support_are_separate(self):
        p1, p2 = resource("p1", "Patient"), resource("p2", "Patient")
        one = resource("one", subject={"reference": "Patient/p1"}, parts=[{"x": 10}, {"x": 10}])
        two = resource("two", subject={"reference": "Patient/p1"}, parts=[{"x": 10}])
        three = resource("three", subject={"reference": "Patient/p2"}, parts=[{"x": 10}])
        unknown = resource("unknown", parts=[{"x": 10}, {"x": 10}])
        self.prepare([p1, p2, one, two, three, unknown, one])
        self.run_stats(rules(fields={"x": selector("x")}, given=[], anchor=("parts", None)))
        context, = self.report["contexts"]
        self.assertEqual((context["group_count"], context["resource_count"], context["patient_count"]), (6, 4, 2))
        self.assertEqual((context["unassigned_group_count"], context["unassigned_resource_count"]), (2, 1))
        self.assertEqual(context["numbers"][0]["sample_count"], 6)

    def test_linked_fields_and_all_exclusion_categories(self):
        def observation(identity, *references):
            return resource(identity, x=10, related=[{"reference": ref} for ref in references])
        ambiguous = observation("ambiguous", "Encounter/e")
        self.prepare([
            resource("e1", "Encounter", **{"class": {"code": "demo"}}),
            resource("e2", "Encounter", **{"class": {"code": "demo"}}), resource("g", "Gadget"),
            observation("good", "Encounter/e1"), observation("repeat", "Encounter/e1", "Encounter/e1"),
            resource("missing", x=10), observation("unresolved", "Encounter/no"), observation("wrong", "Gadget/g"),
            observation("multiple", "Encounter/e1", "Encounter/e2"), observation("mixed", "Encounter/e1", "Encounter/no"),
            bundle((None, ambiguous), ("urn:uuid:first", resource("e", "Encounter"))),
            bundle((None, ambiguous), ("urn:uuid:second", resource("e", "Encounter"))),
        ])
        specification = rules(given=[{"link": "encounter", "field": "class"}], links={"encounter": {
            "reference": selector("related", None), "resource_type": "Encounter", "fields": {"class": path("class")}}})
        self.run_stats(specification)
        self.assertEqual(self.report["counts"]["groups_included"], 2)
        self.assertEqual(self.report["counts"]["groups_excluded"], 6)
        self.assertEqual(self.report["status"], "completed_with_warnings")
        reason_counts = {}
        for row in self.report["rules"][0]["exclusions"]:
            for failure in row["reasons"]:
                for reason in failure["reasons"]:
                    reason_counts[reason] = reason_counts.get(reason, 0) + row["groups"]
        self.assertEqual(reason_counts, {"missing": 1, "unresolved": 2, "wrong_type": 1, "multiple_targets": 1, "ambiguous": 1})
        self.assertEqual(sum(r["groups"] for r in self.report["rules"][0]["exclusions"]), 6)
        self.assertEqual(self.report["contexts"][0]["group_count"], 2)

    def test_multiple_failed_links_exclude_one_group_and_unused_links_do_not_exclude(self):
        self.prepare([resource("one", x=10)])
        bad = {name: {"reference": selector(name), "resource_type": "Encounter", "fields": {"class": path("class")}}
               for name in ("a", "b")}
        specification = rules(given=[], links=bad)
        other = deepcopy(specification["rules"][0])
        other["name"] = "uses_links"
        other["statistics"]["given"] = [{"link": name, "field": "class"} for name in bad]
        specification["rules"].append(other)
        self.run_stats(specification)
        self.assertEqual(self.report["counts"]["groups_included"], 1)
        self.assertEqual(self.report["counts"]["groups_excluded"], 1)
        excluded = next(r for r in self.report["rules"] if r["name"] == "uses_links")
        self.assertEqual(len(excluded["exclusions"][0]["reasons"]), 2)

    def test_contained_target_values_and_linked_numeric_outcomes(self):
        self.prepare([resource("one", related={"reference": "#local"}, contained=[resource("local", "Gadget", x=Decimal("1.20"))])])
        self.run_stats(rules(given=[], targets=[{"link": "contained", "field": "x"}], links={"contained": {
            "reference": selector("related"), "resource_type": "Gadget", "fields": {"x": path("x")}}}))
        self.assertEqual(self.db.execute("SELECT value_json FROM numeric_frequencies").fetchone()[0], "1.20")
        self.assertEqual(self.report["counts"]["groups_seen"], 1)

    def test_rules_without_statistics_are_skipped_and_zero_matches_are_valid(self):
        self.prepare([resource("one", x=10)])
        specification = rules(resource_type="NeverObserved")
        skipped = deepcopy(rules()["rules"][0])
        del skipped["statistics"]
        skipped["name"] = "skipped"
        specification["rules"].append(skipped)
        self.run_stats(specification)
        self.assertEqual(self.report["skipped_rules"], ["skipped"])
        self.assertEqual(self.report["counts"], {"rules": 1, "groups_seen": 0, "groups_included": 0, "groups_excluded": 0, "contexts": 0})
        self.assertEqual(self.report["rules"][0]["groups_seen"], 0)

    def test_invalid_statistics_fail_before_output_creation(self):
        changes = [None, {}, {"given": [], "targets": []}, {"given": "x", "targets": [{"field": "x"}]},
                   {"given": [{"field": "absent"}], "targets": [{"field": "x"}]},
                   {"given": [{"link": "unknown", "field": "x"}], "targets": [{"field": "x"}]},
                   {"given": [], "targets": [{"field": "x", "path": []}]}]
        for change in changes:
            specification = rules()
            specification["rules"][0]["statistics"] = change
            with self.subTest(change=change), self.assertRaises(DependencyError):
                conditional.profile_conditional(self.cohort, self.fields, specification, self.output)
            self.assertFalse(self.output.exists())
        specification = rules()
        del specification["rules"][0]["statistics"]
        with self.assertRaisesRegex(DependencyError, "No statistics"):
            conditional.profile_conditional(self.cohort, self.fields, specification, self.output)

    def test_high_cardinality_frequencies_are_complete(self):
        self.prepare([resource(f"o{i}", x=f"category-{i}") for i in range(1100)])
        self.run_stats(rules(given=[]))
        self.assertEqual(self.db.execute("SELECT count(*),sum(frequency) FROM outcome_frequencies").fetchone()[:], (1100, 1100))
        self.assertEqual(self.report["contexts"][0]["distinct_joint_outcomes"], 1100)
        self.assertEqual(self.report["contexts"][0]["resource_count"], 1100)
        self.assertEqual(self.db.execute("PRAGMA quick_check").fetchone()[0], "ok")
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_reports_and_distributions_are_independent_of_resource_order(self):
        resources = [resource("a", category="B", x=Decimal("1.00")), resource("b", category="A", x=2),
                     resource("c", category="B", x=1)]
        self.prepare(resources)
        self.run_stats()
        original = self.report
        other = self.root / "reordered.ndjson"
        other.write_text("\n".join(dumps(r) for r in reversed(resources)), encoding="utf-8")
        other_ingested, other_fields, other_output = (self.root / name for name in ("other-source", "other-fields", "other-result"))
        ingest([other], other_ingested)
        profile_index(other_ingested / "cohort.sqlite", other_fields)
        conditional.profile_conditional(other_ingested / "cohort.sqlite", other_fields / "field-occurrences.sqlite", rules(), other_output)
        self.assertEqual(original, loads((other_output / "conditional-statistics.json").read_text()))

    def test_read_only_inputs_output_permissions_and_no_overwrite(self):
        self.prepare([resource("one", category="source-secret-category", x="source-secret-result")])
        inputs = (self.cohort, self.fields)
        before = [(hashlib.sha256(p.read_bytes()).digest(), p.stat().st_mtime_ns) for p in inputs]
        directories = {p.parent: sorted(x.name for x in p.parent.iterdir()) for p in inputs}
        self.run_stats()
        self.assertEqual(before, [(hashlib.sha256(p.read_bytes()).digest(), p.stat().st_mtime_ns) for p in inputs])
        self.assertEqual(directories, {p.parent: sorted(x.name for x in p.parent.iterdir()) for p in inputs})
        self.assertNotIn("source-secret", (self.output / "conditional-statistics.json").read_text())
        self.assertEqual(sorted(p.name for p in self.output.iterdir()), ["conditional-statistics.json", "conditional-statistics.sqlite"])
        if os.name == "posix":
            self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)
            for p in self.output.iterdir():
                self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(DependencyError, "already exists"):
            conditional.profile_conditional(self.cohort, self.fields, rules(), self.output)

    def test_incomplete_and_mismatched_databases_rejected_before_output(self):
        self.prepare([resource("one", x=10)])
        with closing(sqlite3.connect(self.fields)) as db:
            db.execute("UPDATE run SET status='interrupted'")
            db.commit()
        with self.assertRaises(InputError):
            conditional.profile_conditional(self.cohort, self.fields, rules(), self.output)
        self.assertFalse(self.output.exists())
        with closing(sqlite3.connect(self.fields)) as db:
            db.execute("UPDATE run SET status='completed'")
            db.execute("UPDATE source_resources SET digest='different'")
            db.commit()
        with self.assertRaises(InputError):
            conditional.profile_conditional(self.cohort, self.fields, rules(), self.output)
        self.assertFalse(self.output.exists())

    def test_interruption_marks_partial_run_and_requires_new_destination(self):
        self.prepare([resource("one", x=10), resource("two", x=20)])
        real = conditional._count_group
        calls = []
        def stop(db, group, *args):
            calls.append(group)
            if len(calls) == 2:
                raise KeyboardInterrupt()
            return real(db, group, *args)
        with patch.object(conditional, "_count_group", side_effect=stop), patch.object(conditional, "BATCH_SIZE", 1):
            with self.assertRaises(KeyboardInterrupt):
                conditional.profile_conditional(self.cohort, self.fields, rules(), self.output)
        with closing(sqlite3.connect(self.output / "conditional-statistics.sqlite")) as db:
            self.assertEqual(db.execute("SELECT status FROM run").fetchone()[0], "interrupted")
            self.assertEqual(db.execute("SELECT sum(frequency) FROM outcome_frequencies").fetchone()[0], 1)
        self.assertFalse((self.output / "conditional-statistics.json").exists())
        with self.assertRaises(InputError):
            conditional.profile_conditional(self.cohort, self.fields, rules(), self.output)

    def test_report_failure_does_not_mark_complete(self):
        self.prepare([resource("one", x=10)])
        with patch.object(conditional, "_write_report", side_effect=OSError("source-secret-value")):
            with self.assertRaises(OSError):
                conditional.profile_conditional(self.cohort, self.fields, rules(), self.output)
        with closing(sqlite3.connect(self.output / "conditional-statistics.sqlite")) as db:
            self.assertEqual(db.execute("SELECT status FROM run").fetchone()[0], "failed")
        self.assertFalse((self.output / "conditional-statistics.json").exists())

    def test_cli_success_and_safe_errors(self):
        self.prepare([resource("one", x="source-secret-value")])
        config = self.root / "rules.json"
        config.write_text(dumps(rules()), encoding="utf-8")
        args = ["profile-conditional", "--input", str(self.cohort), "--fields", str(self.fields),
                "--rules", str(config), "--output", str(self.output)]
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(main(args), 0)
        self.assertIn("1 groups included", stdout.getvalue())
        self.assertNotIn("source-secret", stdout.getvalue() + stderr.getvalue())
        for error, code in [(RuntimeError("source-secret-value"), 2), (KeyboardInterrupt(), 130)]:
            stderr = io.StringIO()
            with patch("fhir_cohort_synth.cli.profile_conditional", side_effect=error), redirect_stderr(stderr):
                self.assertEqual(main(args), code)
            self.assertNotIn("source-secret", stderr.getvalue())

    def test_invented_fhir_example_end_to_end(self):
        ingest([REPO / "examples/mii-demo-bundle.json"], self.cohort.parent)
        profile_index(self.cohort, self.fields.parent)
        self.run_stats(load_rules(REPO / "examples/dependency-rules.json"))
        by_name = {r["name"]: r for r in self.report["rules"]}
        self.assertEqual(by_name["observation_component"]["groups_included"], 2)
        self.assertGreater(by_name["observation_quantity"]["groups_included"], 0)
        self.assertGreater(by_name["observation_encounter"]["groups_included"], 0)
        for row in self.db.execute("SELECT c.id,c.group_count,sum(f.frequency) FROM contexts c JOIN outcome_frequencies f ON f.context_id=c.id GROUP BY c.id"):
            self.assertEqual(row[1], row[2])
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
