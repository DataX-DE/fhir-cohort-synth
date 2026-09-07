"""Translate command-line arguments into one ingestion run.

Both the checkout launcher and ``python -m fhir_cohort_synth`` call main().
FHIR ingestion lives in ingest.py/store.py. The profile command uses
profiling.py to coordinate generic extraction, storage and aggregation.
"""
import argparse
import sqlite3
import sys

from . import __version__
from .ingest import InputError, ingest
from .profiling import profile_index


def profile_command(args):
    """Run profiling with console diagnostics that never quote source values."""
    try:
        report = profile_index(args.input, args.output)
    except InputError as error:
        print(f"Cannot profile: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Profiling interrupted. Use a new output directory to retry.", file=sys.stderr)
        return 130
    except Exception:
        # At the CLI boundary, unexpected database/parser errors also need
        # generic messages. The API still raises them for debugging/tests.
        print("Profiling could not finish. Check the local index, access and disk space; use a new output directory to retry.", file=sys.stderr)
        return 2
    print(f"Profiling: {report['status']}. {report['counts']['root_resources']} roots; "
          f"{report['counts']['nodes']} nodes; {report['counts']['fields']} field paths.")
    print("Created field-occurrences.sqlite, field-inventory.json and field-statistics.json.")
    print("Outputs contain source-derived data and exact local distributions. Synthetic generation has not run.")
    return 0


def main(argv=None):
    """Return 0 for success, 2 for failure, or 130 for interruption.

    ``argv=None`` reads the real command line; tests can supply an argument
    list directly. The launchers convert the returned code to SystemExit.
    Ingestion warnings cause failure only with --strict. Profiling accepts
    completed source indexes with warnings and includes their issue context.
    """
    parser = argparse.ArgumentParser(description="Local FHIR ingestion and exact JSON profiling (generation is not implemented yet).")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("ingest", help="Index a local FHIR R4 export and report its structure.")
    command.add_argument("--input", nargs="+", required=True, help="Files or directories: JSON, NDJSON, JSONL, optionally .gz.")
    command.add_argument("--output", required=True, help="New directory outside the input directories.")
    command.add_argument("--base-url", help="Optional single server base for entries without fullUrl; no network request is made.")
    command.add_argument("--strict", action="store_true", help="Exit with code 2 when warnings remain, as well as on errors.")
    profile = commands.add_parser("profile", help="Extract every JSON field and build exact local distributions.")
    profile.add_argument("--input", required=True, help="Completed ingestion database (cohort.sqlite).")
    profile.add_argument("--output", required=True, help="New directory for the field index and reports.")
    args = parser.parse_args(argv)
    if args.command == "profile":
        return profile_command(args)
    try:
        report = ingest(args.input, args.output, base_url=args.base_url)
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
    print("The database contains source patient data. Synthetic generation and full profile validation have not run.")
    # --strict changes the process exit code, not the recorded data findings.
    warnings = any(i["severity"] == "warning" for i in report["issues"])
    return 2 if report["status"] == "incomplete" or (args.strict and warnings) else 0
