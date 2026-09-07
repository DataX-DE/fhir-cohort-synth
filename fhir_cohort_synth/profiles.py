"""Inventory hints, not profile validation or declarations of conformance.

Exact hospital package versions are unknown. Store every canonical/version as
declared; module recognition must not silently upgrade a historical profile.
"""
from urllib.parse import urlsplit

# These buckets drive the inventory report, not an input allowlist: resources
# outside the hospital's requested modules are still stored in full.
CORE_SCOPE = {
    "Patient": "person", "Encounter": "encounter", "Condition": "diagnosis",
    "Observation": "observation", "Procedure": "procedure",
    "MedicationAdministration": "medication_administration", "Consent": "consent",
    "Location": "location",
}
SUPPORTING = {"Medication", "Organization", "Practitioner", "PractitionerRole",
              "Device", "DeviceMetric", "Specimen", "DiagnosticReport",
              "ServiceRequest", "RelatedPerson"}
# Recognize module names from known MII URL path segments. This does not load
# a profile definition or establish that the resource conforms to it.
MODULES = {
    "modul-person": "person", "modul-fall": "encounter", "modul-diagnose": "diagnosis",
    "modul-labor": "laboratory", "modul-prozedur": "procedure",
    "modul-medikation": "medication", "modul-consent": "consent",
    "modul-icu": "icu",
}


def profile_parts(value):
    """Split 'canonical-url|profile-version'; empty version means unspecified.

    This is the version of the declared profile, not the FHIR base version or
    the resource's meta.versionId revision.
    """
    canonical, separator, version = value.partition("|")
    return canonical, version if separator else ""


def module_for(canonical):
    """Return an MII inventory label, or 'unrecognized' for other profile URLs."""
    try:
        parsed = urlsplit(canonical)
    except ValueError:
        return "unrecognized"
    # A local profile could use the same path text; require a known MII host
    # before labeling it as an MII module.
    if parsed.hostname not in {"www.medizininformatik-initiative.de",
                               "medizininformatik-initiative.de"}:
        return "unrecognized"
    for segment in parsed.path.split("/"):
        if segment in MODULES:
            return MODULES[segment]
    return "unrecognized"


def scope_for(resource_type):
    """Classify a resource as requested, supporting, or other for reporting."""
    if resource_type in CORE_SCOPE:
        return "requested"
    if resource_type in SUPPORTING:
        return "supporting"
    return "other"
