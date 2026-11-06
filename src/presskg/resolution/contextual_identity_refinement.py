
"""Generic contextual identity refinement for PressKG.

Generic organization-name reconciliation:
- legal-form suffixes
- leading articles
- OCR-joined articles and legal suffixes
- exact semantic-family compatibility

Generic person-reference reconciliation:
- full-name / surname suffix compatibility
- occurrence-level relation evidence
- uniqueness among competing full-name identities

All assignments remain provisional.
No hardcoded entity names, LLM calls, or Neo4j writes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata

from collections import Counter, defaultdict
from pathlib import Path


VERSION = "contextual-identity-refinement-v2"

LEGAL_FORMS = {
    ("spa",),
    ("s", "p", "a"),
    ("srl",),
    ("s", "r", "l"),
    ("snc",),
    ("s", "n", "c"),
    ("sas",),
    ("s", "a", "s"),
    ("scarl",),
    ("s", "c", "a", "r", "l"),
}

JOINED_LEGAL_FORMS = (
    "scarl", "spa", "srl", "snc", "sas",
)

ARTICLES = {
    "il", "lo", "la", "l",
    "i", "gli", "le",
}

PERSON_PARTICLES = {
    "de", "di", "del", "della",
    "da", "van", "von",
}

GENERIC_PREDICATES = {
    "OTHER",
    "MENTIONS",
    "RELATED_TO",
    "ASSOCIATED_WITH",
}

# Semantic categories, not names of real entities.
ORGANIZATION_KINDS = {
    "Company",
    "Organization",
    "PublicAuthority",
    "NonProfitOrganization",
    "PoliticalParty",
    "LaborUnion",
    "MediaOutlet",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


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


def sha256(path):
    h = hashlib.sha256()

    with Path(path).open("rb") as f:
        for chunk in iter(
            lambda: f.read(1024 * 1024), b""
        ):
            h.update(chunk)

    return h.hexdigest()


def read_json(path):
    return json.loads(
        Path(path).read_text(encoding="utf-8")
    )


def read_jsonl(path):
    with Path(path).open(encoding="utf-8") as f:
        for number, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path}:{number}: {exc}"
                ) from exc


def write_jsonl(path, records):
    with Path(path).open(
        "w", encoding="utf-8"
    ) as f:
        for record in records:
            f.write(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                ) + "\n"
            )


# ============================================================
# Generic organization-name normalization
# ============================================================

def strip_legal_suffix(tokens):
    tokens = list(tokens)

    while tokens:
        match = next(
            (
                form for form in LEGAL_FORMS
                if len(tokens) >= len(form)
                and tuple(tokens[-len(form):]) == form
            ),
            None,
        )

        if match is None:
            break

        tokens = tokens[:-len(match)]

    return tokens


def organization_base(value):
    """Normalize only explicit, separable name variants.

    This does not remove generic organization words, locations,
    project names, or arbitrary prefixes.
    """

    tokens = normalize(value).split()

    if not tokens:
        return ""

    tokens = strip_legal_suffix(tokens)

    if len(tokens) > 1 and tokens[0] in ARTICLES:
        tokens = tokens[1:]

    tokens = strip_legal_suffix(tokens)

    if not tokens:
        return ""

    # Joined legal suffix: AcmeSpa -> acme.
    # Keep minimum length to avoid very short accidental bases.
    if len(tokens) == 1:
        word = tokens[0]

        for suffix in JOINED_LEGAL_FORMS:
            if (
                word.endswith(suffix)
                and len(word) - len(suffix) >= 3
            ):
                tokens = [word[:-len(suffix)]]
                break

    return " ".join(tokens)


# def organization_base_candidates(value, known_bases):
#     """Return candidate bases, including tightly bounded OCR fixes.

#     Joined articles are removed only when the remaining name
#     already exists as an independently observed organization base.
#     """

#     primary = organization_base(value)
#     if not primary:
#         return []

#     candidates = []

