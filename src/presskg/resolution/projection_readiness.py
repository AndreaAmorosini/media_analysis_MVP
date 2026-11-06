
"""PressKG Phase 4F: Projection Readiness Validation.

Read-only validation of the Phase 4E identity gate.

Checks:
- non-atomic mentions containing independently attested entities
- person identities without a proper personal-name anchor
- long activity descriptions lacking distinctive identity evidence
- semantic-facet consistency
- recovery of qualified MediaOutlet names held only for article stripping

Produces an auditable projection-ready manifest.

No corpus-specific names, LLM calls, or Neo4j writes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata

from collections import Counter, defaultdict
from pathlib import Path


VERSION = "projection-readiness-v1"

APPROVED = "APPROVED_FOR_PROJECTION"
CONTEXTUAL = "CONTEXTUAL_ONLY"
BLOCKED = "BLOCKED"

VALID_STATUSES = {APPROVED, CONTEXTUAL, BLOCKED}

PERSON_PARTICLES = {
    "de", "di", "del", "della",
    "da", "van", "von",
}

NAME_CONNECTORS = {
    "di", "de", "del", "della", "da",
    "van", "von", "e", "ed", "con",
    "per", "in", "su", "sul", "sulla",
}

COORDINATORS = {
    "e", "ed", "con",
}

ARTICLE_WORDS = {
    "il", "lo", "la", "l", "i", "gli", "le",
}

# Linguistic classes, not named entities.
INDEFINITE_DETERMINERS = {
    "un", "uno", "una", "due", "tre",
    "alcuni", "alcune", "diversi", "diverse",
    "molti", "molte", "vari", "varie",
}

PERSON_RELATION_EXCLUSIONS = {
    "OTHER", "MENTIONS",
    "RELATED_TO", "ASSOCIATED_WITH",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(path):
    h = hashlib.sha256()

    with Path(path).open("rb") as f:
        for chunk in iter(
            lambda: f.read(1024 * 1024), b""
        ):
            h.update(chunk)

    return h.hexdigest()


def stable_id(prefix, *parts):
    raw = "\x00".join(str(x) for x in parts)
    return prefix + hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:24]


def read_json(path):
    return json.loads(
        Path(path).read_text(encoding="utf-8")
    )


def read_jsonl(path):
    with Path(path).open(encoding="utf-8") as f:
        for number, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path}:{number}: {exc}"
                ) from exc


def write_jsonl(path, records):
    with Path(path).open(
        "w", encoding="utf-8"
    ) as f:
        for record in records:
            f.write(json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
            ) + "\n")


def normalize(value):
    value = unicodedata.normalize(
        "NFKD", str(value or "")
    ).casefold()

    value = "".join(
        char for char in value
        if not unicodedata.combining(char)
    )

    return " ".join(
        re.sub(r"[^\w]+", " ", value).split()
    )


def tokens(value):
    return normalize(value).split()


def source_words(value):
    return re.findall(
        r"[^\W\d_]+",
        str(value or ""),
        flags=re.UNICODE,
    )



def proper_person_name(value):
    """Conservative structural validation of a full person name.

    Does not use a dictionary of occupations or known people.
    Rejects indefinite descriptors and title/location phrases.
    """

    words = source_words(value)

    if len(words) < 2:
        return False

    norm = [normalize(word) for word in words]

    if norm[0] in INDEFINITE_DETERMINERS:
        return False

    # A prepositional complement between capitalized terms
    # is not treated as a validated personal-name structure.
    #
    # "Sindaco di Scafati" -> reject
    # "Vincenzo De Luca" -> allowed
    for i in range(1, len(norm) - 1):
        if (
            norm[i] in {"di", "del", "della", "da"}
            and words[i].islower()
            and norm[i] not in {
                "de", "van", "von",
            }
        ):
            return False

    significant = [
        word for word in words
        if normalize(word) not in PERSON_PARTICLES
    ]

    return (
        len(significant) >= 2
        and all(
            word[0].isupper()
            for word in significant
        )
    )


def contains_surface(text, surface):
    """Match normalized whole-token spans, not substrings."""

    text_tokens = tokens(text)
    name_tokens = tokens(surface)

    if not name_tokens:
        return False

    n = len(name_tokens)

    return any(
        text_tokens[i:i + n] == name_tokens
        for i in range(len(text_tokens) - n + 1)
    )


def contains_coordination(value):
    return any(
        word in COORDINATORS
        for word in tokens(value)
    )


def relation_anchor(profile):
    ctx = profile.get("fact_context") or {}

    predicate = str(
        ctx.get("predicate") or ""
    ).upper().strip()

    counterpart = normalize(
        ctx.get("counterpart_name")
    )

    role = str(
        profile.get("role") or ""
    ).lower()

    if (
        not predicate
        or predicate in PERSON_RELATION_EXCLUSIONS
        or len(counterpart) < 3
        or role not in {"subject", "object"}
    ):
        return None

    return role, predicate, counterpart


# ============================================================
# Source loading
# ============================================================

def load_inputs(profiles_dir, gate_dir):
    p = Path(profiles_dir)
    g = Path(gate_dir)

    ps = read_json(p / "summary.json")
    gs = read_json(g / "summary.json")

    require(
        ps["version"] == "identity-profiles-v1",
        "Unexpected profile version",
    )

    require(
        gs["version"] == "identity-integrity-gate-v1",
        "Unexpected gate version",
    )

    require(
        ps["source_batch_key"]
        == gs["source_batch_key"],
        "Batch mismatch",
    )

    require(
        ps["graph_writes"] == 0
        and gs["graph_writes"] == 0
        and gs["identity_merges"] == 0,
        "Upstream graph mutations",
    )

    paths = [
        (
            p / "mention_profiles.jsonl",
            ps["mention_profiles_sha256"],
        ),
        (
            p / "component_profiles.jsonl",
            ps["component_profiles_sha256"],
        ),
    ]

    paths.extend(
        (
            g / filename,
            expected_hash,
        )
        for filename, expected_hash in gs["checksums"].items()
    )

    for path, expected_hash in paths:
        require(
            sha256(path) == expected_hash,
            f"Checksum mismatch: {path}",
        )

    profiles = {}

    for filename in (
        "mention_profiles.jsonl",
        "component_profiles.jsonl",
    ):
        for record in read_jsonl(p / filename):
            pid = record["profile_id"]

            require(
                pid not in profiles
                and record["canonical_id"] is None
                and record["merge_approved"] is False,
                f"Invalid profile: {pid}",
            )

            profiles[pid] = record

    assignments = {}

    for record in read_jsonl(
        g / "assignment_gate.jsonl"
    ):
        pid = record["profile_id"]

        require(
            pid in profiles
            and pid not in assignments,
            f"Invalid gate assignment: {pid}",
        )

        require(
            record["gate_status"] in VALID_STATUSES
            and not record["graph_write_approved"]
            and record["canonical_entity_id"] is None,
            f"Invalid gate state: {pid}",
        )

        assignments[pid] = record

    require(
        set(profiles) == set(assignments),
        "Incomplete assignment coverage",
    )

    require(
        len(profiles) == gs["profiles_total"],
        "Profile count mismatch",
    )

    manifest = {}

    for record in read_jsonl(
        g / "projection_manifest.jsonl"
    ):
        iid = record["proposed_identity_id"]

        require(
            iid not in manifest,
            f"Repeated identity: {iid}",
        )

        require(
            record["gate_status"] in VALID_STATUSES
            and not record["graph_write_approved"],
            "Invalid identity gate state",
        )

        manifest[iid] = record

    require(
        len(manifest) == gs["identities_total"],
        "Identity count mismatch",
    )

    # Validate the complete occurrence/identity cross-reference.
    by_identity = defaultdict(list)
    approved_by_identity = defaultdict(set)

    for pid, row in assignments.items():
        iid = row["proposed_identity_id"]

        if iid is None:
            require(
                row["gate_status"] != APPROVED,
                f"Approved occurrence without identity: {pid}",
            )
            continue

        require(
            iid in manifest,
            f"Unknown identity: {iid}",
        )

        by_identity[iid].append(pid)

        if row["gate_status"] == APPROVED:
            approved_by_identity[iid].add(pid)

    for iid, item in manifest.items():
        require(
            len(by_identity[iid])
            == item["total_occurrences"],
            f"Identity occurrence count mismatch: {iid}",
        )

        require(
            approved_by_identity[iid]
            == set(item["approved_profile_ids"]),
            f"Approved profile mismatch: {iid}",
        )

    return (
        ps,
        gs,
        profiles,
        assignments,
        manifest,
        by_identity,
    )


# ============================================================
# Corpus-derived reference inventory
# ============================================================

def build_reference_inventory(
    profiles,
    assignments,
    manifest,
):
    """Index stable, independently attested entity names.

    Only use approved identities with >=2 approved occurrences,
    avoiding circular support from unapproved references.
    """

    result = defaultdict(set)

    for iid, record in manifest.items():
        if (
            record["gate_status"] != APPROVED
            or record["approved_occurrences"] < 2
        ):
            continue

        for surface in record["surface_counts"]:
            name = normalize(surface)

            if len(name) < 3:
                continue

            if not any(
                char.isupper() for char in surface
            ):
                continue

            if tokens(name)[0] in INDEFINITE_DETERMINERS:
                continue

            result[name].add(iid)

    # Ambiguous names are not reliable atomicity anchors.
    return {
        name: next(iter(identities))
        for name, identities in result.items()
        if len(identities) == 1
    }


def independent_embedded_entities(
    surface,
    self_id,
    inventory,
):
    """Find multiple separately attested identities inside a name."""

    if not contains_coordination(surface):
        return []

    matches = {}

    for name, identity in inventory.items():
        if identity == self_id:
            continue

        if normalize(surface) == name:
            continue

        if contains_surface(surface, name):
            matches[identity] = name

    return [
        {
            "identity_id": identity,
            "matched_surface": name,
        }
        for identity, name in sorted(matches.items())
    ]


# ============================================================
# Semantic and identity checks
# ============================================================

def identity_semantic_consistency(
    pids,
    profiles,
    assignments,
):
    facets = set()

    for pid in pids:
        profile = profiles[pid]
        row = assignments[pid]

        # Existing semantic facet is preserved from the gate.
        kind = profile["source_kind"]

        if kind == "Person":
            facet = "PERSON"
        elif kind in {
            "Company", "Organization", "PublicAuthority",
            "NonProfitOrganization", "PoliticalParty",
            "LaborUnion", "MediaOutlet",
        }:
            facet = "ORGANIZATION"
        elif kind in {
            "GPE", "LOC", "Location",
            "GeographicalArea", "Address",
        }:
            facet = "GEOGRAPHY"
        elif kind in {
            "Facility", "Infrastructure", "NaturalResource",
        }:
            facet = "PHYSICAL"
        elif kind in {"Event", "Project"}:
            facet = "ACTIVITY"
        elif kind in {
            "Document", "Law", "Regulation", "Contract",
            "Policy", "MediaWork",
        }:
            facet = "DOCUMENT"
        elif kind in {
            "Product", "Service", "Technology",
        }:
            facet = "PRODUCT_SERVICE"
        else:
            facet = "UNKNOWN"

        # Only directly projectable fact mentions matter here.
        if (
            row["gate_status"] == APPROVED
            and profile["profile_scope"]
            == "mention_occurrence"
        ):
            facets.add(facet)

    return facets


def descriptive_activity(surface, facet):
    """Identify long descriptions needing event-level evidence.

    Not a rejection of the event itself.
    """

    if facet != "ACTIVITY":
        return False

    words = tokens(surface)

    return (
        len(words) >= 6
        and any(
            word in NAME_CONNECTORS
            for word in words[1:]
        )
    )


def event_has_distinct_anchor(pids, profiles):
    """Check event-specific anchors, not publication dates.

    Profile publication_date refers to the article,
    so it cannot be used as an event timestamp.
    """

    for pid in pids:
        ctx = profiles[pid].get("fact_context") or {}

        if any(
            ctx.get(field)
            for field in (
                "event_id",
                "event_date",
                "event_location",
                "event_time",
            )
        ):
            return True

    return False


def identity_risks(
    iid,
    pids,
    manifest_row,
    profiles,
    assignments,
    inventory,
):
    reasons = []
    evidence = []

    facets = identity_semantic_consistency(
        pids, profiles, assignments
    )

    if len(facets) > 1 or "UNKNOWN" in facets:
        return (
            BLOCKED,
            ["incompatible_semantic_families"],
            [],
        )

    facet = (
        next(iter(facets))
        if facets
        else next(
            iter(manifest_row["semantic_facet_counts"]),
            None,
        )
    )
    
    if facet == "PERSON":
        if not any(
            proper_person_name(surface)
            for surface in manifest_row["surface_counts"]
        ):
            return (
                CONTEXTUAL,
                ["person_without_full_name_anchor"],
                [],
            )

    if facet == "ORGANIZATION":
        surfaces = list(
            manifest_row["surface_counts"]
        )

        # Names formed only by a capitalized generic head
        # followed by a lowercase modifier are not treated
        # as globally stable solely because they recur.
        has_distinctive_form = any(
            len(source_words(surface)) >= 2
            and sum(
                word[0].isupper()
                for word in source_words(surface)
            ) >= 2
            for surface in surfaces
        )

        has_alias_cluster = any(
            assignments[pid].get(
                "surface_cluster_id"
            )
            for pid in pids
        )

        source_kinds = {
            profiles[pid]["source_kind"]
            for pid in pids
            if assignments[pid]["gate_status"]
            == APPROVED
        }

        if (
            not has_distinctive_form
            and not has_alias_cluster
            and "MediaOutlet" not in source_kinds
            and all(
                len(source_words(surface)) == 2
                for surface in surfaces
            )
        ):
            return (
                CONTEXTUAL,
                ["organization_name_insufficiently_distinctive"],
                [],
            )

    for surface in manifest_row["surface_counts"]:
        # Detect coordination of multiple separately
        # attested entities, not just the word "e".
        embedded = independent_embedded_entities(
            surface, iid, inventory
        )

        if len(embedded) >= 2:
            reasons.append(
                "multi_entity_composite_surface"
            )
            evidence.append({
                "surface": surface,
                "embedded_entities": embedded,
            })

        if facet == "PERSON":
            if not any(
                proper_person_name(name)
                for name in manifest_row["surface_counts"]
            ):
                reasons.append(
                    "person_without_full_name_anchor"
                )

        if descriptive_activity(surface, facet):
            if not event_has_distinct_anchor(
                pids, profiles
            ):
                reasons.append(
                    "descriptive_activity_without_event_anchor"
                )

    if reasons:
        return (
            CONTEXTUAL,
            sorted(set(reasons)),
            evidence,
        )

    return APPROVED, [], evidence


def media_outlet_recovery(
    iid,
    pids,
    manifest_row,
    profiles,
    assignments,
):
    """Recover a qualified media name blocked only by article removal.

    Do not alter generic organizations or override other blockers.
    """

    if manifest_row["gate_status"] != CONTEXTUAL:
        return False

    if set(manifest_row["reasons"]) != {
        "article_stripped_unverified_name"
    }:
        return False

    if len({
        profiles[pid]["article_id"]
        for pid in pids
    }) < 2:
        return False

    if not all(
        profiles[pid]["source_kind"] == "MediaOutlet"
        for pid in pids
    ):
        return False

    surfaces = list(manifest_row["surface_counts"])

    return any(
        len(tokens(name)) >= 2
        and any(
            char.isupper() for char in name
        )
        for name in surfaces
    )


# ============================================================
# Readiness pipeline
# ============================================================

def build_readiness(
    profiles,
    assignments,
    manifest,
    by_identity,
):
    inventory = build_reference_inventory(
        profiles, assignments, manifest
    )

    decisions = {}

    for iid, record in sorted(manifest.items()):
        pids = by_identity[iid]
        upstream = record["gate_status"]

        status = upstream
        reasons = list(record["reasons"])
        evidence = []

        if upstream == APPROVED:
            risk_status, risk_reasons, evidence = (
                identity_risks(
                    iid,
                    pids,
                    record,
                    profiles,
                    assignments,
                    inventory,
                )
            )

            if risk_status != APPROVED:
                status = risk_status
                reasons = risk_reasons

        elif media_outlet_recovery(
            iid,
            pids,
            record,
            profiles,
            assignments,
        ):
            # Recovery is still restricted to occurrences
            # already approved by the upstream occurrence gate.
            # Therefore no previously held occurrence is
            # silently promoted.
            if record["approved_profile_ids"]:
                status = APPROVED
                reasons = [
                    "qualified_media_outlet_name"
                ]

        decisions[iid] = {
            "proposed_identity_id": iid,
            "upstream_gate_status": upstream,
            "readiness_status": status,
            "readiness_reasons": reasons,
            "readiness_evidence": evidence,
        }

    return decisions


def run(args):
    (
        ps,
        gs,
        profiles,
        assignments,
        manifest,
        by_identity,
    ) = load_inputs(
        args.profiles_dir,
        args.gate_dir,
    )

    decisions = build_readiness(
        profiles,
        assignments,
        manifest,
        by_identity,
    )

    # Identity-level demotions propagate only to occurrences
    # associated with the affected proposed identity.
    occurrence_rows = []
    status_counts = Counter()
    changed_occurrences = Counter()

    permitted_by_identity = defaultdict(list)

    for pid in sorted(profiles):
        profile = profiles[pid]
        upstream = assignments[pid]
        iid = upstream["proposed_identity_id"]

        old_status = upstream["gate_status"]
        status = old_status
        reasons = list(upstream["gate_reasons"])

        if iid is not None:
            decision = decisions[iid]

            if (
                old_status == APPROVED
                and decision["readiness_status"]
                != APPROVED
            ):
                status = decision["readiness_status"]
                reasons = decision["readiness_reasons"]

        if status == APPROVED:
            require(
                old_status == APPROVED
                and iid is not None
                and profile["profile_scope"]
                == "mention_occurrence",
                f"Unverified promotion: {pid}",
            )
            permitted_by_identity[iid].append(pid)

        if status != old_status:
            changed_occurrences[
                f"{old_status}->{status}"
            ] += 1

        status_counts[status] += 1

        occurrence_rows.append({
            "profile_id": pid,
            "mention_id": profile["mention_id"],
            "fact_id": profile["fact_id"],
            "article_id": profile["article_id"],
            "role": profile["role"],
            "surface": profile["surface"],
            "source_kind": profile["source_kind"],
            "profile_scope": profile["profile_scope"],
            "proposed_identity_id": iid,
            "upstream_gate_status": old_status,
            "readiness_status": status,
            "readiness_reasons": reasons,
            "canonical_entity_id": None,
            "graph_write_approved": False,
            "fact_propagation_allowed": False,
        })

    manifest_rows = []
    risk_rows = []
    identity_counts = Counter()

    for iid, record in sorted(manifest.items()):
        decision = decisions[iid]
        approved_pids = sorted(
            permitted_by_identity[iid]
        )

        # A previously approved identity can only retain
        # approval if some occurrences are still approved.
        final_status = decision["readiness_status"]

        if (
            final_status == APPROVED
            and not approved_pids
        ):
            final_status = CONTEXTUAL

        identity_counts[final_status] += 1

        item = {
            "proposed_identity_id": iid,
            "upstream_gate_status":
                record["gate_status"],
            "readiness_status": final_status,
            "readiness_reasons":
                decision["readiness_reasons"],
            "readiness_evidence":
                decision["readiness_evidence"],
            "surface_counts":
                record["surface_counts"],
            "semantic_facet_counts":
                record["semantic_facet_counts"],
            "total_occurrences":
                record["total_occurrences"],
            "approved_occurrences":
                len(approved_pids),
            "approved_profile_ids": approved_pids,
            "canonical_entity_created": False,
            "graph_write_approved": False,
        }

        manifest_rows.append(item)

        if (
            final_status != APPROVED
            and record["gate_status"] == APPROVED
        ):
            risk_rows.append(item)

    require(
        len(occurrence_rows) == len(profiles),
        "Incomplete occurrence coverage",
    )

    require(
        sum(status_counts.values())
        == len(profiles),
        "Status count mismatch",
    )

    require(
        sum(
            item["approved_occurrences"]
            for item in manifest_rows
        ) == status_counts[APPROVED],
        "Approved occurrence mismatch",
    )

    require(
        all(
            item["approved_occurrences"] == 0
            for item in manifest_rows
            if item["readiness_status"] != APPROVED
        ),
        "Non-approved identity contains approved occurrences",
    )

    out = Path(args.out_dir)

    require(
        not out.exists(),
        "Output directory already exists",
    )

    out.mkdir(parents=True)

    assignment_path = (
        out / "readiness_assignments.jsonl"
    )
    manifest_path = (
        out / "readiness_manifest.jsonl"
    )
    risk_path = (
        out / "readiness_risks.jsonl"
    )

    write_jsonl(
        assignment_path,
        occurrence_rows,
    )
    write_jsonl(
        manifest_path,
        manifest_rows,
    )
    write_jsonl(
        risk_path,
        risk_rows,
    )

    summary = {
        "version": VERSION,
        "source_batch_key":
            ps["source_batch_key"],
        "source_gate_sha256": sha256(
            Path(args.gate_dir)
            / "projection_manifest.jsonl"
        ),
        "profiles_total": len(profiles),
        "identities_total": len(manifest_rows),
        "reference_inventory_size": len(
            build_reference_inventory(
                profiles, assignments, manifest
            )
        ),
        "upstream_assignment_status_counts":
            gs["assignment_gate_status_counts"],
        "readiness_assignment_status_counts":
            dict(sorted(status_counts.items())),
        "readiness_identity_status_counts":
            dict(sorted(identity_counts.items())),
        "changed_occurrence_counts":
            dict(sorted(changed_occurrences.items())),
        "demoted_identities": len(risk_rows),
        "projection_ready_identities":
            identity_counts[APPROVED],
        "projection_ready_occurrences":
            status_counts[APPROVED],
        "graph_writes": 0,
        "identity_merges": 0,
        "canonical_entities_created": 0,
        "llm_calls": 0,
        "checksums": {
            path.name: sha256(path)
            for path in (
                assignment_path,
                manifest_path,
                risk_path,
            )
        },
    }

    (out / "summary.json").write_text(
        json.dumps(
            summary,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )

    print(json.dumps(
        summary,
        ensure_ascii=False,
        indent=2,
    ))


def main():
    parser = argparse.ArgumentParser(
        description=__doc__
    )

    parser.add_argument(
        "--profiles-dir",
        default=(
            "output/resolution/"
            "identity-profiles-v1"
        ),
    )
    parser.add_argument(
        "--gate-dir",
        default=(
            "output/resolution/"
            "identity-integrity-gate-v1"
        ),
    )
    parser.add_argument(
        "--out-dir",
        default=(
            "output/resolution/"
            "projection-readiness-v1"
        ),
    )

    run(parser.parse_args())


if __name__ == "__main__":
    main()
