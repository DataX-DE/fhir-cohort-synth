"""Assign patient membership through explicit subjects and encounter context.

Shared resources, such as a Location used by several patients, do not inherit
ownership through arbitrary references. Membership controls which resources
can safely share a patient's numeric factor and date offset during perturbation.
"""
from collections import defaultdict, deque

# These types are expected to have patient context in a cohort export.
PATIENT_RESOURCES = {"Encounter", "Condition", "Observation", "Procedure",
                     "MedicationAdministration", "Consent"}


def group_patients(db, issue):
    """Assign a resource only when its resolved context identifies one patient.

    Start with Patient rows and explicit patient subjects. Then propagate
    that context through encounters, Encounter.partOf and containment.
    Track candidate patients as sets so contradictory paths remain visible
    instead of whichever path was visited first winning the assignment.
    """
    types = dict(db.execute("SELECT id, resource_type FROM resources"))
    # owners[resource] = candidate patient resource IDs.
    # dependents[provider] = resources that inherit that provider's context.
    # direct marks self/explicit patient links for membership provenance.
    owners = defaultdict(set)
    dependents = defaultdict(set)
    direct = set()
    encounter_parents = defaultdict(set)
    # 1. Every Patient is the starting point for its own patient group.
    for resource_id, resource_type in types.items():
        if resource_type == "Patient":
            owners[resource_id].add(resource_id)
            direct.add(resource_id)
    # 2. Select relationships that convey patient context. An Observation
    #    inherits from its Encounter, although its reference points toward
    #    that Encounter. Hence dependents[target].add(source) below.
    #    Arbitrary links to shared Medication/Location rows do not propagate
    #    context; otherwise unrelated patients could be grouped together.
    for ref in db.execute("""
        SELECT DISTINCT source_resource_id, path, target_resource_id
        FROM resource_references WHERE status='resolved'
    """):
        source, path, target = ref
        if path in {"subject.reference", "patient.reference", "beneficiary.reference"} and types[target] == "Patient":
            owners[source].add(target)
            direct.add(source)
        elif path in {"encounter.reference", "context.reference"} and types[target] == "Encounter":
            dependents[target].add(source)
        elif path == "partOf.reference" and types[source] == types[target] == "Encounter":
            dependents[target].add(source)
            encounter_parents[source].add(target)
    for resource_id, parent_id in db.execute("""
        SELECT DISTINCT resource_id, parent_resource_id
        FROM occurrences WHERE parent_resource_id IS NOT NULL
    """):
        dependents[parent_id].add(resource_id)
    # 3. Spread known patient sets until no set grows. For example:
    #    facility Encounter -> department Encounter -> unit -> Observation.
    #    Requeue on change so late discoveries (including contradictions)
    #    reach every dependent. Finite sets also make cycles terminate.
    queue = deque(owners)
    while queue:
        provider = queue.popleft()
        for dependent in dependents[provider]:
            before = len(owners[dependent])
            owners[dependent].update(owners[provider])
            if len(owners[dependent]) != before:
                queue.append(dependent)
    # 4. Persist only unique assignments. Conflicting context remains an
    #    error; patient-scoped resources with no context become warnings.
    for resource_id, patient_ids in owners.items():
        if len(patient_ids) == 1:
            patient_id = next(iter(patient_ids))
            basis = "direct" if resource_id in direct else "inferred_from_context"
            db.execute("INSERT INTO patient_memberships VALUES (?,?,?)",
                       (resource_id, patient_id, basis))
        elif patient_ids:
            issue("conflicting_patient_context", "error", resource_id=resource_id)
    for resource_id, resource_type in types.items():
        if resource_type in PATIENT_RESOURCES and not owners[resource_id]:
            issue("unassigned_patient_resource", resource_id=resource_id)
    # 5. Detect invalid encounter hierarchies separately from propagation.
    _check_encounter_cycles(encounter_parents, issue)


def _check_encounter_cycles(encounter_parents, issue):
    """Report encounters that form, or depend on, a partOf cycle.

    Remove encounters with no remaining parent dependency, then their children.
    Anything left cannot be reached from a root. Patient propagation above still
    terminates for these cycles because each resource's candidate set is finite.
    """
    nodes = set(encounter_parents) | {
        parent for parents in encounter_parents.values() for parent in parents
    }
    remaining_parents = {node: len(encounter_parents[node]) for node in nodes}
    children = defaultdict(set)
    for child, parents in encounter_parents.items():
        for parent in parents:
            children[parent].add(child)
    queue = deque(node for node in nodes if remaining_parents[node] == 0)
    while queue:
        node = queue.popleft()
        for child in children[node]:
            remaining_parents[child] -= 1
            if remaining_parents[child] == 0:
                queue.append(child)
    for resource_id, count in remaining_parents.items():
        if count:
            issue("encounter_cycle_or_dependency", "error", resource_id=resource_id)