#     if primary in known_bases:
#         candidates.append((
#             primary,
#             "NORMALIZED_ORGANIZATION_NAME",
#         ))

#     # OCR: 'laGori' -> 'la' + 'Gori'.
#     # Never infer a new identity from substring similarity alone.
#     if " " not in primary:
#         for article in sorted(ARTICLES, key=len, reverse=True):
#             if not primary.startswith(article):
#                 continue

#             remainder = primary[len(article):]

#             if (
#                 len(remainder) >= 3
#                 and remainder in known_bases
#             ):
#                 candidates.append((
#                     remainder,
#                     "OCR_JOINED_ARTICLE",
#                 ))

#     unique = {}

#     for base, rule in candidates:
#         # Prefer a recognized OCR correction over treating the
#         # joined spelling as an unrelated standalone root.
#         priority = (
#             0 if rule == "OCR_JOINED_ARTICLE" else 1
#         )

#         current = unique.get(base)

#         if current is None or priority < current[0]:
#             unique[base] = (priority, rule)

#     return [
#         (base, item[1])
#         for base, item in sorted(
#             unique.items(),
#             key=lambda pair: (
#                 pair[1][0],
#                 pair[0],
#             ),
#         )
#     ]


def organization_base_candidates(value, known_bases):
    """Generic candidate bases, with conditional OCR recovery.

    Joined-article stripping takes precedence only when it
    produces an independently observed organization base.
    No entity-specific names or fixed alias tables.
    """

    primary = organization_base(value)

    if not primary:
        return []

    ocr_candidates = set()

    if " " not in primary:
        for article in ARTICLES:
            if not primary.startswith(article):
                continue

            remainder = primary[len(article):]

            if (
                len(remainder) >= 3
                and remainder in known_bases
            ):
                ocr_candidates.add(remainder)

    if len(ocr_candidates) == 1:
        return [
            (
                next(iter(ocr_candidates)),
                "OCR_JOINED_ARTICLE",
            )
        ]

    if len(ocr_candidates) > 1:
        # Multiple plausible corrections: do not guess.
        return []

    if primary in known_bases:
        return [
            (
                primary,
                "NORMALIZED_ORGANIZATION_NAME",
            )
        ]

    return []



# ============================================================
# Generic person-name and relation evidence
# ============================================================

def name_tokens(value):
    return tuple(normalize(value).split())


def is_full_person_name(value):
    tokens = name_tokens(value)

    if len(tokens) < 2:
        return False

    if (
        len(tokens) == 2
        and tokens[0] in PERSON_PARTICLES
    ):
        return False

    return True


def compatible_short_name(short_name, full_name):
    short = name_tokens(short_name)
    full = name_tokens(full_name)

    return (
        bool(short)
        and len(full) > len(short)
        and full[-len(short):] == short
    )


def relation_signature(profile):
    """Use only validated fact relations.

    The fact role is included to prevent matching subjects
    and objects with inverted relational positions.
    """

    ctx = profile.get("fact_context") or {}

    predicate = str(
        ctx.get("predicate") or ""
    ).upper().strip()

    counterpart = normalize(
        ctx.get("counterpart_name")
    )

    role = str(
        profile.get("role") or ""
    ).lower()

    if (
        not predicate
        or predicate in GENERIC_PREDICATES
        or len(counterpart) < 3
        or counterpart.isdigit()
        or role not in {"subject", "object"}
    ):
        return None

    return (role, predicate, counterpart)


def evidence_reference(profile):
    return {
        "profile_id": profile["profile_id"],
        "fact_id": profile["fact_id"],
        "article_id": profile["article_id"],
    }


# ============================================================
# Input loading
# ============================================================

