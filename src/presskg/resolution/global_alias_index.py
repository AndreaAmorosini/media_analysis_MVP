
"""PressKG Global Alias Evidence Index v1.

Corpus-wide, evidence-grounded alias retrieval.

No LLM calls, identity merges, graph writes, or automatic
clustering. Existing candidate pairs remain unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

from presskg.resolution.candidate_generation import (
    acronym,
    legal_base,
    normalize,
)

VERSION = "global-alias-index-v1"

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

PARENTHETICAL = re.compile(r"\(([^()\n]{2,100})\)")
LEGAL_FORMS = re.compile(
    r"\b(?:spa|srl|snc|sas|scarl)\b",
    re.IGNORECASE,
)
ACRONYM_BRIDGE = re.compile(
    r"(?P<short>\b[A-Za-z]{2,12}\b)"
    r"\s*(?:,?\s*(?:acronimo|sigla)\s+di|[-:–])"
    r"\s*",
    re.IGNORECASE,
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest_file(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(
            lambda: handle.read(1024 * 1024), b""
        ):
            hasher.update(chunk)
    return hasher.hexdigest()


def digest_text(value):
    return hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def jsonl(path):
    with Path(path).open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path}:{number}: {exc}"
                ) from exc


def emit(handle, record):
    handle.write(json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
    ) + "\n")


def surface_pattern(value):
    """Boundary-aware source matching with original offsets.

    Separators may be missing due to OCR, but the complete
    surface must occur immediately beside the naming cue.
    """
    tokens = normalize(value).split()
    if not tokens:
        return None

    return (
        r"(?<!\w)"
        + r"[\W_]*".join(map(re.escape, tokens))
        + r"(?!\w)"
    )


def candidate_identity_compatible(left, right):
    """Only a warning, never an automatic merge decision."""
    from presskg.resolution.contextual_alias_recovery import (
        type_compatibility,
    )
    return type_compatibility(left, right)


def load_sources(candidates_dir, profiles_dir, matching_dir):
    c = Path(candidates_dir)
    p = Path(profiles_dir)
    m = Path(matching_dir)

    cs = read_json(c / "summary.json")
    ps = read_json(p / "summary.json")
    ms = read_json(m / "summary.json")

    require(
        cs["version"] == "candidate-generation-v1",
        "Unexpected candidate version",
    )
    require(
        ps["version"] == "identity-profiles-v1",
        "Unexpected profile version",
    )
    require(
        ms["version"] == "contextual-alias-recovery-v1",
        "Unexpected matching version",
    )

    require(
        cs["source_batch_key"]
        == ps["source_batch_key"]
        == ms["source_batch_key"],
        "Different source batches",
    )
    require(
        ms["source_candidate_sha256"]
        == cs["candidate_pairs_sha256"],
        "Candidate/matching lineage mismatch",
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

    checks = (
        (
            c / "surface_groups.jsonl",
            cs["surface_groups_sha256"],
        ),
        (
            c / "candidate_pairs.jsonl",
            cs["candidate_pairs_sha256"],
        ),
        (
            p / "mention_profiles.jsonl",
            ps["mention_profiles_sha256"],
        ),
        (
            p / "component_profiles.jsonl",
            ps["component_profiles_sha256"],
        ),
        (
            m / "pair_decisions.jsonl",
            ms["pair_decisions_sha256"],
        ),
    )

    for path, expected in checks:
        require(
            digest_file(path) == expected,
            f"Checksum mismatch: {path}",
        )

    require(
        cs["graph_writes"]
        == ps["graph_writes"]
        == ms["graph_writes"]
        == 0,
        "Unexpected graph mutations",
    )

    groups = {}
    by_surface = {}
    acronym_index = defaultdict(set)
    legal_index = defaultdict(set)

    for group in jsonl(c / "surface_groups.jsonl"):
        gid = group["group_id"]

        require(gid not in groups, "Duplicate group")
        require(
            group["identity_approved"] is False,
            "Unexpected approved group",
        )

        groups[gid] = group
        key = group["normalized_surface"]

        require(
            key not in by_surface,
            "Duplicate normalized surface",
        )
        by_surface[key] = gid

        ac = acronym(key)
        if ac:
            acronym_index[ac].add(gid)

        base = legal_base(key)
        if base:
            legal_index[base].add(gid)

    existing = set()
    for pair in jsonl(c / "candidate_pairs.jsonl"):
        key = tuple(sorted((
            pair["left_group_id"],
            pair["right_group_id"],
        )))
        require(
            key not in existing,
            "Repeated candidate pair",
        )
        existing.add(key)

    require(
        len(groups) == cs["surface_groups"]
        and len(existing) == cs["candidate_pairs"],
        "Candidate count mismatch",
    )

    prior_pairs = set()
    for row in jsonl(m / "pair_decisions.jsonl"):
        key = tuple(sorted((
            row["left_group_id"],
            row["right_group_id"],
        )))
        require(
            key in existing,
            "Matching pair not found in candidates",
        )
        require(
            key not in prior_pairs,
            "Repeated matching pair",
        )
        prior_pairs.add(key)

    require(
        prior_pairs == existing,
        "Matching coverage mismatch",
    )

    # Full article inventory. Include even mentions not eligible
    # for identity resolution, so alias evidence is not lost.
    article_ids = set()

    for filename, expected_count in (
        ("mention_profiles.jsonl", ps["mention_profiles"]),
        ("component_profiles.jsonl", ps["component_profiles"]),
    ):
        count = 0
        for profile in jsonl(p / filename):
            count += 1
            article_ids.add(profile["article_id"])
        require(
            count == expected_count,
            f"Incomplete {filename}",
        )

    return {
        "candidate_summary": cs,
        "groups": groups,
        "by_surface": by_surface,
        "acronym_index": acronym_index,
        "legal_index": legal_index,
        "existing_pairs": existing,
        "article_ids": sorted(article_ids),
    }


def find_long_ending(prefix, group_ids, groups):
    """Match a complete known name immediately before '('.

    Prefer the longest anchored name. This avoids treating an
    arbitrary earlier mention as an alias declaration.
    """
    results = []

    for gid in group_ids:
        name = groups[gid]["preferred_surface"]
        pattern = surface_pattern(name)
        if pattern is None:
            continue

        hit = re.search(
            rf"({pattern})\.?\s*$",
            prefix,
            re.IGNORECASE,
        )
        if hit:
            results.append((
                hit.start(1),
                hit.end(),
                gid,
            ))

    return sorted(
        results,
        key=lambda x: (
            -(x[1] - x[0]),
            x[2],
        ),
    )


def extract_from_text(text, index, limit_per_article=200):
    """Extract grounded alias proposals in one article field.

    Only known surface groups are linked. This is a high
    precision retrieval index, not a generic NER extractor.
    """
    groups = index["groups"]
    by_surface = index["by_surface"]

    found = []

    def record(left_gid, right_gid, rule, start, end):
        if left_gid == right_gid:
            return

        left, right = sorted((left_gid, right_gid))
        require(
            0 <= start < end <= len(text),
            "Invalid source offsets",
        )

        found.append({
            "left_group_id": left,
            "right_group_id": right,
            "rule": rule,
            "start": start,
            "end": end,
            "quote": text[start:end],
        })

    # 1. Full Name (ACRONYM), including legal-form variants.
    for match in PARENTHETICAL.finditer(text):
        inside = match.group(1).strip()
        short_key = normalize(inside)

        short_gid = by_surface.get(short_key)
        if short_gid is None:
            continue

        nearby = text[max(
            0, match.start() - 220
        ):match.start()]

        possible = set(
            index["acronym_index"].get(
                short_key.replace(" ", ""), set()
            )
        )

        possible.update(
            index["legal_index"].get(
                legal_base(inside), set()
            )
        )

        for start, _, long_gid in find_long_ending(
            nearby,
            possible,
            groups,
        )[:1]:
            full_start = max(
                0, match.start() - 220
            ) + start

            rule = (
                "PARENTHETICAL_ACRONYM"
                if acronym(
                    groups[long_gid]["normalized_surface"]
                ) == short_key.replace(" ", "")
                else "PARENTHETICAL_LEGAL_VARIANT"
            )

            record(
                long_gid,
                short_gid,
                rule,
                full_start,
                match.end(),
            )

            if len(found) >= limit_per_article:
                return found

    # 2. EIC, acronimo di Ente Idrico Campano
    #    EIC - Ente Idrico Campano
    for match in ACRONYM_BRIDGE.finditer(text):
        short = normalize(match.group("short"))
        short_gid = by_surface.get(short)
        if short_gid is None:
            continue

        candidates = index["acronym_index"].get(
            short, set()
        )
        if not candidates:
            continue

        following = text[
            match.end():match.end() + 200
        ]

        for long_gid in sorted(candidates):
            pattern = surface_pattern(
                groups[long_gid]["preferred_surface"]
            )
            if pattern is None:
                continue

            hit = re.match(
                pattern,
                following,
                re.IGNORECASE,
            )
            if hit is None:
                continue

            rule = (
                "EXPLICIT_ACRONYM_OF"
                if re.search(
                    r"(?:acronimo|sigla)\s+di",
                    match.group(0),
                    re.IGNORECASE,
                )
                else "DASH_ACRONYM_EXPANSION"
            )

            record(
                short_gid,
                long_gid,
                rule,
                match.start(),
                match.end() + hit.end(),
            )

            if len(found) >= limit_per_article:
                return found

    unique = {
        (
            item["left_group_id"],
            item["right_group_id"],
            item["rule"],
            item["start"],
            item["end"],
        ): item
        for item in found
    }

    return sorted(
        unique.values(),
        key=lambda item: (
            item["start"],
            item["end"],
            item["left_group_id"],
        ),
    )


def iter_articles(ids, batch_size, offline_path=None):
    """Yield article data without loading the whole corpus at once."""

    if offline_path is not None:
        wanted = set(ids)
        seen = set()

        for row in jsonl(offline_path):
            aid = row.get(
                "article_id", row.get("id")
            )

            if aid not in wanted:
                continue

            require(
                aid not in seen,
                "Duplicate offline article",
            )
            seen.add(aid)

            yield {
                "article_id": aid,
                "found_id": aid,
                "body": row.get("body", ""),
                "title": row.get("title", ""),
                "subtitle": row.get("subtitle", ""),
            }
        return

    from neo4j import GraphDatabase
    from presskg.settings import CFG

    driver = GraphDatabase.driver(
        CFG.neo4j_uri,
        auth=(CFG.neo4j_user, CFG.neo4j_password),
    )
    try:
        with driver.session(
            database=CFG.neo4j_database
        ) as session:
            for start in range(0, len(ids), batch_size):
                subset = ids[start:start + batch_size]

                for row in session.run(
                    ARTICLE_QUERY,
                    ids=subset,
                ):
                    yield dict(row)
    finally:
        driver.close()


def run(args):
    require(args.batch_size >= 1, "Invalid batch size")
    require(args.max_per_field >= 1, "Invalid evidence limit")

    index = load_sources(
        args.candidates_dir,
        args.profiles_dir,
        args.matching_dir,
    )

    out = Path(args.out_dir)
    require(
        not out.exists(),
        "Output directory exists",
    )
    out.mkdir(parents=True)

    att_path = out / "alias_attestations.jsonl"
    edges_path = out / "alias_edges.jsonl"
    new_path = out / "new_candidate_pairs.jsonl"

    counts = Counter()
    rules = Counter()
    edges = defaultdict(list)
    seen_articles = set()

    with att_path.open(
        "w", encoding="utf-8"
    ) as evidence_file:
        for article in iter_articles(
            index["article_ids"],
            args.batch_size,
            args.articles_jsonl,
        ):
            aid = article["article_id"]

            require(
                aid not in seen_articles,
                f"Duplicate article: {aid}",
            )
            seen_articles.add(aid)

            if article.get("found_id") is None:
                counts["missing_articles"] += 1
                continue

            counts["articles_scanned"] += 1

            for field in ("body", "title", "subtitle"):
                source = article.get(field)

                if not isinstance(source, str) or not source:
                    continue

                source_sha = digest_text(source)

                proposals = extract_from_text(
                    source,
                    index,
                    limit_per_article=args.max_per_field,
                )

                for item in proposals:
                    key = (
                        item["left_group_id"],
                        item["right_group_id"],
                    )

                    evidence_id = digest_text(
                        "\x00".join(map(str, (
                            aid,
                            field,
                            item["start"],
                            item["end"],
                            item["rule"],
                            *key,
                        )))
                    )[:32]

                    evidence = {
                        **item,
                        "evidence_id": evidence_id,
                        "article_id": aid,
                        "field": field,
                        "source_sha256": source_sha,
                        "evidence_class": "DIRECT_NAMING",
                        "identity_merge_approved": False,
                    }

                    emit(evidence_file, evidence)
                    edges[key].append(evidence_id)

                    counts["attestations"] += 1
                    rules[item["rule"]] += 1

    require(
        seen_articles.issubset(
            set(index["article_ids"])
        ),
        "Scanned unexpected article",
    )

    new_pairs = 0
    type_status = Counter()

    with (
        edges_path.open("w", encoding="utf-8") as edge_file,
        new_path.open("w", encoding="utf-8") as new_file,
    ):
        for (left_id, right_id), ids in sorted(edges.items()):
            left = index["groups"][left_id]
            right = index["groups"][right_id]

            compatibility = candidate_identity_compatible(
                left, right
            )

            type_status[
                compatibility["status"]
            ] += 1

            was_candidate = (
                (left_id, right_id)
                in index["existing_pairs"]
            )

            row = {
                "edge_id": "alias-" + digest_text(
                    left_id + "\x00" + right_id
                )[:24],
                "left_group_id": left_id,
                "right_group_id": right_id,
                "left_surface": left["preferred_surface"],
                "right_surface": right["preferred_surface"],
                "attestation_ids": sorted(set(ids)),
                "attestation_count": len(set(ids)),
                "type_compatibility": compatibility,
                "existing_candidate_pair": was_candidate,
                "matching_status": "EVIDENCE_CANDIDATE",
                "identity_merge_approved": False,
                "graph_writes": 0,
            }

            emit(edge_file, row)

            if not was_candidate:
                emit(new_file, row)
                new_pairs += 1

    summary = {
        "version": VERSION,
        "source_batch_key": index[
            "candidate_summary"
        ]["source_batch_key"],
        "source_candidate_sha256": index[
            "candidate_summary"
        ]["candidate_pairs_sha256"],
        "article_inventory": len(index["article_ids"]),
        "articles_scanned": counts["articles_scanned"],
        "missing_articles": counts["missing_articles"],
        "attestations": counts["attestations"],
        "alias_edges": len(edges),
        "existing_candidate_edges": (
            len(edges) - new_pairs
        ),
        "new_candidate_pairs": new_pairs,
        "rule_counts": dict(sorted(rules.items())),
        "type_status_counts":
            dict(sorted(type_status.items())),
        "alias_attestations_sha256": digest_file(att_path),
        "alias_edges_sha256": digest_file(edges_path),
        "new_candidate_pairs_sha256": digest_file(new_path),
        "infrastructure_scope_policy": "ADVISORY_ONLY",
        "llm_calls": 0,
        "identity_merges": 0,
        "graph_writes": 0,
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
        "--candidates-dir",
        default="output/resolution/candidate-generation-v1",
    )
    parser.add_argument(
        "--profiles-dir",
        default="output/resolution/identity-profiles-v1",
    )
    parser.add_argument(
        "--matching-dir",
        default="output/resolution/contextual-alias-recovery-v1",
    )
    parser.add_argument(
        "--out-dir",
        default="output/resolution/global-alias-index-v1",
    )
    parser.add_argument(
        "--batch-size", type=int, default=200
    )
    parser.add_argument(
        "--max-per-field", type=int, default=200
    )
    parser.add_argument(
        "--articles-jsonl", default=None
    )

    run(parser.parse_args())


if __name__ == "__main__":
    main()
