
"""Constrained surface clustering v1.

Read-only, deterministic clustering of explicitly grounded aliases.
Does not assign individual occurrences to canonical entities.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata

from collections import Counter
from pathlib import Path


VERSIONS = {
    "candidate": "candidate-generation-v1",
    "alias": "global-alias-index-v1",
    "matching": "contextual-alias-recovery-v1",
}

ORG = {
    "Company", "Organization", "PublicAuthority",
    "NonProfitOrganization", "PoliticalParty",
    "LaborUnion", "MediaOutlet",
}
GEO = {
    "GPE", "LOC", "Location",
    "GeographicalArea", "Address",
}
PHYSICAL = {
    "Facility", "Infrastructure", "NaturalResource",
}
DOCUMENT = {
    "Document", "Contract", "Law",
    "Regulation", "Policy", "MediaWork",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(
            lambda: f.read(1 << 20), b""
        ):
            h.update(block)
    return h.hexdigest()


def read_json(path):
    return json.loads(
        Path(path).read_text(encoding="utf-8")
    )


def read_jsonl(path):
    with Path(path).open(encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path}:{n}: {exc}"
                ) from exc


def write_jsonl(path, values):
    with Path(path).open(
        "w", encoding="utf-8"
    ) as f:
        for value in values:
            f.write(
                json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                ) + "\n"
            )


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


def lexical_identity_key(value):
    """Narrow lexical equivalence used only for cluster bridges.

    Never creates a standalone alias edge.
    """
    value = normalize(value)

    value = re.sub(
        r"^(?:l|il|lo|la|i|gli|le|un|una)\s+",
        "",
        value,
    )

    value = re.sub(
        r"(?:\s+(?:s\s*p\s*a|spa|s\s*r\s*l|srl))+$",
        "",
        value,
    )

    return value.strip()


def family(kind):
    if kind == "Person":
        return "PERSON"
    if kind in ORG:
        return "ORGANIZATION"
    if kind in GEO:
        return "GEOGRAPHY"
    if kind in PHYSICAL:
        return "PHYSICAL"
    if kind in {"Project", "Event"}:
        return "ACTIVITY"
    if kind in DOCUMENT:
        return "DOCUMENT"
    if kind in {
        "Technology", "Product", "Service",
    }:
        return "PRODUCT_SERVICE"
    return None


def dominant_family(group):
    counts = Counter()

    for kind, number in group["kind_counts"].items():
        category = family(kind)
        if category:
            counts[category] += number

    total = sum(group["kind_counts"].values())

    if not counts or not total:
        return None, 0.0

    best, count = sorted(
        counts.items(),
        key=lambda item: (-item[1], item[0]),
    )[0]

    return best, count / total


def canonical(groups, members):
    """Prefer a full, article-free denomination to an acronym."""

    def rank(gid):
        name = groups[gid]["preferred_surface"]
        norm = normalize(name)

        is_acronym = (
            len(norm.split()) == 1
            and len(norm) <= 12
        )

        is_article = (
            lexical_identity_key(name) != norm
            and norm.startswith((
                "l ", "il ", "lo ", "la ",
                "i ", "gli ", "le ",
            ))
        )

        return (
            is_acronym,
            is_article,
            -len(norm.split()),
            -len(norm),
            -groups[gid]["occurrence_count"],
            norm,
            gid,
        )

    return min(members, key=rank)


def load_sources(
    candidate_dir,
    alias_dir,
    matching_dir,
):
    c, a, m = (
        Path(path)
        for path in (
            candidate_dir,
            alias_dir,
            matching_dir,
        )
    )

    cs, als, ms = (
        read_json(directory / "summary.json")
        for directory in (c, a, m)
    )

    for tag, summary in (
        ("candidate", cs),
        ("alias", als),
        ("matching", ms),
    ):
        require(
            summary["version"] == VERSIONS[tag],
            f"Wrong {tag} version",
        )
        require(
            summary["graph_writes"] == 0
            and summary["identity_merges"] == 0,
            f"{tag} mutated the graph",
        )

    require(
        cs["source_batch_key"]
        == als["source_batch_key"]
        == ms["source_batch_key"],
        "Batch mismatch",
    )

    require(
        cs["candidate_pairs_sha256"]
        == als["source_candidate_sha256"]
        == ms["source_candidate_sha256"],
        "Candidate lineage mismatch",
    )

    specifications = [
        (
            c / "surface_groups.jsonl",
            cs["surface_groups_sha256"],
        ),
        (
            c / "candidate_pairs.jsonl",
            cs["candidate_pairs_sha256"],
        ),
        (
            a / "alias_edges.jsonl",
            als["alias_edges_sha256"],
        ),
        (
            a / "alias_attestations.jsonl",
            als["alias_attestations_sha256"],
        ),
        (
            m / "pair_decisions.jsonl",
            ms["pair_decisions_sha256"],
        ),
    ]

    for path, expected in specifications:
        require(
            digest(path) == expected,
            f"Checksum mismatch: {path}",
        )

    groups = {}

    for group in read_jsonl(
        c / "surface_groups.jsonl"
    ):
        gid = group["group_id"]

        require(
            gid not in groups
            and not group["identity_approved"],
            f"Invalid group: {gid}",
        )

        groups[gid] = group

    require(
        len(groups) == cs["surface_groups"],
        "Group count mismatch",
    )

    pairs = {}

    for pair in read_jsonl(
        c / "candidate_pairs.jsonl"
    ):
        key = tuple(sorted((
            pair["left_group_id"],
            pair["right_group_id"],
        )))

        require(
            key not in pairs
            and key[0] in groups
            and key[1] in groups,
            "Invalid candidate pair",
        )

        pairs[key] = pair

    require(
        len(pairs) == cs["candidate_pairs"],
        "Candidate count mismatch",
    )

    decisions = {}

    for decision in read_jsonl(
        m / "pair_decisions.jsonl"
    ):
        key = tuple(sorted((
            decision["left_group_id"],
            decision["right_group_id"],
        )))

        require(
            key in pairs
            and key not in decisions,
            "Invalid matching decision",
        )

        decisions[key] = decision

    require(
        len(decisions) == ms["processed_pairs"]
        and decisions.keys() == pairs.keys(),
        "Decision coverage mismatch",
    )

    attestations = {}

    for evidence in read_jsonl(
        a / "alias_attestations.jsonl"
    ):
        eid = evidence["evidence_id"]

        require(
            eid not in attestations,
            "Duplicate evidence ID",
        )

        require(
            evidence["quote"]
            and evidence["end"] > evidence["start"] >= 0
            and evidence["end"] - evidence["start"]
            == len(evidence["quote"]),
            f"Invalid evidence offsets: {eid}",
        )

        attestations[eid] = evidence

    require(
        len(attestations) == als["attestations"],
        "Attestation count mismatch",
    )

    edges = {}
    observed_ids = set()

    for edge in read_jsonl(
        a / "alias_edges.jsonl"
    ):
        key = tuple(sorted((
            edge["left_group_id"],
            edge["right_group_id"],
        )))

        require(
            key not in edges
            and key[0] in groups
            and key[1] in groups
            and key[0] != key[1],
            "Invalid alias edge",
        )

        ids = edge["attestation_ids"]

        require(
            ids
            and len(ids)
            == len(set(ids))
            == edge["attestation_count"],
            "Invalid attestation references",
        )

        for eid in ids:
            require(
                eid in attestations
                and eid not in observed_ids,
                f"Duplicate/missing attestation: {eid}",
            )

            source = attestations[eid]

            require(
                tuple(sorted((
                    source["left_group_id"],
                    source["right_group_id"],
                ))) == key,
                f"Attestation pair mismatch: {eid}",
            )

            require(
                source["evidence_class"] == "DIRECT_NAMING"
                and not source["identity_merge_approved"],
                "Non-grounded or approved evidence",
            )

            observed_ids.add(eid)

        require(
            edge["existing_candidate_pair"]
            == (key in pairs),
            "Incorrect candidate presence flag",
        )

        edges[key] = edge

    require(
        observed_ids == attestations.keys()
        and len(edges) == als["alias_edges"],
        "Alias index incomplete",
    )

    return (
        cs, groups, pairs,
        decisions, edges, attestations,
    )


def build_clusters(
    groups,
    decisions,
    edges,
    *,
    min_purity=0.80,
    min_attestations=1,
):
    require(
        0.5 <= min_purity <= 1.0
        and min_attestations >= 1,
        "Invalid thresholds",
    )

    parent = {
        gid: gid for gid in groups
    }

    members = {
        gid: {gid} for gid in groups
    }

    direct = set(edges)
    accepted = []
    deferred = []

    def root(node):
        while parent[node] != node:
            node = parent[node]
        return node

    # Consider the most strongly attested edges first.
    ranked = sorted(
        edges.items(),
        key=lambda item: (
            -item[1]["attestation_count"],
            item[0],
        ),
    )

    for key, edge in ranked:
        a, b = key
        reason = None

        decision = decisions.get(key)

        family_a, purity_a = dominant_family(groups[a])
        family_b, purity_b = dominant_family(groups[b])

        if edge["attestation_count"] < min_attestations:
            reason = "too_few_attestations"

        elif (
            edge["type_compatibility"]["status"]
            != "COMPATIBLE"
        ):
            reason = "alias_type_not_compatible"

        elif (
            decision is not None
            and (
                decision["verdict"] == "CONFLICT"
                or decision["hard_conflicts"]
            )
        ):
            reason = "matching_conflict"

        elif (
            family_a is None
            or family_b is None
            or purity_a < min_purity
            or purity_b < min_purity
            or family_a != family_b
        ):
            reason = (
                "unstable_or_incompatible_dominant_types"
            )

        ra, rb = root(a), root(b)

        if reason is None and ra != rb:
            # Complete-link guard across the two clusters.
            for x in sorted(members[ra]):
                for y in sorted(members[rb]):
                    cross = tuple(sorted((x, y)))

                    if cross in direct:
                        continue

                    if (
                        lexical_identity_key(
                            groups[x]["preferred_surface"]
                        )
                        != lexical_identity_key(
                            groups[y]["preferred_surface"]
                        )
                    ):
                        reason = (
                            "unsupported_transitive_bridge"
                        )
                        break

                if reason:
                    break

        record = {
            "edge_id": edge["edge_id"],
            "left_group_id": a,
            "right_group_id": b,
            "left_surface":
                groups[a]["preferred_surface"],
            "right_surface":
                groups[b]["preferred_surface"],
            "attestation_ids": edge["attestation_ids"],
            "attestation_count":
                edge["attestation_count"],
        }

        if reason:
            deferred.append({
                **record,
                "reason": reason,
                "approved_for_merge": False,
            })
            continue

        if ra != rb:
            # Deterministic union.
            survivor, absorbed = sorted((ra, rb))

            parent[absorbed] = survivor
            members[survivor].update(
                members.pop(absorbed)
            )

        accepted.append({
            **record,
            "reason":
                "direct_alias_and_constraints_pass",
            "approved_for_merge": False,
        })

    clusters = []
    memberships = []

    components = sorted(
        (
            sorted(group_ids)
            for group_ids in members.values()
        ),
        key=lambda ids: ids[0],
    )

    for group_ids in components:
        # Singletons remain untouched.
        if len(group_ids) <= 1:
            continue

        chosen = canonical(groups, group_ids)

        cid = "sc-" + hashlib.sha256(
            "\x00".join(group_ids).encode()
        ).hexdigest()[:24]

        clusters.append({
            "cluster_id": cid,
            "canonical_surface":
                groups[chosen]["preferred_surface"],
            "canonical_group_id": chosen,
            "group_ids": group_ids,
            "group_count": len(group_ids),
            "occurrence_references": sum(
                groups[gid]["occurrence_count"]
                for gid in group_ids
            ),
            "identity_status":
                "PROVISIONAL_SURFACE_CLUSTER",
            "occurrences_disambiguated": False,
            "canonical_entity_materialized": False,
        })

        for gid in group_ids:
            memberships.append({
                "group_id": gid,
                "cluster_id": cid,
                "status":
                    "PROVISIONAL_SURFACE_MEMBERSHIP",
                "occurrence_assignment_approved": False,
            })

    return (
        clusters, memberships,
        accepted, deferred,
    )


def run(args):
    (
        source,
        groups,
        pairs,
        decisions,
        edges,
        evidences,
    ) = load_sources(
        args.candidates_dir,
        args.alias_dir,
        args.matching_dir,
    )

    (
        clusters,
        memberships,
        accepted,
        deferred,
    ) = build_clusters(
        groups,
        decisions,
        edges,
        min_purity=args.min_type_purity,
        min_attestations=args.min_attestations,
    )

    output = Path(args.out_dir)

    require(
        not output.exists(),
        "Output directory already exists",
    )

    output.mkdir(parents=True)

    outputs = {
        "surface_clusters.jsonl": clusters,
        "cluster_memberships.jsonl": memberships,
        "accepted_alias_edges.jsonl": accepted,
        "deferred_alias_edges.jsonl": deferred,
    }

    for filename, records in outputs.items():
        write_jsonl(
            output / filename,
            records,
        )

    summary = {
        "version": "constrained-clustering-v1",
        "source_batch_key":
            source["source_batch_key"],
        "source_candidate_sha256":
            source["candidate_pairs_sha256"],
        "source_alias_edges_sha256":
            digest(
                Path(args.alias_dir)
                / "alias_edges.jsonl"
            ),
        "source_matching_sha256":
            digest(
                Path(args.matching_dir)
                / "pair_decisions.jsonl"
            ),
        "input_surface_groups": len(groups),
        "input_candidate_pairs": len(pairs),
        "input_alias_edges": len(edges),
        "input_attestations": len(evidences),
        "accepted_alias_edges": len(accepted),
        "deferred_alias_edges": len(deferred),
        "defer_reasons": dict(sorted(
            Counter(
                edge["reason"] for edge in deferred
            ).items()
        )),
        "provisional_clusters": len(clusters),
        "group_memberships": len(memberships),
        "singletons_not_exported":
            len(groups) - len(memberships),
        "referenced_occurrences_in_provisional_clusters":
            sum(
                cluster["occurrence_references"]
                for cluster in clusters
            ),
        "min_type_purity": args.min_type_purity,
        "min_attestations": args.min_attestations,
        "infrastructure_scope_policy":
            "ADVISORY_ONLY",
        "occurrence_identity_approvals": 0,
        "canonical_entities_created": 0,
        "identity_merges": 0,
        "graph_writes": 0,
        "llm_calls": 0,
        "checksums": {
            filename: digest(output / filename)
            for filename in outputs
        },
    }

    (output / "summary.json").write_text(
        json.dumps(
            summary,
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )

    print(json.dumps(
        summary,
        indent=2,
        ensure_ascii=False,
    ))


def main():
    parser = argparse.ArgumentParser(
        description=__doc__
    )

    parser.add_argument(
        "--candidates-dir",
        default="output/resolution/candidate-generation-v1",
    )
    parser.add_argument(
        "--alias-dir",
        default="output/resolution/global-alias-index-v1",
    )
    parser.add_argument(
        "--matching-dir",
        default="output/resolution/contextual-alias-recovery-v1",
    )
    parser.add_argument(
        "--out-dir",
        default="output/resolution/constrained-clustering-v1",
    )
    parser.add_argument(
        "--min-type-purity",
        type=float,
        default=0.80,
    )
    parser.add_argument(
        "--min-attestations",
        type=int,
        default=1,
    )

    run(parser.parse_args())


if __name__ == "__main__":
    main()
