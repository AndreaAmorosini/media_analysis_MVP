
"""PressKG Identity Profile Builder v1.

Builds provenance-aware profiles from every ValidatedMention
and the audited Referent Eligibility output.

Read-only Neo4j access. No LLM calls, identity merges,
canonical nodes, fact expansion, or graph mutations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata

from collections import Counter, defaultdict
from pathlib import Path

from neo4j import GraphDatabase

from presskg.settings import CFG


VERSION = "identity-profiles-v1"
BASELINE_VERSION = "resolution-baseline-v1"
ELIGIBILITY_VERSION = "referent-eligibility-v1"


# These types can describe identifiable objects, including
# projects, events, products and documents. They are not
# automatically considered globally resolved identities.
REFERENTIAL_KINDS = {
    "Person", "Company", "Organization",
    "PublicAuthority", "PoliticalParty",
    "GPE", "Location", "LOC", "GeographicalArea",
    "Infrastructure", "Facility",
    "Project", "Event", "Product",
    "Document", "Contract", "Policy",
    "Service", "NaturalResource",
}

NON_IDENTITY_KINDS = {
    "Money", "Quantity", "Date", "Time",
    "Percentage", "Duration",
}

# No type in this table is treated as a confirmed identity
# type. These are lexical signals only.
TYPE_PATTERNS = (
    (
        "PublicAuthority",
        r"^(?:ministero|regione|comune di|"
        r"provincia di|prefettura|"
        r"ente idrico)\b",
    ),
    (
        "Infrastructure",
        r"^(?:strada statale|ss\s*\d+|"
        r"autostrada|acquedotto|"
        r"collettore|condotta|rete fognaria)\b",
    ),
    (
        "Facility",
        r"^(?:depuratore|impianto|"
        r"stazione di pompaggio)\b",
    ),
    (
        "Location",
        r"^(?:localit[aà]|frazione|"
        r"quartiere|contrada|via|viale)\b",
    ),
)

MENTION_QUERY = """
MATCH (b:ValidatedFactBatch {id:$batch_key, status:'ready'})
      -[:HAS_FACT]->(f:ValidatedFact)
MATCH (f)-[:SUBJECT_MENTION]->(m:ValidatedMention)
MATCH (f)-[:OBJECT_MENTION]->(other:ValidatedMention)
MATCH (a:Article {id:f.article_id})
RETURN
    m.id AS mention_id,
    m.name AS name,
    m.kind AS source_kind,
    m.role AS role,
    m.fact_id AS mention_fact_id,
    m.article_id AS mention_article_id,
    m.claim_id AS mention_claim_id,
    m.batch_id AS mention_batch_id,
    f.id AS fact_id,
    f.article_id AS article_id,
    f.claim_id AS claim_id,
    f.batch_id AS fact_batch_id,
    f.predicate AS predicate,
    f.representation AS representation,
    f.semantic_family AS semantic_family,
    f.modality AS modality,
    f.attribution AS attribution,
    f.temporal_status AS temporal_status,
    f.publication_date AS publication_date,
    f.original_quote AS original_quote,
    f.supporting_span AS supporting_span,
    other.name AS counterpart_name,
    other.kind AS counterpart_kind,
    a.title AS article_title,
    a.source AS article_source

UNION ALL

MATCH (b:ValidatedFactBatch {id:$batch_key, status:'ready'})
      -[:HAS_FACT]->(f:ValidatedFact)