def load_inputs(profiles_dir, assignments_dir):
    p = Path(profiles_dir)
    a = Path(assignments_dir)

    ps = read_json(p / "summary.json")
    ass = read_json(a / "summary.json")

    require(
        ps["version"] == "identity-profiles-v1",
        "Unexpected profile version",
    )

    require(
        ass["version"] == "occurrence-assignment-v1",
        "Unexpected assignment version",
    )

    require(
        ps["source_batch_key"]
        == ass["source_batch_key"],
        "Source batch mismatch",
    )

    require(
        ps["graph_writes"] == 0
        and ass["graph_writes"] == 0
        and ass["identity_merges"] == 0,
        "Input contains graph mutations",
    )

    files = [
        (
            p / "mention_profiles.jsonl",
            ps["mention_profiles_sha256"],
        ),
        (
            p / "component_profiles.jsonl",
            ps["component_profiles_sha256"],
        ),
        (
            a / "occurrence_assignments.jsonl",
            ass["checksums"]["occurrence_assignments.jsonl"],
        ),
    ]

    for path, expected in files:
        require(
            sha256(path) == expected,
            f"Checksum mismatch: {path}",
        )

    profiles = {}

    for filename in (
        "mention_profiles.jsonl",
        "component_profiles.jsonl",
    ):
        for profile in read_jsonl(p / filename):
            pid = profile["profile_id"]

            require(
                pid not in profiles
                and profile["canonical_id"] is None
                and profile["merge_approved"] is False,
                f"Invalid profile: {pid}",
            )

            profiles[pid] = profile

    require(
        len(profiles)
        == ps["mention_profiles"]
        + ps["component_profiles"],
        "Incomplete profile inventory",
    )

    assignments = {}

    for row in read_jsonl(
        a / "occurrence_assignments.jsonl"
    ):
        pid = row["profile_id"]

        require(
            pid in profiles and pid not in assignments,
            f"Invalid assignment: {pid}",
        )

        require(
            row["canonical_entity_id"] is None
            and row["identity_assignment_approved"] is False,
            "An input assignment is already approved",
        )

        assignments[pid] = row

    require(
        set(profiles) == set(assignments),
        "Profile/assignment coverage mismatch",
    )

    return ps, ass, profiles, assignments


# ============================================================
# Organization index
# ============================================================

def build_organization_index(profiles, assignments):
    """Build canonical candidate anchors from observed identities.

    A candidate base must be independently represented in the
    corpus. No application-specific alias dictionary is used.
    """

    base_identities = defaultdict(
        lambda: defaultdict(list)
    )

    for pid, profile in profiles.items():
        row = assignments[pid]

        if (
            row["assignment_status"] != "PROVISIONAL"
            or row["occurrence_semantic_facet"]
            != "ORGANIZATION"
            or profile["source_kind"]
            not in ORGANIZATION_KINDS
        ):
            continue

        identity = row["proposed_identity_id"]

        if not identity:
            continue

        base = organization_base(profile["surface"])

        if base and len(base) >= 3:
            base_identities[base][identity].append(pid)

    # Require at least one observed variant of each base.
    # Choose the identity represented by its most direct spelling,
    # rather than choosing from arbitrary lexical similarities.
    anchors = {}

    for base, identities in base_identities.items():
        ranked = []

        for identity, pids in identities.items():
            exact = sum(
                normalize(profiles[pid]["surface"]) == base
                for pid in pids
            )

            ranked.append((
                -int(exact > 0),
                -exact,
                -len(pids),
                identity,
            ))

        ranked.sort()

        # If two candidates are equally supported, do not
        # manufacture a unique anchor.
        if (
            len(ranked) > 1
            and ranked[0][:3] == ranked[1][:3]
        ):
            continue

        anchors[base] = ranked[0][3]

    return anchors


