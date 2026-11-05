
"""PressKG Attribute Grounding v1.

Grounds candidate identity attributes against original articles.

- No LLM calls
- Read-only Neo4j
- No canonical identities
- No graph mutations
- No automatic approval of identity constraints
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


VERSION = "attribute-grounding-v1"
SOURCE_VERSION = "identity-attributes-v1"


ARTICLE_QUERY = """
UNWIND $ids AS article_id

OPTIONAL MATCH (a:Article {id:article_id})

RETURN
    article_id,
    a.id AS found_id,
    a.body AS body,
    a.title AS title,
    a.subtitle AS subtitle
"""


# Lexical cues are used for prioritization, never
# as definitive semantic entailment.
PREDICATE_CUES = {
    "WORKS_FOR": (
        r"\b(?:lavora\s+per|dipendente\s+di|"
        r"dirigente\s+di|responsabile\b.{0,70}?\bdi|"
        r"head\s+of\b.{0,70}?\bdi|"
        r"manager\s+(?:di|presso))\b"
    ),
    "WORKS_AT": (
        r"\b(?:lavora\s+(?:presso|in)|"
        r"in\s+servizio\s+presso|"
        r"impiegat[oa]\s+presso)\b"
    ),
    "LOCATED_IN": (
        r"\b(?:situat[oaie]|ubicat[oaie]|"
        r"localizzat[oaie]|"
        r"in\s+localit[aà]|nel\s+comune\s+di|"
        r"si\s+trova)\b"
    ),
    "LOCATED_AT": (
        r"\b(?:con\s+sede\s+in|"
        r"con\s+sede\s+a|"
        r"ubicat[oaie]|"
        r"si\s+trova|"
        r"all'indirizzo)\b"
    ),
    "OPERATES_IN": (
        r"\b(?:opera\s+(?:a|in|nel|nella)|"
        r"operativa?\s+(?:a|in|nel|nella)|"
        r"attiva?\s+(?:a|in|nel|nella))\b"
    ),
    "REPRESENTS": (
        r"\b(?:rappresenta|rappresentante\s+di|"
        r"in\s+rappresentanza\s+di)\b"
    ),
    "PRESIDES_OVER": (
        r"\b(?:presidente\s+di|"
        r"presiede|alla\s+presidenza\s+di)\b"
    ),
}

SCOPE_BY_PREDICATE = {
    "WORKS_FOR": "PROFESSIONAL_TEMPORAL",
    "WORKS_AT": "PROFESSIONAL_TEMPORAL",
    "REPRESENTS": "REPRESENTATIONAL_TEMPORAL",
    "PRESIDES_OVER": "ORGANIZATIONAL_TEMPORAL",
    "LOCATED_IN": "GEOGRAPHIC",
    "LOCATED_AT": "LOCATION_OR_ADDRESS",
    "OPERATES_IN": "OPERATIONAL_GEOGRAPHIC",
}

INFRASTRUCTURE_PATTERN = re.compile(
    r"^(?:strada\s+statale|"
    r"ss\s*\d+|s\.s\.\s*\d+|"
    r"autostrada|ferrovia|"
    r"acquedotto|condotta|"
    r"collettore|rete\s+fognaria)\b",
    re.IGNORECASE,
)

SOURCE_FIELDS = (
    "body",
    "title",
    "subtitle",
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256_text(text):
    return hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()


def file_sha256(path):
    hasher = hashlib.sha256()

    with Path(path).open("rb") as handle:
        for chunk in iter(
            lambda: handle.read(1024 * 1024),
            b"",
        ):
            hasher.update(chunk)

    return hasher.hexdigest()


def read_json(path):
    return json.loads(
        Path(path).read_text(encoding="utf-8")
    )


def iter_jsonl(path):
    with Path(path).open(
        encoding="utf-8"
    ) as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue

            try:
                yield json.loads(line)

            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path}:{number}: {exc}"
                ) from exc


def jsonline(row):
    return json.dumps(
        row,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    ) + "\n"


def load_source(directory):
    directory = Path(directory)

    summary = read_json(
        directory / "summary.json"
    )

    require(
        summary["version"] == SOURCE_VERSION,
        "Unexpected attribute extraction version",
    )

    require(
        summary["graph_writes"] == 0
        and summary["identity_merges"] == 0
        and summary["approved_attributes"] == 0,
        "Input contains unexpected modifications",
    )

    source_path = (
        directory / "attribute_evidence.jsonl"
    )

    require(
        file_sha256(source_path)
        == summary["attribute_evidence_sha256"],
        "Attribute evidence checksum mismatch",
    )

    records = list(
        iter_jsonl(source_path)
    )

    require(
        len(records) == summary["attribute_candidates"],
        "Attribute count mismatch",
    )

    evidence_ids = set()

    for record in records:
        evidence_id = record["evidence_id"]

        require(
            evidence_id not in evidence_ids,
            f"Duplicate evidence ID: {evidence_id}",
        )

        evidence_ids.add(evidence_id)

        require(
            record["category"]
            == "identity_attribute_candidate",
            "Unexpected evidence category",
        )

        require(
            record["predicate"] in PREDICATE_CUES,
            "Unsupported predicate",
        )

        require(
            record["attribute_verified"] is False
            and record["match_constraint_approved"] is False,
            "Unexpected approved attribute",
        )

        quote = record["evidence"]["quote"]

        require(
            isinstance(quote, str)
            and sha256_text(quote)
            == record["evidence"]["quote_sha256"],
            "Quote checksum mismatch",
        )

    return summary, records


# ============================================================
# Text normalization with real source offsets
# ============================================================

def normalize_with_offsets(text):
    """Unicode/case/whitespace normalization.

    Return an offset for every normalized character.
    The offsets always refer to the ORIGINAL source.
    """

    chars = []
    offsets = []
    whitespace = False

    for index, char in enumerate(text):
        transformed = unicodedata.normalize(
            "NFKC", char
        ).casefold()

        for output_char in transformed:
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


def occurrences(text, surface, limit=32):
    """Boundary-aware matches with verifiable original offsets."""

    if (
        not isinstance(text, str)
        or not isinstance(surface, str)
        or not surface.strip()
    ):
        return []

    normalized_text, offsets = normalize_with_offsets(text)
    normalized_surface, _ = normalize_with_offsets(surface)

    if not normalized_surface:
        return []

    pattern = re.compile(
        r"(?<!\w)"
        + re.escape(normalized_surface)
        + r"(?!\w)"
    )

    result = []

    for match in pattern.finditer(normalized_text):
        start = offsets[match.start()]
        end = offsets[match.end() - 1] + 1

        result.append({
            "start": start,
            "end": end,
            "surface": text[start:end],
            "match_type": (
                "LITERAL"
                if text[start:end] == surface
                else "NORMALIZED"
            ),
        })

        if len(result) >= limit:
            break

    return result


def embedded_surface_present(text, surface):
    """Diagnostic only: never grounds an argument.

    Example: 'LaGoriha' contains 'Gori' as a substring,
    but word boundaries are ambiguous.
    """

    if not text or not surface:
        return False

    return (
        unicodedata.normalize(
            "NFKC", surface
        ).casefold()
        in unicodedata.normalize(
            "NFKC", text
        ).casefold()
    )


# ============================================================
# Evidence selection
# ============================================================

def pair_distance(left, right):
    if left["end"] <= right["start"]:
        return right["start"] - left["end"]

    if right["end"] <= left["start"]:
        return left["start"] - right["end"]

    return 0


def best_pair(subjects, objects):
    best = None

    for subject in subjects:
        for object_ in objects:
            distance = pair_distance(
                subject, object_
            )

            if best is None or distance < best["distance"]:
                best = {
                    "subject": subject,
                    "object": object_,
                    "distance": distance,
                }

    return best


def choose_evidence(record, article, max_span=350, radius=130):
    """Recover the strongest available source passage.

    A pair of names in one article is not itself proof
    that the asserted predicate is correct.
    """

    subject = record["subject_surface"]
    object_ = record["object_surface"]

    if not article or not article.get("found_id"):
        return {
            "status": "NO_GROUNDING",
            "reason": "article_not_found",
            "evidence": None,
        }

    candidates = []
    partials = []
    embedded = False

    quotes = [
        record["evidence"].get("quote") or "",
    ]

    for field in SOURCE_FIELDS:
        text = article.get(field)

        if not isinstance(text, str) or not text:
            continue

        # Prefer spans that were already associated with
        # the validated fact, provided they exist verbatim
        # in the article.
        quoted_ranges = []

        for quote in quotes:
            if not quote:
                continue

            start = text.find(quote)

            if start >= 0:
                quoted_ranges.append(
                    (start, start + len(quote))
                )

        subject_matches = occurrences(
            text, subject
        )

        object_matches = occurrences(
            text, object_
        )

        embedded |= (
            embedded_surface_present(text, subject)
            and not subject_matches
        ) or (
            embedded_surface_present(text, object_)
            and not object_matches
        )

        if subject_matches and object_matches:
            pair = best_pair(
                subject_matches,
                object_matches,
            )

            if pair is None:
                continue

            left = min(
                pair["subject"]["start"],
                pair["object"]["start"],
            )

            right = max(
                pair["subject"]["end"],
                pair["object"]["end"],
            )

            in_quote = any(
                start <= left and right <= end
                for start, end in quoted_ranges
            )

            # Two names that are far apart in the same
            # article are not a grounded relation.
            if pair["distance"] > max_span:
                continue

            snippet_start = max(
                0, left - radius
            )

            snippet_end = min(
                len(text),
                right + radius,
            )

            candidates.append({
                "field": field,
                "source_sha256": sha256_text(text),
                "source_start": snippet_start,
                "source_end": snippet_end,
                "excerpt": text[
                    snippet_start:snippet_end
                ],
                "subject": pair["subject"],
                "object": pair["object"],
                "distance": pair["distance"],
                "within_validated_quote": in_quote,
                "anchor_start": left,
                "anchor_end": right,
            })

        else:
            count = (
                bool(subject_matches)
                + bool(object_matches)
            )

            if count:
                partials.append({
                    "field": field,
                    "argument_count": count,
                    "subject_found": bool(subject_matches),
                    "object_found": bool(object_matches),
                })

    if candidates:
        # Prefer a pair inside the validated quote,
        # then a short pair in the article itself.
        candidates.sort(
            key=lambda c: (
                not c["within_validated_quote"],
                c["distance"],
                SOURCE_FIELDS.index(c["field"]),
            )
        )

        chosen = candidates[0]

        return {
            "status": "BOTH_GROUNDED",
            "reason": (
                "within_validated_quote"
                if chosen["within_validated_quote"]
                else "article_passage"
            ),
            "evidence": chosen,
        }

    if partials:
        return {
            "status": "PARTIAL_GROUNDING",
            "reason": "one_or_more_arguments_unmatched",
            "evidence": None,
            "partial_sources": partials,
        }

    return {
        "status": "NO_GROUNDING",
        "reason": (
            "embedded_substring_not_accepted"
            if embedded
            else "arguments_not_found"
        ),
        "evidence": None,
    }


# ============================================================
# Predicate and identity scope
# ============================================================

def predicate_cue(record, grounding):
    evidence = grounding.get("evidence")

    if grounding["status"] != "BOTH_GROUNDED":
        return {
            "status": "NOT_EVALUATED",
            "cue": None,
        }

    predicate = record["predicate"]
    pattern = PREDICATE_CUES[predicate]

    snippet = evidence["excerpt"]

    anchor_start = (
        evidence["anchor_start"]
        - evidence["source_start"]
    )

    anchor_end = (
        evidence["anchor_end"]
        - evidence["source_start"]
    )

    # Restrict lexical cues to the passage that actually
    # contains the two arguments, with a small margin.
    local_start = max(
        0,
        anchor_start - 45,
    )

    local_end = min(
        len(snippet),
        anchor_end + 45,
    )

    passage = snippet[local_start:local_end]

    found = re.search(
        pattern,
        passage,
        re.IGNORECASE | re.DOTALL,
    )

    if found is None:
        return {
            "status": "NO_EXPLICIT_CUE",
            "cue": None,
        }

    return {
        "status": "LEXICAL_CUE_FOUND",
        "cue": found.group(0),
    }


def scope_assessment(record):
    predicate = record["predicate"]

    reasons = []

    subject = record["subject_surface"]
    subject_kind = record["subject_kind"]

    if (
        predicate in {"LOCATED_IN", "LOCATED_AT"}
        and (
            subject_kind == "Infrastructure"
            or INFRASTRUCTURE_PATTERN.search(subject)
        )
    ):
        reasons.append(
            "infrastructure_extent_not_verified"
        )

    if record["disposition"] in {
        "HELD_COMPOSITE_SCOPE",
        "HELD_NON_IDENTITY",
    }:
        reasons.append(
            "source_subject_scope_held"
        )

    return {
        "scope": SCOPE_BY_PREDICATE[predicate],
        "reasons": reasons,
        "scope_verified": False,
    }


def evaluate(record, article, max_span=350, radius=130):
    grounding = choose_evidence(
        record,
        article,
        max_span=max_span,
        radius=radius,
    )

    cue = predicate_cue(
        record,
        grounding,
    )

    scope = scope_assessment(record)

    if scope["reasons"]:
        status = "SCOPE_HELD"

    elif grounding["status"] == "BOTH_GROUNDED":
        status = (
            "GROUNDED_CUE_CANDIDATE"
            if cue["status"] == "LEXICAL_CUE_FOUND"
            else "GROUNDED_RELATION_UNVERIFIED"
        )

    else:
        status = grounding["status"]

    return {
        "evidence_id": record["evidence_id"],
        "fact_id": record["fact_id"],
        "claim_id": record["claim_id"],
        "article_id": record["article_id"],

        "subject_profile_id":
            record["subject_profile_id"],

        "subject_surface":
            record["subject_surface"],
        "object_surface":
            record["object_surface"],

        "subject_kind": record["subject_kind"],
        "object_kind": record["object_kind"],

        "predicate": record["predicate"],
        "attribute_kind": record["attribute_kind"],

        "status": status,
        "argument_grounding": grounding,
        "predicate_grounding": cue,
        "scope_assessment": scope,

        "publication_date":
            record["evidence"].get("publication_date"),

        "original_disposition":
            record["disposition"],

        "source_fact_validated": True,
        "attribute_semantically_verified": False,
        "match_constraint_approved": False,
        "canonical_identity": None,
        "fact_expansion_allowed": False,
    }


# ============================================================
# Pipeline
# ============================================================

def run(args):
    source_summary, records = load_source(
        args.source_dir
    )

    out = Path(args.out_dir)

    require(
        not out.exists(),
        "Output already exists. Use a new directory.",
    )

    require(
        args.batch_size > 0
        and args.max_span >= 50
        and args.context_radius >= 0,
        "Invalid CLI options",
    )

    # Create output only after validating all source files.
    out.mkdir(
        parents=True,
        exist_ok=False,
    )

    output_path = (
        out / "grounded_attributes.jsonl"
    )

    review_path = (
        out / "review_queue.jsonl"
    )

    counts = Counter()
    cue_counts = Counter()
    grounding_counts = Counter()

    driver = GraphDatabase.driver(
        CFG.neo4j_uri,
        auth=(
            CFG.neo4j_user,
            CFG.neo4j_password,
        ),
    )

    try:
        with (
            driver.session(
                database=CFG.neo4j_database
            ) as session,
            output_path.open(
                "w",
                encoding="utf-8",
            ) as output,
            review_path.open(
                "w",
                encoding="utf-8",
            ) as review,
        ):
            for start in range(
                0,
                len(records),
                args.batch_size,
            ):
                batch = records[
                    start:start + args.batch_size
                ]

                ids = sorted({
                    record["article_id"]
                    for record in batch
                })

                articles = {
                    row["article_id"]: row
                    for row in session.run(
                        ARTICLE_QUERY,
                        ids=ids,
                    ).data()
                }

                require(
                    len(articles) == len(ids),
                    "Article query returned incomplete rows",
                )

                for record in batch:
                    result = evaluate(
                        record,
                        articles.get(
                            record["article_id"]
                        ),
                        max_span=args.max_span,
                        radius=args.context_radius,
                    )

                    output.write(
                        jsonline(result)
                    )

                    counts[result["status"]] += 1

                    grounding_counts[
                        result["argument_grounding"]["status"]
                    ] += 1

                    cue_counts[
                        result["predicate_grounding"]["status"]
                    ] += 1

                    if result["status"] != (
                        "GROUNDED_CUE_CANDIDATE"
                    ):
                        review.write(
                            jsonline({
                                "evidence_id":
                                    result["evidence_id"],
                                "fact_id":
                                    result["fact_id"],
                                "article_id":
                                    result["article_id"],
                                "status":
                                    result["status"],
                                "predicate":
                                    result["predicate"],
                                "subject":
                                    result["subject_surface"],
                                "object":
                                    result["object_surface"],
                                "reason":
                                    result[
                                        "argument_grounding"
                                    ]["reason"],
                                "scope_reasons":
                                    result[
                                        "scope_assessment"
                                    ]["reasons"],
                            })
                        )

                        counts["review_queue"] += 1

        require(
            sum(
                count
                for status, count in counts.items()
                if status != "review_queue"
            ) == len(records),
            "Incomplete attribute processing",
        )

        report = {
            "version": VERSION,
            "source_version": SOURCE_VERSION,
            "source_batch_key":
                source_summary["source_batch_key"],
            "source_attribute_sha256":
                source_summary[
                    "attribute_evidence_sha256"
                ],

            "processed_attributes": len(records),
            "status_counts": {
                key: value
                for key, value in sorted(counts.items())
                if key != "review_queue"
            },

            "argument_grounding_counts":
                dict(sorted(grounding_counts.items())),

            "predicate_cue_counts":
                dict(sorted(cue_counts.items())),

            "review_queue_count":
                counts["review_queue"],

            "max_span": args.max_span,
            "context_radius": args.context_radius,

            "grounded_attributes_sha256":
                file_sha256(output_path),
            "review_queue_sha256":
                file_sha256(review_path),

            "llm_calls": 0,
            "attributes_semantically_approved": 0,
            "match_constraints_approved": 0,
            "identity_merges": 0,
            "graph_writes": 0,
        }

        (
            out / "summary.json"
        ).write_text(
            json.dumps(
                report,
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            ) + "\n",
            encoding="utf-8",
        )

        print(json.dumps(
            report,
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
        "--source-dir",
        default=(
            "output/resolution/"
            "identity-attributes-v1"
        ),
    )

    parser.add_argument(
        "--out-dir",
        default=(
            "output/resolution/"
            "attribute-grounding-v1"
        ),
    )

    parser.add_argument(
        "--batch-size",
        type=int,
        default=200,
    )

    parser.add_argument(
        "--max-span",
        type=int,
        default=350,
    )

    parser.add_argument(
        "--context-radius",
        type=int,
        default=130,
    )

    run(parser.parse_args())


if __name__ == "__main__":
    main()
