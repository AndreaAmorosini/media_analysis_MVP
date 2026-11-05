
"""Contextual evidence recovery for PressKG mention decomposition.

Read-only. Does not approve splits, create facts, or modify Neo4j.
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


VERSION = "mention-contextual-evidence-v1"
SOURCE_VERSION = "mention-decomposition-structural-v1"


SOURCE_QUERY = """
UNWIND $items AS item

OPTIONAL MATCH (f:ValidatedFact {id:item.fact_id})
OPTIONAL MATCH (a:Article {id:item.article_id})
OPTIONAL MATCH (c:Claim {id:f.claim_id})

RETURN
    item.mention_id AS mention_id,
    f.id AS fact_id,
    f.article_id AS fact_article_id,
    f.claim_id AS fact_claim_id,

    a.id AS article_id,
    a.body AS body,
    a.title AS title,
    a.subtitle AS subtitle,

    f.original_quote AS original_quote,
    f.supporting_span AS supporting_span,

    c.id AS claim_id,
    c.article_id AS claim_article_id,
    c.quote AS claim_quote
"""


def require(condition, message):
    if not condition:
        raise ValueError(message)


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def read_json(path):
    return json.loads(
        Path(path).read_text(encoding="utf-8")
    )


def read_jsonl(path):
    return [
        json.loads(line)
        for line in Path(path).read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip()
    ]


def write_immutable(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        require(
            path.read_text(encoding="utf-8") == content,
            f"Output differs: {path}. Use a new directory.",
        )
        return

    temp = path.with_name(path.name + ".tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)


def normalized_with_offsets(text):
    """Normalize case/spacing while preserving source offsets."""

    chars = []
    offsets = []
    whitespace = False

    for index, char in enumerate(text):
        folded = unicodedata.normalize(
            "NFKC", char
        ).casefold()

        for output_char in folded:
            if output_char.isspace():
                if chars and not whitespace:
                    chars.append(" ")
                    offsets.append(index)
                whitespace = True
            else:
                chars.append(output_char)
                offsets.append(index)
                whitespace = False

    while chars and chars[-1] == " ":
        chars.pop()
        offsets.pop()

    return "".join(chars), offsets


def occurrences(text, surface, max_matches=12):
    """Find bounded normalized matches with source offsets."""

    if not isinstance(text, str):
        return []

    if not isinstance(surface, str) or not surface.strip():
        return []

    normalized_text, offsets = normalized_with_offsets(text)
    normalized_surface, _ = normalized_with_offsets(surface)

    if not normalized_surface:
        return []

    pattern = re.compile(
        r"(?<!\w)"
        + re.escape(normalized_surface)
        + r"(?!\w)"
    )

    matches = []

    for match in pattern.finditer(normalized_text):
        start = offsets[match.start()]
        end = offsets[match.end() - 1] + 1

        matches.append({
            "start": start,
            "end": end,
            "surface": text[start:end],
            "match_type": (
                "literal"
                if text[start:end] == surface
                else "normalized"
            ),
        })

        if len(matches) >= max_matches:
            break

    return matches


def trusted_sources(row):
    """Only accept quotes whose source association is verified."""

    sources = []

    body = row.get("body") or ""
    title = row.get("title") or ""
    subtitle = row.get("subtitle") or ""

    if row.get("article_id") is None:
        return sources

    if body:
        sources.append(("body", body))

    if title:
        sources.append(("title", title))

    if subtitle:
        sources.append(("subtitle", subtitle))

    for property_name in (
        "original_quote",
        "supporting_span",
    ):
        value = row.get(property_name)

        if not isinstance(value, str) or not value.strip():
            continue

        if any(
            value in source
            for source in (body, title, subtitle)
        ):
            sources.append((property_name, value))

    if (
        row.get("claim_id") is not None
        and row.get("claim_id") == row.get("fact_claim_id")
        and row.get("claim_article_id")
        == row.get("article_id")
    ):
        value = row.get("claim_quote")

        if (
            isinstance(value, str)
            and value.strip()
            and any(
                value in source
                for source in (body, title, subtitle)
            )
        ):
            sources.append(("claim_quote", value))

    return sources


def best_window(
    text,
    components,
    max_span,
):
    """Find the smallest window covering all components.

    Window co-occurrence is contextual evidence, NOT an identity
    or splitting decision.
    """

    if not components:
        return None

    groups = []

    for component in components:
        found = occurrences(
            text, component, max_matches=20
        )

        if not found:
            return None

        groups.append(found)

    candidates = sorted({
        match["start"]
        for group in groups
        for match in group
    })

    best = None

    for start in candidates:
        selected = []

        for group in groups:
            eligible = [
                match
                for match in group
                if match["start"] >= start
            ]

            if not eligible:
                break

            selected.append(eligible[0])

        if len(selected) != len(groups):
            continue

        left = min(m["start"] for m in selected)
        right = max(m["end"] for m in selected)

        if right - left > max_span:
            continue

        if best is None or right - left < (
            best["end"] - best["start"]
        ):
            best = {
                "start": left,
                "end": right,
                "components": selected,
            }

    return best


def recover(candidate, source, radius, max_span):
    """Recover source-grounded evidence for one candidate."""

    require(
        source["mention_id"] == candidate["mention_id"],
        "Mention ID mismatch",
    )

    require(
        source["fact_id"] == candidate["fact_id"]
        and source["fact_article_id"]
        == candidate["article_id"]
        and source["article_id"]
        == candidate["article_id"],
        "Invalid fact/article provenance",
    )

    components = [
        part["surface"]
        for part in candidate["components"]
    ]

    source_results = []

    for field, text in trusted_sources(source):
        full = occurrences(
            text, candidate["name"]
        )

        window = best_window(
            text,
            components,
            max_span,
        ) if len(components) >= 2 else None

        if not full and window is None:
            continue

        if full:
            anchor_start = full[0]["start"]
            anchor_end = full[0]["end"]
            evidence_type = "FULL_MENTION"
        else:
            anchor_start = window["start"]
            anchor_end = window["end"]
            evidence_type = "COMPONENTS_COLOCATED"

        start = max(0, anchor_start - radius)
        end = min(len(text), anchor_end + radius)

        source_results.append({
            "field": field,
            "evidence_type": evidence_type,
            "source_fingerprint":
                hashlib.sha256(
                    text.encode("utf-8")
                ).hexdigest(),
            "start": anchor_start,
            "end": anchor_end,
            "excerpt_start": start,
            "excerpt_end": end,
            "excerpt": text[start:end],
            "full_name_matches": full,
            "component_matches": (
                window["components"]
                if window else []
            ),
        })

    # Prefer original claim/fact evidence over broad article
    # context when both are present.
    field_priority = {
        "claim_quote": 0,
        "original_quote": 1,
        "supporting_span": 2,
        "body": 3,
        "title": 4,
        "subtitle": 5,
    }

    source_results.sort(
        key=lambda entry: (
            entry["evidence_type"]
            != "FULL_MENTION",
            field_priority.get(entry["field"], 99),
            entry["end"] - entry["start"],
        )
    )

    if not source_results:
        status = "NO_CONTEXT_FOUND"
    elif source_results[0]["evidence_type"] == "FULL_MENTION":
        status = "FULL_MENTION_GROUNDED"
    else:
        status = "COMPONENT_CONTEXT_RECOVERED"

    return {
        "mention_id": candidate["mention_id"],
        "fact_id": candidate["fact_id"],
        "article_id": candidate["article_id"],
        "name": candidate["name"],
        "kind": candidate["original_kind"],
        "role": candidate["role"],
        "structural_status":
            candidate["structural_status"],
        "original_status": candidate["status"],
        "components": candidate["components"],
        "status": status,
        "evidence": source_results[:5],
        "split_approved": False,
        "fact_expansion_allowed": False,
    }


def run(args):
    folder = Path(args.source_dir)

    source_summary = read_json(
        folder / "mention_decomposition_summary.json"
    )

    candidates = read_jsonl(
        folder / "mention_decomposition_candidates.jsonl"
    )

    require(
        source_summary["version"] == SOURCE_VERSION,
        "Wrong source version",
    )

    require(
        source_summary["candidate_count"]
        == len(candidates)
        and source_summary["candidate_fingerprint"]
        == fingerprint(candidates),
        "Source candidate fingerprint mismatch",
    )

    require(
        source_summary["graph_writes"] == 0
        and source_summary["split_approvals"] == 0,
        "Unexpected source modifications",
    )

    driver = GraphDatabase.driver(
        CFG.neo4j_uri,
        auth=(CFG.neo4j_user, CFG.neo4j_password),
    )

    results = []

    try:
        with driver.session(
            database=CFG.neo4j_database
        ) as session:

            for start in range(
                0, len(candidates), args.batch_size
            ):
                batch = candidates[
                    start:start + args.batch_size
                ]

                rows = session.run(
                    SOURCE_QUERY,
                    items=[
                        {
                            "mention_id": item["mention_id"],
                            "fact_id": item["fact_id"],
                            "article_id": item["article_id"],
                        }
                        for item in batch
                    ],
                ).data()

                by_id = {
                    row["mention_id"]: row
                    for row in rows
                }

                require(
                    len(rows) == len(batch)
                    and len(by_id) == len(batch),
                    "Duplicate or missing source query rows",
                )

                for candidate in batch:
                    row = by_id[
                        candidate["mention_id"]
                    ]

                    results.append(
                        recover(
                            candidate,
                            row,
                            args.context_radius,
                            args.max_span,
                        )
                    )

    finally:
        driver.close()

    results.sort(
        key=lambda item: item["mention_id"]
    )

    counts = Counter(
        item["status"]
        for item in results
    )

    output_summary = {
        "version": VERSION,
        "source_version":
            source_summary["version"],
        "source_candidate_fingerprint":
            source_summary["candidate_fingerprint"],
        "source_baseline_fingerprint":
            source_summary[
                "source_baseline_fingerprint"
            ],
        "candidates": len(results),
        "status_counts": dict(sorted(counts.items())),
        "context_radius": args.context_radius,
        "max_span": args.max_span,
        "results_fingerprint": fingerprint(results),
        "split_approvals": 0,
        "identity_merges": 0,
        "graph_writes": 0,
    }

    out = Path(args.out_dir)

    write_immutable(
        out / "contextual_evidence.jsonl",
        "".join(
            json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
            ) + "\n"
            for item in results
        ),
    )

    write_immutable(
        out / "summary.json",
        json.dumps(
            output_summary,
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        ) + "\n",
    )

    print(json.dumps(
        output_summary,
        indent=2,
        ensure_ascii=False,
    ))


def main():
    parser = argparse.ArgumentParser(
        description=__doc__
    )

    parser.add_argument(
        "--source-dir",
        default=(
            "output/resolution/"
            "mention-decomposition-v1-refined"
        ),
    )

    parser.add_argument(
        "--out-dir",
        default=(
            "output/resolution/"
            "mention-contextual-evidence-v1"
        ),
    )

    parser.add_argument(
        "--context-radius",
        type=int,
        default=250,
    )

    parser.add_argument(
        "--max-span",
        type=int,
        default=450,
    )

    parser.add_argument(
        "--batch-size",
        type=int,
        default=200,
    )

    args = parser.parse_args()

    require(
        args.context_radius >= 50
        and args.max_span >= 50
        and args.batch_size > 0,
        "Invalid options",
    )

    run(args)


if __name__ == "__main__":
    main()