def resolve_organization(profile, previous, anchors):
    if (
        previous["assignment_status"] != "PROVISIONAL"
        or previous["occurrence_semantic_facet"]
        != "ORGANIZATION"
        or profile["source_kind"]
        not in ORGANIZATION_KINDS
    ):
        return None

    candidates = organization_base_candidates(
        profile["surface"],
        set(anchors),
    )

    matches = {
        (base, anchors[base], rule)
        for base, rule in candidates
    }

    identities = {
        identity for _, identity, _ in matches
    }

    if len(identities) != 1:
        return None

    base, identity, rule = sorted(
        matches,
        key=lambda item: (
            item[2] != "OCR_JOINED_ARTICLE",
            item[0],
        ),
    )[0]

    if identity == previous["proposed_identity_id"]:
        return None

    return {
        "identity_id": identity,
        "rule": (
            "ORGANIZATION_" + rule
        ),
        "evidence": [{
            "name_base": base,
            "surface": profile["surface"],
            "evidence_class": "LEXICAL_NORMALIZATION",
        }],
    }


# ============================================================
# Person index
# ============================================================

def build_person_index(profiles, assignments):
    """Index full-name identities and their relation evidence."""

    full_names = defaultdict(set)
    evidence = defaultdict(
        lambda: defaultdict(list)
    )

    for pid, profile in profiles.items():
        row = assignments[pid]

        if (
            row["assignment_status"] != "PROVISIONAL"
            or row["occurrence_semantic_facet"] != "PERSON"
            or profile["source_kind"] != "Person"
            or not is_full_person_name(profile["surface"])
        ):
            continue

        identity = row["proposed_identity_id"]
        name = normalize(profile["surface"])

        full_names[name].add(identity)

        signature = relation_signature(profile)

        if signature:
            evidence[identity][signature].append(
                evidence_reference(profile)
            )

    # Only unambiguous full-name identity proposals.
    full_index = {
        name: next(iter(ids))
        for name, ids in full_names.items()
        if len(ids) == 1
    }

    return full_index, evidence


def build_article_person_relations(profiles):
    """Keep short-name evidence local to the source article."""

    index = defaultdict(
        lambda: defaultdict(
            lambda: defaultdict(list)
        )
    )

    for profile in profiles.values():
        if profile["source_kind"] != "Person":
            continue

        signature = relation_signature(profile)

        if signature is None:
            continue

        index[
            profile["article_id"]
        ][normalize(profile["surface"])][
            signature
        ].append(evidence_reference(profile))

    return index


def resolve_person(
    profile,
    full_index,
    person_evidence,
    article_relations,
):
    """Resolve only a supported occurrence of an abbreviated name.

    The current occurrence must have its own relation signature.
    Other occurrences of the same surname are not evidence
    for the current occurrence.

    Same article: one identical relation signature.
    Cross article: two distinct relation signatures in the
    current article, with different counterpart names, also
    supported by the candidate full-name identity.

    In all cases the compatible full-name identity must
    be unique, including among unsupported competitors.
    """

    short_name = normalize(profile["surface"])

    compatible = {
        identity
        for full_name, identity in full_index.items()
        if compatible_short_name(short_name, full_name)
    }

    if len(compatible) != 1:
        return None

    identity = next(iter(compatible))

    own_signature = relation_signature(profile)

    if own_signature is None:
        return None

    full_evidence = person_evidence.get(
        identity, {}
    )

    if own_signature not in full_evidence:
        return None

    article_id = profile["article_id"]

    full_same_article = [
        reference
        for reference in full_evidence[own_signature]
        if reference["article_id"] == article_id
    ]

    if full_same_article:
        return {
            "identity_id": identity,
            "rule": "PERSON_SAME_ARTICLE_SHARED_RELATION",
            "evidence": [
                evidence_reference(profile),
                full_same_article[0],
            ],
        }

    # The current short-name reference must be corroborated
    # by another distinct relation in its own article.
    current_article = article_relations.get(
        article_id, {}
    ).get(short_name, {})

    shared = (
        set(current_article)
        & set(full_evidence)
    )

    counterparts = {
        signature[2]
        for signature in shared
    }

    if (
        own_signature not in shared
        or len(shared) < 2
        or len(counterparts) < 2
    ):
        return None

    # Require support from at least two source articles
    # for the full-name candidate, not just one event.
    full_articles = {
        reference["article_id"]
        for signature in shared
        for reference in full_evidence[signature]
    }

    if len(full_articles) < 2:
        return None

    refs = [evidence_reference(profile)]

    for signature in sorted(shared):
        refs.append(
            full_evidence[signature][0]
        )

    return {
        "identity_id": identity,
        "rule": "PERSON_CROSS_ARTICLE_MULTI_RELATION",
        "evidence": refs,
    }


