
"""PressKG Phase 4E: Final Identity Integrity Gate.

Creates a conservative projection manifest from Phase 4D.

Does not create canonical entities, merge nodes, update
ValidatedFacts, or modify Neo4j.

All identity rules are corpus-independent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata

from collections import Counter, defaultdict
from pathlib import Path


VERSION = "identity-integrity-gate-v1"

APPROVED = "APPROVED_FOR_PROJECTION"
CONTEXTUAL = "CONTEXTUAL_ONLY"
BLOCKED = "BLOCKED"

PERSON = "PERSON"
ORGANIZATION = "ORGANIZATION"
GEOGRAPHY = "GEOGRAPHY"

LEGAL_SUFFIXES = (
    "spa", "srl", "snc", "sas", "scarl",
)

LEGAL_TOKENS = {
    ("spa",), ("s", "p", "a"),
    ("srl",), ("s", "r", "l"),
    ("snc",), ("s", "n", "c"),
    ("sas",), ("s", "a", "s"),
    ("scarl",), ("s", "c", "a", "r", "l"),
}

ARTICLES = {
    "il", "lo", "la", "l", "i", "gli", "le",
}

PERSON_PARTICLES = {
    "de", "di", "del", "della",
    "da", "van", "von",
}

GENERATED_PERSON_RULES = {
    "PERSON_UNIQUE_IN_ARTICLE",
    "PERSON_MULTI_RELATION",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(path):
    digest = hashlib.sha256()

    with Path(path).open("rb") as f:
        for block in iter(
            lambda: f.read(1024 * 1024), b""
        ):
            digest.update(block)

    return digest.hexdigest()


def read_json(path):
    return json.loads(
        Path(path).read_text(encoding="utf-8")
    )


def read_jsonl(path):
    with Path(path).open(encoding="utf-8") as f:
        for line_number, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path}:{line_number}: {exc}"
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


def has_legal_form(surface):
    tokens = normalize(surface).split()

    if not tokens:
        return False

    if any(
        len(tokens) >= len(form)
        and tuple(tokens[-len(form):]) == form
        for form in LEGAL_TOKENS
    ):
        return True

    if len(tokens) == 1:
        word = tokens[0]
        return any(
            word.endswith(suffix)
            and len(word) >= len(suffix) + 3
            for suffix in LEGAL_SUFFIXES
        )

    return False


def meaningful_tokens(surface):
    tokens = normalize(surface).split()

    if len(tokens) > 1 and tokens[0] in ARTICLES:
        tokens = tokens[1:]

    return tokens


def proper_name_surface(surface):
    """A weak capitalization signal, never identity proof."""

    text = str(surface or "").strip()

    if not text:
        return False

    return any(c.isupper() for c in text)


def plausible_full_person_name(surface):
    """Reject descriptive phrases masquerading as full names.

    Requires at least two capitalized lexical name components,
    ignoring surname particles.

    Does not claim to solve all person-name structures.
    """

    raw = re.findall(
        r"[^\W\d_]+",
        str(surface or ""),
        flags=re.UNICODE,
    )

    if len(raw) < 2:
        return False

    significant = [
        token for token in raw
        if normalize(token) not in PERSON_PARTICLES
    ]

    return (
        len(significant) >= 2
        and all(
            token[0].isupper()
            for token in significant
        )
    )


def short_name_matches(short, full):
    a = normalize(short).split()
    b = normalize(full).split()

    return (
        bool(a)
        and len(b) > len(a)
        and b[-len(a):] == a
    )


def relation_signature(profile):
    context = profile.get("fact_context") or {}

    predicate = str(
        context.get("predicate") or ""
    ).upper().strip()

    counterpart = normalize(
        context.get("counterpart_name")
    )

    role = str(
        profile.get("role") or ""
    ).lower()

    if (
        not predicate
        or predicate in {
            "OTHER", "MENTIONS",
            "RELATED_TO", "ASSOCIATED_WITH",
        }
        or not counterpart
        or role not in {"subject", "object"}
    ):
        return None

    return (role, predicate, counterpart)


# ============================================================
# Source integrity
# ============================================================

def load_inputs(
    profiles_dir,
    assignment_dir,
    refinement_dir,
    audit_dir,
):
    paths = [
        Path(profiles_dir),
        Path(assignment_dir),
        Path(refinement_dir),
        Path(audit_dir),
    ]

    ps, ass, rs, ds = [
        read_json(p / "summary.json")
        for p in paths
    ]

    expected = [
        "identity-profiles-v1",
        "occurrence-assignment-v1",
        "contextual-identity-refinement-v2",
        "coreference-audit-v1",
    ]

    for summary, version in zip(
        (ps, ass, rs, ds), expected
    ):
        require(
            summary["version"] == version,
            f"Unexpected version: {summary['version']}",
        )
        require(
            summary["graph_writes"] == 0
            and summary["identity_merges"] == 0,
            "Upstream graph mutations detected",
        )

    batch = ps["source_batch_key"]

    require(
        all(
            summary["source_batch_key"] == batch
            for summary in (ass, rs, ds)
        ),
        "Source batch mismatch",
    )

    require(
        rs["source_assignment_sha256"]
        == ass["checksums"]["occurrence_assignments.jsonl"],
        "4B/4C lineage mismatch",
    )

    require(
        ds["source_refinement_sha256"]
        == rs["checksums"]["refined_assignments.jsonl"],
        "4C/4D lineage mismatch",
    )

    files = [
        (
            paths[0] / "mention_profiles.jsonl",
            ps["mention_profiles_sha256"],
        ),
        (
            paths[0] / "component_profiles.jsonl",
            ps["component_profiles_sha256"],
        ),
        (
            paths[1] / "occurrence_assignments.jsonl",
            ass["checksums"]["occurrence_assignments.jsonl"],
        ),
        (
            paths[2] / "refined_assignments.jsonl",
            rs["checksums"]["refined_assignments.jsonl"],
        ),
        (
            paths[3] / "refined_assignments.jsonl",
            ds["checksums"]["refined_assignments.jsonl"],
        ),
        (
            paths[3] / "refined_identities.jsonl",
            ds["checksums"]["refined_identities.jsonl"],
        ),
    ]

    for path, expected_hash in files:
        require(
            sha256(path) == expected_hash,
            f"Checksum mismatch: {path}",
        )

    profiles = {}

    for filename in (
        "mention_profiles.jsonl",
        "component_profiles.jsonl",
    ):
        for profile in read_jsonl(paths[0] / filename):
            pid = profile["profile_id"]
            require(pid not in profiles, "Duplicate profile")
            require(
                profile["canonical_id"] is None
                and not profile["merge_approved"],
                "Already canonicalized source profile",
            )
            profiles[pid] = profile

    rows = {}

    for record in read_jsonl(
        paths[3] / "refined_assignments.jsonl"
    ):
        pid = record["profile_id"]
        require(
            pid not in rows,
            "Duplicate assignment",
        )
        rows[pid] = record

    require(
        set(profiles) == set(rows),
        "Incomplete assignment coverage",
    )

    require(
        len(profiles)
        == ps["mention_profiles"]
        + ps["component_profiles"]
        == ds["profiles_total"],
        "Profile count mismatch",
    )

    identity_summary = {}

    for record in read_jsonl(
        paths[3] / "refined_identities.jsonl"
    ):
        iid = record["proposed_identity_id"]
        require(
            iid not in identity_summary,
            "Duplicate identity summary",
        )
        identity_summary[iid] = record

    return (
        batch,
        profiles,
        rows,
        identity_summary,
        ds,
    )


# ============================================================
# Corpus-derived identity evidence
# ============================================================

def build_evidence(profiles, rows):
    members = defaultdict(list)
    person_anchors = defaultdict(
        lambda: defaultdict(list)
    )

    for pid, row in rows.items():
        identity = row["proposed_identity_id"]

        if row["assignment_status"] != "PROVISIONAL":
            require(
                identity is None,
                f"Non-provisional identity: {pid}",
            )
            continue

        require(
            identity is not None,
            f"Missing provisional identity: {pid}",
        )

        profile = profiles[pid]
        members[identity].append(pid)

        if (
            profile["source_kind"] == "Person"
            and plausible_full_person_name(
                profile["surface"]
            )
        ):
            person_anchors[
                profile["article_id"]
            ][identity].append(pid)

    return members, person_anchors


def validate_person_coreference(
    profile,
    identity,
    profiles,
    anchors,
):
    """Validate the exact occurrence, not a whole surname group."""

    article = profile["article_id"]

    candidates = defaultdict(list)

    for candidate_identity, pids in anchors.get(
        article, {}
    ).items():
        for pid in pids:
            other = profiles[pid]

            if short_name_matches(
                profile["surface"],
                other["surface"],
            ):
                candidates[candidate_identity].append(pid)

    # An unrelated or descriptive "full name" must not be
    # accepted as an anchor.
    if set(candidates) != {identity}:
        return False, "missing_or_competing_person_anchor"

    if not proper_name_surface(profile["surface"]):
        return False, "non_proper_person_reference"

    matching = candidates[identity]
    own_signature = relation_signature(profile)

    shared = any(
        own_signature is not None
        and own_signature == relation_signature(
            profiles[pid]
        )
        for pid in matching
    )

    if shared:
        return True, "same_article_shared_relation"

    # Same-article unique full-name anchor is sufficient
    # only for a contextual proposal, not global approval.
    return False, "coreference_without_relation_corroboration"


def identity_strength(
    identity,
    pids,
    profiles,
    rows,
):
    """Return whether an identity can be proposed globally.

    Repetition alone is insufficient for generic descriptions.
    """

    facets = {
        rows[pid]["occurrence_semantic_facet"]
        for pid in pids
    }

    if len(facets) != 1 or None in facets:
        return BLOCKED, ["mixed_or_unknown_semantic_facets"]

    facet = next(iter(facets))
    articles = {
        profiles[pid]["article_id"]
        for pid in pids
    }

    names = {
        profiles[pid]["surface"]
        for pid in pids
    }

    has_cluster = any(
        rows[pid].get("surface_cluster_id")
        for pid in pids
    )

    warnings = {
        warning
        for pid in pids
        for warning in rows[pid].get(
            "phase4d_warnings", []
        )
    }

    if warnings:
        return CONTEXTUAL, sorted(warnings)

    if len(articles) < 2:
        return CONTEXTUAL, ["single_article_identity"]

    if facet == PERSON:
        if any(
            plausible_full_person_name(name)
            for name in names
        ):
            return APPROVED, [
                "repeated_full_person_name"
            ]

        return CONTEXTUAL, [
            "no_valid_full_person_name"
        ]

    if facet == ORGANIZATION:
        # An explicit legal-form variant or documented alias
        # distinguishes a single-word proper name from a
        # generic descriptive label.
        if any(has_legal_form(name) for name in names):
            return APPROVED, [
                "observed_legal_form"
            ]

        if has_cluster:
            return APPROVED, [
                "documented_alias_cluster"
            ]

        if any(
            len(meaningful_tokens(name)) >= 2
            and proper_name_surface(name)
            for name in names
        ):
            return APPROVED, [
                "repeated_multiword_organization_name"
            ]

        return CONTEXTUAL, [
            "unanchored_single_word_organization"
        ]

    if facet == GEOGRAPHY:
        if any(
            proper_name_surface(name)
            for name in names
        ):
            return APPROVED, [
                "repeated_proper_geographical_name"
            ]

        return CONTEXTUAL, [
            "geography_without_proper_name_evidence"
        ]

    if (
        has_cluster
        or any(
            len(meaningful_tokens(name)) >= 2
            and proper_name_surface(name)
            for name in names
        )
    ):
        return APPROVED, [
            "repeated_distinctive_name"
        ]

    return CONTEXTUAL, [
        "insufficient_global_identity_evidence"
    ]


# ============================================================
# Final decision
# ============================================================

def decide_occurrence(
    profile,
    row,
    identity_status,
    identity_reasons,
    person_anchors,
    profiles,
):
    previous = row["assignment_status"]

    if previous == "OUT_OF_SCOPE":
        return BLOCKED, [
            "excluded_by_upstream_eligibility"
        ]

    if previous == "HOLD_FOR_CONTEXT":
        return CONTEXTUAL, (
            row.get("blockers") or ["upstream_context_hold"]
        )
        
    if profile["profile_scope"] != "mention_occurrence":
        return CONTEXTUAL, [
            "decomposed_component_requires_fact_reanchoring"
        ]

    require(
        previous == "PROVISIONAL",
        f"Unknown upstream status: {previous}",
    )

    if row.get("phase4d_warnings"):
        return CONTEXTUAL, sorted(
            set(row["phase4d_warnings"])
        )

    if (
        row.get("phase4d_rule")
        in GENERATED_PERSON_RULES
    ):
        supported, reason = validate_person_coreference(
            profile,
            row["proposed_identity_id"],
            profiles,
            person_anchors,
        )

        if not supported:
            return CONTEXTUAL, [reason]

    return identity_status, identity_reasons


# ============================================================
# Runner
# ============================================================

def run(args):
    (
        batch,
        profiles,
        rows,
        identity_summaries,
        upstream_summary,
    ) = load_inputs(
        args.profiles_dir,
        args.assignments_dir,
        args.refinement_dir,
        args.audit_dir,
    )

    members, person_anchors = build_evidence(
        profiles, rows
    )

    require(
        set(members) == set(identity_summaries),
        "Identity inventory mismatch",
    )

    identity_decisions = {}

    for identity, pids in sorted(members.items()):
        require(
            len(pids)
            == identity_summaries[identity][
                "occurrence_count"
            ],
            f"Identity count mismatch: {identity}",
        )

        status, reasons = identity_strength(
            identity, pids, profiles, rows
        )

        identity_decisions[identity] = {
            "proposed_identity_id": identity,
            "gate_status": status,
            "reasons": reasons,
        }

    assignment_decisions = {}
    assignment_statuses = Counter()
    identity_statuses = Counter()
    reason_counts = Counter()

    for decision in identity_decisions.values():
        identity_statuses[
            decision["gate_status"]
        ] += 1

    accepted_occurrences = defaultdict(list)
    contextual_occurrences = defaultdict(list)

    for pid in sorted(profiles):
        profile = profiles[pid]
        row = rows[pid]
        identity = row["proposed_identity_id"]

        if identity is None:
            parent_status = CONTEXTUAL
            parent_reasons = [
                "no_provisional_identity"
            ]
        else:
            parent = identity_decisions[identity]
            parent_status = parent["gate_status"]
            parent_reasons = parent["reasons"]

        status, reasons = decide_occurrence(
            profile,
            row,
            parent_status,
            parent_reasons,
            person_anchors,
            profiles,
        )

        record = {
            "profile_id": pid,
            "mention_id": profile["mention_id"],
            "fact_id": profile["fact_id"],
            "article_id": profile["article_id"],
            "role": profile["role"],
            "surface": profile["surface"],
            "source_kind": profile["source_kind"],
            "profile_scope": profile["profile_scope"],
            "proposed_identity_id": identity,
            "gate_status": status,
            "gate_reasons": reasons,
            "upstream_status": row["assignment_status"],
            "phase4d_rule": row.get("phase4d_rule"),
            "canonical_entity_id": None,
            "graph_write_approved": False,
            "fact_propagation_allowed": False,
        }

        assignment_decisions[pid] = record
        assignment_statuses[status] += 1
        reason_counts.update(reasons)

        if identity and status == APPROVED:
            accepted_occurrences[identity].append(pid)

        elif identity and status == CONTEXTUAL:
            contextual_occurrences[identity].append(pid)

    manifest = []

    for identity, value in sorted(
        identity_decisions.items()
    ):
        pids = members[identity]
        permitted = accepted_occurrences[identity]

        # An identity without any approved occurrences
        # cannot be materialized even if its group passed.
        final_status = (
            APPROVED
            if value["gate_status"] == APPROVED
            and permitted
            else CONTEXTUAL
            if value["gate_status"] != BLOCKED
            else BLOCKED
        )

        manifest.append({
            "proposed_identity_id": identity,
            "gate_status": final_status,
            "reasons": value["reasons"],
            "semantic_facet_counts":
                identity_summaries[identity][
                    "semantic_facet_counts"
                ],
            "surface_counts":
                identity_summaries[identity][
                    "surface_counts"
                ],
            "total_occurrences": len(pids),
            "approved_occurrences": len(permitted),
            "contextual_occurrences": len(
                contextual_occurrences[identity]
            ),
            "approved_profile_ids": sorted(permitted),
            "canonical_entity_created": False,
            "graph_write_approved": False,
        })

    out = Path(args.out_dir)
    require(
        not out.exists(),
        "Output directory already exists",
    )

    out.mkdir(parents=True)

    assignment_path = (
        out / "assignment_gate.jsonl"
    )
    manifest_path = (
        out / "projection_manifest.jsonl"
    )
    risk_path = (
        out / "integrity_risks.jsonl"
    )

    write_jsonl(
        assignment_path,
        (
            assignment_decisions[pid]
            for pid in sorted(assignment_decisions)
        ),
    )

    write_jsonl(manifest_path, manifest)

    write_jsonl(
        risk_path,
        (
            {
                "proposed_identity_id":
                    record["proposed_identity_id"],
                "gate_status": record["gate_status"],
                "reasons": record["reasons"],
                "total_occurrences":
                    record["total_occurrences"],
                "surface_counts":
                    record["surface_counts"],
            }
            for record in manifest
            if record["gate_status"] != APPROVED
        ),
    )

    require(
        sum(assignment_statuses.values())
        == len(profiles),
        "Incomplete assignment gate coverage",
    )

    require(
        sum(
            record["total_occurrences"]
            for record in manifest
        )
        == upstream_summary["status_counts"]["PROVISIONAL"],
        "Identity occurrence count mismatch",
    )

    require(
        sum(
            record["approved_occurrences"]
            for record in manifest
        )
        == assignment_statuses[APPROVED],
        "Approved occurrence count mismatch",
    )

    summary = {
        "version": VERSION,
        "source_batch_key": batch,
        "source_audit_sha256": sha256(
            Path(args.audit_dir)
            / "refined_assignments.jsonl"
        ),
        "profiles_total": len(profiles),
        "identities_total": len(manifest),
        "assignment_gate_status_counts":
            dict(sorted(assignment_statuses.items())),
        "identity_gate_status_counts":
            dict(Counter(
                record["gate_status"]
                for record in manifest
            )),
        "reason_counts": dict(
            sorted(reason_counts.items())
        ),
        "identity_merges": 0,
        "canonical_entities_created": 0,
        "graph_writes": 0,
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
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        "--profiles-dir",
        default="output/resolution/identity-profiles-v1",
    )
    parser.add_argument(
        "--assignments-dir",
        default="output/resolution/occurrence-assignment-v1",
    )
    parser.add_argument(
        "--refinement-dir",
        default="output/resolution/contextual-identity-refinement-v2",
    )
    parser.add_argument(
        "--audit-dir",
        default="output/resolution/coreference-audit-v1",
    )
    parser.add_argument(
        "--out-dir",
        default="output/resolution/identity-integrity-gate-v1",
    )

    run(parser.parse_args())


if __name__ == "__main__":
    main()
