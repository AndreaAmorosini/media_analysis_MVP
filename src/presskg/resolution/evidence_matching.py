
"""PressKG Evidence-based Matching v1.

Pair-level assessment over candidate surface groups.

MATCH_SUPPORTED requires a directly attested alias expression.
Lexical similarity, acronym initials and shared attributes are
never sufficient on their own.

No LLM calls. No graph writes. No identity merges.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata

from collections import Counter, defaultdict
from pathlib import Path


VERSION = "evidence-matching-v1"
CANDIDATE_VERSION = "candidate-generation-v1"
PROFILE_VERSION = "identity-profiles-v1"
RELATION_VERSION = "relation-type-scope-v1"

PERSON_KINDS = {"Person"}
ORG_KINDS = {
    "Company", "Organization", "PublicAuthority",
    "NonProfitOrganization", "PoliticalParty",
    "LaborUnion", "MediaOutlet",
}

LEGAL_SUFFIX = re.compile(
    r"(?:\s+(?:s\s*p\s*a|s\s*r\s*l|"
    r"s\s*n\s*c|s\s*a\s*s|"
    r"s\s*c\s*a\s*r\s*l|"
    r"societa\s+per\s+azioni|"
    r"societa\s+a\s+responsabilita\s+limitata))+$"
)

QUANTITY = re.compile(r"\b\d+(?:[.,]\d+)?\b")

# Distinguish an institution from the person holding office.
ROLE_PATTERNS = (
    (r"\bministero\b", r"\bministr[oa]\b"),
    (r"\bpresidenza\b", r"\bpresidente\b"),
    (r"\bsindaco\b", r"\bcomune\b"),
)

# Only explicit naming constructions can support an alias.
ALIAS_CUES = (
    r"(?:noto|nota|conosciuto|conosciuta)\s+anche\s+come",
    r"(?:denominato|denominata|chiamato|chiamata)\s+anche",
    r"alias",
    r"ovvero",
)

STOPWORDS = {
    "di", "del", "della", "delle", "dei", "degli",
    "da", "a", "in", "per", "con", "e", "ed",
    "il", "la", "lo", "le", "i", "gli", "un",
    "una", "the", "of", "and",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_jsonl(path):
    with Path(path).open(encoding="utf-8") as handle:
        for index, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{index}: {exc}") from exc


def write_jsonl(path, records):
    with Path(path).open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                ) + "\n"
            )


def normalize(value):
    value = unicodedata.normalize(
        "NFKD", str(value or "")
    ).casefold()
    value = "".join(
        c for c in value
        if not unicodedata.combining(c)
    )
    return " ".join(
        re.sub(r"[^\w]+", " ", value).split()
    )


def legal_base(value):
    return LEGAL_SUFFIX.sub("", normalize(value)).strip()


def significant_tokens(value):
    return [
        t for t in normalize(value).split()
        if t not in STOPWORDS and len(t) >= 2
    ]


def acronym(value):
    tokens = [
        t for t in significant_tokens(value)
        if len(t) >= 3
    ]
    if not 2 <= len(tokens) <= 8:
        return None
    return "".join(t[0] for t in tokens)


def dominant_type(group):
    """Dominant family and fraction of annotated occurrences."""

    counts = group["kind_counts"]
    total = sum(counts.values())

    families = Counter()

    for kind, count in counts.items():
        if kind == "Person":
            family = "PERSON"
        elif kind in ORG_KINDS:
            family = "ORGANIZATION"
        elif kind in {
            "GPE", "Location", "LOC",
            "GeographicalArea", "Address",
        }:
            family = "GEOGRAPHY"
        elif kind in {"Facility", "Infrastructure"}:
            family = "PHYSICAL"
        elif kind in {"Project", "Event"}:
            family = kind.upper()
        elif kind in {
            "Money", "Quantity", "Date", "Time",
        }:
            family = "NON_IDENTITY"
        else:
            family = "OTHER"

        families[family] += count

    if not families or total == 0:
        return {"family": "UNKNOWN", "ratio": 0.0}

    family, count = sorted(
        families.items(),
        key=lambda item: (-item[1], item[0]),
    )[0]

    return {
        "family": family,
        "ratio": round(count / total, 5),
    }


def lexical_conflicts(left, right):
    """Detect a few high-precision contradictions.

    These are conservative negative signals, not a
    complete identity contradiction ontology.
    """

    a = normalize(left)
    b = normalize(right)
    reasons = []

    for first, second in ROLE_PATTERNS:
        if (
            re.search(first, a)
            and re.search(second, b)
        ) or (
            re.search(second, a)
            and re.search(first, b)
        ):
            reasons.append("institution_vs_office_holder")

    nums_a = QUANTITY.findall(a)
    nums_b = QUANTITY.findall(b)

    if (
        nums_a and nums_b
        and nums_a != nums_b
        and (
            len(significant_tokens(a)) >= 2
            and len(significant_tokens(b)) >= 2
        )
    ):
        ta = {
            t for t in significant_tokens(a)
            if not t.isdigit()
        }
        tb = {
            t for t in significant_tokens(b)
            if not t.isdigit()
        }
        if ta and tb and len(ta & tb) / len(ta | tb) >= 0.6:
            reasons.append("different_numeric_designators")

    return sorted(set(reasons))


def quote_snippets(profile):
    """Only original retained evidence, never invented text."""

    provenance = profile.get("provenance") or {}

    result = []
    seen = set()

    for field in ("supporting_span", "original_quote"):
        value = provenance.get(field)

        if not isinstance(value, str):
            continue

        value = value.strip()
        if not value or value in seen:
            continue

        seen.add(value)
        result.append({
            "field": field,
            "quote": value,
        })

    return result



def explicit_alias(quote, left, right):
    """Accept explicit alias constructions only.

    Parentheses are preserved when matching the source quote.
    Returned evidence is a naming cue, not a merge approval.
    """

    if not all(
        isinstance(x, str) and x.strip()
        for x in (quote, left, right)
    ):
        return None

    # Keep parentheses and lexical punctuation required
    # by the explicit naming construction.
    raw = unicodedata.normalize("NFKC", quote).casefold()
    raw = re.sub(r"\s+", " ", raw)

    def surface_regex(value):
        tokens = normalize(value).split()
        if not tokens:
            return None

        return r"[\W_]*".join(
            re.escape(token)
            for token in tokens
        )

    for long_name, short_name in (
        (left, right),
        (right, left),
    ):
        short = normalize(short_name).replace(" ", "")
        long_acronym = acronym(long_name)

        if (
            long_acronym != short
            or len(short) > 10
        ):
            continue

        full_pattern = surface_regex(long_name)
        short_pattern = surface_regex(short_name)

        for pattern in (
            (
                rf"(?<!\w){full_pattern}"
                rf"\s*\(\s*{short_pattern}\s*\)"
            ),
            (
                rf"(?<!\w){short_pattern}"
                rf"\s*\(\s*{full_pattern}\s*\)"
            ),
        ):
            if re.search(pattern, raw, re.IGNORECASE):
                return "explicit_parenthetical_acronym"

    # For other naming constructions, normalized text is
    # sufficient, but an explicit lexical cue is mandatory.
    q = normalize(quote)
    a = normalize(left)
    b = normalize(right)

    for first, second in ((a, b), (b, a)):
        pattern = (
            r"(?<!\w)"
            + re.escape(first)
            + r"(?!\w)(.{1,65}?)"
            + re.escape(second)
            + r"(?!\w)"
        )

        for match in re.finditer(pattern, q):
            between = match.group(1)

            if any(
                re.search(cue, between)
                for cue in ALIAS_CUES
                if cue != "ovvero"
            ):
                return "explicit_naming_cue"

    return None


def load_groups_and_pairs(directory):
    directory = Path(directory)
    summary = read_json(directory / "summary.json")

    require(
        summary["version"] == CANDIDATE_VERSION,
        "Unexpected Candidate Generation version",
    )
    require(
        summary["graph_writes"] == 0
        and summary["approved_pairs"] == 0,
        "Candidate source has approved merges",
    )

    groups_path = directory / "surface_groups.jsonl"
    pairs_path = directory / "candidate_pairs.jsonl"

    require(
        sha256_file(groups_path)
        == summary["surface_groups_sha256"],
        "Group checksum mismatch",
    )
    require(
        sha256_file(pairs_path)
        == summary["candidate_pairs_sha256"],
        "Pair checksum mismatch",
    )

    groups = {}
    occurrences = {}

    for group in read_jsonl(groups_path):
        gid = group["group_id"]
        require(gid not in groups, "Repeated group ID")
        require(
            group["identity_approved"] is False,
            "Already-approved group",
        )
        require(
            len(group["occurrence_ids"])
            == group["occurrence_count"],
            "Group occurrence count mismatch",
        )

        groups[gid] = group

        for pid in group["occurrence_ids"]:
            require(
                pid not in occurrences,
                f"Occurrence assigned twice: {pid}",
            )
            occurrences[pid] = gid

    pairs = list(read_jsonl(pairs_path))

    require(
        len(groups) == summary["surface_groups"],
        "Group coverage mismatch",
    )
    require(
        len(pairs) == summary["candidate_pairs"],
        "Pair coverage mismatch",
    )

    seen_pairs = set()

    for pair in pairs:
        require(
            pair["pair_id"] not in seen_pairs,
            "Repeated pair ID",
        )
        seen_pairs.add(pair["pair_id"])

        require(
            pair["left_group_id"] in groups
            and pair["right_group_id"] in groups,
            "Pair refers to missing group",
        )
        require(
            pair["identity_approved"] is False,
            "Pair already approved",
        )

    return summary, groups, pairs, occurrences


def load_profile_evidence(directory, expected_summary, occurrences):
    directory = Path(directory)
    summary = read_json(directory / "summary.json")

    require(
        summary["version"] == PROFILE_VERSION,
        "Unexpected profile version",
    )

    for name, sha_key, expected_sha in (
        (
            "mention_profiles.jsonl",
            "mention_profiles_sha256",
            expected_summary["source_mention_sha256"],
        ),
        (
            "component_profiles.jsonl",
            "component_profiles_sha256",
            expected_summary["source_component_sha256"],
        ),
    ):
        path = directory / name

        require(
            summary[sha_key] == expected_sha
            and sha256_file(path) == expected_sha,
            "Profile checksum or lineage mismatch",
        )

    evidence = defaultdict(lambda: defaultdict(list))
    seen = set()

    for name in (
        "mention_profiles.jsonl",
        "component_profiles.jsonl",
    ):
        for profile in read_jsonl(directory / name):
            pid = profile["profile_id"]

            if pid not in occurrences:
                continue

            require(pid not in seen, "Duplicate occurrence")
            seen.add(pid)

            group_id = occurrences[pid]
            article_id = profile["article_id"]

            for item in quote_snippets(profile):
                bucket = evidence[group_id][article_id]

                if len(bucket) >= 4:
                    break

                if any(
                    x["quote"] == item["quote"]
                    for x in bucket
                ):
                    continue

                bucket.append({
                    **item,
                    "profile_id": pid,
                    "fact_id": profile["fact_id"],
                    "article_id": article_id,
                })

    require(
        len(seen) == len(occurrences),
        "Some group occurrences lack profiles",
    )

    return evidence


def load_attribute_support(directory, expected_batch, occurrences):
    """Load only previously admitted *soft* attribute hints."""

    if directory is None:
        return {}

    directory = Path(directory)
    summary = read_json(directory / "summary.json")

    require(
        summary["version"] == RELATION_VERSION,
        "Unexpected relation validation version",
    )
    require(
        summary["source_batch_key"] == expected_batch,
        "Relation/profiles batch mismatch",
    )

    path = directory / "assessed_relations.jsonl"

    require(
        sha256_file(path)
        == summary["assessed_relations_sha256"],
        "Relation assessment checksum mismatch",
    )

    attributes = defaultdict(set)
    count = 0

    for record in read_jsonl(path):
        count += 1

        if record["matching_disposition"] != "SUPPORTING_CANDIDATE":
            continue

        gid = occurrences.get(
            record["subject_profile_id"]
        )
        if gid is None:
            continue

        signature = (
            record["predicate"],
            normalize(record["object_surface"]),
        )
        attributes[gid].add(signature)

    require(
        count == summary["processed_relations"],
        "Relation assessment count mismatch",
    )

    return attributes


def alias_evidence(left, right, evidence, max_articles=30):
    """Check shared article quotes for explicit naming.

    The same article alone is not alias evidence.
    """

    lid = left["group_id"]
    rid = right["group_id"]

    shared = sorted(
        set(evidence.get(lid, {}))
        & set(evidence.get(rid, {}))
    )

    found = []

    for article_id in shared[:max_articles]:
        snippets = (
            evidence[lid][article_id]
            + evidence[rid][article_id]
        )

        for item in snippets:
            rule = explicit_alias(
                item["quote"],
                left["preferred_surface"],
                right["preferred_surface"],
            )

            if rule is None:
                continue

            found.append({
                "rule": rule,
                "article_id": article_id,
                "fact_id": item["fact_id"],
                "profile_id": item["profile_id"],
                "quote_field": item["field"],
                "quote": item["quote"][:1200],
            })
            break

        if len(found) >= 3:
            break

    return {
        "shared_article_count": len(shared),
        "articles_inspected": min(
            len(shared), max_articles
        ),
        "alias_attestations": found,
    }


def evaluate_pair(pair, left, right, evidence, attributes):
    conflicts = lexical_conflicts(
        left["preferred_surface"],
        right["preferred_surface"],
    )

    left_type = dominant_type(left)
    right_type = dominant_type(right)

    stable_types = (
        left_type["ratio"] >= 0.80
        and right_type["ratio"] >= 0.80
        and left_type["family"] != "OTHER"
        and right_type["family"] != "OTHER"
    )

    if (
        stable_types
        and left_type["family"] != right_type["family"]
    ):
        conflicts.append("dominant_type_family_conflict")

    conflicts = sorted(set(conflicts))

    alias = alias_evidence(left, right, evidence)

    shared_attributes = sorted(
        attributes.get(left["group_id"], set())
        & attributes.get(right["group_id"], set())
    )

    base_equal = (
        legal_base(left["preferred_surface"])
        == legal_base(right["preferred_surface"])
    )

    if conflicts:
        verdict = "CONFLICT"

    elif alias["alias_attestations"] and stable_types:
        verdict = "MATCH_SUPPORTED"

    elif (
        alias["alias_attestations"]
        or base_equal
        or (
            pair["lexical_score"] >= 0.94
            and pair["type_compatibility"] == "OVERLAPPING"
        )
        or shared_attributes
    ):
        verdict = "POSSIBLE_MATCH"

    else:
        verdict = "INSUFFICIENT_EVIDENCE"

    return {
        "pair_id": pair["pair_id"],
        "left_group_id": left["group_id"],
        "right_group_id": right["group_id"],
        "left_surface": left["preferred_surface"],
        "right_surface": right["preferred_surface"],

        "candidate_lexical_score":
            pair["lexical_score"],
        "blocking_reasons":
            pair["blocking_reasons"],

        "left_type_profile": left_type,
        "right_type_profile": right_type,
        "types_stable": stable_types,

        "legal_base_equal": base_equal,
        "alias_evidence": alias,
        "shared_attribute_signatures": [
            {
                "predicate": predicate,
                "object_normalized": object_name,
            }
            for predicate, object_name in shared_attributes[:10]
        ],

        "conflicts": conflicts,
        "verdict": verdict,

        "alias_semantically_verified": bool(
            alias["alias_attestations"]
        ),
        "identity_merge_approved": False,
        "canonical_entity_id": None,
        "graph_writes": 0,
    }


def run(args):
    (
        source_summary,
        groups,
        pairs,
        occurrences,
    ) = load_groups_and_pairs(
        args.candidates_dir
    )

    evidence = load_profile_evidence(
        args.profiles_dir,
        source_summary,
        occurrences,
    )

    attributes = load_attribute_support(
        args.relations_dir,
        source_summary["source_batch_key"],
        occurrences,
    )

    out = Path(args.out_dir)
    require(
        not out.exists(),
        "Output directory already exists",
    )
    out.mkdir(parents=True)

    output = out / "pair_decisions.jsonl"
    review = out / "review_queue.jsonl"

    verdicts = Counter()
    conflict_counts = Counter()
    alias_counts = Counter()
    shared_articles = 0

    with (
        output.open("w", encoding="utf-8") as destination,
        review.open("w", encoding="utf-8") as review_file,
    ):
        for pair in pairs:
            result = evaluate_pair(
                pair,
                groups[pair["left_group_id"]],
                groups[pair["right_group_id"]],
                evidence,
                attributes,
            )

            line = json.dumps(
                result,
                ensure_ascii=False,
                sort_keys=True,
            ) + "\n"

            destination.write(line)
            verdicts[result["verdict"]] += 1

            if result["alias_evidence"]["shared_article_count"]:
                shared_articles += 1

            for reason in result["conflicts"]:
                conflict_counts[reason] += 1

            for item in result["alias_evidence"]["alias_attestations"]:
                alias_counts[item["rule"]] += 1

            if result["verdict"] != "MATCH_SUPPORTED":
                review_file.write(line)

    require(
        sum(verdicts.values()) == len(pairs),
        "Incomplete pair evaluation",
    )

    report = {
        "version": VERSION,
        "source_version": CANDIDATE_VERSION,
        "source_batch_key":
            source_summary["source_batch_key"],
        "source_candidate_sha256":
            source_summary["candidate_pairs_sha256"],
        "source_groups_sha256":
            source_summary["surface_groups_sha256"],

        "processed_pairs": len(pairs),
        "verdict_counts": dict(sorted(verdicts.items())),
        "conflict_reason_counts":
            dict(sorted(conflict_counts.items())),
        "alias_evidence_counts":
            dict(sorted(alias_counts.items())),

        "pairs_with_shared_articles": shared_articles,
        "review_queue_count":
            len(pairs) - verdicts["MATCH_SUPPORTED"],

        "pair_decisions_sha256":
            sha256_file(output),
        "review_queue_sha256":
            sha256_file(review),

        "infrastructure_scope_policy": "ADVISORY_ONLY",
        "llm_calls": 0,
        "identity_merges": 0,
        "approved_cluster_edges": 0,
        "graph_writes": 0,
    }

    (out / "summary.json").write_text(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )

    print(json.dumps(
        report,
        ensure_ascii=False,
        indent=2,
    ))


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
        "--relations-dir",
        default="output/resolution/relation-type-scope-v1",
    )
    parser.add_argument(
        "--out-dir",
        default="output/resolution/evidence-matching-v1",
    )

    run(parser.parse_args())


if __name__ == "__main__":
    main()
