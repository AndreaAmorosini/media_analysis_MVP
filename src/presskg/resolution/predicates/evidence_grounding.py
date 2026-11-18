
"""Predicate Evidence Grounding Gate.

Checks OTHER fact quotations against their source Article.

Does not confuse a fact's own quote with independent evidence.
"""

from __future__ import annotations

from argparse import Namespace
import hashlib
import json
import os
import unicodedata

from collections import Counter
from pathlib import Path

from neo4j import GraphDatabase, READ_ACCESS
from presskg.common import require
from presskg.common.hashing import sha256_file
from presskg.common.io import iter_jsonl, read_json, write_jsonl
from presskg.resolution import paths
from presskg.resolution._common import close_step_driver, step_driver
from presskg.resolution.context import StepContext, StepResult



VERSION = "predicate-evidence-grounding-v1"
AUDIT_VERSION = "predicate-quality-audit-v1"

DEFAULT_TEXT_FIELDS = (
    "body", "content", "full_text",
    "article_text", "text",
    "raw_text", "extracted_text",
)

APPROVED = {
    "VERIFIED_EXACT",
    "VERIFIED_NORMALIZED",
}


def normalize(text):
    """Preserve words and punctuation; normalize only layout."""
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\u00a0", " ")
    return " ".join(text.split())


def get_article_text(props, allowed_fields):
    """Use only explicit article text, never metadata or fact text."""
    candidates = []

    for field in allowed_fields:
        value = props.get(field)

        if isinstance(value, str):
            text = value
        elif (
            isinstance(value, list)
            and all(isinstance(x, str) for x in value)
        ):
            text = "\n".join(value)
        else:
            continue

        if text.strip():
            candidates.append((len(text), field, text))

    if not candidates:
        return None, None

    _, field, text = max(
        candidates,
        key=lambda item: (item[0], item[1]),
    )

    return field, text


