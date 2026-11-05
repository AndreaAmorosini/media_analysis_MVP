
"""PressKG: Structural Mention Decomposition v1.

Read-only discovery of potentially composite validated mentions.

Produces:
- mention_decomposition_candidates.jsonl
- mention_decomposition_summary.json

Never:
- changes Neo4j
- splits facts
- creates canonical identities
- distributes predicates across components
- invokes an LLM
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata

from collections import Counter
from pathlib import Path

from neo4j import GraphDatabase

from presskg.settings import CFG


VERSION = "mention-decomposition-structural-v1"
FACT_VIEW = "validated-fact-view-v1"

# These patterns produce candidates, not confirmed splits.
SEPARATORS = re.compile(
    r"\s+(?:e|ed|and)\s+"
    r"|\s*&\s*"
    r"|\s*(?:(?<!\d),|,(?!\d))\s*"
    r"|\s*;\s*"
    r"|\s+/\s+",
    re.IGNORECASE,
)

NON_REFERENTIAL_KINDS = {
    "Money",
    "Quantity",
    "Date",
    "Time",
}

# Explicitly collective expressions should not be expanded into
# hypothetical people, organizations, or municipalities.
COLLECTIVE_PREFIX = re.compile(
    r"^\s*(?:"
    r"i sindaci|le amministrazioni|"
    r"i comuni|le aziende|"
    r"gli enti|le associazioni|"
    r"i cittadini|le autorità|"
    r"il gruppo|la rete|"
    r"il consorzio"
    r")\b",
    re.IGNORECASE,
)


MENTION_QUERY = """
MATCH (
    b:ValidatedFactBatch {
        id:$batch_key,
        status:'ready'
    }
)-[:HAS_FACT]->(f:ValidatedFact)

MATCH (f)-[rel]->(m:ValidatedMention)

WHERE type(rel) IN [
    'SUBJECT_MENTION',
    'OBJECT_MENTION'
]

MATCH (a:Article {id:f.article_id})

RETURN
    m.id AS mention_id,
    m.fact_id AS mention_fact_id,
    m.article_id AS mention_article_id,
    m.claim_id AS mention_claim_id,
    m.batch_id AS mention_batch_id,
    m.name AS name,
    m.kind AS kind,
    m.role AS role,

    f.id AS fact_id,
    f.article_id AS article_id,
    f.claim_id AS claim_id,
    f.batch_id AS fact_batch_id,
    f.predicate AS predicate,
    f.representation AS representation,
    f.original_quote AS original_quote,
    f.supporting_span AS supporting_span,

    a.body AS body,
    a.title AS title,
    a.subtitle AS subtitle,

    type(rel) AS mention_relation

