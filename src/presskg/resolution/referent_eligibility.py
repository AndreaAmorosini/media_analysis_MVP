
"""PressKG Referent Eligibility & Relation Safety v1.

Consumes the audited Semantic Mention Decomposition output.

No Neo4j writes, no LLM requests, no fact expansion.
All identity and relation decisions remain proposals.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata

from collections import Counter
from pathlib import Path


VERSION = "referent-eligibility-v1"
SOURCE_VERSION = "mention-semantic-decomposition-v1"

# These categories can contain named real-world referents.
REFERENTIAL_KINDS = {
    "Person",
    "Company",
    "Organization",
    "PublicAuthority",
    "GPE",
    "Location",
    "LOC",
    "GeographicalArea",
    "Infrastructure",
    "Facility",
}

# These categories are not automatically eligible for splitting.
# They can still describe useful nodes in later pipelines.
CONTEXTUAL_KINDS = {
    "Project",
    "Event",
    "Service",
    "Policy",
    "PopulationGroup",
    "Group",
}

NON_IDENTITY_KINDS = {
    "Topic",
    "Money",
    "Quantity",
    "Date",
    "Time",
}

GEO_KINDS = {
    "GPE",
    "Location",
    "LOC",
    "GeographicalArea",
}

INFRASTRUCTURE_HEAD = re.compile(
    r"^(?:"
    r"strada|strada statale|"
    r"ss\s*\d+|s\.s\.\s*\d+|"
    r"autostrada|ferrovia|"
    r"rete|condotta|collettore|"
    r"acquedotto|impianto|"
    r"stazione|tratta|"
    r"via|viale|corso"
    r")\b",
    re.IGNORECASE,
)

GENERIC_HEAD = re.compile(
    r"^(?:"
    r"lavori|interventi|"
    r"attivit[aà]|operazioni|"
    r"riduzione|gestione|"
    r"completamento|"
    r"distrettualizzazione|"
    r"pescatori|cittadini|"
    r"associazioni|aziende|"
    r"comuni|enti|"
    r"amministrazioni"
    r")\b",
    re.IGNORECASE,
)

EXPLICIT_GEO_HEAD = re.compile(
    r"^(?:"
    r"comune di|citt[aà] di|"
    r"regione|provincia di|"
    r"localit[aà]|frazione|"
    r"quartiere|contrada"
    r")\s+",
    re.IGNORECASE,
)

VALID_CLASSIFICATIONS = {
    "COMPOSITE_NAME",
    "MULTI_ENTITY",
    "HIERARCHICAL_REFERENCE",
    "RELATIONAL_REFERENCE",
    "COLLECTIVE",
    "NON_REFERENTIAL",
    "UNRESOLVED",
}

VALID_RELATIONS = {
    "LOCATED_IN",
    "SERVES_AREA",
    "PART_OF",
    "AFFILIATED_WITH",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def fingerprint(value):
    data = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(
        Path(path).read_text(encoding="utf-8")
    )


def read_jsonl(path):
    path = Path(path)

    return [
        json.loads(line)
        for line in path.read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip()
    ]


def jsonlines(rows):
    return "".join(
        json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
        ) + "\n"
        for row in rows
    )


def write_immutable(path, content):
    path = Path(path)

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    if path.exists():
        require(
            path.read_text(encoding="utf-8")
            == content,
            f"Output differs: {path}. "
            "Choose another output directory.",
        )
        return

    temporary = path.with_name(
        path.name + ".tmp"
    )

    temporary.write_text(
        content,
        encoding="utf-8",
    )

    temporary.replace(path)


def normalized(value):
    value = unicodedata.normalize(
        "NFKC",
        str(value or ""),
    ).casefold()

    return " ".join(value.split())


def clean_surface(value):
    return " ".join(
        str(value or "").strip().split()
    )


def load_source(directory):
    root = Path(directory)

    audit = read_json(
        root / "audit_summary.json"
    )

    decisions = read_jsonl(
        root / "semantic_decisions.jsonl"
    )

    require(
        audit["version"] == SOURCE_VERSION,
        "Unexpected semantic audit version",
    )

    require(
        audit["source_count"] == len(decisions),
        "Semantic decision count mismatch",
    )

    require(
        audit["final_fingerprint"]
        == fingerprint(decisions),
        "Semantic decision fingerprint mismatch",
    )

    require(
        audit["graph_writes"] == 0
        and audit["split_approvals"] == 0
        and audit["fact_expansions"] == 0,
        "Source contains unexpected mutations",
    )

    ids = [
        item["mention_id"]
        for item in decisions
    ]

    require(
        len(set(ids)) == len(ids),
        "Duplicate semantic mention IDs",
    )

    return audit, decisions


def component_is_plausible(surface, kind):
    """Conservative lexical eligibility.

    This is a candidate filter, NOT entity verification.
    """

    surface = clean_surface(surface)

    if len(surface) < 2:
        return False, "surface_too_short"

    if not re.search(r"[^\W\d_]", surface):
        return False, "numeric_or_symbolic_surface"

    if kind not in REFERENTIAL_KINDS:
        return False, "unsupported_parent_kind"

    if GENERIC_HEAD.search(surface):
        return False, "generic_group_or_activity"

    if re.search(
        r"\b(?:che|per|finalizzat[oi]|"
        r"relativ[oi]|riguardant[ei])\b",
        surface,
        re.IGNORECASE,
    ):
        return False, "descriptive_expression"

    # Person, organization, company and geographic names
    # require a plausible proper-name surface.
    if kind in {
        "Person",
        "Company",
        "Organization",
        "PublicAuthority",
        "GPE",
        "Location",
        "LOC",
        "GeographicalArea",
    }:
        if not any(
            token[0].isupper()
            for token in surface.split()
            if token
        ):
            return False, "no_proper_name_signal"

    return True, "named_referent_candidate"


def component_record(
    decision,
    component,
    index,
    *,
    from_relation=False,
):
    name = decision["name"]

    start = component["mention_start"]
    end = component["mention_end"]

    require(
        type(start) is int
        and type(end) is int
        and 0 <= start < end <= len(name),
        "Invalid component offsets",
    )

    require(
        name[start:end] == component["surface"],
        "Component not anchored to mention",
    )

    surface = component.get(
        "referent_surface",
        component["surface"],
    )

    # A relation may include a geographic qualifier such
    # as "comune di". Strip it for candidate identity lookup,
    # while preserving the original surface and provenance.
    if from_relation:
        surface = EXPLICIT_GEO_HEAD.sub(
            "",
            surface,
        ).strip()

    eligible, reason = component_is_plausible(
        surface,
        decision["kind"],
    )

    return {
        "candidate_id": (
            f"{decision['mention_id']}:{index}"
        ),
        "mention_id": decision["mention_id"],
        "fact_id": decision["fact_id"],
        "article_id": decision["article_id"],
        "role": decision["role"],

        "component_index": index,
        "surface": component["surface"],
        "identity_surface": surface,
        "normalized_surface": normalized(surface),

        "parent_kind": decision["kind"],
        "inferred_kind": None,

        "mention_start": start,
        "mention_end": end,

        "eligibility": (
            "CANDIDATE"
            if eligible
            else "HELD"
        ),
        "reason": reason,

        "identity_id": None,
        "canonical_id": None,
        "merge_approved": False,
        "source_classification":
            decision["classification"],
    }


def relation_safety(decision, candidates):
    """Check whether a proposed relation is safe to carry
    into a later grounding stage.

    SAFE_FOR_REVIEW is not authorization to materialize.
    """

    relation = decision.get("relation")

    if not isinstance(relation, dict):
        return None

    predicate = relation.get("predicate")
    x = relation.get("subject_index")
    y = relation.get("object_index")

    require(
        predicate in VALID_RELATIONS,
        "Unknown relation type",
    )

    require(
        type(x) is int
        and type(y) is int
        and x != y
        and 0 <= x < len(candidates)
        and 0 <= y < len(candidates),
        "Invalid relation indexes",
    )

    subject = candidates[x]
    object_ = candidates[y]

    quote = relation.get("support_quote")

    evidence = decision.get("evidence") or {}

    excerpt = evidence.get("excerpt") or ""

    reasons = []

    if (
        not isinstance(quote, str)
        or not quote.strip()
        or quote not in excerpt
    ):
        reasons.append(
            "relation_quote_not_grounded"
        )

    if (
        subject["eligibility"] != "CANDIDATE"
        or object_["eligibility"] != "CANDIDATE"
    ):
        reasons.append(
            "referent_not_eligible"
        )

    # Critical: a road or long network crossing multiple
    # municipalities must not be globally localized from
    # a passage about one local construction site.
    if (
        predicate in {"LOCATED_IN", "PART_OF"}
        and INFRASTRUCTURE_HEAD.search(
            subject["identity_surface"]
        )
    ):
        reasons.append(
            "infrastructure_scope_not_verified"
        )

    if predicate == "LOCATED_IN":
        if decision["kind"] not in (
            GEO_KINDS | {"Infrastructure", "Facility"}
        ):
            reasons.append(
                "invalid_geo_parent_kind"
            )

    if predicate == "SERVES_AREA":
        if decision["kind"] not in {
            "Infrastructure",
            "Facility",
            "Project",
            "Service",
            "Location",
        }:
            reasons.append(
                "serves_area_type_unverified"
            )

    status = (
        "SAFE_FOR_REVIEW"
        if not reasons
        else "HELD"
    )

    return {
        "relation_candidate_id": (
            decision["mention_id"] + ":relation"
        ),
        "mention_id": decision["mention_id"],
        "fact_id": decision["fact_id"],
        "article_id": decision["article_id"],

        "subject_candidate_id":
            subject["candidate_id"],
        "predicate": predicate,
        "object_candidate_id":
            object_["candidate_id"],

        "support_quote": quote,
        "evidence_field":
            evidence.get("field"),
        "source_fingerprint":
            evidence.get("source_fingerprint"),

        "status": status,
        "reasons": reasons,

        "approved": False,
        "graph_writes": 0,
    }


def interpret(decision):
    """Produce eligibility proposals for a semantic decision."""

    classification = decision["classification"]

    require(
        classification in VALID_CLASSIFICATIONS,
        "Unexpected classification",
    )

    base = {
        "mention_id": decision["mention_id"],
        "fact_id": decision["fact_id"],
        "article_id": decision["article_id"],
        "role": decision["role"],
        "name": decision["name"],
        "kind": decision["kind"],
        "classification": classification,
        "rule_id": decision["rule_id"],

        "fact_distribution": "NOT_AUTHORIZED",
        "original_fact_preserved": True,
        "graph_writes": 0,
    }

    candidates = []
    relations = []

    if classification in {
        "UNRESOLVED",
        "NON_REFERENTIAL",
    }:
        status = (
            "HOLD_UNRESOLVED"
            if classification == "UNRESOLVED"
            else "NON_IDENTITY"
        )

    elif classification == "COLLECTIVE":
        status = "COLLECTIVE_HOLD"

    elif classification == "COMPOSITE_NAME":
        # Preserve the complete official or compound name
        # as one identity candidate, never its syntactic parts.
        eligible, reason = component_is_plausible(
            decision["name"],
            decision["kind"],
        )

        status = (
            "KEEP_SINGLE"
            if eligible
            else "HOLD_UNRESOLVED"
        )

        if eligible:
            candidates.append({
                "candidate_id": (
                    decision["mention_id"] + ":whole"
                ),
                "mention_id": decision["mention_id"],
                "fact_id": decision["fact_id"],
                "article_id": decision["article_id"],
                "role": decision["role"],
                "component_index": None,
                "surface": decision["name"],
                "identity_surface": decision["name"],
                "normalized_surface":
                    normalized(decision["name"]),
                "parent_kind": decision["kind"],
                "inferred_kind": None,
                "mention_start": 0,
                "mention_end": len(
                    decision["name"]
                ),
                "eligibility": "CANDIDATE",
                "reason": reason,
                "identity_id": None,
                "canonical_id": None,
                "merge_approved": False,
                "source_classification":
                    classification,
            })

    elif classification in {
        "MULTI_ENTITY",
        "HIERARCHICAL_REFERENCE",
        "RELATIONAL_REFERENCE",
    }:
        components = decision["components"]

        require(
            len(components) >= 2,
            "Missing decomposed referents",
        )

        for index, component in enumerate(components):
            candidates.append(
                component_record(
                    decision,
                    component,
                    index,
                    from_relation=(
                        classification in {
                            "HIERARCHICAL_REFERENCE",
                            "RELATIONAL_REFERENCE",
                        }
                    ),
                )
            )

        if any(
            c["eligibility"] != "CANDIDATE"
            for c in candidates
        ):
            status = "HOLD_COMPONENTS"

        elif classification == "MULTI_ENTITY":
            status = "COMPONENT_CANDIDATES"

        else:
            status = "RELATION_CANDIDATE"

        if classification in {
            "HIERARCHICAL_REFERENCE",
            "RELATIONAL_REFERENCE",
        }:
            proposed = relation_safety(
                decision,
                candidates,
            )

            if proposed is not None:
                relations.append(proposed)

                if proposed["status"] == "HELD":
                    status = "HOLD_RELATION"

    else:
        raise ValueError(
            f"Unsupported classification: {classification}"
        )

    return (
        {
            **base,
            "interpretation_status": status,
            "candidate_count": sum(
                c["eligibility"] == "CANDIDATE"
                for c in candidates
            ),
            "held_component_count": sum(
                c["eligibility"] == "HELD"
                for c in candidates
            ),
            "relation_count": len(relations),
        },
        candidates,
        relations,
    )


def run(args):
    audit, decisions = load_source(
        args.source_dir
    )

    interpretations = []
    candidates = []
    relations = []

    for decision in decisions:
        interpretation, found, proposed = interpret(
            decision
        )

        interpretations.append(
            interpretation
        )

        candidates.extend(found)
        relations.extend(proposed)

    require(
        len(interpretations) == len(decisions),
        "Incomplete interpretation",
    )

    require(
        len({
            c["candidate_id"]
            for c in candidates
        }) == len(candidates),
        "Duplicate candidate IDs",
    )

    counts = Counter(
        x["interpretation_status"]
        for x in interpretations
    )

    relation_counts = Counter(
        x["status"]
        for x in relations
    )

    candidate_counts = Counter(
        x["eligibility"]
        for x in candidates
    )

    summary = {
        "version": VERSION,
        "semantic_source_version":
            audit["version"],
        "semantic_source_fingerprint":
            audit["final_fingerprint"],

        "processed_mentions":
            len(interpretations),
        "total_candidates":
            len(candidates),
        "eligible_candidates":
            candidate_counts["CANDIDATE"],
        "held_components":
            candidate_counts["HELD"],

        "total_relation_candidates":
            len(relations),
        "relation_status_counts":
            dict(sorted(relation_counts.items())),
        "interpretation_status_counts":
            dict(sorted(counts.items())),

        "interpretations_fingerprint":
            fingerprint(interpretations),
        "candidates_fingerprint":
            fingerprint(candidates),
        "relations_fingerprint":
            fingerprint(relations),

        "identity_merges": 0,
        "approved_relations": 0,
        "fact_expansions": 0,
        "graph_writes": 0,
    }

    out = Path(args.out_dir)

    write_immutable(
        out / "mention_interpretations.jsonl",
        jsonlines(interpretations),
    )

    write_immutable(
        out / "referent_candidates.jsonl",
        jsonlines(candidates),
    )

    write_immutable(
        out / "relation_candidates.jsonl",
        jsonlines(relations),
    )

    write_immutable(
        out / "summary.json",
        json.dumps(
            summary,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n",
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
        "--source-dir",
        default=(
            "output/resolution/"
            "mention-semantic-decomposition-v1"
        ),
    )

    parser.add_argument(
        "--out-dir",
        default=(
            "output/resolution/"
            "referent-eligibility-v1"
        ),
    )

    run(parser.parse_args())


if __name__ == "__main__":
    main()