def grounding_status(quote, source, min_chars=35):
    if (
        not isinstance(quote, str)
        or not quote.strip()
    ):
        return "INVALID", None

    if not isinstance(source, str) or not source.strip():
        return "SOURCE_UNAVAILABLE", None

    if len(normalize(quote)) < min_chars:
        return "INVALID", None

    if quote in source:
        return "VERIFIED_EXACT", None

    nq = normalize(quote)
    ns = normalize(source)

    if nq in ns:
        return "VERIFIED_NORMALIZED", None

    # Diagnostic only: partial evidence never becomes VERIFIED.
    # Require a substantial contiguous prefix or suffix.
    tokens = nq.split()

    if len(tokens) >= 12:
        n = max(10, len(tokens) // 2)

        candidates = [
            " ".join(tokens[:n]),
            " ".join(tokens[-n:]),
        ]

        if any(
            len(fragment) >= 80 and fragment in ns
            for fragment in candidates
        ):
            return "PARTIAL_ONLY", "CONTIGUOUS_FRAGMENT"

    return "NOT_FOUND", None


def load_queue(audit_dir):
    root = Path(audit_dir)
    summary = read_json(root / "summary.json")

    require(
        summary["version"] == AUDIT_VERSION,
        "Unexpected predicate audit version",
    )
    require(
        summary["graph_writes"] == 0,
        "Upstream audit must be read-only",
    )

    for filename, digest in summary["checksums"].items():
        require(
            sha256_file(root / filename) == digest,
            f"Checksum mismatch: {filename}",
        )

    queue = list(
        iter_jsonl(root / "refinement_queue.jsonl")
    )

    require(
        len(queue) == summary["other_fact_count"],
        "Queue coverage mismatch",
    )

    seen_ids = set()
    seen_keys = set()

    for row in queue:
        fid = row["fact_id"]
        key = row["projection_key"]

        require(
            fid not in seen_ids and key not in seen_keys,
            f"Duplicate fact or projection key: {fid}",
        )

        seen_ids.add(fid)
        seen_keys.add(key)

        require(
            row["current_predicate"] == "OTHER"
            and row["classification_status"]
                == "NOT_CLASSIFIED"
            and row["proposed_predicate"] is None,
            f"Unexpected classification state: {fid}",
        )

    return summary, queue


ARTICLE_QUERY = """
UNWIND $article_ids AS article_id

OPTIONAL MATCH (a:Article {id: article_id})

RETURN
    article_id,
    count(a) AS article_count,
    CASE WHEN count(a) = 1
         THEN properties(head(collect(a)))
         ELSE null
    END AS article_properties
"""


def fetch_articles(tx, article_ids):
    return {
        record["article_id"]: dict(record)
        for record in tx.run(
            ARTICLE_QUERY,
            article_ids=article_ids,
        )
    }


def evaluate(row, article_result, fields, min_chars):
    fid = row["fact_id"]
    aid = row["article_id"]
    evidence = row["evidence"]

    problems = row.get("structural_problems") or []

    status = None
    partial_reason = None
    field = None
    source_length = 0

    if problems or not aid or not evidence.get("text"):
        status = "INVALID"

    elif not article_result or article_result["article_count"] == 0:
        status = "SOURCE_UNAVAILABLE"

    elif article_result["article_count"] != 1:
        status = "SOURCE_AMBIGUOUS"

    else:
        props = article_result["article_properties"] or {}

        field, source = get_article_text(props, fields)

        if source is None:
            status = "SOURCE_UNAVAILABLE"
        else:
            source_length = len(source)

            status, partial_reason = grounding_status(
                evidence["text"],
                source,
                min_chars,
            )

    # The 6A audit may have truncated an excerpt. A successful
    # substring match of a truncated prefix is not full grounding.
    if (
        evidence.get("truncated") is True
        and status in APPROVED
    ):
        status = "PARTIAL_ONLY"
        partial_reason = "AUDIT_EXCERPT_TRUNCATED"

    eligible = status in APPROVED

    return {
        "fact_id": fid,
        "article_id": aid,
        "projection_key": row["projection_key"],
        "subject_key": row["subject_key"],
        "object_key": row["object_key"],
        "subject_name": row["subject_name"],
        "object_name": row["object_name"],
        "subject_facet": row["subject_facet"],
        "object_facet": row["object_facet"],
        "current_predicate": "OTHER",
        "source_text_field": field,
        "source_text_length": source_length,
        "quote_text": evidence.get("text"),
        "quote_sha256": (
            hashlib.sha256(
                evidence["text"].encode("utf-8")
            ).hexdigest()
            if isinstance(evidence.get("text"), str)
            else None
        ),
        "grounding_status": status,
        "partial_reason": partial_reason,
        "eligible_for_refinement": eligible,
        "next_action": (
            "CLASSIFY_WITH_EVIDENCE"
            if eligible
            else "REVIEW_OR_RETRIEVE_EVIDENCE"
        ),
        "structural_problems": problems,
        "proposed_predicate": None,
        "classification_status": "NOT_CLASSIFIED",
    }


def run(ctx: StepContext) -> StepResult:
    args = ctx.args
    audit_dir = getattr(args, "audit_dir", str(paths.step_dir(args, "quality_audit")))
    out_dir = getattr(args, "out_dir", str(paths.step_dir(args, "evidence_grounding")))
    database = getattr(args, "database", "neo4j")
    batch_size = getattr(args, "batch_size", 100)
    min_quote_chars = getattr(args, "min_quote_chars", 35)
    article_text_fields = getattr(args, "article_text_fields", ",".join(DEFAULT_TEXT_FIELDS))
    
    try:
    
        require(batch_size > 0, "Invalid batch size")
        require(min_quote_chars > 0, "Invalid quote length")

        fields = tuple(
            s.strip()
            for s in article_text_fields.split(",")
            if s.strip()
        )

        require(bool(fields), "No Article text fields configured")

        summary, queue = load_queue(audit_dir)

        out = Path(out_dir)
        require(not out.exists(), "Output directory already exists")

        password = os.environ.get("NEO4J_PASSWORD")
        require(password, "NEO4J_PASSWORD is required")

        driver = step_driver(args)

        decisions = []

        try:
            driver.verify_connectivity()

            with driver.session(
                database=database,
                default_access_mode=READ_ACCESS,
            ) as session:

                for start in range(0, len(queue), batch_size):
                    batch = queue[start:start + batch_size]

                    article_ids = sorted({
                        row["article_id"]
                        for row in batch
                        if row.get("article_id")
                    })

                    articles = session.execute_read(
                        fetch_articles, article_ids
                    )

                    for row in batch:
                        decisions.append(
                            evaluate(
                                row,
                                articles.get(row["article_id"]),
                                fields,
                                min_quote_chars,
                            )
                        )
        finally:
            close_step_driver(args)

        require(
            len(decisions) == len(queue),
            "Incomplete grounding evaluation",
        )

        eligible = [
            row for row in decisions
            if row["eligible_for_refinement"]
        ]

        counts = Counter(
            row["grounding_status"] for row in decisions
        )

        fields_counts = Counter(
            row["source_text_field"]
            for row in decisions
            if row["source_text_field"]
        )

        out.mkdir(parents=True)

        decision_path = out / "grounding_decisions.jsonl"
        eligible_path = out / "refinement_eligible.jsonl"
        blocked_path = out / "refinement_blocked.jsonl"

        write_jsonl(decision_path, decisions)
        write_jsonl(eligible_path, eligible)
        write_jsonl(
            blocked_path,
            [
                row for row in decisions
                if not row["eligible_for_refinement"]
            ],
        )

        result = {
            "version": VERSION,
            "source_audit_version": AUDIT_VERSION,
            "source_audit_queue_sha256": sha256_file(
                Path(audit_dir) / "refinement_queue.jsonl"
            ),
            "source_batch_key": summary["source_batch_key"],
            "database": database,
            "input_other_facts": len(queue),
            "eligible_for_refinement": len(eligible),
            "blocked_from_refinement":
                len(queue) - len(eligible),
            "grounding_status_counts": dict(sorted(counts.items())),
            "article_text_field_counts":
                dict(sorted(fields_counts.items())),
            "configured_article_text_fields": list(fields),
            "proposed_predicates": 0,
            "llm_calls": 0,
            "graph_writes": 0,
            "checksums": {
                path.name: sha256_file(path)
                for path in (
                    decision_path,
                    eligible_path,
                    blocked_path,
                )
            },
        }

        (out / "summary.json").write_text(
            json.dumps(
                result,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ) + "\n",
            encoding="utf-8",
        )

        print(json.dumps(result, ensure_ascii=False, indent=2))
        
    except Exception as e:
        print(f"Unexpected error: {e}")
        return StepResult.fail(f"{type(e).__name__}: {e}")
    
    return StepResult.from_summary(result)
