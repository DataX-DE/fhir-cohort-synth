"""Expose the export workflow and its ingestion/perturbation stages.

Both the checkout launcher and ``python -m fhir_cohort_synth`` call main().
FHIR ingestion lives in ingest.py/store.py; perturbation.py changes the
indexed records and verifies the written export.
"""
import argparse
import sqlite3
import sys

from . import __version__
from .ingest import InputError, ingest
from .perturbation import perturb
from .workflow import run_export


def perturb_command(args):
    """Run the export workflow or an already indexed cohort with the same options."""
    operation = run_export if args.command == 'run' else perturb
    try:
        # Hospital runs use the API defaults. Advanced configuration remains
        # available to Python callers, without a second set of CLI defaults.
        report = operation(args.input, args.output)
    except InputError as error:
        print(f"Cannot perturb: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Perturbation interrupted. Use a new output directory to retry.", file=sys.stderr)
        return 130
    except Exception:
        print("Perturbation could not finish. Check the inputs, access and disk space; use a new output directory to retry.", file=sys.stderr)
        return 2
    print(f"Perturbation: {report['status']}. {report['counts']['root_resources']} resource roots; "
          f"{report['validation']['changes_checked']} changed fields verified.")
    print(f"Created {report['export']['files_checked']} source-layout files in result/fhir/ "
          "and reports in result/reports/; databases are in intermediates/.")
    print("Outputs are local perturbed source-derived data. No privacy or full profile-conformance guarantee is made.")
    return 0


def main(argv=None):
    """Return 0 for success, 2 for failure, or 130 for interruption.

    ``argv=None`` reads the real command line; tests can supply an argument
    list directly. The launchers convert the returned code to SystemExit.
    Completed runs with warnings return success. Perturbation accepts
    completed source indexes with warnings and includes their issue context.
    """
    parser = argparse.ArgumentParser(description="Local FHIR ingestion and source-derived perturbation.")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Create a perturbed export directly from local FHIR files.")
    run.add_argument("--input", nargs="+", required=True, help="Files or directories: JSON, NDJSON, JSONL, optionally .gz.")
    run.add_argument("--output", required=True, help="New directory outside the input directories.")
    command = commands.add_parser("ingest", help="Index a local FHIR R4 export and report its structure.")
    command.add_argument("--input", nargs="+", required=True, help="Files or directories: JSON, NDJSON, JSONL, optionally .gz.")
    command.add_argument("--output", required=True, help="New directory outside the input directories.")
    command = commands.add_parser("perturb", help="Perturb supported values while retaining existing linked records.")
    command.add_argument("--input", required=True, help="Completed ingestion database (cohort.sqlite).")
    command.add_argument("--output", required=True, help="New output directory.")
    args = parser.parse_args(argv)
    if args.command in {"run", "perturb"}:
        return perturb_command(args)
    try:
        report = ingest(args.input, args.output)
    except InputError as error:
        # InputError messages are deliberately written without source values.
        print(f"Cannot ingest: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Ingestion interrupted. Use a new output directory to retry.", file=sys.stderr)
        return 130
    except (OSError, sqlite3.Error):
        # Low-level exceptions may expose local paths or database contents.
        # Keep console output generic; an interrupted index is not a success.
        print("Ingestion could not finish due to a local file or database error. Check access and free disk space; use a new output directory to retry.", file=sys.stderr)
        return 2
    print(f"Ingestion: {report['status']}. "
          f"{report['counts']['unique_resources']} resources; {report['counts']['patients']} patient resources.")
    print("Created cohort.sqlite, report.json and report.txt in the output directory.")
    print("The database contains source patient data. Record perturbation and full profile validation have not run.")
    return 2 if report["status"] == "incomplete" else 0
