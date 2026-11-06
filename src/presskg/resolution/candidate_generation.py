
"""PressKG Candidate Generation v1.

Build candidate pairs between groups of normalized surfaces,
never between every pair of mention occurrences.

Uses:
- Unicode and punctuation normalization
- Legal-form normalization
- Acronym blocking
- Rare-token blocking
- Conservative fuzzy comparison

Read-only, deterministic, no LLM and no entity merging.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata

from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path


VERSION = "candidate-generation-v1"
SOURCE_VERSION = "identity-profiles-v1"

EXCLUDED_KINDS = {
    "Money", "Quantity", "Date", "Time",
    "Percentage", "Duration", "Topic",
    "PopulationGroup",
}

EXCLUDED_STATUSES = {
    "NON_IDENTITY",
    "COLLECTIVE_HOLD",
    "COMPOSITE_MENTION_HOLD",
    "PROFILE_ONLY_HELD",
    "PROFILE_ONLY_UNRESOLVED",
}

LEGAL_SUFFIX = re.compile(
    r"(?:\s+(?:s\s*p\s*a|s\s*r\s*l|"
    r"s\s*n\s*c|s\s*a\s*s|"
    r"s\s*c\s*a\s*r\s*l|"
    r"societa\s+per\s+azioni|"
    r"societa\s+a\s+responsabilita\s+limitata))+$"
)

STOPWORDS = {
    "di", "del", "della", "delle", "dei", "degli",
    "da", "in", "per", "con", "sul", "sulla",
    "e", "ed", "il", "lo", "la", "le", "i", "gli",
    "un", "una", "uno", "al", "alla", "alle",
    "ai", "agli", "a", "the", "of", "and",
}

TYPE_FAMILIES = {
    "Person": "PERSON",
    "Company": "ORGANIZATION",
    "Organization": "ORGANIZATION",
    "PublicAuthority": "ORGANIZATION",
    "NonProfitOrganization": "ORGANIZATION",
    "PoliticalParty": "ORGANIZATION",
    "LaborUnion": "ORGANIZATION",
    "MediaOutlet": "ORGANIZATION",

    "GPE": "GEOGRAPHY",
    "Location": "GEOGRAPHY",
    "LOC": "GEOGRAPHY",
    "GeographicalArea": "GEOGRAPHY",
    "Address": "GEOGRAPHY",

    "Infrastructure": "PHYSICAL",
    "Facility": "PHYSICAL",
    "NaturalResource": "PHYSICAL",

    "Project": "PROJECT",
    "Event": "EVENT",
    "Service": "SERVICE",
    "Product": "PRODUCT",
    "Document": "DOCUMENT",
    "Contract": "DOCUMENT",
    "Law": "DOCUMENT",
    "Regulation": "DOCUMENT",
    "Policy": "DOCUMENT",
    "MediaWork": "DOCUMENT",
    "Technology": "TECHNOLOGY",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256_file(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(
            lambda: file.read(1024 * 1024), b""
        ):
            hasher.update(chunk)
    return hasher.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def iter_jsonl(path):
    with Path(path).open(encoding="utf-8") as file:
        for line_number, line in enumerate(file, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path}:{line_number}: {exc}"
                ) from exc


def write_jsonl(path, rows):
    with Path(path).open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(
                json.dumps(
                    row,
                    ensure_ascii=False,
                    sort_keys=True,
                ) + "\n"
            )


def normalize(name):
    text = unicodedata.normalize(
        "NFKD", str(name or "")
    ).casefold()

    text = "".join(
        char for char in text
        if not unicodedata.combining(char)
    )

    text = re.sub(r"[^\w]+", " ", text)
    return " ".join(text.split())


def legal_base(name):
    return LEGAL_SUFFIX.sub("", normalize(name)).strip()


def significant_tokens(name):
    return tuple(
        token for token in normalize(name).split()
        if token not in STOPWORDS and len(token) >= 3
    )


def acronym(name):
    tokens = significant_tokens(name)

    if not 2 <= len(tokens) <= 8:
        return None

    result = "".join(
        token[0] for token in tokens
    )

    return result if len(result) >= 2 else None


def family(kind):
    return TYPE_FAMILIES.get(kind)


def pair_types(left, right):
    lf = {
        family(k) for k in left["kind_counts"]
        if family(k)
    }
    rf = {
        family(k) for k in right["kind_counts"]
        if family(k)
    }

    if not lf or not rf:
        return "UNKNOWN"

    if lf & rf:
        return "OVERLAPPING"

    return "POTENTIAL_CONFLICT"


def eligible(profile):
    if profile["source_kind"] in EXCLUDED_KINDS:
        return False

    if profile["resolution_status"] in EXCLUDED_STATUSES:
        return False

    if not profile["surface"].strip():
        return False

    # Short names are allowed, but require strong blocking.
    if len(normalize(profile["surface"])) < 2:
        return False

    return True


def load_groups(directory):
    directory = Path(directory)
    summary = read_json(directory / "summary.json")

    require(
        summary["version"] == SOURCE_VERSION,
        "Unexpected profile source version",
    )

    require(
        summary["graph_writes"] == 0
        and summary["identity_merges"] == 0,
        "Source includes identity mutations",
    )

    sources = [
        (
            "mention_profiles.jsonl",
            "mention_profiles",
            "mention_profiles_sha256",
        ),
        (
            "component_profiles.jsonl",
            "component_profiles",
            "component_profiles_sha256",
        ),
    ]

    groups = {}
    counts = Counter()
    seen_profiles = set()

    for filename, count_key, hash_key in sources:
        path = directory / filename

        require(
            sha256_file(path) == summary[hash_key],
            f"Checksum mismatch: {filename}",
        )

        rows = 0

        for profile in iter_jsonl(path):
            rows += 1

            pid = profile["profile_id"]

            require(
                pid not in seen_profiles,
                f"Duplicate profile: {pid}",
            )
            seen_profiles.add(pid)

            require(
                profile["canonical_id"] is None
                and profile["merge_approved"] is False,
                "Unexpected resolved profile",
            )

            if not eligible(profile):
                counts["excluded_profiles"] += 1
                continue

            key = normalize(profile["surface"])

            if key not in groups:
                groups[key] = {
                    "group_id": "sg-" + hashlib.sha256(
                        key.encode("utf-8")
                    ).hexdigest()[:24],
                    "normalized_surface": key,
                    "occurrence_ids": [],
                    "surface_counts": Counter(),
                    "kind_counts": Counter(),
                    "status_counts": Counter(),
                    "article_ids": set(),
                    "component_count": 0,
                }

            group = groups[key]

            group["occurrence_ids"].append(pid)
            group["surface_counts"][profile["surface"]] += 1
            group["kind_counts"][
                str(profile["source_kind"])
            ] += 1
            group["status_counts"][
                profile["resolution_status"]
            ] += 1
            group["article_ids"].add(
                profile["article_id"]
            )

            if profile["profile_scope"] == "decomposed_component":
                group["component_count"] += 1

            counts["eligible_profiles"] += 1

        require(
            rows == summary[count_key],
            f"Coverage mismatch for {filename}",
        )

    for group in groups.values():
        group["occurrence_ids"].sort()
        group["kind_counts"] = dict(
            sorted(group["kind_counts"].items())
        )
        group["status_counts"] = dict(
            sorted(group["status_counts"].items())
        )

        group["preferred_surface"] = sorted(
            group["surface_counts"].items(),
            key=lambda item: (-item[1], item[0])
        )[0][0]

        group["variant_count"] = len(group["surface_counts"])
        group["occurrence_count"] = len(group["occurrence_ids"])
        group["article_count"] = len(group["article_ids"])

        # The grouping records lexical equivalence only.
        group["identity_approved"] = False

        del group["surface_counts"]
        del group["article_ids"]

    return summary, groups, counts


def lexical_score(left, right, reason):
    a = left["normalized_surface"]
    b = right["normalized_surface"]

    if reason == "legal_form":
        return 1.0

    if reason == "acronym":
        return 0.80

    ratio = SequenceMatcher(
        None, a, b, autojunk=False
    ).ratio()

    at = set(significant_tokens(a))
    bt = set(significant_tokens(b))

    jaccard = (
        len(at & bt) / len(at | bt)
        if at and bt else 0.0
    )

    return round(
        max(ratio, 0.55 * ratio + 0.45 * jaccard),
        5,
    )


def generate_pairs(
    groups,
    *,
    max_block=100,
    max_candidates=25,
    min_score=0.80,
):
    """Generate bounded pairs of distinct surface groups."""

    require(max_block >= 2, "max_block must be >= 2")
    require(max_candidates >= 1, "Invalid candidate limit")
    require(0 <= min_score <= 1, "Invalid score threshold")

    keys = sorted(groups)

    # Strong blocking: legal forms.
    legal_index = defaultdict(list)

    # Acronym lookup, using complete surface groups.
    acronym_index = defaultdict(list)

    # Weak blocking: discriminative tokens.
    token_index = defaultdict(list)

    for key in keys:
        base = legal_base(key)

        if base:
            legal_index[base].append(key)

        ac = acronym(key)
        if ac:
            acronym_index[ac].append(key)

        for token in sorted(set(significant_tokens(key))):
            token_index[token].append(key)

    proposals = {}
    skipped_blocks = Counter()

    def add_pair(a, b, reason):
        if a == b:
            return

        if a > b:
            a, b = b, a

        candidate_id = (a, b)

        if candidate_id not in proposals:
            proposals[candidate_id] = set()

        proposals[candidate_id].add(reason)

    # Exact legal-form bases.
    for members in legal_index.values():
        if len(members) > max_block:
            skipped_blocks["legal_form"] += 1
            continue

        for i, a in enumerate(members):
            for b in members[i + 1:]:
                add_pair(a, b, "legal_form")

    # Acronym against a literal short surface.
    for ac, expanded in acronym_index.items():
        if ac not in groups:
            continue

        if len(expanded) > max_block:
            skipped_blocks["acronym"] += 1
            continue

        for key in expanded:
            if key != ac:
                add_pair(ac, key, "acronym")

    # Only low-frequency tokens are used.
    # Do not build massive candidate sets from common terms.
    for members in token_index.values():
        if len(members) > max_block:
            skipped_blocks["rare_token"] += 1
            continue

        for i, a in enumerate(members):
            for b in members[i + 1:]:
                add_pair(a, b, "rare_token")

    neighbors = defaultdict(list)

    for (a, b), reasons in sorted(proposals.items()):
        strong = (
            "legal_form" in reasons
            or "acronym" in reasons
        )

        primary_reason = (
            "legal_form" if "legal_form" in reasons
            else "acronym" if "acronym" in reasons
            else "rare_token"
        )

        score = lexical_score(
            groups[a], groups[b], primary_reason
        )

        # Single-token names should not match each other
        # through a weak fuzzy similarity alone.
        single = (
            len(significant_tokens(a)) <= 1
            or len(significant_tokens(b)) <= 1
        )

        threshold = (
            max(min_score, 0.93)
            if single and not strong
            else min_score
        )

        if score < threshold:
            continue

        type_status = pair_types(groups[a], groups[b])

        candidate = {
            "pair_id": "pair-" + hashlib.sha256(
                (a + "\x00" + b).encode("utf-8")
            ).hexdigest()[:24],
            "left_group_id": groups[a]["group_id"],
            "right_group_id": groups[b]["group_id"],
            "left_surface": groups[a]["preferred_surface"],
            "right_surface": groups[b]["preferred_surface"],
            "left_normalized": a,
            "right_normalized": b,
            "left_occurrences": groups[a]["occurrence_count"],
            "right_occurrences": groups[b]["occurrence_count"],
            "lexical_score": score,
            "blocking_reasons": sorted(reasons),
            "type_compatibility": type_status,
            "matching_status": "NOT_EVALUATED",
            "identity_approved": False,
            "graph_writes": 0,
        }

        neighbors[a].append(candidate)
        neighbors[b].append(candidate)

    # Bound each group's shortlist; retain the union
    # so a useful one-sided match is not discarded.
    selected = {}

    for key, candidates in sorted(neighbors.items()):
        ranked = sorted(
            candidates,
            key=lambda row: (
                -int("legal_form" in row["blocking_reasons"]),
                -int("acronym" in row["blocking_reasons"]),
                -row["lexical_score"],
                row["pair_id"],
            ),
        )

        for candidate in ranked[:max_candidates]:
            selected[candidate["pair_id"]] = candidate

    return (
        sorted(
            selected.values(),
            key=lambda row: row["pair_id"],
        ),
        dict(sorted(skipped_blocks.items())),
    )


def run(args):
    summary, groups, counts = load_groups(
        args.source_dir
    )

    pairs, skipped = generate_pairs(
        groups,
        max_block=args.max_block,
        max_candidates=args.max_candidates,
        min_score=args.min_score,
    )

    output = Path(args.out_dir)

    require(
        not output.exists(),
        "Output exists; use a new directory",
    )

    output.mkdir(parents=True)

    group_path = output / "surface_groups.jsonl"
    pair_path = output / "candidate_pairs.jsonl"

    write_jsonl(
        group_path,
        [groups[key] for key in sorted(groups)],
    )

    write_jsonl(pair_path, pairs)

    reasons = Counter()
    type_counts = Counter()

    for pair in pairs:
        type_counts[pair["type_compatibility"]] += 1
        for reason in pair["blocking_reasons"]:
            reasons[reason] += 1

    report = {
        "version": VERSION,
        "source_version": SOURCE_VERSION,
        "source_batch_key": summary["source_batch_key"],
        "source_mention_sha256":
            summary["mention_profiles_sha256"],
        "source_component_sha256":
            summary["component_profiles_sha256"],

        "original_mentions":
            summary["mention_profiles"],
        "original_components":
            summary["component_profiles"],

        "eligible_profiles": counts["eligible_profiles"],
        "excluded_profiles": counts["excluded_profiles"],

        "surface_groups": len(groups),
        "candidate_pairs": len(pairs),

        "blocking_reason_counts":
            dict(sorted(reasons.items())),
        "type_compatibility_counts":
            dict(sorted(type_counts.items())),

        "skipped_oversized_blocks": skipped,

        "max_block": args.max_block,
        "max_candidates": args.max_candidates,
        "min_score": args.min_score,

        "infrastructure_scope_policy":
            "ADVISORY_ONLY",

        "surface_groups_sha256":
            sha256_file(group_path),
        "candidate_pairs_sha256":
            sha256_file(pair_path),

        "llm_calls": 0,
        "identity_merges": 0,
        "approved_pairs": 0,
        "graph_writes": 0,
    }

    (output / "summary.json").write_text(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )

    print(
        json.dumps(
            report,
            indent=2,
            ensure_ascii=False,
        )
    )


def main():
    parser = argparse.ArgumentParser(
        description=__doc__
    )

    parser.add_argument(
        "--source-dir",
        default="output/resolution/identity-profiles-v1",
    )
    parser.add_argument(
        "--out-dir",
        default="output/resolution/candidate-generation-v1",
    )
    parser.add_argument(
        "--max-block",
        type=int,
        default=100,
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=25,
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=0.80,
    )

    run(parser.parse_args())


if __name__ == "__main__":
    main()
