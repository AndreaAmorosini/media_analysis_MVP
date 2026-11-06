
"""PressKG Contextual Alias Recovery & Soft Type Matching v1.

Consumes candidate-generation-v1, identity-profiles-v1 and
evidence-matching-v1.

Uses original article text to recover explicit alias evidence.
No LLM, no Neo4j writes, no approved entity merges.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata

from collections import Counter, defaultdict
from pathlib import Path

VERSION = "contextual-alias-recovery-v1"

STOPWORDS = {
    "di", "del", "della", "delle", "dei", "degli",
    "e", "ed", "il", "lo", "la", "le", "i", "gli",
    "un", "una", "per", "a", "in", "of", "the", "and",
}

ORGANIZATION = {
    "Company", "Organization", "PublicAuthority",
    "NonProfitOrganization", "PoliticalParty",
    "LaborUnion", "MediaOutlet",
}
GEOGRAPHY = {"GPE", "LOC", "Location", "GeographicalArea", "Address"}
PHYSICAL = {"Facility", "Infrastructure", "NaturalResource"}
DOCUMENT = {"Document", "Contract", "Law", "Regulation", "Policy"}

LEGAL = re.compile(
    r"(?:\s+(?:s\s*p\s*a|s\s*r\s*l|s\s*n\s*c|"
    r"s\s*a\s*s|s\s*c\s*a\s*r\s*l))+$"
)

ROLE_CONTRASTS = (
    (r"\bministero\b", r"\bministr[oa]\b"),
    (r"\bpresidenza\b", r"\bpresidente\b"),
    (r"\bcomune\b", r"\bsindaco\b"),
)

ARTICLE_QUERY = """
UNWIND $ids AS article_id
OPTIONAL MATCH (a:Article {id:article_id})
RETURN
  article_id,
  a.id AS found_id,
  a.title AS title,
  a.subtitle AS subtitle,
  coalesce(
    a.body, a.article_text, a.full_text,
    a.content, a.text, ''
  ) AS body
