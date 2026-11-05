
"""PressKG Relation Type & Scope Validation v1.

Deterministic, offline assessment of identity attribute evidence.

Input:
    attribute-grounding-v1/grounded_attributes.jsonl
    attribute-grounding-v1/summary.json

Output:
    assessed_relations.jsonl
    review_queue.jsonl
    summary.json

No LLM, Neo4j writes, automatic inversions or identity merges.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path


VERSION = "relation-type-scope-v1"
SOURCE_VERSION = "attribute-grounding-v1"

PERSON = {"Person"}
ORGANIZATION = {
    "Company",
    "Organization",
    "PublicAuthority",
    "NonProfitOrganization",
    "PoliticalParty",
    "LaborUnion",
    "MediaOutlet",
}
GEOGRAPHY = {
    "GPE",
    "Location",
    "LOC",
    "GeographicalArea",
}
PLACE = GEOGRAPHY | {
    "Facility",
    "Address",
}
LOCATABLE = {
    "Person",
    "Company",
    "Organization",
    "PublicAuthority",
    "NonProfitOrganization",
    "Infrastructure",
    "Facility",
    "Project",
    "Event",
    "Service",
    "Product",
    "Location",
    "LOC",
    "GeographicalArea",
}
EVENTS = {"Event", "Project"}
INFRASTRUCTURE = {"Infrastructure"}

STREET = re.compile(
    r"^(?:via|viale|corso|piazza|"
    r"strada|strada statale|ss\s*\d+|"
    r"s\.s\.\s*\d+)\b",
    re.IGNORECASE,
)

EXTENDED_INFRASTRUCTURE = re.compile(
    r"^(?:strada|strada statale|ss\s*\d+|"
    r"s\.s\.\s*\d+|autostrada|ferrovia|"
    r"acquedotto|rete|condotta|collettore)\b",
    re.IGNORECASE,
)

MUNICIPALITY = re.compile(
    r"^(?:comune di|citt[aà] di|municipio di)\b",
    re.IGNORECASE,
)

ADMINISTRATIVE_AREA = re.compile(
    r"^(?:regione|provincia di|comune di|"
    r"citt[aà] di|localit[aà]|frazione)\b",
    re.IGNORECASE,
)

INDUSTRY_DESCRIPTION = re.compile(
    r"^(?:lavorazione|produzione|"
    r"commercializzazione|trasformazione|"
    r"smaltimento|distribuzione|"
    r"settore|attivit[aà]|"
    r"servizio|servizi|"
    r"industria|mercato)\b",
    re.IGNORECASE,
)

SPECIFIC_SEGMENT = re.compile(
    r"\b(?:tratto|segmento|cantiere|"
    r"intervento|lavori|porzione)\b",
    re.IGNORECASE,
)

TEMPORAL = {
    "WORKS_FOR",
    "WORKS_AT",
    "REPRESENTS",
    "PRESIDES_OVER",
}

GEOGRAPHIC = {
    "LOCATED_IN",
    "LOCATED_AT",
    "OPERATES_IN",
}

PREDICATES = TEMPORAL | GEOGRAPHIC


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256_file(path):
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


def read_jsonl(path):
    with Path(path).open(encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            if not line.strip():
                continue

            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path}:{lineno}: {exc}"
                ) from exc


def serialize(record):
    return json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
    ) + "\n"


def kind_of(record, side):
    """Use the original annotated kind, not an inferred truth."""
    value = record.get(f"{side}_kind")

    if not isinstance(value, str):
        return None

    return value.strip() or None


def type_assessment(record):
    """Check predicate/domain/range compatibility.

    Unknown kinds remain UNKNOWN, not INCOMPATIBLE.
    """

    predicate = record["predicate"]
    subject = kind_of(record, "subject")
    object_ = kind_of(record, "object")

    name_s = record["subject_surface"]
    name_o = record["object_surface"]

    reasons = []

    if predicate not in PREDICATES:
        return {
            "status": "UNKNOWN",
            "reasons": ["unsupported_predicate"],
        }

    if predicate == "OPERATES_IN":
        # This test is stronger than an ontology tag:
        # "lavorazione di carta" is not a geographical area.
        if INDUSTRY_DESCRIPTION.search(name_o):
            reasons.append(
                "object_describes_industry_not_geography"
            )

    if predicate in {"LOCATED_IN", "OPERATES_IN"}:
        # A whole municipality is not located inside a street.
        if (
            MUNICIPALITY.search(name_s)
            and STREET.search(name_o)
        ):
            reasons.append(
                "administrative_area_to_street"
            )

    if reasons:
        return {
            "status": "INCOMPATIBLE",
            "reasons": reasons,
        }

    if subject is None or object_ is None:
        return {
            "status": "UNKNOWN",
            "reasons": ["missing_type_annotation"],
        }

    domain = None
    range_ = None

    if predicate in {"WORKS_FOR", "WORKS_AT"}:
        domain = PERSON
        range_ = ORGANIZATION | {"Facility"}

    elif predicate in {"REPRESENTS", "PRESIDES_OVER"}:
        domain = PERSON
        range_ = ORGANIZATION

    elif predicate == "OPERATES_IN":
        domain = (
            ORGANIZATION
            | {"Service", "Infrastructure", "Project"}
        )
        range_ = GEOGRAPHY

    elif predicate == "LOCATED_IN":
        domain = LOCATABLE
        range_ = PLACE | {"PublicAuthority"}

    elif predicate == "LOCATED_AT":
        domain = LOCATABLE
        range_ = PLACE | {"PublicAuthority"}

    assert domain is not None and range_ is not None

    # Unknown ontology labels should not be treated as
    # definite contradictions.
    known = domain | range_

    if subject not in known or object_ not in known:
        return {
            "status": "UNKNOWN",
            "reasons": ["type_outside_known_contract"],
        }

    if subject not in domain or object_ not in range_:
        return {
            "status": "INCOMPATIBLE",
            "reasons": [
                "predicate_domain_range_mismatch"
            ],
        }

    return {
        "status": "COMPATIBLE",
        "reasons": [],
    }


def direction_assessment(record):
    """Flag only explicit direction problems.

    Do not invert a claim automatically.
    """

    predicate = record["predicate"]
    subject = record["subject_surface"]
    object_ = record["object_surface"]

    subject_kind = kind_of(record, "subject")
    object_kind = kind_of(record, "object")

    reasons = []

    if predicate in {"LOCATED_IN", "LOCATED_AT"}:
        if (
            MUNICIPALITY.search(subject)
            and STREET.search(object_)
        ):
            reasons.append(
                "municipality_as_subject_street_as_object"
            )

        if (
            subject_kind == "PublicAuthority"
            and object_kind in {"Address", "Infrastructure"}
            and MUNICIPALITY.search(subject)
        ):
            reasons.append(
                "authority_to_address_direction_suspect"
            )

    if predicate in TEMPORAL:
        if (
            subject_kind in ORGANIZATION
            and object_kind == "Person"
        ):
            reasons.append(
                "organization_to_person_direction_suspect"
            )

    return {
        "status": (
            "SUSPECTED_INVERSION"
            if reasons
            else "PLAUSIBLE"
            if (
                record["predicate"] in PREDICATES
                and subject_kind is not None
                and object_kind is not None
            )
            else "UNKNOWN"
        ),
        "reasons": reasons,
        "inversion_approved": False,
    }


def scope_assessment(record):
    """Represent scope and temporal constraints independently."""

    predicate = record["predicate"]
    subject = record["subject_surface"]

    reasons = list(
        record.get("scope_assessment", {})
        .get("reasons", [])
    )

    if predicate in TEMPORAL:
        return {
            "status": "SCOPE_UNVERIFIED",
            "scope": "TEMPORAL_ROLE",
            "reasons": sorted(set(
                reasons + [
                    "relation_may_change_over_time"
                ]
            )),
            "time_anchor":
                record.get("publication_date"),
            "valid_from": None,
            "valid_to": None,
        }

    if (
        predicate in {"LOCATED_IN", "LOCATED_AT"}
        and (
            kind_of(record, "subject") in INFRASTRUCTURE
            or EXTENDED_INFRASTRUCTURE.search(subject)
        )
    ):
        evidence = (
            record.get("argument_grounding", {})
            .get("evidence") or {}
        )

        excerpt = evidence.get("excerpt") or ""

        return {
            "status": "ENTITY_EXTENT_RISK",
            "scope": "INFRASTRUCTURE",
            "reasons": sorted(set(
                reasons + [
                    "whole_entity_vs_local_segment_unresolved",
                    *(
                        ["local_intervention_mentioned"]
                        if SPECIFIC_SEGMENT.search(excerpt)
                        else []
                    ),
                ]
            )),
            "time_anchor":
                record.get("publication_date"),
            "valid_from": None,
            "valid_to": None,
        }

    if predicate == "OPERATES_IN":
        return {
            "status": "SCOPE_UNVERIFIED",
            "scope": "OPERATIONAL_GEOGRAPHY",
            "reasons": sorted(set(
                reasons + [
                    "operational_extent_unverified"
                ]
            )),
            "time_anchor":
                record.get("publication_date"),
            "valid_from": None,
            "valid_to": None,
        }

    return {
        "status": (
            "SCOPE_UNVERIFIED"
            if reasons else "BOUNDED"
        ),
        "scope": "LOCAL_OR_ENTITY_LOCATION",
        "reasons": sorted(set(reasons)),
        "time_anchor":
            record.get("publication_date"),
        "valid_from": None,
        "valid_to": None,
    }


def assess(record):
    """Combine checks without conflating type, grounding and truth."""

    require(
        record["predicate"] in PREDICATES,
        "Unexpected predicate",
    )

    require(
        record["source_fact_validated"] is True
        and record["attribute_semantically_verified"] is False
        and record["match_constraint_approved"] is False,
        "Unexpected source approval state",
    )

    types = type_assessment(record)
    direction = direction_assessment(record)
    scope = scope_assessment(record)

    grounding = record["argument_grounding"]["status"]
    cue = record["predicate_grounding"]["status"]

    blockers = []

    if types["status"] == "INCOMPATIBLE":
        blockers.append("type_incompatibility")

    if direction["status"] == "SUSPECTED_INVERSION":
        blockers.append("direction_suspect")

    if scope["status"] == "ENTITY_EXTENT_RISK":
        blockers.append("entity_extent_risk")

    if record["original_disposition"] in {
        "HELD_COMPOSITE_SCOPE",
        "HELD_NON_IDENTITY",
    }:
        blockers.append("original_scope_hold")

    if blockers:
        decision = "BLOCKED"

    elif (
        types["status"] == "COMPATIBLE"
        and direction["status"] == "PLAUSIBLE"
        and grounding == "BOTH_GROUNDED"
        and cue == "LEXICAL_CUE_FOUND"
    ):
        decision = "SUPPORTING_CANDIDATE"

    else:
        decision = "REVIEW_REQUIRED"

    # Identity matching may inspect these proposals, but
    # must not interpret them as confirmed identity evidence.
    return {
        "evidence_id": record["evidence_id"],
        "fact_id": record["fact_id"],
        "claim_id": record["claim_id"],
        "article_id": record["article_id"],
        "subject_profile_id":
            record["subject_profile_id"],

        "subject_surface": record["subject_surface"],
        "subject_kind": record["subject_kind"],
        "predicate": record["predicate"],
        "object_surface": record["object_surface"],
        "object_kind": record["object_kind"],
        "attribute_kind": record["attribute_kind"],

        "original_grounding_status": record["status"],
        "argument_grounding_status": grounding,
        "predicate_cue_status": cue,

        "type_assessment": types,
        "direction_assessment": direction,
        "scope_assessment": scope,

        "matching_disposition": decision,
        "blockers": blockers,

        "source_fact_validated": True,
        "attribute_semantically_verified": False,
        "relation_direction_verified": False,
        "scope_verified": False,
        "hard_identity_constraint": False,
        "identity_merge_approved": False,
        "graph_writes": 0,
    }


def run(args):
    source = Path(args.source_dir)
    summary = read_json(source / "summary.json")

    require(
        summary["version"] == SOURCE_VERSION,
        "Unsupported grounding version",
    )

    require(
        summary["graph_writes"] == 0
        and summary["attributes_semantically_approved"] == 0
        and summary["match_constraints_approved"] == 0,
        "Source contains unexpected approvals",
    )

    input_path = source / "grounded_attributes.jsonl"

    require(
        sha256_file(input_path)
        == summary["grounded_attributes_sha256"],
        "Grounding input checksum mismatch",
    )

    out = Path(args.out_dir)

    require(
        not out.exists(),
        "Output already exists. Use a new directory.",
    )

    out.mkdir(parents=True)

    assessed_path = out / "assessed_relations.jsonl"
    review_path = out / "review_queue.jsonl"

    counts = Counter()
    types = Counter()
    directions = Counter()
    scopes = Counter()
    blockers = Counter()
    seen = set()

    with (
        assessed_path.open("w", encoding="utf-8") as assessed_file,
        review_path.open("w", encoding="utf-8") as review_file,
    ):
        for record in read_jsonl(input_path):
            evidence_id = record["evidence_id"]

            require(
                evidence_id not in seen,
                f"Repeated evidence ID: {evidence_id}",
            )
            seen.add(evidence_id)

            result = assess(record)

            assessed_file.write(serialize(result))

            counts[result["matching_disposition"]] += 1
            types[result["type_assessment"]["status"]] += 1
            directions[
                result["direction_assessment"]["status"]
            ] += 1
            scopes[result["scope_assessment"]["status"]] += 1

            for reason in result["blockers"]:
                blockers[reason] += 1

            if result["matching_disposition"] != "SUPPORTING_CANDIDATE":
                review_file.write(serialize(result))

    require(
        len(seen) == summary["processed_attributes"],
        "Incomplete grounding coverage",
    )

    report = {
        "version": VERSION,
        "source_version": SOURCE_VERSION,
        "source_batch_key": summary["source_batch_key"],
        "source_grounding_sha256":
            summary["grounded_attributes_sha256"],

        "processed_relations": len(seen),
        "matching_disposition_counts":
            dict(sorted(counts.items())),
        "type_status_counts": dict(sorted(types.items())),
        "direction_status_counts":
            dict(sorted(directions.items())),
        "scope_status_counts": dict(sorted(scopes.items())),
        "blocker_counts": dict(sorted(blockers.items())),

        "review_queue_count": (
            counts["REVIEW_REQUIRED"] + counts["BLOCKED"]
        ),

        "assessed_relations_sha256":
            sha256_file(assessed_path),
        "review_queue_sha256":
            sha256_file(review_path),

        "llm_calls": 0,
        "approved_relations": 0,
        "hard_identity_constraints": 0,
        "identity_merges": 0,
        "graph_writes": 0,
    }

    (out / "summary.json").write_text(
        json.dumps(
            report,
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )

    print(json.dumps(
        report,
        indent=2,
        ensure_ascii=False,
    ))


def main():
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        "--source-dir",
        default="output/resolution/attribute-grounding-v1",
    )

    parser.add_argument(
        "--out-dir",
        default="output/resolution/relation-type-scope-v1",
    )

    run(parser.parse_args())


if __name__ == "__main__":
    main()
