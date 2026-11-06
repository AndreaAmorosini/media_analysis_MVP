
"""PressKG Phase 4D: generic coreference and fragmentation audit.

Inputs:
- identity-profiles-v1
- occurrence-assignment-v1
- contextual-identity-refinement-v2

Outputs:
- refined_assignments.jsonl
- refined_identities.jsonl
- identity_risk_audit.jsonl
- summary.json

No corpus-specific entity names, no LLM, no Neo4j writes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata

from collections import Counter, defaultdict
from pathlib import Path


VERSION = "coreference-audit-v1"

ARTICLES = ("gli", "il", "lo", "la", "le", "i", "l")
PERSON_PARTICLES = {
    "de", "di", "del", "della", "da", "van", "von",
}
GENERIC_PREDICATES = {
    "OTHER", "MENTIONS", "RELATED_TO", "ASSOCIATED_WITH",
}

ORGANIZATION_KINDS = {
    "Company", "Organization", "PublicAuthority",
    "NonProfitOrganization", "PoliticalParty",
    "LaborUnion", "MediaOutlet",
}

LEGAL_FORMS = {
    ("spa",), ("s", "p", "a"),
    ("srl",), ("s", "r", "l"),
    ("snc",), ("s", "n", "c"),
    ("sas",), ("s", "a", "s"),
    ("scarl",), ("s", "c", "a", "r", "l"),
}
JOINED_LEGAL = ("scarl", "spa", "srl", "snc", "sas")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


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
    with Path(path).open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(
                record, ensure_ascii=False, sort_keys=True
            ) + "\n")


def normalize(value):
    text = unicodedata.normalize(
        "NFKD", str(value or "")
    ).casefold()
    text = "".join(
        c for c in text
        if not unicodedata.combining(c)
    )
    return " ".join(
        re.sub(r"[^\w]+", " ", text).split()
    )


def legal_base(value):
    tokens = normalize(value).split()
    if not tokens:
        return ""

    for article in ARTICLES:
        if len(tokens) > 1 and tokens[0] == article:
            tokens = tokens[1:]
            break

    changed = True
    while changed:
        changed = False
        for form in LEGAL_FORMS:
            if tuple(tokens[-len(form):]) == form:
                tokens = tokens[:-len(form)]
                changed = True
                break

    if len(tokens) == 1:
        word = tokens[0]
        for suffix in JOINED_LEGAL:
            if word.endswith(suffix) and len(word) > len(suffix) + 2:
                tokens = [word[:-len(suffix)]]
                break

    return " ".join(tokens)


def has_legal_suffix(surface):
    norm = normalize(surface)
    base = legal_base(surface)
    if not base or base == norm:
        return False

    # An article alone is not evidence of legal form.
    tokens = norm.split()
    last = tokens[-1] if tokens else ""

    return (
        any(
            tuple(tokens[-len(form):]) == form
            for form in LEGAL_FORMS
        )
        or any(
            last.endswith(suffix)
            and len(last) > len(suffix) + 2
            for suffix in JOINED_LEGAL
        )
    )


def joined_article_base(surface):
    """Recover an article only at a visible CamelCase boundary.

    LaGori -> Gori, LaGiunta -> Giunta.
    lagoriper -> no inferred segmentation.
    """
    text = unicodedata.normalize("NFKC", str(surface).strip())
    matches = []

    for article in ARTICLES:
        if len(text) <= len(article) + 2:
            continue

        prefix = text[:len(article)]
        rest = text[len(article):]

        if (
            prefix.casefold() == article
            and rest[0].isupper()
            and rest[0].isalpha()
        ):
            matches.append(normalize(rest))

    # Never guess between multiple candidate segmentations.
    return matches[0] if len(set(matches)) == 1 else None


def person_tokens(value):
    return tuple(normalize(value).split())


def full_person_name(value):
    tokens = person_tokens(value)
    return (
        len(tokens) >= 2
        and not (
            len(tokens) == 2
            and tokens[0] in PERSON_PARTICLES
        )
    )


def short_name_of(short, full):
    a, b = person_tokens(short), person_tokens(full)
    return bool(a) and len(b) > len(a) and b[-len(a):] == a


def relation_signature(profile):
    ctx = profile.get("fact_context") or {}
    predicate = str(ctx.get("predicate") or "").upper().strip()
    counterpart = normalize(ctx.get("counterpart_name"))
    role = str(profile.get("role") or "").lower()

    if (
        predicate in GENERIC_PREDICATES
        or not predicate
        or len(counterpart) < 3
        or counterpart.isdigit()
        or role not in {"subject", "object"}
    ):
        return None

    return (role, predicate, counterpart)


def local_quote_contains(profile, surface):
    """Check a source excerpt, not merely an article ID."""
    provenance = profile.get("provenance") or {}
    pattern = (
        r"(?<!\w)"
        + r"[\W_]*".join(
            re.escape(t)
            for t in normalize(surface).split()
        )
        + r"(?!\w)"
    )

    for field in ("supporting_span", "original_quote"):
        excerpt = provenance.get(field)
        if not isinstance(excerpt, str):
            continue

        if re.search(pattern, normalize(excerpt)):
            return True

    return False


def load_inputs(profiles_dir, assignments_dir, refinement_dir):
    p, a, r = map(
        Path, (profiles_dir, assignments_dir, refinement_dir)
    )
    ps, ass, rs = [
        read_json(x / "summary.json") for x in (p, a, r)
    ]

    require(ps["version"] == "identity-profiles-v1", "Profile version")
    require(
        ass["version"] == "occurrence-assignment-v1",
        "Assignment version",
    )
    require(
        rs["version"] == "contextual-identity-refinement-v2",
        "Refinement version",
    )

    batch = ps["source_batch_key"]
    require(
        ass["source_batch_key"] == batch
        and rs["source_batch_key"] == batch,
        "Batch lineage mismatch",
    )

    require(
        rs["source_assignment_sha256"]
        == ass["checksums"]["occurrence_assignments.jsonl"],
        "Refinement source checksum mismatch",
    )

    for summary in (ps, ass, rs):
        require(
            summary["graph_writes"] == 0
            and summary["identity_merges"] == 0,
            "Source contains graph mutations",
        )

    files = [
        (p / "mention_profiles.jsonl",
         ps["mention_profiles_sha256"]),
        (p / "component_profiles.jsonl",
         ps["component_profiles_sha256"]),
        (a / "occurrence_assignments.jsonl",
         ass["checksums"]["occurrence_assignments.jsonl"]),
        (r / "refined_assignments.jsonl",
         rs["checksums"]["refined_assignments.jsonl"]),
    ]

    for path, expected in files:
        require(
            sha256(path) == expected,
            f"Checksum mismatch: {path}",
        )

    profiles = {}
    for filename in ("mention_profiles.jsonl", "component_profiles.jsonl"):
        for record in read_jsonl(p / filename):
            pid = record["profile_id"]
            require(pid not in profiles, f"Duplicate profile: {pid}")
            require(
                record["canonical_id"] is None
                and record["merge_approved"] is False,
                "Unexpected resolved source profile",
            )
            profiles[pid] = record

    original = {}
    for record in read_jsonl(
        a / "occurrence_assignments.jsonl"
    ):
        pid = record["profile_id"]
        require(pid not in original, "Duplicate original assignment")
        original[pid] = record

    refined = {}
    for record in read_jsonl(r / "refined_assignments.jsonl"):
        pid = record["profile_id"]
        require(pid not in refined, "Duplicate refined assignment")
        refined[pid] = record

    require(
        len(profiles)
        == ps["mention_profiles"] + ps["component_profiles"],
        "Incomplete profile inventory",
    )
    require(
        set(profiles) == set(original) == set(refined),
        "Assignment coverage mismatch",
    )
    require(
        len(refined) == rs["profiles_total"],
        "Refinement count mismatch",
    )

    for pid, row in refined.items():
        require(
            row["canonical_entity_id"] is None
            and row["identity_assignment_approved"] is False
            and row["fact_propagation_allowed"] is False,
            f"Already approved assignment: {pid}",
        )
        require(
            row["previous_identity_id"]
            == original[pid]["proposed_identity_id"],
            f"Previous assignment lineage mismatch: {pid}",
        )

    return ps, rs, profiles, original, refined


def build_indexes(profiles, assignments):
    organizations = defaultdict(lambda: defaultdict(set))
    legal_attested = set()

    people_by_article = defaultdict(
        lambda: defaultdict(set)
    )
    people_relations = defaultdict(
        lambda: defaultdict(list)
    )
    short_relations = defaultdict(
        lambda: defaultdict(
            lambda: defaultdict(list)
        )
    )

    for pid, profile in profiles.items():
        row = assignments[pid]
        name = profile["surface"]
        aid = profile["article_id"]
        identity = row["proposed_identity_id"]

        if (
            row["assignment_status"] == "PROVISIONAL"
            and identity
            and row["occurrence_semantic_facet"] == "ORGANIZATION"
            and profile["source_kind"] in ORGANIZATION_KINDS
        ):
            base = legal_base(name)
            if base:
                organizations[base][identity].add(pid)
                if has_legal_suffix(name):
                    legal_attested.add(base)

        if profile["source_kind"] != "Person":
            continue

        signature = relation_signature(profile)

        if (
            row["assignment_status"] == "PROVISIONAL"
            and identity and full_person_name(name)
        ):
            people_by_article[aid][normalize(name)].add(identity)
            if signature:
                people_relations[identity][signature].append({
                    "article_id": aid,
                    "profile_id": pid,
                    "fact_id": profile["fact_id"],
                })

        elif signature:
            short_relations[aid][normalize(name)][signature].append(pid)

    org_anchors = {}
    for base, identities in organizations.items():
        # Require an observed standalone base spelling.
        candidates = []
        for identity, pids in identities.items():
            exact = sum(
                normalize(profiles[pid]["surface"]) == base
                for pid in pids
            )
            if exact:
                candidates.append((-exact, identity))

        candidates.sort()
        if candidates and (
            len(candidates) == 1
            or candidates[0][0] != candidates[1][0]
        ):
            org_anchors[base] = candidates[0][1]

    return {
        "organizations": org_anchors,
        "legal_attested": legal_attested,
        "people_by_article": people_by_article,
        "people_relations": people_relations,
        "short_relations": short_relations,
    }


def resolve_person(profile, indexes):
    """Per-occurrence coreference, never global surname propagation."""

    aid = profile["article_id"]
    short = normalize(profile["surface"])
    signature = relation_signature(profile)

    article_people = indexes["people_by_article"].get(aid, {})
    local_candidates = {
        identity
        for name, identities in article_people.items()
        if short_name_of(short, name)
        for identity in identities
    }

    # Intra-article uniqueness + actual attested short reference.
    if len(local_candidates) == 1 and local_quote_contains(
        profile, profile["surface"]
    ):
        identity = next(iter(local_candidates))
        return {
            "identity": identity,
            "rule": "PERSON_UNIQUE_IN_ARTICLE",
            "evidence": {
                "article_id": aid,
                "profile_id": profile["profile_id"],
                "relation_signature": signature,
            },
        }

    if len(local_candidates) > 1 or signature is None:
        return None

    # Cross-article: do not aggregate unrelated people merely
    # because they share a surname.
    known_names = defaultdict(set)
    for names in indexes["people_by_article"].values():
        for name, identities in names.items():
            if short_name_of(short, name):
                for identity in identities:
                    known_names[identity].add(name)

    # A single possible full-name identity across the corpus
    # is required for the cross-article rule.
    if len(known_names) != 1:
        return None

    identity = next(iter(known_names))
    full_relations = indexes["people_relations"].get(identity, {})

    current_signatures = indexes["short_relations"].get(
        aid, {}
    ).get(short, {})

    shared = set(current_signatures) & set(full_relations)

    if (
        signature not in shared
        or len(shared) < 2
        or len({sig[2] for sig in shared}) < 2
    ):
        return None

    full_articles = {
        ref["article_id"]
        for sig in shared
        for ref in full_relations[sig]
    }
    if len(full_articles) < 2:
        return None

    return {
        "identity": identity,
        "rule": "PERSON_MULTI_RELATION",
        "evidence": {
            "profile_id": profile["profile_id"],
            "article_id": aid,
            "shared_signatures": [list(x) for x in sorted(shared)],
            "full_name_articles": sorted(full_articles),
        },
    }


def refine_one(profile, old, original, indexes):
    row = dict(old)
    row["phase4d_rule"] = "UNCHANGED"
    row["phase4d_evidence"] = []
    row["phase4d_warnings"] = []
    row["global_projection_eligible"] = False

    status = row["assignment_status"]
    identity = row["proposed_identity_id"]

    if (
        status == "PROVISIONAL"
        and identity
        and row["occurrence_semantic_facet"] == "ORGANIZATION"
        and profile["source_kind"] in ORGANIZATION_KINDS
    ):
        surface = profile["surface"]
        joined = joined_article_base(surface)

        target = (
            indexes["organizations"].get(joined)
            if joined
            else None
        )

        # A CamelCase boundary alone is not enough.
        # Require an independently attested legal-form variant
        # of the recovered organization name.
        corroborated = (
            joined is not None
            and target is not None
            and joined in indexes["legal_attested"]
        )

        was_ocr_merge = (
            old.get("refinement_rule")
            == "ORGANIZATION_OCR_JOINED_ARTICLE"
        )

        if was_ocr_merge and not corroborated:
            # Restore the identity from Phase 4B.
            row["proposed_identity_id"] = (
                original["proposed_identity_id"]
            )

            row["phase4d_rule"] = (
                "REVERT_UNGROUNDED_OCR"
            )

            row["phase4d_warnings"].append(
                "ocr_segmentation_not_corroborated"
            )

        elif corroborated:
            if target != identity:
                row["proposed_identity_id"] = target

                row["phase4d_rule"] = (
                    "RECOVER_CAMELCASE_ARTICLE"
                )

                row["phase4d_evidence"].append({
                    "observed_surface": surface,
                    "recovered_base": joined,
                    "anchor_identity_id": target,
                    "corroboration":
                        "INDEPENDENT_LEGAL_FORM",
                })

        elif joined is not None and target is not None:
            # Plausible OCR segmentation, but insufficient
            # evidence for a provisional identity change.
            row["phase4d_warnings"].append(
                "uncorroborated_camelcase_candidate"
            )

        # Detect potentially generic article-stripped names.
        tokens = normalize(surface).split()

        if (
            len(tokens) == 2
            and tokens[0] in ARTICLES
            and len(tokens[1]) >= 3
            and tokens[1] not in indexes["legal_attested"]
        ):
            row["phase4d_warnings"].append(
                "article_stripped_unverified_name"
            )
    elif (
        profile["source_kind"] == "Person"
        and status == "HOLD_FOR_CONTEXT"
        and set(row["blockers"])
        == {"short_person_name_ambiguous"}
        and row["profile_scope"] == "mention_occurrence"
    ):
        result = resolve_person(profile, indexes)
        if result:
            row["assignment_status"] = "PROVISIONAL"
            row["proposed_identity_id"] = result["identity"]
            row["blockers"] = []
            row["phase4d_rule"] = result["rule"]
            row["phase4d_evidence"].append(result["evidence"])

    # Projection remains a separate decision. We do not approve
    # identity assignment, only mark candidates without known risk.
    row["global_projection_eligible"] = (
        row["assignment_status"] == "PROVISIONAL"
        and row["proposed_identity_id"] is not None
        and not row["phase4d_warnings"]
    )

    row["canonical_entity_id"] = None
    row["identity_assignment_approved"] = False
    row["fact_propagation_allowed"] = False

    return row


def run(args):
    ps, rs, profiles, original, refined = load_inputs(
        args.profiles_dir,
        args.assignments_dir,
        args.refinement_dir,
    )

    indexes = build_indexes(profiles, refined)

    decisions = {}
    for pid in sorted(profiles):
        decisions[pid] = refine_one(
            profiles[pid], refined[pid], original[pid], indexes
        )

    # Propagate a lexical risk to every occurrence of the
    # resulting identity, not only to the triggering variant.
    risky_ids = defaultdict(set)
    for row in decisions.values():
        identity = row["proposed_identity_id"]
        if identity:
            risky_ids[identity].update(row["phase4d_warnings"])

    for row in decisions.values():
        identity = row["proposed_identity_id"]
        if identity and risky_ids[identity]:
            row["global_projection_eligible"] = False
            row["phase4d_warnings"] = sorted(
                set(row["phase4d_warnings"])
                | risky_ids[identity]
            )

    counts = Counter()
    changes = Counter()
    warnings = Counter()
    identities = defaultdict(lambda: {
        "articles": set(),
        "surfaces": Counter(),
        "facets": Counter(),
        "count": 0,
        "eligible": True,
        "warnings": set(),
    })

    for pid, row in decisions.items():
        profile = profiles[pid]
        status = row["assignment_status"]
        identity = row["proposed_identity_id"]
        require(
            (status == "PROVISIONAL") == (identity is not None),
            f"Invalid identity state: {pid}",
        )

        counts[status] += 1
        warnings.update(row["phase4d_warnings"])

        if (
            status != refined[pid]["assignment_status"]
            or identity != refined[pid]["proposed_identity_id"]
        ):
            changes[row["phase4d_rule"]] += 1

        if identity:
            item = identities[identity]
            item["count"] += 1
            item["articles"].add(profile["article_id"])
            item["surfaces"][profile["surface"]] += 1
            item["facets"][row["occurrence_semantic_facet"]] += 1
            item["eligible"] &= row["global_projection_eligible"]
            item["warnings"].update(row["phase4d_warnings"])

    for identity, item in identities.items():
        require(
            len(item["facets"]) == 1,
            f"Mixed semantic families in identity: {identity}",
        )

    out = Path(args.out_dir)
    require(not out.exists(), "Output already exists")
    out.mkdir(parents=True)

    assignments_path = out / "refined_assignments.jsonl"
    identities_path = out / "refined_identities.jsonl"
    audit_path = out / "identity_risk_audit.jsonl"

    write_jsonl(
        assignments_path,
        (decisions[pid] for pid in sorted(decisions)),
    )

    identity_rows = []
    audit_rows = []

    for identity, item in sorted(identities.items()):
        summary = {
            "proposed_identity_id": identity,
            "occurrence_count": item["count"],
            "article_count": len(item["articles"]),
            "surface_counts": dict(item["surfaces"].most_common()),
            "semantic_facet_counts": dict(item["facets"]),
            "global_projection_eligible": item["eligible"],
            "warnings": sorted(item["warnings"]),
            "canonical_entity_created": False,
            "identity_approved": False,
        }
        identity_rows.append(summary)

        if item["warnings"]:
            audit_rows.append(summary)

    write_jsonl(identities_path, identity_rows)
    write_jsonl(audit_path, audit_rows)

    require(
        sum(counts.values()) == len(profiles),
        "Incomplete profile coverage",
    )
    require(
        sum(i["occurrence_count"] for i in identity_rows)
        == counts["PROVISIONAL"],
        "Identity coverage mismatch",
    )

    summary = {
        "version": VERSION,
        "source_batch_key": ps["source_batch_key"],
        "source_refinement_sha256": sha256(
            Path(args.refinement_dir) / "refined_assignments.jsonl"
        ),
        "profiles_total": len(profiles),
        "previous_status_counts": rs["refined_status_counts"],
        "status_counts": dict(sorted(counts.items())),
        "changed_assignments": sum(changes.values()),
        "change_reason_counts": dict(sorted(changes.items())),
        "warning_counts": dict(sorted(warnings.items())),
        "provisional_identities": len(identity_rows),
        "projection_eligible_identities": sum(
            i["global_projection_eligible"] for i in identity_rows
        ),
        "risk_flagged_identities": len(audit_rows),
        "infrastructure_scope_policy": "ADVISORY_ONLY",
        "checksums": {
            p.name: sha256(p)
            for p in (
                assignments_path,
                identities_path,
                audit_path,
            )
        },
        "llm_calls": 0,
        "identity_merges": 0,
        "canonical_entities_created": 0,
        "graph_writes": 0,
    }

    (out / "summary.json").write_text(
        json.dumps(
            summary, ensure_ascii=False, indent=2, sort_keys=True
        ) + "\n",
        encoding="utf-8",
    )

    print(json.dumps(summary, indent=2, ensure_ascii=False))


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
        "--refinement-dir",
        default="output/resolution/contextual-identity-refinement-v2",
    )
    parser.add_argument(
        "--out-dir",
        default="output/resolution/coreference-audit-v1",
    )
    run(parser.parse_args())


if __name__ == "__main__":
    main()