# ============================================================
# Refinement
# ============================================================

def refine(
    profile,
    previous,
    organization_anchors,
    full_person_index,
    person_evidence,
    article_relations,
):
    row = dict(previous)

    row["previous_assignment_status"] = (
        previous["assignment_status"]
    )
    row["previous_identity_id"] = (
        previous["proposed_identity_id"]
    )
    row["refinement_rule"] = "UNCHANGED"
    row["refinement_evidence"] = []

    organization = resolve_organization(
        profile,
        previous,
        organization_anchors,
    )

    if organization:
        row["proposed_identity_id"] = (
            organization["identity_id"]
        )
        row["refinement_rule"] = organization["rule"]
        row["refinement_evidence"] = organization["evidence"]
        return row

    # Only remove the ambiguity that we have actually resolved.
    # Never override independent blockers or OUT_OF_SCOPE cases.
    if (
        profile["source_kind"] == "Person"
        and previous["assignment_status"] == "HOLD_FOR_CONTEXT"
        and set(previous["blockers"])
        == {"short_person_name_ambiguous"}
    ):
        person = resolve_person(
            profile,
            full_person_index,
            person_evidence,
            article_relations,
        )

        if person:
            row["assignment_status"] = "PROVISIONAL"
            row["proposed_identity_id"] = person["identity_id"]
            row["blockers"] = []
            row["refinement_rule"] = person["rule"]
            row["refinement_evidence"] = person["evidence"]

    # These are proposal changes only.
    row["canonical_entity_id"] = None
    row["identity_assignment_approved"] = False
    row["fact_propagation_allowed"] = False

    return row


# ============================================================
# Main pipeline
# ============================================================