ORDER BY m.id
"""


def digest(value):
    data = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    return hashlib.sha256(data).hexdigest()


def normalized(text):
    return " ".join(
        unicodedata.normalize(
            "NFKC",
            str(text or ""),
        ).casefold().split()
    )


def require(condition, message):
    if not condition:
        raise ValueError(message)


def find_literal(text, needle):
    """Exact first occurrence, preserving original offsets."""
    if not text or not needle:
        return -1

    return text.find(needle)


def split_surface(name):
    """Return proposed component spans within the mention string.

    No component is automatically accepted as an identity.
    """

    if not isinstance(name, str):
        return []

    matches = list(SEPARATORS.finditer(name))

    if not matches:
        return []

    parts = []
    cursor = 0

    for match in matches:
        raw = name[cursor:match.start()]
        stripped = raw.strip()

        if not stripped:
            return []

        relative = raw.find(stripped)
        start = cursor + relative

        parts.append({
            "text": stripped,
            "start": start,
            "end": start + len(stripped),
        })

        cursor = match.end()

    raw = name[cursor:]
    stripped = raw.strip()

    if not stripped:
        return []

    relative = raw.find(stripped)
    start = cursor + relative

    parts.append({
        "text": stripped,
        "start": start,
        "end": start + len(stripped),
    })

    if not 2 <= len(parts) <= 8:
        return []

    if any(
        len(normalized(part["text"])) < 2
        for part in parts
    ):
        return []

    return parts


def classify_mention(name, kind=None):
    """Pure structural classifier.

    A POSSIBLE_* result is never an approved split.
    """

    if kind in NON_REFERENTIAL_KINDS:
        return {
            "status": "NON_REFERENTIAL_TYPE",
            "components": [],
            "reasons": [
                "quantitative_or_temporal_value",
                "not_an_identity_resolution_candidate"
            ]
        }
    
    if not isinstance(name, str) or not name.strip():
        return {
            "status": "INVALID_NAME",
            "components": [],
            "reasons": ["missing_name"],
        }

    parts = split_surface(name)

    if COLLECTIVE_PREFIX.search(name):
        return {
            "status": "COLLECTIVE_OR_GROUP_NAME",
            "components": parts,
            "reasons": [
                "collective_expression",
                "do_not_expand_members_automatically",
            ],
        }

    if not SEPARATORS.search(name):
        return {
            "status": "NO_STRUCTURAL_SPLIT",
            "components": [],
            "reasons": ["no_suspicious_separator"],
        }

    if not parts:
        return {
            "status": "AMBIGUOUS_STRUCTURE",
            "components": [],
            "reasons": ["unreliable_segmentation"],
        }

    if len(parts) == 2:
        status = "POSSIBLE_COORDINATION"
    else:
        status = "POSSIBLE_ENUMERATION"

    return {
        "status": status,
        "components": parts,
        "reasons": [
            "syntactic_separator_detected",
            "identity_not_verified",
            "official_name_not_excluded",
            "predicate_distribution_not_verified",
        ],
    }


def choose_context(row, radius):
    """Find the mention in an attributable source.

    Prefer the original claim quote, then the supporting
    span, then the article body and its metadata.

    Source offsets are always local to the selected field.
    """

    name = row["name"]

    for field in (
        "original_quote",
        "supporting_span",
        "body",
        "title",
        "subtitle",
    ):
        source = row.get(field)

        if not isinstance(source, str):
            continue

        # Supporting spans not literally in the article
        # are not considered verified evidence.
        if field == "supporting_span":
            body = row.get("body") or ""

            if source not in body:
                continue

        if field == "original_quote":
            body = row.get("body") or ""
            title = row.get("title") or ""
            subtitle = row.get("subtitle") or ""

            if (
                source not in body
                and source not in title
                and source not in subtitle
            ):
                continue

        start = find_literal(source, name)

        if start < 0:
            continue

        end = start + len(name)

        excerpt_start = max(
            0,
            start - radius,
        )

        excerpt_end = min(
            len(source),
            end + radius,
        )

        return {
            "field": field,
            "start": start,
            "end": end,
            "excerpt_start": excerpt_start,
            "excerpt_end": excerpt_end,
            "excerpt": source[
                excerpt_start:excerpt_end
            ],
            "literal_match": True,
            "source_fingerprint": hashlib.sha256(
                source.encode("utf-8")
            ).hexdigest(),
        }

    return {
        "field": None,
        "literal_match": False,
        "excerpt": None,
        "reason": "mention_not_found_verbatim",
    }


def analyze_mention(row, radius=250):
    """Build a provenance-aware, non-binding decomposition."""

    result = classify_mention(row["name"], row["kind"])

    # Ordinary names do not need an evidence packet at this
    # stage. This does not assert that they are real-world atomic.
    if result["status"] in {
        "NO_STRUCTURAL_SPLIT",
        "NON_REFERENTIAL_TYPE",
    }:
        return None

    evidence = choose_context(row, radius)

    requires_context = result["status"] in {
        "POSSIBLE_COORDINATION",
        "POSSIBLE_ENUMERATION",
        "AMBIGUOUS_STRUCTURE",
    }

    status = result["status"]

    if requires_context and not evidence["literal_match"]:
        status = "INSUFFICIENT_TEXTUAL_EVIDENCE"

    components = []

    for index, part in enumerate(result["components"]):
        components.append({
            "component_index": index,
            "surface": part["text"],
            "mention_start": part["start"],
            "mention_end": part["end"],

            # Component type is deliberately left unresolved.
            # The parent's kind cannot safely be inherited.
            "kind": None,
            "identity_id": None,
            "verified": False,
        })

    return {
        "mention_id": row["mention_id"],
        "fact_id": row["fact_id"],
        "claim_id": row["claim_id"],
        "article_id": row["article_id"],

        "role": row["role"],
        "name": row["name"],
        "original_kind": row["kind"],
        "predicate": row["predicate"],
        "representation": row["representation"],

        "structural_status": result["status"],
        "status": status,
        "reasons": result["reasons"],
        "components": components,
        "evidence": evidence,

        "split_approved": False,
        "fact_expansion_allowed": False,
        "requires_semantic_resolution": True,
    }


def check_source_row(row, batch_id):
    """Reject broken source references rather than hiding them."""

    expected_relation = {
        "subject": "SUBJECT_MENTION",
        "object": "OBJECT_MENTION",
    }

    require(
        row["role"] in expected_relation,
        f"Invalid mention role: {row['mention_id']}",
    )

    require(
        row["mention_relation"]
        == expected_relation[row["role"]],
        f"Mention relation mismatch: {row['mention_id']}",
    )

    require(
        row["mention_id"]
        == f"{row['fact_id']}:{row['role']}",
        f"Mention ID mismatch: {row['mention_id']}",
    )

    for property_name in (
        "fact_id",
        "article_id",
        "claim_id",
    ):
        require(
            row["mention_" + property_name]
            == row[property_name],
            "Mention provenance mismatch: "
            f"{row['mention_id']}",
        )

    require(
        row["mention_batch_id"]
        == row["fact_batch_id"]
        == batch_id,
        f"Batch mismatch: {row['mention_id']}",
    )


def write_immutable(path, content):
    path = Path(path)

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    if path.exists():
        require(
            path.read_text(encoding="utf-8") == content,
            f"Existing output differs: {path}. "
            "Use another output directory.",
        )
        return

    temp = path.with_name(path.name + ".tmp")

    temp.write_text(
        content,
        encoding="utf-8",
    )

    temp.replace(path)


def run(args):
    baseline = json.loads(
        Path(args.baseline).read_text(
            encoding="utf-8"
        )
    )

    require(
        baseline["version"]
        == "resolution-baseline-v1",
        "Unexpected baseline version",
    )

    require(
        baseline["status"] == "PASS"
        and all(baseline["invariants"].values()),
        "Baseline audit did not pass",
    )

    batch_key = baseline["batch_key"]
    batch_id = baseline[
        "batch_properties"
    ]["batch_id"]

    require(
        batch_key == f"{FACT_VIEW}:{batch_id}",
        "Incompatible fact view",
    )

    expected_facts = baseline["scanned_facts"]
    expected_mentions = 2 * expected_facts

    driver = GraphDatabase.driver(
        CFG.neo4j_uri,
        auth=(
            CFG.neo4j_user,
            CFG.neo4j_password,
        ),
    )

    candidates = []
    counts = Counter()
    kinds = Counter()
    seen = set()
    scanned = 0

    # A stable fingerprint of the complete mention inventory.
    inventory_hasher = hashlib.sha256()

    try:
        with driver.session(
            database=CFG.neo4j_database
        ) as session:

            batch = session.run(
                """
                MATCH (
                    b:ValidatedFactBatch {
                        id:$key,
                        status:'ready'
                    }
                )

                RETURN
                    b.expected_facts AS expected,
                    b.source_fingerprint
                        AS source_fingerprint
                """,
                key=batch_key,
            ).single()

            require(
                batch is not None
                and batch["expected"] == expected_facts
                and batch["source_fingerprint"]
                == baseline["batch_properties"][
                    "source_fingerprint"
                ],
                "Source batch changed",
            )

            for record in session.run(
                MENTION_QUERY,
                batch_key=batch_key,
            ):
                row = dict(record)

                check_source_row(
                    row,
                    batch_id,
                )

                mention_id = row["mention_id"]

                require(
                    mention_id not in seen,
                    f"Repeated mention ID: {mention_id}",
                )

                seen.add(mention_id)
                scanned += 1

                inventory_row = {
                    "id": mention_id,
                    "name": row["name"],
                    "kind": row["kind"],
                    "fact_id": row["fact_id"],
                    "article_id": row["article_id"],
                    "claim_id": row["claim_id"],
                    "predicate": row["predicate"],
                }

                inventory_hasher.update(
                    (
                        json.dumps(
                            inventory_row,
                            ensure_ascii=False,
                            sort_keys=True,
                        ) + "\n"
                    ).encode("utf-8")
                )

                structural = classify_mention(
                    row["name"],
                    row["kind"]
                )

                counts[
                    structural["status"]
                ] += 1

                kinds[
                    str(row["kind"])
                ] += 1

                candidate = analyze_mention(
                    row,
                    radius=args.context_radius,
                )

                if candidate is not None:
                    candidates.append(candidate)

            require(
                scanned == expected_mentions,
                "Mention inventory count differs "
                f"from baseline: {scanned} "
                f"vs {expected_mentions}",
            )

    finally:
        driver.close()

    candidates.sort(
        key=lambda x: x["mention_id"]
    )

    final_counts = Counter(
        item["status"]
        for item in candidates
    )

    summary = {
        "version": VERSION,
        "source_batch_key": batch_key,
        "source_baseline_fingerprint":
            baseline["baseline_fingerprint"],
        "source_facts": expected_facts,
        "scanned_mentions": scanned,
        "inventory_fingerprint":
            inventory_hasher.hexdigest(),

        "structural_counts":
            dict(sorted(counts.items())),
        "candidate_status_counts":
            dict(sorted(final_counts.items())),
        "mention_kind_counts":
            dict(sorted(kinds.items())),

        "candidate_count": len(candidates),
        "candidate_fingerprint":
            digest(candidates),

        "context_radius": args.context_radius,
        "split_approvals": 0,
        "fact_expansions": 0,
        "identity_merges": 0,
        "graph_writes": 0,
    }

    out = Path(args.out_dir)

    write_immutable(
        out / "mention_decomposition_candidates.jsonl",
        "".join(
            json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
            ) + "\n"
            for item in candidates
        ),
    )

    write_immutable(
        out / "mention_decomposition_summary.json",
        json.dumps(
            summary,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        ) + "\n",
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
        "--baseline",
        default=(
            "output/resolution/baseline-v1/"
            "baseline_summary.json"
        ),
    )

    parser.add_argument(
        "--out-dir",
        default=(
            "output/resolution/"
            "mention-decomposition-v1"
        ),
    )

    parser.add_argument(
        "--context-radius",
        type=int,
        default=250,
    )

    args = parser.parse_args()

    require(
        args.context_radius >= 50,
        "Context radius must be >= 50",
    )

    run(args)


if __name__ == "__main__":
    main()
