"""Exercise ingestion with invented data and isolated temporary output.

Tests are named for the behavior they protect: reference scope, duplicate
handling, patient grouping, malformed input and output handling. The fixtures
below are minimal ingestion examples, not claims of full FHIR conformance.
"""
import contextlib
from decimal import Decimal
import gzip
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fhir_cohort_synth.cli import main
from fhir_cohort_synth.ingest import InputError, ingest
from fhir_cohort_synth.jsonio import dumps, loads
from fhir_cohort_synth.store import Store


def resource(kind, identity, **fields):
    """Build a small resource; fields can override defaults for edge cases."""
    return {"resourceType": kind, "id": identity,
            "meta": {"profile": [f"http://hl7.org/fhir/StructureDefinition/{kind}"]}, **fields}


def bundle(*entries, **fields):
    """Wrap (full URL or None, resource) pairs in a collection Bundle."""
    return {"resourceType": "Bundle", "type": "collection", "entry": [
        {"fullUrl": url, "resource": r} if url else {"resource": r} for url, r in entries], **fields}


class IngestionTests(unittest.TestCase):
    """Run each scenario with fresh files and a fresh SQLite index."""

    def setUp(self):
        # unittest cleanups also run after a failed assertion. Register the
        # directory first so later database cleanups close it before removal.
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "input"
        self.source.mkdir()
        self.output = self.root / "output"

    def write(self, value, name="data.json"):
        """Write an invented input with the same decimal-preserving JSON codec."""
        path = self.source / name
        path.write_text(dumps(value), encoding="utf-8")
        return path

    def run_ingest(self, **options):
        """Run the public ingestion function and open its index for assertions."""
        report = ingest([self.source], self.output, **options)
        self.db = sqlite3.connect(self.output / "cohort.sqlite")
        self.addCleanup(self.db.close)
        return report

    def codes(self, report):
        """Extract issue names when a test cares about findings, not their order."""
        return {i["code"] for i in report["issues"]}

    def test_single_resource_precision_and_source_unchanged(self):
        value = resource("Patient", "p1", extension=[{"url": "https://example.org/decimal", "valueDecimal": Decimal("0.12345678901234567890")}])
        path = self.write(value)
        original = path.read_bytes()
        report = self.run_ingest()
        payload = self.db.execute("SELECT payload FROM resources").fetchone()[0]
        self.assertIn("0.12345678901234567890", payload)
        self.assertEqual(loads(payload), value)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["counts"]["grouped_resources"], 1)

    def test_urn_forward_references_keep_bundle_lookup_context(self):
        self.write(bundle(("urn:uuid:e", resource("Encounter", "e", subject={"reference": "urn:uuid:p"})),
                          ("urn:uuid:p", resource("Patient", "p")), total=2))
        report = self.run_ingest()
        self.assertEqual(report["reference_status"], {"resolved": 1})
        self.assertEqual(report["counts"]["grouped_resources"], 2)
        occurrences = self.db.execute("SELECT context,full_url,locator FROM occurrences ORDER BY id").fetchall()
        self.assertEqual(occurrences, [("source:1:$", "urn:uuid:e", "$.entry[0].resource"),
                                       ("source:1:$", "urn:uuid:p", "$.entry[1].resource")])

    def test_relative_references_with_absolute_server_namespace(self):
        self.write(bundle(("https://a.test/fhir/Patient/p", resource("Patient", "p")),
                          ("https://b.test/fhir/Patient/p", resource("Patient", "p")),
                          ("https://a.test/fhir/Encounter/e", resource("Encounter", "e", subject={"reference": "Patient/p"}))))
        report = self.run_ingest()
        target = self.db.execute("SELECT r.full_url FROM resource_references f JOIN resources r ON r.id=f.target_resource_id").fetchone()[0]
        self.assertEqual(target, "https://a.test/fhir/Patient/p")
        self.assertEqual(report["status"], "completed")

    def test_missing_namespace_does_not_match_other_server(self):
        self.write(bundle(("https://b.test/fhir/Patient/p", resource("Patient", "p")),
                          ("https://a.test/fhir/Encounter/e", resource("Encounter", "e", subject={"reference": "Patient/p"}))))
        report = self.run_ingest()
        self.assertEqual(report["reference_status"], {"unresolved": 1})
        self.assertEqual(report["counts"]["grouped_resources"], 1)

    def test_absolute_reference_does_not_fall_back_to_unqualified_id(self):
        self.write(bundle((None, resource("Patient", "p")),
                          (None, resource("Encounter", "e", subject={"reference": "https://a.test/fhir/Patient/p"}))))
        self.assertEqual(self.run_ingest()["reference_status"], {"unresolved": 1})

    def test_explicit_base_resolves_bulk_absolute_references(self):
        self.write(resource("Patient", "p"), "patient.json")
        self.write(resource("Encounter", "e", subject={"reference": "https://a.test/fhir/Patient/p"}), "encounter.json")
        self.assertEqual(self.run_ingest(base_url="https://a.test/fhir/")["reference_status"], {"resolved": 1})

    def test_cross_file_ndjson_and_bom(self):
        self.write(resource("Patient", "p"))
        (self.source / "events.ndjson").write_text("\ufeff\n" + dumps(resource("Encounter", "e", subject={"reference": "Patient/p"})) + "\n", encoding="utf-8")
        report = self.run_ingest()
        self.assertEqual(report["counts"]["files"], 2)
        self.assertEqual(report["reference_status"], {"resolved": 1})

    def test_gzip_json_and_jsonl(self):
        for name, value in [("p.json.gz", resource("Patient", "p")),
                            ("e.jsonl.gz", resource("Encounter", "e", subject={"reference": "Patient/p"}))]:
            with gzip.open(self.source / name, "wt", encoding="utf-8") as stream:
                stream.write(dumps(value) + "\n")
        self.assertEqual(self.run_ingest()["reference_status"], {"resolved": 1})

    def test_interrupted_ndjson_keeps_complete_documents_without_success(self):
        # Interrupt just beyond a transaction boundary. Each saved document
        # must retain its contained subtree, with no completed-run claim.
        values = [resource("Patient", f"p{i}", contained=[resource("Observation", "inside")])
                  for i in range(999)]
        # A blank 1,000th line must not skip the checkpoint.
        content = "\n".join(dumps(value) for value in values)
        (self.source / "batch.ndjson").write_text(content + "\n\n" + dumps(resource("Patient", "stop")) + "\n")
        original = Store.add_document

        def interrupt(store, value, *args, **kwargs):
            if value.get("id") == "stop":
                raise KeyboardInterrupt
            return original(store, value, *args, **kwargs)

        stderr = io.StringIO()
        with patch("fhir_cohort_synth.ingest.INGEST_CHECKPOINT_LINES", 1000), \
                patch.object(Store, "add_document", interrupt), contextlib.redirect_stderr(stderr):
            code = main(["ingest", "--input", str(self.source), "--output", str(self.output)])
        self.assertEqual(code, 130)
        self.assertIn("Ingestion interrupted", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertFalse((self.output / "report.json").exists())
        with contextlib.closing(sqlite3.connect(self.output / "cohort.sqlite")) as db:
            self.assertEqual(db.execute("SELECT status FROM run").fetchone()[0], "in_progress")
            self.assertEqual(db.execute("SELECT count(*) FROM resources").fetchone()[0], 1998)
            self.assertEqual(db.execute("SELECT count(*) FROM resources WHERE contained=1").fetchone()[0], 999)
            self.assertIsNone(db.execute("PRAGMA foreign_key_check").fetchone())

    def test_duplicates_do_not_inflate_resource_counts(self):
        self.write(resource("Patient", "p"), "p1.json")
        self.write(resource("Patient", "p"), "p2.json")
        report = self.run_ingest()
        self.assertEqual(report["counts"]["unique_resources"], 1)
        self.assertEqual(report["counts"]["duplicate_occurrences"], 1)
        self.assertEqual(report["status"], "completed")

    def test_conflicting_identity_retains_both_and_blocks_resolution(self):
        self.write(bundle((None, resource("Patient", "p", gender="female")),
                          (None, resource("Patient", "p", gender="male")),
                          (None, resource("Encounter", "e", subject={"reference": "Patient/p"}))))
        report = self.run_ingest()
        self.assertEqual(report["status"], "incomplete")
        self.assertIn("conflicting_resource_identity", self.codes(report))
        self.assertEqual(report["reference_status"], {"ambiguous": 1})
        self.assertEqual(report["counts"]["unique_resources"], 3)

    def test_ambiguous_unqualified_id_across_servers(self):
        self.write(bundle(("https://a.test/Patient/p", resource("Patient", "p")),
                          ("https://b.test/Patient/p", resource("Patient", "p")),
                          ("urn:uuid:e", resource("Encounter", "e", subject={"reference": "Patient/p"}))))
        self.assertEqual(self.run_ingest()["reference_status"], {"ambiguous": 1})

    def test_versioned_references_filter_meta_version(self):
        self.write(bundle(("https://a.test/Patient/p", resource("Patient", "p", meta={"versionId": "2"})),
                          ("https://a.test/Encounter/e", resource("Encounter", "e", subject={"reference": "Patient/p/_history/2"}))))
        self.assertEqual(self.run_ingest()["reference_status"], {"resolved": 1})

    def test_missing_requested_version_is_unresolved(self):
        self.write(bundle((None, resource("Patient", "p", meta={"versionId": "2"})),
                          (None, resource("Encounter", "e", subject={"reference": "Patient/p/_history/1"}))))
        self.assertEqual(self.run_ingest()["reference_status"], {"unresolved": 1})

    def test_missing_local_version_does_not_escape_bundle_namespace(self):
        self.write(bundle(("urn:uuid:p-a", resource("Patient", "p", meta={"versionId": "2"})),
                          ("urn:uuid:e", resource("Encounter", "e", subject={"reference": "Patient/p/_history/1"}))), "local.json")
        self.write(bundle(("urn:uuid:p-b", resource("Patient", "p", meta={"versionId": "1"}))), "other.json")
        self.assertEqual(self.run_ingest()["reference_status"], {"unresolved": 1})

    def test_full_url_identity_mismatch_and_invalid_full_url(self):
        self.write(bundle(("https://example.org/Patient/wrong", resource("Patient", "p")),
                          ("Patient/relative", resource("Patient", "relative"))))
        report = self.run_ingest()
        self.assertTrue({"full_url_identity_mismatch", "invalid_full_url"} <= self.codes(report))

    def test_invented_module_example_end_to_end(self):
        example = Path(__file__).resolve().parents[1] / "examples" / "mii-demo-bundle.json"
        self.write(loads(example.read_text(encoding="utf-8")))
        report = self.run_ingest()
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["counts"]["patients"], 2)
        self.assertEqual(report["counts"]["unique_resources"], 23)
        self.assertEqual(report["counts"]["grouped_resources"], 21)
        self.assertEqual(report["reference_status"], {"resolved": 37})
        self.assertEqual(self.db.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        self.assertEqual({path.name for path in self.output.iterdir()},
                         {"cohort.sqlite", "report.json", "report.txt"})
        self.assertTrue({"Patient", "Encounter", "Condition", "Observation", "Procedure", "MedicationAdministration", "Consent", "Location", "Medication"} <= report["resource_types"].keys())

    def test_contained_references_are_scoped_to_root(self):
        patients = [resource("Patient", f"p{i}", generalPractitioner=[{"reference": "#doc"}],
                             contained=[resource("Practitioner", "doc", extension=[{"url": "https://example.org/owner", "valueReference": {"reference": "#"}}])]) for i in range(2)]
        self.write(bundle(*[(None, p) for p in patients]))
        report = self.run_ingest()
        self.assertEqual(report["reference_status"], {"resolved": 4})
        self.assertEqual(report["counts"]["unique_resources"], 4)
        self.assertEqual(report["counts"]["grouped_resources"], 4)
        self.assertNotIn("conflicting_patient_context", self.codes(report))

    def test_encounter_hierarchy_and_shared_support_resource(self):
        values = [resource("Patient", "p"), resource("Location", "ward"),
                  resource("Encounter", "facility", subject={"reference": "Patient/p"}),
                  resource("Encounter", "department", partOf={"reference": "Encounter/facility"}),
                  resource("Encounter", "unit", partOf={"reference": "Encounter/department"}, location=[{"location": {"reference": "Location/ward"}}]),
                  resource("Observation", "o", encounter={"reference": "Encounter/unit"}, valueString="positive")]
        self.write(bundle(*[(None, r) for r in values]))
        report = self.run_ingest()
        self.assertEqual(report["counts"]["grouped_resources"], 5)
        self.assertEqual(self.db.execute("SELECT count(*) FROM patient_memberships m JOIN resources r ON r.id=m.resource_id WHERE r.resource_type='Location'").fetchone()[0], 0)

    def test_subject_and_encounter_disagreement_is_not_assigned(self):
        self.write(bundle(*[(None, r) for r in [resource("Patient", "p1"), resource("Patient", "p2"),
                         resource("Encounter", "e", subject={"reference": "Patient/p1"}),
                         resource("Observation", "o", subject={"reference": "Patient/p2"}, encounter={"reference": "Encounter/e"})]]))
        report = self.run_ingest()
        self.assertIn("conflicting_patient_context", self.codes(report))
        self.assertEqual(report["counts"]["grouped_resources"], 3)

    def test_encounter_cycle_is_reported(self):
        self.write(bundle(*[(None, r) for r in [resource("Patient", "p"),
                         resource("Encounter", "a", subject={"reference": "Patient/p"}, partOf={"reference": "Encounter/b"}),
                         resource("Encounter", "b", partOf={"reference": "Encounter/a"})]]))
        self.assertIn("encounter_cycle_or_dependency", self.codes(self.run_ingest()))

    def test_invalid_ndjson_continues_without_disclosing_text(self):
        secret = "PRIVATE_PATIENT_NAME"
        (self.source / "data.ndjson").write_text('{"name":' + secret + '}\n' + dumps(resource("Patient", "p")) + "\n", encoding="utf-8")
        report = self.run_ingest()
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["counts"]["patients"], 1)
        self.assertNotIn(secret, (self.output / "report.json").read_text())
        self.assertNotIn(secret, str(self.db.execute("SELECT * FROM issues").fetchall()))

    def test_invalid_json_constants_duplicate_keys_and_surrogates(self):
        for index, text in enumerate(['{"resourceType":"Patient","id":"a","x":NaN}',
                                      '{"resourceType":"Patient","id":"a","id":"b"}',
                                      '{"resourceType":"Patient","id":"a","x":"\\ud800"}']):
            (self.source / f"bad{index}.json").write_text(text, encoding="utf-8")
        report = self.run_ingest()
        self.assertEqual(report["counts"]["unique_resources"], 0)
        self.assertEqual(next(i["count"] for i in report["issues"] if i["code"] == "invalid_json"), 3)

    def test_malformed_resource_shapes_are_reported(self):
        for index, value in enumerate([[], {"resourceType": "Bundle", "type": []},
                                       resource("Patient", "p", meta=[]),
                                       resource("Observation", "o", component=[None], contained={})]):
            self.write(value, f"bad{index}.json")
        report = self.run_ingest()
        self.assertEqual(report["status"], "incomplete")
        self.assertIn("invalid_bundle_type", self.codes(report))

    def test_unknown_resources_profiles_and_extensions_are_retained(self):
        value = resource("ResearchStudy", "r", meta={"profile": ["https://example.org/custom|0.2"]},
                         modifierExtension=[{"url": "https://example.org/meaning", "valueBoolean": True}])
        self.write(value)
        report = self.run_ingest()
        self.assertEqual(loads(self.db.execute("SELECT payload FROM resources").fetchone()[0]), value)
        self.assertEqual(report["resource_types"], {"ResearchStudy": 1})
        self.assertNotIn("outside_requested_scope", self.codes(report))

    def test_mii_profile_versions_are_not_fhir_versions(self):
        canonical = "https://www.medizininformatik-initiative.de/fhir/core/modul-person/StructureDefinition/PatientPseudonymisiert"
        self.write(resource("Patient", "p", meta={"profile": [canonical + "|2024.0.0", canonical]}))
        report = self.run_ingest()
        payload = loads(self.db.execute("SELECT payload FROM resources").fetchone()[0])
        self.assertEqual(payload["meta"]["profile"], [canonical + "|2024.0.0", canonical])
        self.assertEqual(report["status"], "completed")
        self.assertFalse(report["fhir"]["hospital_version_confirmed"])

    def test_quantitative_qualitative_and_component_observations(self):
        coding = {"coding": [{"system": "http://loinc.org", "code": "test-code"}]}
        self.write(bundle((None, resource("Observation", "q", code=coding, valueQuantity={"value": Decimal("4.20"), "system": "http://unitsofmeasure.org", "code": "mmol/L", "unit": "mmol/L"})),
                          (None, resource("Observation", "qual", code=coding, valueCodeableConcept={"text": "positive"})),
                          (None, resource("Observation", "bp", code=coding, component=[{"code": coding, "valueQuantity": {"value": 120, "code": "mm[Hg]"}}]))))
        report = self.run_ingest()
        payloads = [loads(row[0]) for row in self.db.execute("SELECT payload FROM resources")]
        values = {value["id"]: value for value in payloads}
        self.assertEqual(report["resource_types"], {"Observation": 3})
        self.assertEqual(values["q"]["valueQuantity"]["value"], Decimal("4.20"))
        self.assertIn('4.20', self.db.execute("SELECT payload FROM resources WHERE logical_id='q'").fetchone()[0])
        self.assertEqual(values["qual"]["valueCodeableConcept"], {"text": "positive"})
        self.assertEqual(values["bp"]["component"][0]["valueQuantity"], {"value": 120, "code": "mm[Hg]"})

    def test_observation_value_conflict(self):
        self.write(resource("Observation", "o", valueString="a", valueBoolean=True))
        self.assertIn("observation_value_conflict", self.codes(self.run_ingest()))

    def test_incompatible_declared_fhir_version(self):
        self.write(resource("CapabilityStatement", "cap", fhirVersion="5.0.0"))
        report = self.run_ingest()
        self.assertEqual(report["status"], "incomplete")
        self.assertIn("incompatible_fhir_version_declaration", self.codes(report))
        self.assertEqual(loads(self.db.execute("SELECT payload FROM resources").fetchone()[0])["fhirVersion"], "5.0.0")

    def test_minimal_index_does_not_duplicate_resource_inventories(self):
        self.write({"resourceType": "Patient", "id": "p"})
        report = self.run_ingest()
        tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {"run", "sources", "resources", "occurrences", "aliases",
                                  "resource_references", "patient_memberships", "issues"})
        self.assertEqual(self.db.execute("SELECT schema_version FROM run").fetchone()[0], 2)
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(report["status"], "completed")  # meta.profile is optional.
        self.assertEqual(report["issues"], [])
        self.assertTrue({"profiles", "extensions", "observation_fields", "scope"}.isdisjoint(report))
        self.assertNotIn("declarations_in_input", report["fhir"])

    def test_inventory_removal_keeps_structural_checks(self):
        self.write(bundle(
            (None, resource("Patient", "p", meta={"profile": "not-an-array"})),
            (None, resource("Observation", "o", component=[None, {"valueString": "x", "dataAbsentReason": {}}])),
            (None, resource("Observation", "bad", component={})),
            (None, resource("ImplementationGuide", "ig", fhirVersion=["4.0.1", "5.0.0"])),
        ))
        report = self.run_ingest()
        self.assertEqual(report["status"], "incomplete")
        self.assertTrue({"invalid_profile_declaration", "invalid_observation_component",
                         "invalid_observation_components", "observation_value_conflict",
                         "incompatible_fhir_version_declaration"} <= self.codes(report))

    def test_history_pagination_and_tombstones_not_silently_accepted(self):
        self.write({"resourceType": "Bundle", "type": "history", "link": [{"relation": "next", "url": "https://example.org/next"}],
                    "entry": [{"resource": resource("Patient", "p")}, {"request": {"method": "DELETE", "url": "Patient/old"}}]})
        report = self.run_ingest()
        self.assertTrue({"history_bundle_requires_snapshot", "pagination_link_present", "bundle_entry_without_resource"} <= self.codes(report))

    def test_identifier_only_reference_is_never_guessed(self):
        self.write(bundle((None, resource("Patient", "p", identifier=[{"system": "https://example.org/id", "value": "1"}])),
                          (None, resource("Consent", "c", patient={"identifier": {"system": "https://example.org/id", "value": "1"}}))))
        self.assertEqual(self.run_ingest()["reference_status"], {"logical_unresolved": 1})

    def test_no_overwrites_and_no_output_inside_input(self):
        self.write(resource("Patient", "p"))
        with self.assertRaises(InputError):
            ingest([self.source], self.source / "output")
        self.run_ingest()
        original = (self.output / "cohort.sqlite").read_bytes()
        with self.assertRaises(InputError):
            ingest([self.source], self.output)
        self.assertEqual((self.output / "cohort.sqlite").read_bytes(), original)

    def test_symlinks_are_rejected(self):
        path = self.write(resource("Patient", "p"))
        (self.source / "link.json").symlink_to(path)
        with self.assertRaises(InputError):
            self.run_ingest()

    def test_empty_and_missing_inputs(self):
        with self.assertRaises(InputError):
            self.run_ingest()
        with self.assertRaises(InputError):
            ingest([self.root / "missing.json"], self.output)

    def test_corrupt_gzip_is_reported(self):
        (self.source / "broken.json.gz").write_bytes(b"not gzip")
        self.assertIn("source_read_error", self.codes(self.run_ingest()))

    def test_invalid_deflate_stream_is_reported(self):
        (self.source / "broken.json.gz").write_bytes(b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff\x07" + b"\x00" * 20)
        self.assertIn("source_read_error", self.codes(self.run_ingest()))

    def test_cli_exit_codes_and_no_patient_values_in_console(self):
        self.write(resource("Patient", "p", name=[{"family": "PRIVATE_NAME"}],
                            managingOrganization={"reference": "Organization/missing"}))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["ingest", "--input", str(self.source), "--output", str(self.output)])
        self.assertEqual(code, 0)  # Completed runs retain warnings in the report.
        self.assertEqual(json.loads((self.output / "report.json").read_text())["reference_status"], {"unresolved": 1})
        self.assertNotIn("PRIVATE_NAME", output.getvalue())
        self.assertNotIn("PRIVATE_NAME", (self.output / "report.json").read_text())
        with contextlib.redirect_stderr(output):
            self.assertEqual(main(["ingest", "--input", str(self.source), "--output", str(self.output)]), 2)

    @unittest.skipIf(os.name == "nt", "POSIX permission check")
    def test_private_output_permissions(self):
        self.write(resource("Patient", "p"))
        self.run_ingest()
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)
        for path in self.output.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_base_url_rejects_credentials(self):
        self.write(resource("Patient", "p"))
        with self.assertRaises(InputError):
            self.run_ingest(base_url="https://user:secret@example.org/fhir")


if __name__ == "__main__":
    unittest.main()