"""


def require(ok, message):
    if not ok:
        raise ValueError(message)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def rows(path):
    with Path(path).open(encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{i}: {exc}") from exc


def emit(handle, value):
    handle.write(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True
        ) + "\n"
    )


def normalize(name):
    name = unicodedata.normalize(
        "NFKD", str(name or "")
    ).casefold()
    name = "".join(
        c for c in name if not unicodedata.combining(c)
    )
    return " ".join(re.sub(r"[^\w]+", " ", name).split())


def legal_base(name):
    return LEGAL.sub("", normalize(name)).strip()


def acronym(name):
    tokens = [
        token for token in normalize(name).split()
        if token not in STOPWORDS and len(token) >= 3
    ]
    if 2 <= len(tokens) <= 8:
        return "".join(token[0] for token in tokens)
    return None


def surface_pattern(name):
    """Match a name retaining real offsets in the original text."""
    tokens = re.findall(
        r"\w+",
        unicodedata.normalize("NFKC", name).casefold()
    )
    require(tokens, "Empty alias surface")
    return (
        r"(?<!\w)"
        + r"[\W_]*".join(map(re.escape, tokens))
        + r"(?!\w)"
    )


def is_abbreviation(long_name, short_name):
    short = normalize(short_name).replace(" ", "")
    return (
        len(short) >= 2
        and len(short) <= 12
        and acronym(long_name) == short
    )


def find_aliases(text, left, right, limit=3):
    """Return literal, offset-backed explicit naming evidence.

    Plain co-occurrence is never an alias.
    """

    if not isinstance(text, str) or not text:
        return []

    # NFKC/casefold is length-preserving for ordinary Italian
    # article text, but not universally. Search source text via
    # regex IGNORECASE to preserve original offsets.
    candidates = []

    for long_name, short_name in ((left, right), (right, left)):
        lp = surface_pattern(long_name)
        sp = surface_pattern(short_name)

        valid_short = (
            is_abbreviation(long_name, short_name)
            or (
                legal_base(long_name)
                == legal_base(short_name)
                and normalize(long_name) != normalize(short_name)
            )
        )

        patterns = []

        if valid_short:
            patterns.extend([
                (
                    "explicit_parenthetical",
                    rf"{lp}\.?\s*\(\s*{sp}\.?\s*\)",
                ),
                (
                    "explicit_parenthetical",
                    rf"{sp}\.?\s*\(\s*{lp}\.?\s*\)",
                ),
            ])

        patterns.extend([
            (
                "explicit_acronym_of",
                rf"{sp}\s*,?\s*(?:acronimo|sigla)"
                rf"\s+di\s+{lp}",
            ),
            (
                "explicit_abbreviation",
                rf"{lp}\s*,?\s*(?:abbreviat[oa]"
                rf"\s+(?:in|come)|in\s+sigla)\s+{sp}",
            ),
            (
                "explicit_naming",
                rf"{lp}\s*,?\s*(?:noto|nota|"
                rf"conosciuto|conosciuta|denominato|"
                rf"denominata)\s+anche\s+come\s+{sp}",
            ),
            (
                "explicit_naming",
                rf"{lp}\s*,?\s*(?:detto|detta|alias)"
                rf"\s+{sp}",
            ),
        ])

        for rule, pattern in patterns:
            for match in re.finditer(
                pattern, text, re.IGNORECASE
            ):
                start, end = match.span()
                candidates.append({
                    "rule": rule,
                    "start": start,
                    "end": end,
                    "quote": text[start:end],
                })

    unique = {
        (r["start"], r["end"], r["rule"]): r
        for r in candidates
    }
    return sorted(
        unique.values(),
        key=lambda r: (r["start"], r["end"], r["rule"])
    )[:limit]


def family(kind):
    if kind == "Person":
        return "PERSON"
    if kind in ORGANIZATION:
        return "ORGANIZATION"
    if kind in GEOGRAPHY:
        return "GEOGRAPHY"
    if kind in PHYSICAL:
        return "PHYSICAL"
    if kind in {"Project", "Event"}:
        return "ACTIVITY"
    if kind in DOCUMENT:
        return "DOCUMENT"
    if kind in {"Product", "Service", "Technology"}:
        return "PRODUCT_SERVICE"
    return "UNKNOWN"


def group_families(group):
    counts = Counter()
    for kind, count in group["kind_counts"].items():
        f = family(kind)
        if f != "UNKNOWN":
            counts[f] += count
    return counts


def type_compatibility(left, right):
    a = group_families(left)
    b = group_families(right)

    total_a = sum(left["kind_counts"].values())
    total_b = sum(right["kind_counts"].values())

    if not a or not b:
        return {
            "status": "UNKNOWN",
            "reasons": ["missing_or_unknown_type"],
        }

    fa, ca = a.most_common(1)[0]
    fb, cb = b.most_common(1)[0]
    confidence_a = ca / total_a
    confidence_b = cb / total_b

    if fa == fb:
        return {
            "status": "COMPATIBLE",
            "reasons": [],
        }

    # A company's name can also refer to its facility;
    # a project may be annotated as an event.
    soft_pairs = {
        frozenset({"ORGANIZATION", "PHYSICAL"}),
        frozenset({"ORGANIZATION", "GEOGRAPHY"}),
        frozenset({"ACTIVITY", "PRODUCT_SERVICE"}),
        frozenset({"ACTIVITY", "DOCUMENT"}),
        frozenset({"PHYSICAL", "GEOGRAPHY"}),
    }

    if frozenset({fa, fb}) in soft_pairs:
        status = "AMBIGUOUS"
        reasons = ["related_but_distinct_type_families"]
    elif confidence_a < 0.95 or confidence_b < 0.95:
        status = "AMBIGUOUS"
        reasons = ["mixed_or_uncertain_type_annotations"]
    elif frozenset({fa, fb}) == frozenset({
        "PERSON", "ORGANIZATION"
    }):
        status = "HARD_CONFLICT"
        reasons = ["person_vs_organization"]
    else:
        status = "AMBIGUOUS"
        reasons = ["different_type_families"]

    return {"status": status, "reasons": reasons}


def lexical_conflicts(left, right, type_status):
    a = normalize(left)
    b = normalize(right)
    hard = []
    soft = []

    for p, q in ROLE_CONTRASTS:
        if (
            (re.search(p, a) and re.search(q, b))
            or (re.search(q, a) and re.search(p, b))
        ):
            hard.append("institution_vs_office_holder")

    nums_a = re.findall(r"\b\d+\b", a)
    nums_b = re.findall(r"\b\d+\b", b)

    if nums_a and nums_b and nums_a != nums_b:
        soft.append("different_numeric_designators")

    if type_status["status"] == "HARD_CONFLICT":
        hard.extend(type_status["reasons"])

    return sorted(set(hard)), sorted(set(soft))


def load_inputs(candidate_dir, profile_dir, matching_dir):
    c = Path(candidate_dir)
    p = Path(profile_dir)
    m = Path(matching_dir)

    cs = read_json(c / "summary.json")
    ps = read_json(p / "summary.json")
    ms = read_json(m / "summary.json")

    require(cs["version"] == "candidate-generation-v1", "Candidates version")
    require(ps["version"] == "identity-profiles-v1", "Profiles version")
    require(ms["version"] == "evidence-matching-v1", "Matching version")

    checks = (
        (c / "surface_groups.jsonl", cs["surface_groups_sha256"]),
        (c / "candidate_pairs.jsonl", cs["candidate_pairs_sha256"]),
        (p / "mention_profiles.jsonl", ps["mention_profiles_sha256"]),
        (p / "component_profiles.jsonl", ps["component_profiles_sha256"]),
        (m / "pair_decisions.jsonl", ms["pair_decisions_sha256"]),
    )

    for path, expected in checks:
        require(sha(path) == expected, f"Checksum mismatch: {path}")

    require(
        cs["source_batch_key"] == ps["source_batch_key"]
        == ms["source_batch_key"],
        "Source batches differ",
    )
    require(
        ms["source_candidate_sha256"]
        == cs["candidate_pairs_sha256"],
        "Matching/candidate lineage mismatch",
    )
    require(
        cs["source_mention_sha256"]
        == ps["mention_profiles_sha256"]
        and cs["source_component_sha256"]
        == ps["component_profiles_sha256"],
        "Profile lineage mismatch",
    )
    require(
        cs["graph_writes"] == ps["graph_writes"]
        == ms["graph_writes"] == 0,
        "Unexpected graph mutations",
    )

    groups = {
        row["group_id"]: row
        for row in rows(c / "surface_groups.jsonl")
    }
    pairs = {
        row["pair_id"]: row
        for row in rows(c / "candidate_pairs.jsonl")
    }
    previous = {
        row["pair_id"]: row
        for row in rows(m / "pair_decisions.jsonl")
    }

    require(
        len(groups) == cs["surface_groups"]
        and len(pairs) == cs["candidate_pairs"]
        and len(previous) == ms["processed_pairs"],
        "Source count mismatch",
    )
    require(
        set(pairs) == set(previous),
        "Candidate/matching pair mismatch",
    )

    # Build article index from eligible occurrence profiles.
    occurrence_to_group = {}
    for group in groups.values():
        for occurrence_id in group["occurrence_ids"]:
            require(
                occurrence_id not in occurrence_to_group,
                "Occurrence assigned to multiple groups",
            )
            occurrence_to_group[occurrence_id] = group["group_id"]

    article_index = defaultdict(set)
    found_occurrences = set()

    for name in ("mention_profiles.jsonl", "component_profiles.jsonl"):
        for profile in rows(p / name):
            pid = profile["profile_id"]
            gid = occurrence_to_group.get(pid)
            if gid is None:
                continue
            require(pid not in found_occurrences, "Repeated profile")
            found_occurrences.add(pid)
            article_index[gid].add(profile["article_id"])

    require(
        len(found_occurrences) == len(occurrence_to_group),
        "Missing eligible profiles",
    )

    return cs, groups, pairs, previous, article_index


def select_articles(left, right, index, limit):
    a = index.get(left, set())
    b = index.get(right, set())
    shared = sorted(a & b)
    remaining = sorted((a | b) - set(shared))

    return (
        (shared + remaining)[:limit],
        len(shared),
    )


def should_recover(pair, previous, shared, minimum_score):
    return bool(
        shared
        or "acronym" in pair["blocking_reasons"]
        or "legal_form" in pair["blocking_reasons"]
        or pair["lexical_score"] >= minimum_score
        or previous["verdict"] == "POSSIBLE_MATCH"
    )


def load_articles(ids, *, articles_jsonl=None, batch_size=200):
    """Load only selected articles.

    Optionally accept a JSONL article export for offline testing.
    """

    if articles_jsonl:
        wanted = set(ids)
        found = {}
        for article in rows(articles_jsonl):
            aid = article.get("article_id", article.get("id"))
            if aid in wanted:
                found[aid] = article
        return found

    from neo4j import GraphDatabase
    from presskg.settings import CFG

    driver = GraphDatabase.driver(
        CFG.neo4j_uri,
        auth=(CFG.neo4j_user, CFG.neo4j_password),
    )

    found = {}
    try:
        with driver.session(database=CFG.neo4j_database) as session:
            for start in range(0, len(ids), batch_size):
                subset = ids[start:start + batch_size]
                for result in session.run(
                    ARTICLE_QUERY, ids=subset
                ):
                    item = dict(result)
                    if item["found_id"] is not None:
                        found[item["article_id"]] = item
    finally:
        driver.close()

    return found


def recover_pair(pair, groups, article_ids, articles):
    left = groups[pair["left_group_id"]]
    right = groups[pair["right_group_id"]]

    attestations = []
    inspected = 0

    for article_id in article_ids:
        article = articles.get(article_id)
        if not article:
            continue

        inspected += 1

        for field in ("body", "subtitle", "title"):
            content = article.get(field)
            if not isinstance(content, str) or not content:
                continue

            matches = find_aliases(
                content,
                left["preferred_surface"],
                right["preferred_surface"],
                limit=2,
            )

            for match in matches:
                start = max(0, match["start"] - 100)
                end = min(len(content), match["end"] + 100)

                attestations.append({
                    "article_id": article_id,
                    "field": field,
                    "source_sha256": hashlib.sha256(
                        content.encode("utf-8")
                    ).hexdigest(),
                    "start": match["start"],
                    "end": match["end"],
                    "quote": match["quote"],
                    "context": content[start:end],
                    "rule": match["rule"],
                })

            if len(attestations) >= 3:
                break

        if len(attestations) >= 3:
            break

    return attestations[:3], inspected


def adjudicate(pair, old, left, right, attestations):
    types = type_compatibility(left, right)
    hard, soft = lexical_conflicts(
        left["preferred_surface"],
        right["preferred_surface"],
        types,
    )

    if hard:
        verdict = "CONFLICT"
    elif attestations and types["status"] in {
        "COMPATIBLE", "AMBIGUOUS"
    }:
        verdict = "MATCH_SUPPORTED"
    elif (
        old["verdict"] == "POSSIBLE_MATCH"
        or old["legal_base_equal"]
        or pair["lexical_score"] >= 0.94
        or attestations
    ):
        verdict = "POSSIBLE_MATCH"
    else:
        verdict = "INSUFFICIENT_EVIDENCE"

    return {
        "pair_id": pair["pair_id"],
        "left_group_id": pair["left_group_id"],
        "right_group_id": pair["right_group_id"],
        "left_surface": left["preferred_surface"],
        "right_surface": right["preferred_surface"],
        "lexical_score": pair["lexical_score"],
        "blocking_reasons": pair["blocking_reasons"],
        "previous_verdict": old["verdict"],
        "verdict": verdict,
        "type_compatibility": types,
        "hard_conflicts": hard,
        "soft_warnings": soft,
        "alias_attestations": attestations,
        "attribute_support": old.get(
            "shared_attribute_signatures", []
        ),
        "identity_merge_approved": False,
        "canonical_entity_id": None,
        "graph_writes": 0,
    }


def run(args):
    require(args.max_articles_per_pair >= 1, "Invalid article limit")
    require(args.batch_size >= 1, "Invalid batch size")
    require(
        0 <= args.min_priority_score <= 1,
        "Invalid priority score",
    )

    cs, groups, pairs, previous, index = load_inputs(
        args.candidates_dir,
        args.profiles_dir,
        args.matching_dir,
    )

    selections = {}
    target_ids = set()
    prioritized = 0

    for pid, pair in pairs.items():
        selected, shared = select_articles(
            pair["left_group_id"],
            pair["right_group_id"],
            index,
            args.max_articles_per_pair,
        )

        if should_recover(
            pair, previous[pid],
            shared, args.min_priority_score,
        ):
            selections[pid] = (selected, shared)
            target_ids.update(selected)
            prioritized += 1
        else:
            selections[pid] = ([], shared)

    articles = load_articles(
        sorted(target_ids),
        articles_jsonl=args.articles_jsonl,
        batch_size=args.batch_size,
    )

    out = Path(args.out_dir)
    require(not out.exists(), "Output directory already exists")
    out.mkdir(parents=True)

    decisions_path = out / "pair_decisions.jsonl"
    review_path = out / "review_queue.jsonl"

    verdicts = Counter()
    types = Counter()
    changes = Counter()
    alias_rules = Counter()
    articles_inspected = 0

    with (
        decisions_path.open("w", encoding="utf-8") as dest,
        review_path.open("w", encoding="utf-8") as review,
    ):
        for pid in sorted(pairs):
            pair = pairs[pid]
            selected, shared = selections[pid]

            attestations, inspected = recover_pair(
                pair, groups, selected, articles
            )
            articles_inspected += inspected

            record = adjudicate(
                pair,
                previous[pid],
                groups[pair["left_group_id"]],
                groups[pair["right_group_id"]],
                attestations,
            )

            record["shared_article_count"] = shared
            record["articles_selected"] = len(selected)
            record["articles_inspected"] = inspected

            emit(dest, record)
            verdicts[record["verdict"]] += 1
            types[record["type_compatibility"]["status"]] += 1

            if record["verdict"] != record["previous_verdict"]:
                changes[
                    record["previous_verdict"]
                    + " -> " + record["verdict"]
                ] += 1

            for attestation in attestations:
                alias_rules[attestation["rule"]] += 1

            if record["verdict"] != "MATCH_SUPPORTED":
                emit(review, record)

    require(
        sum(verdicts.values()) == len(pairs),
        "Incomplete pair processing",
    )

    summary = {
        "version": VERSION,
        "source_batch_key": cs["source_batch_key"],
        "source_candidate_sha256":
            cs["candidate_pairs_sha256"],
        "source_matching_sha256":
            sha(Path(args.matching_dir) / "pair_decisions.jsonl"),
        "processed_pairs": len(pairs),
        "prioritized_pairs": prioritized,
        "requested_articles": len(target_ids),
        "retrieved_articles": len(articles),
        "article_inspections": articles_inspected,
        "verdict_counts": dict(sorted(verdicts.items())),
        "type_status_counts": dict(sorted(types.items())),
        "verdict_changes": dict(sorted(changes.items())),
        "alias_evidence_counts": dict(sorted(alias_rules.items())),
        "review_queue_count": (
            len(pairs) - verdicts["MATCH_SUPPORTED"]
        ),
        "max_articles_per_pair": args.max_articles_per_pair,
        "infrastructure_scope_policy": "ADVISORY_ONLY",
        "pair_decisions_sha256": sha(decisions_path),
        "review_queue_sha256": sha(review_path),
        "llm_calls": 0,
        "identity_merges": 0,
        "approved_cluster_edges": 0,
        "graph_writes": 0,
    }

    (out / "summary.json").write_text(
        json.dumps(
            summary, ensure_ascii=False,
            indent=2, sort_keys=True
        ) + "\n",
        encoding="utf-8",
    )

    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--candidates-dir",
        default="output/resolution/candidate-generation-v1",
    )
    parser.add_argument(
        "--profiles-dir",
        default="output/resolution/identity-profiles-v1",
    )
    parser.add_argument(
        "--matching-dir",
        default="output/resolution/evidence-matching-v1",
    )
    parser.add_argument(
        "--out-dir",
        default="output/resolution/contextual-alias-recovery-v1",
    )
    parser.add_argument("--max-articles-per-pair", type=int, default=16)
    parser.add_argument("--min-priority-score", type=float, default=0.90)
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument(
        "--articles-jsonl",
        default=None,
        help="Optional offline article export",
    )
    run(parser.parse_args())


if __name__ == "__main__":
    main()