MATCH (f)-[:OBJECT_MENTION]->(m:ValidatedMention)
MATCH (f)-[:SUBJECT_MENTION]->(other:ValidatedMention)
MATCH (a:Article {id:f.article_id})
RETURN
    m.id AS mention_id,
    m.name AS name,
    m.kind AS source_kind,
    m.role AS role,
    m.fact_id AS mention_fact_id,
    m.article_id AS mention_article_id,
    m.claim_id AS mention_claim_id,
    m.batch_id AS mention_batch_id,
    f.id AS fact_id,
    f.article_id AS article_id,
    f.claim_id AS claim_id,
    f.batch_id AS fact_batch_id,
    f.predicate AS predicate,
    f.representation AS representation,
    f.semantic_family AS semantic_family,
    f.modality AS modality,
    f.attribution AS attribution,
    f.temporal_status AS temporal_status,
    f.publication_date AS publication_date,
    f.original_quote AS original_quote,
    f.supporting_span AS supporting_span,
    other.name AS counterpart_name,
    other.kind AS counterpart_kind,
    a.title AS article_title,
    a.source AS article_source

ORDER BY mention_id
"""


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(value):
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize(value):
    value = unicodedata.normalize(
        "NFKC", str(value or "")
    ).casefold()
    return " ".join(value.split())


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_jsonl(path):
    with Path(path).open(encoding="utf-8") as handle:
        return [
            json.loads(line)
            for line in handle
            if line.strip()
        ]


def json_line(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    ) + "\n"


def load_inputs(baseline_path, eligibility_dir):
    baseline = read_json(baseline_path)
    directory = Path(eligibility_dir)

    eligibility = read_json(directory / "summary.json")
    interpretations = read_jsonl(
        directory / "mention_interpretations.jsonl"
    )
    components = read_jsonl(
        directory / "referent_candidates.jsonl"
    )
    relations = read_jsonl(
        directory / "relation_candidates.jsonl"
    )

    require(
        baseline["version"] == BASELINE_VERSION
        and baseline["status"] == "PASS"
        and all(baseline["invariants"].values()),
        "Invalid baseline",
    )
    require(
        eligibility["version"] == ELIGIBILITY_VERSION,
        "Unexpected eligibility version",
    )

    for name, rows in (
        ("interpretations", interpretations),
        ("candidates", components),
        ("relations", relations),
    ):
        require(
            eligibility[f"{name}_fingerprint"] == digest(rows),
            f"{name} fingerprint mismatch",
        )

    require(
        len(interpretations)
        == eligibility["processed_mentions"],
        "Interpretation count mismatch",
    )
    require(
        len(components) == eligibility["total_candidates"]
        and len(relations)
        == eligibility["total_relation_candidates"],
        "Candidate/relation count mismatch",
    )
    require(
        eligibility["graph_writes"] == 0
        and eligibility["identity_merges"] == 0
        and eligibility["approved_relations"] == 0,
        "Unexpected prior graph modifications",
    )

    interpretations_by_id = {}
    components_by_id = defaultdict(list)

    for item in interpretations:
        mention_id = item["mention_id"]
        require(
            mention_id not in interpretations_by_id,
            f"Repeated interpretation: {mention_id}",
        )
        interpretations_by_id[mention_id] = item

    candidate_ids = set()

    for item in components:
        candidate_id = item["candidate_id"]
        require(
            candidate_id not in candidate_ids,
            f"Repeated candidate: {candidate_id}",
        )
        require(
            item["mention_id"] in interpretations_by_id,
            "Candidate without interpretation",
        )
        candidate_ids.add(candidate_id)
        components_by_id[item["mention_id"]].append(item)

    for relation in relations:
        require(
            relation["mention_id"] in interpretations_by_id,
            "Relation without interpretation",
        )
        require(
            relation["subject_candidate_id"] in candidate_ids
            and relation["object_candidate_id"] in candidate_ids,
            "Relation with unknown component",
        )
        require(
            relation["approved"] is False,
            "Unexpected approved relation",
        )

    return (
        baseline,
        eligibility,
        interpretations_by_id,
        components_by_id,
        relations,
    )


def type_hints(surface, source_kind):
    """Record evidence-supported type hypotheses, not final types."""
    hints = []

    if source_kind:
        hints.append({
            "kind": source_kind,
            "origin": "validated_mention",
            "strength": "source_annotation",
        })

    for kind, pattern in TYPE_PATTERNS:
        if re.search(pattern, surface, re.IGNORECASE):
            hints.append({
                "kind": kind,
                "origin": "lexical_pattern",
                "strength": "weak",
            })

    return hints


def profile_status(kind, interpretation):
    """Decide whether to profile, without declaring identity resolved."""

    if interpretation is None:
        if kind in NON_IDENTITY_KINDS:
            return "NON_IDENTITY"
        if kind in REFERENTIAL_KINDS:
            return "PROFILE_ONLY_UNANALYZED"
        return "PROFILE_ONLY_UNKNOWN_TYPE"

    status = interpretation["interpretation_status"]

    if status == "NON_IDENTITY":
        return "NON_IDENTITY"
    if status == "COLLECTIVE_HOLD":
        return "COLLECTIVE_HOLD"
    if status == "KEEP_SINGLE":
        return "PROFILE_ONLY_SINGLE"
    if status in {
        "COMPONENT_CANDIDATES",
        "HOLD_COMPONENTS",
        "RELATION_CANDIDATE",
        "HOLD_RELATION",
    }:
        return "COMPOSITE_MENTION_HOLD"
    return "PROFILE_ONLY_UNRESOLVED"


def validate_mention_row(row, batch_id):
    require(
        row["role"] in {"subject", "object"},
        "Invalid mention role",
    )
    require(
        row["mention_id"]
        == f"{row['fact_id']}:{row['role']}",
        "Mention ID mismatch",
    )

    for suffix in ("fact_id", "article_id", "claim_id"):
        require(
            row["mention_" + suffix] == row[suffix],
            f"Mention {suffix} mismatch",
        )

    require(
        row["mention_batch_id"]
        == row["fact_batch_id"]
        == batch_id,
        "Batch mismatch",
    )
    require(
        isinstance(row["name"], str) and row["name"].strip(),
        "Missing mention name",
    )


def make_mention_profile(row, interpretation=None):
    status = profile_status(
        row["source_kind"], interpretation
    )

    quote = row["original_quote"]
    span = row["supporting_span"]

    return {
        "profile_id": row["mention_id"],
        "profile_version": VERSION,
        "profile_scope": "mention_occurrence",
        "mention_id": row["mention_id"],
        "fact_id": row["fact_id"],
        "claim_id": row["claim_id"],
        "article_id": row["article_id"],
        "role": row["role"],

        "surface": row["name"],
        "normalized_surface": normalize(row["name"]),
        "source_kind": row["source_kind"],
        "type_hints": type_hints(
            row["name"], row["source_kind"]
        ),
        "resolved_type": None,

        "resolution_status": status,
        "decomposition_status": (
            interpretation["interpretation_status"]
            if interpretation else "NOT_ANALYZED"
        ),

        "fact_context": {
            "predicate": row["predicate"],
            "counterpart_name": row["counterpart_name"],
            "counterpart_kind": row["counterpart_kind"],
            "semantic_family": row["semantic_family"],
            "representation": row["representation"],
            "modality": row["modality"],
            "attribution": row["attribution"],
            "temporal_status": row["temporal_status"],
        },

        "provenance": {
            "publication_date": (
                str(row["publication_date"])
                if row["publication_date"] is not None
                else None
            ),
            "article_title": row["article_title"],
            "article_source": row["article_source"],
            "original_quote": quote,
            "supporting_span": span,
        },

        "identity_attributes": [],
        "canonical_id": None,
        "merge_approved": False,
    }


def make_component_profile(component, mention_profile):
    """Retain the component's own type as unresolved.

    The parent kind remains a hint, never a constraint.
    """

    require(
        component["mention_id"] == mention_profile["mention_id"],
        "Component provenance mismatch",
    )
    require(
        component["fact_id"] == mention_profile["fact_id"]
        and component["article_id"]
        == mention_profile["article_id"],
        "Component fact/article mismatch",
    )

    surface = component["identity_surface"]

    hints = type_hints(
        surface, component["parent_kind"]
    )

    eligibility = component["eligibility"]

    if eligibility == "CANDIDATE":
        status = "PROFILE_ONLY_CANDIDATE"
    else:
        status = "PROFILE_ONLY_HELD"

    # An obsolete eligibility filter must not permanently
    # exclude named events/projects/products/documents.
    if (
        eligibility == "HELD"
        and component["parent_kind"] in REFERENTIAL_KINDS
        and len(surface.strip()) >= 2
    ):
        status = "PROFILE_ONLY_REASSESS"

    return {
        "profile_id": component["candidate_id"],
        "profile_version": VERSION,
        "profile_scope": "decomposed_component",

        "mention_id": component["mention_id"],
        "fact_id": component["fact_id"],
        "claim_id": mention_profile["claim_id"],
        "article_id": component["article_id"],
        "role": component["role"],

        "surface": surface,
        "original_component_surface":
            component["surface"],
        "normalized_surface": normalize(surface),

        "source_kind": component["parent_kind"],
        "type_hints": hints,
        "resolved_type": None,

        "resolution_status": status,
        "decomposition_status":
            component["source_classification"],
        "component_index": component["component_index"],
        "mention_start": component["mention_start"],
        "mention_end": component["mention_end"],

        "eligibility_reason": component["reason"],
        "fact_context": mention_profile["fact_context"],
        "provenance": mention_profile["provenance"],

        "identity_attributes": [],
        "canonical_id": None,
        "merge_approved": False,
    }


def run(args):
    (
        baseline,
        eligibility,
        interpretations,
        components,
        relations,
    ) = load_inputs(args.baseline, args.eligibility_dir)

    batch_key = baseline["batch_key"]
    batch_id = baseline["batch_properties"]["batch_id"]

    expected = 2 * baseline["scanned_facts"]
    expected_interpreted = set(interpretations)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    paths = {
        "mentions": out / "mention_profiles.jsonl",
        "components": out / "component_profiles.jsonl",
        "relations": out / "relation_evidence.jsonl",
        "summary": out / "summary.json",
    }

    require(
        not any(path.exists() for path in paths.values()),
        "Output already exists. Use a new directory.",
    )

    tmp_mentions = paths["mentions"].with_suffix(".jsonl.tmp")
    tmp_components = paths["components"].with_suffix(".jsonl.tmp")

    counters = Counter()
    statuses = Counter()
    kinds = Counter()
    component_statuses = Counter()
    processed_interpretations = set()
    seen_mentions = set()

    mention_hash = hashlib.sha256()
    component_hash = hashlib.sha256()

    driver = GraphDatabase.driver(
        CFG.neo4j_uri,
        auth=(CFG.neo4j_user, CFG.neo4j_password),
    )

    try:
        with (
            tmp_mentions.open("w", encoding="utf-8") as mf,
            tmp_components.open("w", encoding="utf-8") as cf,
            driver.session(database=CFG.neo4j_database) as session
        ):
            batch = session.run(
                """
                MATCH (b:ValidatedFactBatch {id:$key})
                RETURN b.status AS status,
                       b.expected_facts AS expected
                """,
                key=batch_key,
            ).single()

            require(
                batch is not None
                and batch["status"] == "ready"
                and batch["expected"]
                == baseline["scanned_facts"],
                "Materialized fact batch changed",
            )

            for record in session.run(
                MENTION_QUERY,
                batch_key=batch_key,
            ):
                row = dict(record)
                validate_mention_row(row, batch_id)

                mid = row["mention_id"]

                require(
                    mid not in seen_mentions,
                    f"Duplicate mention: {mid}",
                )
                seen_mentions.add(mid)

                interpretation = interpretations.get(mid)

                if interpretation is not None:
                    require(
                        interpretation["fact_id"] == row["fact_id"]
                        and interpretation["article_id"]
                        == row["article_id"]
                        and interpretation["role"] == row["role"]
                        and interpretation["name"] == row["name"]
                        and interpretation["kind"]
                        == row["source_kind"],
                        f"Stale interpretation: {mid}",
                    )
                    processed_interpretations.add(mid)

                profile = make_mention_profile(
                    row, interpretation
                )
                line = json_line(profile)
                mf.write(line)
                mention_hash.update(line.encode("utf-8"))

                counters["mentions"] += 1
                statuses[profile["resolution_status"]] += 1
                kinds[str(row["source_kind"])] += 1

                for component in components.get(mid, []):
                    # A component is an independent candidate
                    # profile, not a validated new fact.
                    cp = make_component_profile(
                        component, profile
                    )
                    cline = json_line(cp)
                    cf.write(cline)
                    component_hash.update(
                        cline.encode("utf-8")
                    )
                    counters["components"] += 1
                    component_statuses[
                        cp["resolution_status"]
                    ] += 1

        require(
            counters["mentions"] == expected,
            f"Expected {expected} mentions, "
            f"found {counters['mentions']}",
        )
        require(
            processed_interpretations
            == expected_interpreted,
            "Some interpreted mentions are missing",
        )
        require(
            counters["components"]
            == eligibility["total_candidates"],
            "Component count mismatch",
        )

        # Relations are preserved as proposals, never approved.
        relation_hash = hashlib.sha256()
        relation_temp = paths["relations"].with_suffix(
            ".jsonl.tmp"
        )

        with relation_temp.open(
            "w", encoding="utf-8"
        ) as handle:
            for relation in sorted(
                relations,
                key=lambda x: x["relation_candidate_id"],
            ):
                line = json_line({
                    **relation,
                    "materialization_allowed": False,
                    "type_consistency_verified": False,
                    "referent_scope_verified": False,
                })
                handle.write(line)
                relation_hash.update(
                    line.encode("utf-8")
                )

        summary = {
            "version": VERSION,
            "source_batch_key": batch_key,
            "source_baseline_fingerprint":
                baseline["baseline_fingerprint"],
            "source_eligibility_fingerprint":
                eligibility["interpretations_fingerprint"],

            "mention_profiles": counters["mentions"],
            "component_profiles": counters["components"],
            "relation_proposals": len(relations),

            "mention_status_counts":
                dict(sorted(statuses.items())),
            "component_status_counts":
                dict(sorted(component_statuses.items())),
            "source_kind_counts":
                dict(sorted(kinds.items())),

            "mention_profiles_sha256":
                mention_hash.hexdigest(),
            "component_profiles_sha256":
                component_hash.hexdigest(),
            "relation_evidence_sha256":
                relation_hash.hexdigest(),

            "canonical_entities": 0,
            "identity_merges": 0,
            "approved_relations": 0,
            "fact_expansions": 0,
            "graph_writes": 0,
        }

        # Files become visible only after all checks succeed.
        tmp_mentions.replace(paths["mentions"])
        tmp_components.replace(paths["components"])
        relation_temp.replace(paths["relations"])

        paths["summary"].write_text(
            json.dumps(
                summary,
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            ) + "\n",
            encoding="utf-8",
        )

        print(json.dumps(
            summary,
            indent=2,
            ensure_ascii=False,
        ))

    finally:
        driver.close()


def main():
    parser = argparse.ArgumentParser(
        description=__doc__
    )
    parser.add_argument(
        "--baseline",
        default=(
            "output/resolution/baseline-v1/"
            "baseline_summary.json"
        ),
    )
    parser.add_argument(
        "--eligibility-dir",
        default=(
            "output/resolution/"
            "referent-eligibility-v1"
        ),
    )
    parser.add_argument(
        "--out-dir",
        default=(
            "output/resolution/"
            "identity-profiles-v1"
        ),
    )

    run(parser.parse_args())


if __name__ == "__main__":
    main()
