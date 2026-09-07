"""Verify complete extraction and distributions using only invented JSON.

The tests deliberately use unfamiliar fields instead of relying on clinical
extractors. Hand-calculated populations distinguish per-parent, per-element
and per-resource statistics. All databases and outputs use temporary folders.
"""
import contextlib
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fhir_cohort_synth.cli import main
from fhir_cohort_synth.field_store import FieldStore
from fhir_cohort_synth.ingest import ingest
from fhir_cohort_synth.json_fields import path_json, walk_json
from fhir_cohort_synth.jsonio import dumps, loads
from fhir_cohort_synth.profiling import ProfileError, open_source, profile_index, write_report


def patient(identity, **fields):
    """Known root type with arbitrary nested test fields and no missing profile."""
    return {"resourceType": "Patient", "id": identity,
            "meta": {"profile": ["http://hl7.org/fhir/StructureDefinition/Patient"]}, **fields}


def normalized(*segments):
    """None stands for an array item; every string stands for an object key."""
    return path_json(tuple(("item", None) if item is None else ("key", item) for item in segments))


def reconstruct(rows):
    """Test helper: recover parsed JSON exclusively from nodes and parent links."""
    values, root = {}, None
    for row in rows:
        value = {} if row["kind"] == "object" else [] if row["kind"] == "array" else loads(row["scalar_json"])
        values[row["id"]] = value
        if row["parent_id"] is None:
            root = value
        else:
            parent = values[row["parent_id"]]
            segment = loads(row["concrete_path"])[-1]
            if segment[0] == "key":
                parent[segment[1]] = value
            else:
                # The source array order must agree with traversal order.
                if segment[1] != len(parent):
                    raise AssertionError("Lost array order")
                parent.append(value)
    return root


class WalkerTests(unittest.TestCase):
    def test_walks_all_types_and_empty_containers(self):
        nodes = list(walk_json({"a": [None, True, 1, "1", {}, []]}))
        self.assertEqual([n.kind for n in nodes], ["object", "array", "null", "boolean", "number", "string", "object", "array"])
        self.assertEqual(nodes[1].array_length, 6)
        self.assertEqual(nodes[-1].array_types_json, "[]")
        self.assertEqual(nodes[-2].object_keys_json, "[]")

    def test_parent_and_normalized_paths_preserve_array_membership(self):
        nodes = list(walk_json({"parts": [{"code": "A", "value": 1}, {"code": "B", "value": 2}]}))
        codes = [n for n in nodes if n.path and n.path[-1] == ("key", "code")]
        values = [n for n in nodes if n.path and n.path[-1] == ("key", "value")]
        self.assertEqual(codes[0].parent_ordinal, values[0].parent_ordinal)
        self.assertEqual(codes[1].parent_ordinal, values[1].parent_ordinal)
        self.assertNotEqual(codes[0].parent_ordinal, codes[1].parent_ordinal)
        self.assertEqual(codes[0].statistical_path, codes[1].statistical_path)
        self.assertNotEqual(codes[0].path, codes[1].path)

    def test_path_tokens_do_not_collide_with_literal_keys(self):
        nodes = list(walk_json({"a.b": 1, "a": {"b": 2}, "x[*]": 3,
                               "x": [4], "": {"0": 5}, "*": 6, 'q"[\\]': 7}))
        concrete = [path_json(n.path) for n in nodes]
        self.assertEqual(len(concrete), len(set(concrete)))
        leaves = {path_json(n.statistical_path): n.scalar_json for n in nodes if n.kind == "number"}
        self.assertEqual(leaves[normalized("a.b")], "1")
        self.assertEqual(leaves[normalized("a", "b")], "2")
        self.assertEqual(leaves[normalized("x[*]")], "3")
        self.assertEqual(leaves[normalized("x", None)], "4")

    def test_scalar_root_and_invalid_python_values(self):
        self.assertEqual(list(walk_json(Decimal("4.20")))[0].scalar_json, "4.20")
        for value in [float("nan"), object(), {1: "invalid key"}]:
            with self.assertRaises(ValueError):
                list(walk_json(value))


class ProfilingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root / "input"
        self.input.mkdir()
        self.ingested = self.root / "ingested"
        self.output = self.root / "profile"

    def prepare(self, resources):
        path = self.input / "resources.ndjson"
        path.write_text("\n".join(dumps(r) for r in resources) + "\n", encoding="utf-8")
        ingest([self.input], self.ingested)
        return self.ingested / "cohort.sqlite"

    def run_profile(self, resources):
        source = self.prepare(resources)
        report = profile_index(source, self.output)
        self.db = sqlite3.connect(self.output / "field-occurrences.sqlite")
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.statistics = loads((self.output / "field-statistics.json").read_text())
        self.inventory = loads((self.output / "field-inventory.json").read_text())
        return report

    def fid(self, *segments, resource_type="Patient"):
        return self.db.execute("SELECT id FROM fields WHERE resource_type=? AND path=?", (resource_type, normalized(*segments))).fetchone()[0]

    def field(self, *segments, resource_type="Patient"):
        return next(f for f in self.statistics["fields"] if f["resource_type"] == resource_type and dumps(f["path"]) == normalized(*segments))

    def test_round_trip_complete_resource_from_nodes(self):
        value = patient("p", **{"unfamiliar": [{"amount": Decimal("0.12345678901234567890"), "unit": "xyz"}, [], None],
                               "a.b": {"x[*]": [False, 0, "", {}, [1, 2]]},
                               "extension": [{"url": "https://example.invalid/new", "valueString": "invented"}],
                               "_birthDate": {"extension": [{"url": "https://example.invalid/unknown", "valueBoolean": True}]}})
        self.run_profile([value])
        actual = reconstruct(self.db.execute("SELECT * FROM nodes ORDER BY ordinal"))
        self.assertEqual(dumps(actual), dumps(value))
        self.assertEqual(self.field("unfamiliar", None, "amount")["numbers"]["minimum"], Decimal("0.12345678901234567890"))

    def test_missingness_uses_existing_object_parents(self):
        self.run_profile([patient("p1", group={"x": 1}), patient("p2", group={}),
                          patient("p3"), patient("p4", group=None), patient("p5", group="different type")])
        group = self.field("group")
        self.assertEqual(group["presence"]["eligible"], 5)
        self.assertEqual(group["presence"]["absent"], 1)
        self.assertEqual(group["types"], {"null": 1, "object": 2, "string": 1})
        child = self.field("group", "x")
        self.assertEqual(child["presence"], {"unit": "parent_objects", "eligible": 2, "present": 1, "absent": 1,
                                             "present_probability": Decimal("0.5"), "absent_probability": Decimal("0.5")})
        self.assertEqual(child["root_resource_presence"]["present"], 1)
        self.assertEqual(child["root_resource_presence"]["population"], 5)

    def test_arrays_keep_element_and_resource_denominators_separate(self):
        self.run_profile([patient("p1", items=[{"x": 1}, {}]), patient("p2", items=[]),
                          patient("p3", items=[None, {"x": 3}]), patient("p4")])
        items = self.field("items")
        self.assertEqual(items["arrays"]["element_types"], {"null": 1, "object": 3})
        self.assertEqual(items["arrays"]["empty_count"], 1)
        elements = self.field("items", None)
        self.assertEqual(elements["occurrences"], 4)
        self.assertEqual(elements["presence"]["eligible"], 3)
        self.assertEqual(elements["presence"]["present"], 2)
        self.assertEqual(elements["presence"]["absent"], 1)
        child = self.field("items", None, "x")
        self.assertEqual(child["presence"]["eligible"], 3)
        self.assertEqual(child["presence"]["present"], 2)
        self.assertEqual(child["root_resource_presence"]["population"], 4)
        lengths = dict(self.db.execute("SELECT length,frequency FROM array_length_frequencies WHERE field_id=?", (self.fid("items"),)))
        self.assertEqual(lengths, {0: 1, 2: 2})
        patterns = dict(self.db.execute("SELECT types_json,frequency FROM array_type_pattern_frequencies WHERE field_id=?", (self.fid("items"),)))
        self.assertEqual(patterns, {'["object","object"]': 1, '[]': 1, '["null","object"]': 1})

    def test_object_shape_frequencies_include_empty_objects(self):
        self.run_profile([patient("p1", group={"a": 1, "b": 2}), patient("p2", group={"b": 3, "a": 4}), patient("p3", group={})])
        shapes = dict(self.db.execute("SELECT keys_json,frequency FROM object_shape_frequencies WHERE field_id=?", (self.fid("group"),)))
        self.assertEqual(shapes, {'["a","b"]': 2, '[]': 1})
        self.assertEqual(self.field("group")["objects"]["empty_count"], 1)

    def test_exact_typed_values_keep_booleans_strings_and_numeric_tokens_distinct(self):
        values = [True, False, 1, Decimal("1.0"), Decimal("1.00"), "1", None, "", "1"]
        self.run_profile([patient(f"p{i}", value=value) for i, value in enumerate(values)])
        counts = {(row[0], row[1]): row[2] for row in self.db.execute("SELECT kind,value_json,frequency FROM scalar_frequencies WHERE field_id=?", (self.fid("value"),))}
        self.assertEqual(counts, {("boolean", "true"): 1, ("boolean", "false"): 1,
                                 ("number", "1"): 1, ("number", "1.0"): 1, ("number", "1.00"): 1,
                                 ("string", '"1"'): 2, ("string", '""'): 1, ("null", "null"): 1})
        field = self.field("value")
        self.assertEqual(field["types"], {"boolean": 2, "null": 1, "number": 3, "string": 3})
        self.assertEqual(field["strings"]["distinct_values"], 2)
        self.assertEqual(field["strings"]["empty_count"], 1)
        self.assertEqual(field["numbers"]["representations"], {"decimal": 2, "integer": 1})

    def test_weighted_quantiles_count_occurrences_not_distinct_values(self):
        self.run_profile([patient(f"p{i}", value=value) for i, value in enumerate([1, 2, 2, 10])])
        numbers = self.field("value")["numbers"]
        self.assertEqual(numbers["sample_count"], 4)
        self.assertEqual(numbers["minimum"], 1)
        self.assertEqual(numbers["maximum"], 10)
        self.assertEqual(numbers["quantiles"], {"p05": 1, "p25": 1, "p50": 2, "p75": 2, "p95": 10})

    def test_numeric_order_is_not_lexical_or_float_rounded(self):
        values = [Decimal("10.0"), Decimal("2.20"), Decimal("-0.01"),
                  Decimal("0.12345678901234567890"), Decimal("0.12345678901234567891")]
        self.run_profile([patient(f"p{i}", value=value) for i, value in enumerate(values)])
        numbers = self.field("value")["numbers"]
        self.assertEqual(numbers["minimum"], Decimal("-0.01"))
        self.assertEqual(numbers["maximum"], Decimal("10.0"))
        self.assertEqual(numbers["quantiles"]["p50"], Decimal("0.12345678901234567891"))
        text = (self.output / "field-statistics.json").read_text()
        self.assertIn('"p50": 0.12345678901234567891', text)

    def test_strings_remain_strings_and_lengths_count_unicode_characters(self):
        self.run_profile([patient("p1", label="001", date="2020-01-01"),
                          patient("p2", label="é🙂", date="2021-01-01"), patient("p3", label="")])
        self.assertNotIn("numbers", self.field("label"))
        self.assertEqual(dict(self.db.execute("SELECT length,frequency FROM string_length_frequencies WHERE field_id=?", (self.fid("label"),))), {0: 1, 2: 1, 3: 1})
        self.assertEqual(self.field("date")["types"], {"string": 2})

    def test_unknown_resource_and_nested_array_fields_are_profiled(self):
        self.run_profile([{"resourceType": "LocalUnknown", "id": "r", "new": [[1, 2], [], [None]]}])
        field = self.field("new", None, None, resource_type="LocalUnknown")
        self.assertEqual(field["occurrences"], 3)
        self.assertEqual(field["types"], {"null": 1, "number": 2})
        self.assertEqual(field["presence"]["eligible"], 3)
        self.assertEqual(field["presence"]["absent"], 1)

    def test_root_types_have_separate_populations(self):
        self.run_profile([patient("p", value=1), {"resourceType": "LocalUnknown", "id": "r", "value": 100}])
        self.assertEqual(self.field("value")["numbers"]["maximum"], 1)
        self.assertEqual(self.field("value", resource_type="LocalUnknown")["numbers"]["minimum"], 100)

    def test_duplicates_and_contained_rows_are_not_counted_twice(self):
        resource = patient("p", contained=[{"resourceType": "Medication", "id": "m", "extra": {"value": 4}}])
        report = self.run_profile([resource, resource])
        self.assertEqual(report["counts"]["root_resources"], 1)
        self.assertEqual(report["counts"]["source_root_occurrences"], 2)
        self.assertEqual(report["counts"]["duplicate_root_occurrences_excluded"], 1)
        self.assertEqual(report["counts"]["separate_contained_rows_excluded"], 1)
        self.assertEqual(self.field("contained", None, "extra", "value")["occurrences"], 1)
        self.assertEqual(report["resource_types"], {"Patient": 1})
        self.assertEqual(self.db.execute("SELECT count(*) FROM source_occurrences").fetchone()[0], 2)

    def test_multiple_profiles_do_not_multiply_roots(self):
        report = self.run_profile([patient("p", meta={"profile": ["https://example.invalid/a|1", "https://example.invalid/b|2"]})])
        self.assertEqual(report["counts"]["root_resources"], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM source_profiles").fetchone()[0], 2)
        self.assertEqual(self.field("meta", "profile", None)["occurrences"], 2)

    def test_parent_links_preserve_component_pairs(self):
        self.run_profile([patient("p", components=[{"code": "A", "value": 10}, {"code": "B", "value": 20}])])
        rows = self.db.execute("SELECT c.scalar_json,v.scalar_json FROM nodes c JOIN nodes v ON c.parent_id=v.parent_id WHERE c.field_id=? AND v.field_id=? ORDER BY c.id", (self.fid("components", None, "code"), self.fid("components", None, "value")))
        self.assertEqual([tuple(row) for row in rows], [('"A"', '10'), ('"B"', '20')])

    def test_high_cardinality_frequencies_are_complete(self):
        # Cross an extraction commit boundary and retain the final partial
        # batch in exact frequencies and the weighted median.
        report = self.run_profile([patient(f"p{i}", label=f"category-{i}", value=i) for i in range(1100)])
        self.assertEqual(report["counts"]["root_resources"], 1100)
        count, total = self.db.execute("SELECT count(*),sum(frequency) FROM scalar_frequencies WHERE field_id=?", (self.fid("label"),)).fetchone()
        self.assertEqual((count, total), (1100, 1100))
        self.assertEqual(self.field("label")["strings"]["distinct_values"], 1100)
        self.assertEqual(self.field("value")["numbers"]["quantiles"]["p50"], 549)

    def test_aggregate_reports_are_independent_of_input_order(self):
        resources = [patient("p1", second={"x": 2}), patient("p2", first=[1, 2]), patient("p3", first=[3], second={})]
        self.run_profile(resources)
        first = (self.output / "field-statistics.json").read_bytes()
        source2 = self.root / "other.ndjson"
        source2.write_text("\n".join(dumps(r) for r in reversed(resources)), encoding="utf-8")
        ingest([source2], self.root / "other-index")
        profile_index(self.root / "other-index" / "cohort.sqlite", self.root / "other-profile")
        self.assertEqual(first, (self.root / "other-profile" / "field-statistics.json").read_bytes())

    def test_source_is_opened_read_only_and_unchanged(self):
        source = self.prepare([patient("p")])
        before = hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_mtime_ns
        source_files = {path.name for path in source.parent.iterdir()}
        with open_source(source) as (db, _, _):
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("UPDATE run SET status='changed'")
        if os.name != "nt":
            source.chmod(0o400)
        profile_index(source, self.output)
        self.assertEqual(before, (hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_mtime_ns))
        self.assertEqual(source_files, {path.name for path in source.parent.iterdir()})
        # A completed database is portable without write-ahead-log sidecars.
        with contextlib.closing(sqlite3.connect(self.output / "field-occurrences.sqlite")) as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            self.assertEqual({path.name for path in self.output.iterdir()},
                             {"field-occurrences.sqlite", "field-inventory.json", "field-statistics.json"})

    def test_accepts_and_carries_source_warnings(self):
        report = self.run_profile([{"resourceType": "Patient", "id": "p"}])
        self.assertEqual(report["status"], "completed_with_warnings")
        self.assertIn("missing_profile_declaration", {i["code"] for i in report["source"]["issues"]})
        self.assertEqual(self.db.execute("SELECT status FROM run").fetchone()[0], "completed_with_warnings")

    def test_existing_output_is_not_overwritten(self):
        self.run_profile([patient("p")])
        before = (self.output / "field-occurrences.sqlite").read_bytes()
        with self.assertRaises(ProfileError):
            profile_index(self.ingested / "cohort.sqlite", self.output)
        self.assertEqual(before, (self.output / "field-occurrences.sqlite").read_bytes())

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_output_permissions(self):
        self.run_profile([patient("p")])
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)
        for path in self.output.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_rejects_incomplete_and_unsupported_versions_before_creating_output(self):
        source = self.prepare([patient("p")])
        for status, version in [("in_progress", 1), ("incomplete", 1), ("completed", 2)]:
            with contextlib.closing(sqlite3.connect(source)) as db, db:
                db.execute("UPDATE run SET status=?,schema_version=?", (status, version))
            with self.assertRaises(ProfileError):
                profile_index(source, self.output)
            self.assertFalse(self.output.exists())

    def test_rejects_missing_damaged_and_unrelated_databases(self):
        for name, content in [("missing.sqlite", None), ("bad.sqlite", b"not a database")]:
            path = self.root / name
            if content is not None:
                path.write_bytes(content)
            with self.assertRaises(ProfileError):
                profile_index(path, self.output)
            self.assertFalse(self.output.exists())
        source = self.prepare([patient("p")])
        with contextlib.closing(sqlite3.connect(source)) as db, db:
            db.execute("DROP TABLE profiles")
        with self.assertRaises(ProfileError):
            profile_index(source, self.output)
        self.assertFalse(self.output.exists())

    def test_rejects_symlink_inputs_and_outputs(self):
        source = self.prepare([patient("p")])
        alias = self.root / "alias.sqlite"
        alias.symlink_to(source)
        with self.assertRaises(ProfileError):
            profile_index(alias, self.output)
        self.output.symlink_to(self.root / "not-created")
        with self.assertRaises(ProfileError):
            profile_index(source, self.output)

    def test_interruption_records_status_and_does_not_publish_reports(self):
        source = self.prepare([patient("p")])
        with patch.object(FieldStore, "add_resource", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                profile_index(source, self.output)
        with contextlib.closing(sqlite3.connect(self.output / "field-occurrences.sqlite")) as db:
            self.assertEqual(db.execute("SELECT status FROM run").fetchone()[0], "interrupted")
        self.assertFalse((self.output / "field-inventory.json").exists())
        self.assertFalse((self.output / "field-statistics.json").exists())

    def test_second_report_failure_leaves_failed_run(self):
        source = self.prepare([patient("p")])
        calls = 0
        def fail_second(path, header, fields):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("invented disk error")
            write_report(path, header, fields)
        with patch("fhir_cohort_synth.profiling.write_report", side_effect=fail_second):
            with self.assertRaises(OSError):
                profile_index(source, self.output)
        with contextlib.closing(sqlite3.connect(self.output / "field-occurrences.sqlite")) as db:
            self.assertEqual(db.execute("SELECT status FROM run").fetchone()[0], "failed")
        self.assertFalse((self.output / "field-inventory.json").exists())
        self.assertFalse((self.output / "field-statistics.json").exists())

    def test_invalid_payload_fails_without_console_source_values(self):
        source = self.prepare([patient("p")])
        with contextlib.closing(sqlite3.connect(source)) as db, db:
            db.execute("UPDATE resources SET payload=?", ('{"secret": PRIVATE_SOURCE_VALUE}',))
        console = io.StringIO()
        with contextlib.redirect_stderr(console):
            self.assertEqual(main(["profile", "--input", str(source), "--output", str(self.output)]), 2)
        self.assertNotIn("PRIVATE_SOURCE_VALUE", console.getvalue())
        with contextlib.closing(sqlite3.connect(self.output / "field-occurrences.sqlite")) as db:
            self.assertEqual(db.execute("SELECT status FROM run").fetchone()[0], "failed")

    def test_cli_success_and_reports_do_not_echo_source_string_values(self):
        source = self.prepare([patient("p", note="PRIVATE_SOURCE_VALUE")])
        console = io.StringIO()
        with contextlib.redirect_stdout(console):
            self.assertEqual(main(["profile", "--input", str(source), "--output", str(self.output)]), 0)
        self.assertNotIn("PRIVATE_SOURCE_VALUE", console.getvalue())
        for name in ("field-inventory.json", "field-statistics.json"):
            self.assertNotIn("PRIVATE_SOURCE_VALUE", (self.output / name).read_text())
        with contextlib.closing(sqlite3.connect(self.output / "field-occurrences.sqlite")) as db:
            self.assertEqual(db.execute("SELECT frequency FROM scalar_frequencies WHERE value_json=?", ('"PRIVATE_SOURCE_VALUE"',)).fetchone()[0], 1)

    def test_cli_masks_unexpected_exception_and_handles_interrupt(self):
        source = self.prepare([patient("p")])
        for error, expected in [(RuntimeError("PRIVATE_SOURCE_VALUE"), 2), (KeyboardInterrupt(), 130)]:
            console = io.StringIO()
            with patch("fhir_cohort_synth.cli.profile_index", side_effect=error), contextlib.redirect_stderr(console):
                self.assertEqual(main(["profile", "--input", str(source), "--output", str(self.output)]), expected)
            self.assertNotIn("PRIVATE_SOURCE_VALUE", console.getvalue())

    def test_existing_fhir_example_end_to_end_and_integrity(self):
        example = Path(__file__).resolve().parents[1] / "examples" / "mii-demo-bundle.json"
        report = self.run_profile([loads(example.read_text())])
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["counts"]["root_resources"], 23)
        self.assertEqual(report["counts"]["nodes"], 425)
        self.assertEqual(report["counts"]["fields"], 165)
        self.assertEqual(self.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertFalse(self.db.execute("PRAGMA foreign_key_check").fetchall())
        self.assertEqual(self.db.execute("SELECT status FROM run").fetchone()[0], "completed")
        self.assertFalse(report["statistics"]["dependencies_modeled"])


if __name__ == "__main__":
    unittest.main()
