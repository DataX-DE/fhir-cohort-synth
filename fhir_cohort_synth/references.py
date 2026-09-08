"""Resolve stored FHIR references after all source files have been ingested.

Lookup is scoped by containment, Bundle/file context and server identity.
Only a unique matching target is accepted; missing or ambiguous targets are
recorded as issues. Repeated occurrences do not create additional candidates.
"""
import re
from functools import lru_cache, partial
from urllib.parse import urlsplit

# Patient/p1 and Patient/p1/_history/2 share an identity but name different
# revision constraints. The groups are resource type, ID and optional version.
RELATIVE = re.compile(r"^([A-Z][A-Za-z0-9]+)/([A-Za-z0-9\-.]{1,64})(?:/_history/([A-Za-z0-9\-.]{1,64}))?$")


def resolve_references(db, issue):
    """Resolve stored literal references after the entire export is indexed.

    Missing targets become warnings; several candidates become errors.
    Identifier-only references remain logical_unresolved from ingestion.
    """
    # Aliases are complete and immutable throughout this pass. Repeated links
    # to the same patient/encounter can reuse a bounded cache of candidate sets.
    # The cache key includes scope and version, so namespaces stay separate.
    candidates = lru_cache(maxsize=16384)(partial(_candidates, db))
    for ref in db.execute("""
        SELECT f.*, o.context, o.full_url, o.root_resource_id,
               r.full_url AS root_full_url
        FROM resource_references f JOIN occurrences o ON o.id=f.occurrence_id
        JOIN resources r ON r.id=o.root_resource_id WHERE f.kind='literal'
    """):
        matches = _resolve_one(ref["literal"], ref, candidates)
        # Never use the first of several matches to break an ambiguity.
        target = None
        if len(matches) == 1:
            status = "resolved"
            target = next(iter(matches))
        elif matches:
            status = "ambiguous"
        else:
            status = "unresolved"
        db.execute(
            "UPDATE resource_references SET target_resource_id=?, status=? WHERE id=?",
            (target, status, ref["id"]),
        )
        if status != "resolved":
            issue(f"{status}_reference", "error" if status == "ambiguous" else "warning",
                  resource_id=ref["source_resource_id"], detail=ref["path"])


def _resolve_one(literal, occurrence, candidates):
    """Find candidate targets using the referencing occurrence's scope.

    Handle local # references, relative REST references, absolute REST
    references, then exact full URLs such as URNs. Return a set rather than
    choosing a target here: resolve_references() accepts only a unique match.
    """
    # '#' refers back to the containing root; '#med1' finds a child under
    # that same root. Neither form searches another patient's resource.
    if literal == "#":
        return {occurrence["root_resource_id"]}
    if literal.startswith("#"):
        return candidates(literal, "contained", f"contained:{occurrence['root_resource_id']}")
    relative = RELATIVE.fullmatch(literal)
    if relative:
        alias, version = f"{relative[1]}/{relative[2]}", relative[3]
        # Absolute source identity determines the server namespace. A same-ID
        # resource from another server must never be used as a fallback.
        root_url = occurrence["root_full_url"]
        source = rest_parts(occurrence["full_url"] or root_url)
        if source:
            return candidates(source[0] + alias, "absolute", version=version)
        # With no known server base, prefer the enclosing Bundle/file.
        # Only if that identity is absent locally do we search other files.
        local = candidates(alias, "relative", occurrence["context"])
        if local:
            # A local identity with the wrong version must not cause a
            # search in an unrelated Bundle/server for that version.
            if version is None:
                return local
            return candidates(alias, "relative", occurrence["context"], version)
        return candidates(alias, "relative", version=version)
    absolute = rest_parts(literal)
    if absolute:
        # Match the declared server and revision exactly. Falling back to
        # type/ID could attach a resource from an unrelated server.
        base, alias, version = absolute
        return candidates(base + alias, "absolute", version=version)
    # URNs and non-REST fullUrls are resolved by exact identity only.
    return candidates(literal, "absolute")


def _candidates(db, alias, kind, context=None, version=None):
    """Return all matching resource IDs, optionally restricted by scope/revision.

    A set lets the caller distinguish missing, unique and ambiguous targets.
    DISTINCT prevents repeated occurrences of one resource from creating
    false ambiguity. ``version`` matches meta.versionId, not a profile version.
    """
    sql = """SELECT DISTINCT r.id FROM aliases a
             JOIN resources r ON r.id=a.resource_id WHERE a.alias=? AND a.kind=?"""
    args = [alias, kind]
    if context is not None:
        sql += " AND a.context=?"
        args.append(context)
    if version is not None:
        sql += " AND r.version_id=?"
        args.append(version)
    return {r[0] for r in db.execute(sql, args)}


def rest_parts(full_url):
    """Split a recognizable HTTP(S) FHIR resource URL, or return None.

    For 'https://host/fhir/Patient/p1/_history/2', return
    ('https://host/fhir/', 'Patient/p1', '2'). Unversioned URLs return None
    for the version. URNs and arbitrary non-REST URLs must use exact matching
    instead; stripping their final segments would invent an identity.
    """
    if not isinstance(full_url, str):
        return None
    try:
        parsed = urlsplit(full_url)
    except ValueError:
        return None
    if (parsed.scheme not in {"http", "https"} or not parsed.netloc
            or parsed.query or parsed.fragment or full_url.endswith("/")):
        return None
    parts = parsed.path.strip("/").split("/")
    versioned = len(parts) >= 4 and parts[-2] == "_history"
    tail = "/".join(parts[-4:] if versioned else parts[-2:])
    match = RELATIVE.fullmatch(tail)
    if not match:
        return None
    return full_url[:-len(tail)], f"{match[1]}/{match[2]}", match[3]
