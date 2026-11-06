
"""Occurrence-aware provisional identity assignments.

Conservative rules:
- Exact surface is a blocking bucket, not a canonical entity.
- Different semantic facets never share a provisional identity.
- Minority or incompatible annotations remain unresolved.
- Short person names require contextual disambiguation.
- Cluster membership is advisory for occurrences.
- No automatic merge approval, fact propagation or graph writes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata

from collections import Counter, defaultdict
from pathlib import Path


VERSION = "occurrence-assignment-v1"

ORG = {
    "Company",
    "Organization",
    "PublicAuthority",
    "NonProfitOrganization",
    "PoliticalParty",
    "LaborUnion",
    "MediaOutlet",
}

GEO = {
    "GPE",
    "Location",
    "LOC",
    "GeographicalArea",
    "Address",
}

PHYSICAL = {
    "Facility",
    "Infrastructure",
    "NaturalResource",
}

DOCUMENT = {
    "Document",
    "Contract",
    "Law",
    "Regulation",
    "Policy",
    "MediaWork",
}

REJECTED_STATUSES = {
    "NON_IDENTITY",
    "COLLECTIVE_HOLD",
    "COMPOSITE_MENTION_HOLD",
    "PROFILE_ONLY_HELD",
}

REJECTED_KINDS = {
    "Money",
    "Quantity",
    "Date",
    "Time",
    "Percentage",
    "Duration",
    "Topic",
    "PopulationGroup",
}

AUDIT_TARGETS = {
    "regione campania",
    "campania",
    "gori",
    "pasquale aliberti",
    "luca mascolo",
    "vincenzo de luca",
    "de luca",
    "aliberti",
}


# ============================================================
# Utilities
# ============================================================

def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    hasher = hashlib.sha256()

    with Path(path).open("rb") as handle:
        for chunk in iter(
            lambda: handle.read(1024 * 1024),
            b"",
        ):
            hasher.update(chunk)

    return hasher.hexdigest()


def load_json(path):
    return json.loads(
        Path(path).read_text(encoding="utf-8")
    )


def read_jsonl(path):
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue

            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path}:{line_number}: {exc}"
                ) from exc


def write(handle, record):
    handle.write(
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
        ) + "\n"
    )


def stable_id(prefix, *parts):
    payload = "\x00".join(map(str, parts))

    return prefix + hashlib.sha256(
        payload.encode("utf-8")
    ).hexdigest()[:24]


def normalize(value):
    value = unicodedata.normalize(
        "NFKD",
        str(value or ""),
    ).casefold()

    value = "".join(
        char
        for char in value
        if not unicodedata.combining(char)
    )

    return " ".join(
        re.sub(r"[^\w]+", " ", value).split()
    )


def facet(kind):
    """Map the original ontology to broad semantic facets."""

    if kind == "Person":
        return "PERSON"

    if kind in ORG:
        return "ORGANIZATION"

    if kind in GEO:
        return "GEOGRAPHY"

    if kind in PHYSICAL:
        return "PHYSICAL"

    if kind in DOCUMENT:
        return "DOCUMENT"

    if kind in {"Project", "Event"}:
        return "ACTIVITY"

    if kind in {"Product", "Service", "Technology"}:
        return "PRODUCT_SERVICE"

    return None


# ============================================================
# Input loading and integrity checks
# ============================================================

def load_inputs(
    profiles_dir,
    candidates_dir,
    clusters_dir,
):
    profiles_path = Path(profiles_dir)
    candidates_path = Path(candidates_dir)
    clusters_path = Path(clusters_dir)

    ps = load_json(
        profiles_path / "summary.json"
    )

    cs = load_json(
        candidates_path / "summary.json"
    )

    ks = load_json(
        clusters_path / "summary.json"
    )

    expected_versions = (
        (ps, "identity-profiles-v1"),
        (cs, "candidate-generation-v1"),
        (ks, "constrained-clustering-v1"),
    )

    for summary, expected_version in expected_versions:
        require(
            summary["version"] == expected_version,
            f"Unexpected version: {summary['version']}",
        )

        require(
            summary["graph_writes"] == 0
            and summary["identity_merges"] == 0,
            "Unexpected source mutations",
        )

    require(
        ps["source_batch_key"]
        == cs["source_batch_key"]
        == ks["source_batch_key"],
        "Batch mismatch",
    )

    require(
        cs["source_mention_sha256"]
        == ps["mention_profiles_sha256"],
        "Mention lineage mismatch",
    )

    require(
        cs["source_component_sha256"]
        == ps["component_profiles_sha256"],
        "Component lineage mismatch",
    )

    require(
        ks["source_candidate_sha256"]
        == cs["candidate_pairs_sha256"],
        "Cluster lineage mismatch",
    )

    checks = [
        (
            profiles_path / "mention_profiles.jsonl",
            ps["mention_profiles_sha256"],
        ),
        (
            profiles_path / "component_profiles.jsonl",
            ps["component_profiles_sha256"],
        ),
        (
            candidates_path / "surface_groups.jsonl",
            cs["surface_groups_sha256"],
        ),
    ]

    for filename, expected in ks["checksums"].items():
        checks.append((
            clusters_path / filename,
            expected,
        ))

    for path, expected_checksum in checks:
        require(
            digest(path) == expected_checksum,
            f"Checksum mismatch: {path}",
        )

    # --------------------------------------------------------
    # Surface groups and eligible occurrence mapping
    # --------------------------------------------------------

    groups = {}
    occurrence_to_group = {}

    for group in read_jsonl(
        candidates_path / "surface_groups.jsonl"
    ):
        gid = group["group_id"]

        require(
            gid not in groups
            and not group["identity_approved"],
            f"Invalid surface group: {gid}",
        )

        require(
            len(group["occurrence_ids"])
            == group["occurrence_count"],
            f"Group count mismatch: {gid}",
        )

        groups[gid] = group

        for profile_id in group["occurrence_ids"]:
            require(
                profile_id not in occurrence_to_group,
                f"Repeated occurrence: {profile_id}",
            )

            occurrence_to_group[profile_id] = gid

    require(
        len(groups) == cs["surface_groups"],
        "Surface group count mismatch",
    )

    require(
        len(occurrence_to_group)
        == cs["eligible_profiles"],
        "Eligible occurrence count mismatch",
    )

    # --------------------------------------------------------
    # Constrained surface clusters
    # --------------------------------------------------------

    cluster_for_group = {}
    clusters = {}

    for cluster in read_jsonl(
        clusters_path / "surface_clusters.jsonl"
    ):
        cid = cluster["cluster_id"]

        require(
            cid not in clusters,
            f"Repeated cluster: {cid}",
        )

        require(
            not cluster["canonical_entity_materialized"],
            "Unexpected canonical materialization",
        )

        clusters[cid] = cluster

        for gid in cluster["group_ids"]:
            require(
                gid in groups
                and gid not in cluster_for_group,
                f"Invalid/repeated group in clusters: {gid}",
            )

            cluster_for_group[gid] = cid

    memberships = {}

    for membership in read_jsonl(
        clusters_path / "cluster_memberships.jsonl"
    ):
        gid = membership["group_id"]
        cid = membership["cluster_id"]

        require(
            gid not in memberships
            and cluster_for_group.get(gid) == cid,
            "Cluster membership mismatch",
        )

        require(
            not membership["occurrence_assignment_approved"],
            "Occurrence already approved",
        )

        memberships[gid] = cid

    require(
        memberships == cluster_for_group,
        "Incomplete cluster memberships",
    )

    require(
        len(clusters) == ks["provisional_clusters"],
        "Cluster count mismatch",
    )

    # --------------------------------------------------------
    # All mention and component profiles
    # --------------------------------------------------------

    profiles = {}

    sources = (
        (
            "mention_profiles.jsonl",
            "mention_profiles",
        ),
        (
            "component_profiles.jsonl",
            "component_profiles",
        ),
    )

    for filename, count_key in sources:
        count = 0

        for profile in read_jsonl(
            profiles_path / filename
        ):
            count += 1

            pid = profile["profile_id"]

            require(
                pid not in profiles
                and profile["canonical_id"] is None
                and profile["merge_approved"] is False,
                f"Invalid/repeated profile: {pid}",
            )

            profiles[pid] = profile

        require(
            count == ps[count_key],
            f"Profile count mismatch: {filename}",
        )

    require(
        len(profiles)
        == ps["mention_profiles"]
        + ps["component_profiles"],
        "Missing profiles",
    )

    require(
        set(occurrence_to_group).issubset(profiles),
        "Group occurrence missing from profiles",
    )

    for pid, gid in occurrence_to_group.items():
        require(
            normalize(profiles[pid]["surface"])
            == groups[gid]["normalized_surface"],
            f"Profile/group surface mismatch: {pid}",
        )

    return (
        ps,
        cs,
        ks,
        profiles,
        groups,
        occurrence_to_group,
        cluster_for_group,
        clusters,
    )


# ============================================================
# Surface semantic statistics
# ============================================================

def group_statistics(groups):
    """Calculate dominant semantic family for each surface."""

    result = {}

    for gid, group in groups.items():
        totals = Counter()

        for kind, count in group["kind_counts"].items():
            semantic_facet = facet(kind)

            if semantic_facet:
                totals[semantic_facet] += count

        if totals:
            dominant, count = sorted(
                totals.items(),
                key=lambda item: (
                    -item[1],
                    item[0],
                ),
            )[0]
        else:
            dominant, count = None, 0

        total_occurrences = group["occurrence_count"]

        result[gid] = {
            "dominant_facet": dominant,
            "dominant_fraction": (
                count / total_occurrences
                if total_occurrences
                else 0.0
            ),
            "facet_counts": dict(
                sorted(totals.items())
            ),
        }

    return result


# ============================================================
# Occurrence-aware decision
# ============================================================

def decide(
    profile,
    group,
    stats,
    cluster_id,
    *,
    min_dominance=0.80,
):
    """Propose an occurrence identity without approving it.

    Returns:
        (assignment_status, proposed_identity_id, blockers)
    """

    source_kind = profile["source_kind"]
    occurrence_facet = facet(source_kind)

    status = profile["resolution_status"]
    surface = group["normalized_surface"]

    dominant = stats["dominant_facet"]
    dominance = stats["dominant_fraction"]

    blockers = []

    # --------------------------------------------------------
    # Source eligibility
    # --------------------------------------------------------

    if (
        status in REJECTED_STATUSES
        or source_kind in REJECTED_KINDS
    ):
        blockers.append(
            "nonreferential_or_held_source"
        )

    elif occurrence_facet is None:
        blockers.append(
            "unknown_semantic_facet"
        )

    elif (
        not dominant
        or dominance < min_dominance
    ):
        blockers.append(
            "unstable_surface_group_facet"
        )

    elif occurrence_facet != dominant:
        blockers.append(
            "minority_or_conflicting_semantic_facet"
        )

    # --------------------------------------------------------
    # Person name ambiguity
    # --------------------------------------------------------

    tokens = surface.split()

    if occurrence_facet == "PERSON":
        short_person_name = (
            len(tokens) < 2
            or (
                len(tokens) == 2
                and tokens[0] in {
                    "de",
                    "di",
                    "del",
                    "della",
                    "da",
                    "van",
                    "von",
                }
            )
        )

        if short_person_name:
            blockers.append(
                "short_person_name_ambiguous"
            )

    # --------------------------------------------------------
    # Decomposed component eligibility
    # --------------------------------------------------------

    if (
        profile["profile_scope"]
        == "decomposed_component"
        and status != "PROFILE_ONLY_CANDIDATE"
    ):
        blockers.append(
            "component_requires_reassessment"
        )

    # --------------------------------------------------------
    # Deferred occurrence
    # --------------------------------------------------------

    if blockers:
        return (
            "HOLD_FOR_CONTEXT",
            None,
            sorted(set(blockers)),
        )

    # --------------------------------------------------------
    # Provisional identity
    # --------------------------------------------------------

    # Prefer the documented alias cluster when present.
    # Otherwise use the exact surface group.
    #
    # The semantic facet is part of the identifier so that
    # PERSON and ORGANIZATION cannot accidentally share it.

    scope = (
        cluster_id
        if cluster_id is not None
        else group["group_id"]
    )

    proposed_identity_id = stable_id(
        "pi-",
        profile["profile_version"],
        scope,
        occurrence_facet,
    )

    return (
        "PROVISIONAL",
        proposed_identity_id,
        [],
    )


# ============================================================
# Main pipeline
# ============================================================

def run(args):
    require(
        0.5 <= args.min_dominance <= 1.0,
        "Invalid dominance threshold",
    )

    (
        profile_summary,
        candidate_summary,
        cluster_summary,
        profiles,
        groups,
        occurrence_to_group,
        cluster_for_group,
        clusters,
    ) = load_inputs(
        args.profiles_dir,
        args.candidates_dir,
        args.clusters_dir,
    )

    stats = group_statistics(groups)

    output_dir = Path(args.out_dir)

    require(
        not output_dir.exists(),
        "Output directory already exists",
    )

    output_dir.mkdir(parents=True)

    assignments_path = (
        output_dir / "occurrence_assignments.jsonl"
    )

    identities_path = (
        output_dir / "provisional_identities.jsonl"
    )

    audit_path = (
        output_dir / "fragmentation_audit.jsonl"
    )

    # --------------------------------------------------------
    # Accumulators
    # --------------------------------------------------------

    assignment_counts = Counter()
    blocker_counts = Counter()

    proposals = {}

    assigned_by_group = Counter()
    total_by_group = Counter()

    audit = defaultdict(
        lambda: {
            "status_counts": Counter(),
            "kind_status_counts": Counter(),
            "blocker_counts": Counter(),
            "proposal_ids": set(),
            "article_ids": set(),
        }
    )

    # --------------------------------------------------------
    # Evaluate every profile
    # --------------------------------------------------------

    with assignments_path.open(
        "w",
        encoding="utf-8",
    ) as destination:

        for profile_id in sorted(profiles):
            profile = profiles[profile_id]

            group_id = occurrence_to_group.get(
                profile_id
            )

            group = groups.get(group_id)

            if group is None:
                assignment_status = "OUT_OF_SCOPE"
                proposed_identity_id = None

                reasons = [
                    "not_eligible_for_candidate_generation"
                ]

                cluster_id = None
                group_stats = None

            else:
                cluster_id = cluster_for_group.get(
                    group_id
                )

                group_stats = stats[group_id]

                (
                    assignment_status,
                    proposed_identity_id,
                    reasons,
                ) = decide(
                    profile,
                    group,
                    group_stats,
                    cluster_id,
                    min_dominance=args.min_dominance,
                )

                total_by_group[group_id] += 1

                if proposed_identity_id:
                    assigned_by_group[group_id] += 1

                    proposal = proposals.setdefault(
                        proposed_identity_id,
                        {
                            "provisional_identity_id":
                                proposed_identity_id,

                            "surface_group_ids": set(),
                            "surface_cluster_ids": set(),

                            "semantic_facet":
                                facet(profile["source_kind"]),

                            "occurrence_count": 0,
                            "article_ids": set(),

                            "canonical_entity_created": False,
                            "identity_approved": False,
                        },
                    )

                    require(
                        proposal["semantic_facet"]
                        == facet(profile["source_kind"]),
                        "Semantic facet collision",
                    )

                    proposal["surface_group_ids"].add(
                        group_id
                    )

                    if cluster_id:
                        proposal["surface_cluster_ids"].add(
                            cluster_id
                        )

                    proposal["occurrence_count"] += 1

                    proposal["article_ids"].add(
                        profile["article_id"]
                    )

            # ------------------------------------------------
            # Assignment record
            # ------------------------------------------------

            record = {
                "profile_id": profile_id,
                "mention_id": profile["mention_id"],
                "profile_scope": profile["profile_scope"],

                "fact_id": profile["fact_id"],
                "article_id": profile["article_id"],
                "role": profile["role"],

                "surface": profile["surface"],
                "source_kind": profile["source_kind"],

                "source_resolution_status":
                    profile["resolution_status"],

                "surface_group_id": group_id,
                "surface_cluster_id": cluster_id,

                "dominant_semantic_facet": (
                    group_stats or {}
                ).get("dominant_facet"),

                "dominant_facet_fraction": (
                    round(
                        group_stats["dominant_fraction"],
                        5,
                    )
                    if group_stats
                    else None
                ),

                "occurrence_semantic_facet":
                    facet(profile["source_kind"]),

                "assignment_status":
                    assignment_status,

                "proposed_identity_id":
                    proposed_identity_id,

                "blockers": reasons,

                "canonical_entity_id": None,
                "identity_assignment_approved": False,
                "fact_propagation_allowed": False,
            }

            write(destination, record)

            assignment_counts[
                assignment_status
            ] += 1

            blocker_counts.update(reasons)

            # ------------------------------------------------
            # Fragmentation audit
            # ------------------------------------------------

            normalized_surface = (
                group["normalized_surface"]
                if group
                else profile["normalized_surface"]
            )

            if normalized_surface in AUDIT_TARGETS:
                item = audit[normalized_surface]

                item["status_counts"][
                    assignment_status
                ] += 1

                kind_status = (
                    f"{profile['source_kind']}:"
                    f"{assignment_status}"
                )

                item["kind_status_counts"][
                    kind_status
                ] += 1

                item["blocker_counts"].update(
                    reasons
                )

                if proposed_identity_id:
                    item["proposal_ids"].add(
                        proposed_identity_id
                    )

                item["article_ids"].add(
                    profile["article_id"]
                )

    # --------------------------------------------------------
    # Export provisional identities
    # --------------------------------------------------------

    with identities_path.open(
        "w",
        encoding="utf-8",
    ) as destination:

        for identity_id in sorted(proposals):
            proposal = proposals[identity_id]

            proposal["surface_group_ids"] = sorted(
                proposal["surface_group_ids"]
            )

            proposal["surface_cluster_ids"] = sorted(
                proposal["surface_cluster_ids"]
            )

            proposal["article_count"] = len(
                proposal.pop("article_ids")
            )

            write(destination, proposal)

    # --------------------------------------------------------
    # Export fragmentation audit
    # --------------------------------------------------------

    with audit_path.open(
        "w",
        encoding="utf-8",
    ) as destination:

        for surface in sorted(audit):
            item = audit[surface]

            record = {
                "normalized_surface": surface,

                "occurrence_count": sum(
                    item["status_counts"].values()
                ),

                "distinct_articles": len(
                    item["article_ids"]
                ),

                "status_counts": dict(
                    sorted(
                        item["status_counts"].items()
                    )
                ),

                "kind_status_counts": dict(
                    sorted(
                        item["kind_status_counts"].items()
                    )
                ),

                "blocker_counts": dict(
                    sorted(
                        item["blocker_counts"].items()
                    )
                ),

                "distinct_provisional_identities": len(
                    item["proposal_ids"]
                ),

                "identity_approved": False,
            }

            write(destination, record)

    # --------------------------------------------------------
    # Final integrity checks
    # --------------------------------------------------------

    require(
        sum(assignment_counts.values())
        == len(profiles),
        "Assignment count mismatch",
    )

    require(
        sum(
            item["occurrence_count"]
            for item in proposals.values()
        ) == assignment_counts["PROVISIONAL"],
        "Provisional identity count mismatch",
    )

    require(
        sum(total_by_group.values())
        == candidate_summary["eligible_profiles"],
        "Candidate coverage mismatch",
    )

    require(
        all(
            assigned_by_group[gid]
            <= total_by_group[gid]
            for gid in total_by_group
        ),
        "Invalid group assignment counts",
    )

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    summary = {
        "version": VERSION,

        "source_batch_key":
            profile_summary["source_batch_key"],

        "source_candidate_sha256":
            candidate_summary["candidate_pairs_sha256"],

        "source_clusters_sha256": digest(
            Path(args.clusters_dir)
            / "surface_clusters.jsonl"
        ),

        "profiles_total": len(profiles),

        "candidate_profiles":
            candidate_summary["eligible_profiles"],

        "assignment_status_counts": dict(
            sorted(assignment_counts.items())
        ),

        "blocker_counts": dict(
            sorted(blocker_counts.items())
        ),

        "provisional_identities": len(proposals),
        "surface_clusters": len(clusters),

        "min_dominance": args.min_dominance,

        "infrastructure_scope_policy":
            "ADVISORY_ONLY",

        "identity_assignment_approvals": 0,
        "identity_merges": 0,
        "canonical_entities_created": 0,
        "fact_propagations": 0,
        "graph_writes": 0,
        "llm_calls": 0,

        "checksums": {
            path.name: digest(path)
            for path in (
                assignments_path,
                identities_path,
                audit_path,
            )
        },
    }

    (output_dir / "summary.json").write_text(
        json.dumps(
            summary,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )

    print(
        json.dumps(
            summary,
            ensure_ascii=False,
            indent=2,
        )
    )


# ============================================================
# CLI
# ============================================================

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
        "--candidates-dir",
        default=(
            "output/resolution/"
            "candidate-generation-v1"
        ),
    )

    parser.add_argument(
        "--clusters-dir",
        default=(
            "output/resolution/"
            "constrained-clustering-v1"
        ),
    )

    parser.add_argument(
        "--out-dir",
        default=(
            "output/resolution/"
            "occurrence-assignment-v1"
        ),
    )

    parser.add_argument(
        "--min-dominance",
        type=float,
        default=0.80,
    )

    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