def run(args):
    ps, ass, profiles, assignments = load_inputs(
        args.profiles_dir,
        args.assignments_dir,
    )

    organization_anchors = build_organization_index(
        profiles, assignments
    )

    full_person_index, person_evidence = (
        build_person_index(profiles, assignments)
    )

    article_relations = build_article_person_relations(
        profiles
    )

    output = Path(args.out_dir)

    require(
        not output.exists(),
        "Output directory already exists",
    )

    output.mkdir(parents=True)

    decision_path = output / "refined_assignments.jsonl"
    identity_path = output / "refined_identities.jsonl"
    audit_path = output / "fragmentation_audit.jsonl"

    statuses = Counter()
    changes = Counter()
    rules = Counter()
    identities = defaultdict(lambda: {
        "count": 0,
        "articles": set(),
        "surfaces": Counter(),
        "facets": Counter(),
    })
    audits = defaultdict(lambda: {
        "total": 0,
        "status": Counter(),
        "rules": Counter(),
        "identities": set(),
    })

    with decision_path.open(
        "w", encoding="utf-8"
    ) as f:
        for pid in sorted(profiles):
            profile = profiles[pid]
            previous = assignments[pid]

            row = refine(
                profile,
                previous,
                organization_anchors,
                full_person_index,
                person_evidence,
                article_relations,
            )

            require(
                not row["identity_assignment_approved"]
                and row["canonical_entity_id"] is None
                and not row["fact_propagation_allowed"],
                "Refinement approved an identity or fact",
            )

            status = row["assignment_status"]
            identity = row["proposed_identity_id"]
            rule = row["refinement_rule"]

            require(
                (status == "PROVISIONAL") == bool(identity),
                f"Invalid assignment: {pid}",
            )

            statuses[status] += 1
            rules[rule] += 1

            if (
                status != previous["assignment_status"]
                or identity != previous["proposed_identity_id"]
            ):
                changes[rule] += 1

            if identity:
                item = identities[identity]
                item["count"] += 1
                item["articles"].add(profile["article_id"])
                item["surfaces"][profile["surface"]] += 1
                item["facets"][
                    row["occurrence_semantic_facet"]
                ] += 1

            surface = normalize(profile["surface"])
            item = audits[surface]

            item["total"] += 1
            item["status"][status] += 1
            item["rules"][rule] += 1

            if identity:
                item["identities"].add(identity)

            f.write(
                json.dumps(
                    row, ensure_ascii=False, sort_keys=True
                ) + "\n"
            )

    identity_rows = []

    for identity, value in sorted(identities.items()):
        require(
            len(value["facets"]) == 1,
            f"Mixed semantic facets: {identity}",
        )

        identity_rows.append({
            "proposed_identity_id": identity,
            "occurrence_count": value["count"],
            "article_count": len(value["articles"]),
            "surface_counts": dict(
                value["surfaces"].most_common()
            ),
            "semantic_facet_counts": dict(
                value["facets"]
            ),
            "identity_approved": False,
            "canonical_entity_created": False,
        })

    write_jsonl(identity_path, identity_rows)

    # Audit all repeated surfaces, not a hardcoded name list.
    audit_rows = []

    for surface, value in sorted(audits.items()):
        if value["total"] < args.audit_min_occurrences:
            continue

        audit_rows.append({
            "normalized_surface": surface,
            "occurrences": value["total"],
            "status_counts": dict(value["status"]),
            "rule_counts": dict(value["rules"]),
            "distinct_provisional_identities":
                len(value["identities"]),
        })

    write_jsonl(audit_path, audit_rows)

    require(
        sum(statuses.values()) == len(profiles),
        "Incomplete profile coverage",
    )

    require(
        sum(item["occurrence_count"] for item in identity_rows)
        == statuses["PROVISIONAL"],
        "Identity aggregation mismatch",
    )

    summary = {
        "version": VERSION,
        "source_batch_key": ps["source_batch_key"],
        "source_assignment_sha256": sha256(
            Path(args.assignments_dir)
            / "occurrence_assignments.jsonl"
        ),
        "profiles_total": len(profiles),
        "previous_status_counts":
            ass["assignment_status_counts"],
        "refined_status_counts": dict(sorted(statuses.items())),
        "changed_assignments": sum(changes.values()),
        "change_reason_counts": dict(sorted(changes.items())),
        "refinement_rule_counts": dict(sorted(rules.items())),
        "organization_anchor_bases":
            len(organization_anchors),
        "unambiguous_full_person_names":
            len(full_person_index),
        "refined_identities": len(identity_rows),
        "audit_surfaces": len(audit_rows),
        "infrastructure_scope_policy": "ADVISORY_ONLY",
        "checksums": {
            path.name: sha256(path)
            for path in (
                decision_path,
                identity_path,
                audit_path,
            )
        },
        "llm_calls": 0,
        "identity_merges": 0,
        "canonical_entities_created": 0,
        "graph_writes": 0,
    }

    (output / "summary.json").write_text(
        json.dumps(
            summary,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )

    print(json.dumps(
        summary, ensure_ascii=False, indent=2
    ))


def main():
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        "--profiles-dir",
        default="output/resolution/identity-profiles-v1",
    )
    parser.add_argument(
        "--assignments-dir",
        default="output/resolution/occurrence-assignment-v1",
    )
    parser.add_argument(
        "--out-dir",
        default="output/resolution/contextual-identity-refinement-v2",
    )
    parser.add_argument(
        "--audit-min-occurrences",
        type=int,
        default=5,
    )

    args = parser.parse_args()
    require(
        args.audit_min_occurrences >= 1,
        "Invalid audit minimum",
    )
    run(args)


if __name__ == "__main__":
    main()
