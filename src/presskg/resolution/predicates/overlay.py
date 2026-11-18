
"""Canonical predicate overlay and taxonomy proposals.

Consolidates verified predicate refinements from previous phases.
"""

from __future__ import annotations

from argparse import Namespace
import hashlib
import json
import os
import re

from collections import Counter, defaultdict
from pathlib import Path

from neo4j import GraphDatabase, READ_ACCESS
from presskg.common import require
from presskg.common.io import iter_jsonl, latest_records, read_json, write_json_stable
from presskg.resolution import paths
from presskg.resolution._common import close_step_driver, step_driver
from presskg.resolution.context import StepContext, StepResult



VERSION = "predicate-overlay-v1"

POSITIVE_CHECKS = (
    "subject_identity_ok",
    "object_identity_ok",
    "direction_ok",
    "evidence_supports_relation",
    "predicate_semantics_ok",
)

PROVENANCE_FIELDS = (
    "fact_id",
    "projection_key",
    "article_id",
    "subject_key",
    "object_key",
    "quote_sha256",
    "context_sha256",
)


def stable_hash(value):
    content = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(
        content.encode("utf-8")
    ).hexdigest()


def load_inputs(candidate_directory, verification_directory):
    candidate_root = Path(candidate_directory)
    verification_root = Path(verification_directory)

    candidate_manifest = read_json(
        candidate_root / "selection_manifest.json"
    )
    verification_manifest = read_json(
        verification_root / "verification_manifest.json"
    )

    taxonomy = read_json(
        candidate_root / "taxonomy.json"
    )

    require(
        candidate_manifest["version"] == "predicate-candidates-v1",
        "Unsupported candidate generation version",
    )

    require(
        verification_manifest["version"]
        == "predicate-semantic-verification-v1",
        "Unsupported semantic verification version",
    )

    require(
        verification_manifest["source_version"]
        == candidate_manifest["version"],
        "Verification source version mismatch",
    )

    require(
        isinstance(taxonomy, list)
        and len(taxonomy) == len(set(taxonomy))
        and all(isinstance(p, str) for p in taxonomy),
        "Invalid taxonomy",
    )

    contexts = {}

    for row in iter_jsonl(
        candidate_root / "selected_contexts.jsonl"
    ):
        fid = row["fact_id"]

        require(
            fid not in contexts,
            f"Duplicate context: {fid}",
        )
        contexts[fid] = row

    candidates = latest_records(
        candidate_root / "candidate_attempts.jsonl"
    )

    verifications = latest_records(
        verification_root / "verification_attempts.jsonl"
    )

    require(
        len(contexts) == candidate_manifest["prepared_contexts"],
        "Unexpected prepared context count",
    )

    require(
        set(candidates) == set(contexts),
        "Candidate checkpoint does not cover all contexts",
    )

    require(
        set(verifications).issubset(candidates),
        "Verification contains unknown facts",
    )

    return {
        "candidate_manifest": candidate_manifest,
        "verification_manifest": verification_manifest,
        "taxonomy": set(taxonomy),
        "contexts": contexts,
        "candidates": candidates,
        "verifications": verifications,
    }


def validate_provenance(context, candidate, verification):
    fid = context["fact_id"]

    for field in PROVENANCE_FIELDS:
        require(
            candidate.get(field) == context.get(field),
            f"Candidate provenance mismatch: {fid}/{field}",
        )

        require(
            verification.get(field) == context.get(field),
            f"Verification provenance mismatch: {fid}/{field}",
        )

    require(
        candidate["status"] == "CANDIDATE",
        f"Candidate is not valid: {fid}",
    )

    require(
        verification["status"] == "VERIFIED",
        f"Verification is not valid: {fid}",
    )

    require(
        verification["candidate"] == candidate["proposal"],
        f"Candidate snapshot changed: {fid}",
    )

    require(
        candidate.get("approved") is False
        and verification.get("approved") is False,
        f"Unexpected prior approval: {fid}",
    )


def supported_positive(verification, decision):
    if verification.get("verdict") != "SUPPORTED":
        return False

    if not all(
        verification.get(field) is True
        for field in POSITIVE_CHECKS
    ):
        return False

    if decision == "TAXONOMY_GAP":
        return verification.get("taxonomy_gap_genuine") is True

    return True


def build_plan(data):
    contexts = data["contexts"]
    candidates = data["candidates"]
    verifications = data["verifications"]
    taxonomy = data["taxonomy"]

    refinements = []
    gaps = []

    counts = Counter()
    excluded = Counter()

    for fid, context in contexts.items():
        candidate = candidates[fid]

        if candidate["status"] != "CANDIDATE":
            excluded["GENERATION_FAILURE"] += 1
            continue

        verification = verifications.get(fid)

        if verification is None:
            excluded["MISSING_VERIFICATION"] += 1
            continue

        if verification["status"] != "VERIFIED":
            excluded["VERIFICATION_FAILURE"] += 1
            continue

        validate_provenance(
            context,
            candidate,
            verification,
        )

        proposal = candidate["proposal"]
        assessment = verification["verification"]
        decision = proposal["decision"]

        counts[decision] += 1

        if not supported_positive(assessment, decision):
            excluded["NOT_POSITIVELY_VERIFIED"] += 1
            continue

        base = {
            "fact_id": fid,
            "projection_key": context["projection_key"],
            "article_id": context["article_id"],
            "subject_key": context["subject_key"],
            "object_key": context["object_key"],
            "source_batch_key": data[
                "candidate_manifest"
            ]["source_batch_key"],
            "evidence_quote": proposal.get("evidence_quote"),
            "quote_sha256": context["quote_sha256"],
            "context_sha256": context["context_sha256"],
        }

        if decision == "RECLASSIFY":
            predicate = proposal["proposed_predicate"]

            require(
                predicate in taxonomy and predicate != "OTHER",
                f"Invalid refined predicate: {fid}",
            )

            require(
                isinstance(proposal["evidence_quote"], str)
                and proposal["evidence_quote"] in context["quote"],
                f"Evidence mismatch: {fid}",
            )

            refinements.append({
                **base,
                "effective_predicate": predicate,
                "status": "REFINED",
            })

        elif decision == "TAXONOMY_GAP":
            label = proposal["suggested_new_predicate"]

            require(
                isinstance(label, str)
                and re.fullmatch(
                    r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*",
                    label,
                ) is not None
                and label not in taxonomy
                and label != "OTHER",
                f"Invalid proposed predicate: {fid}",
            )

            require(
                isinstance(proposal["evidence_quote"], str)
                and proposal["evidence_quote"] in context["quote"],
                f"Evidence mismatch: {fid}",
            )

            gaps.append({
                **base,
                "proposed_new_predicate": label,
                "effective_predicate": "OTHER",
                "status": "PROPOSED_NEW_PREDICATE",
            })

    require(
        not (
            {x["fact_id"] for x in refinements}
            & {x["fact_id"] for x in gaps}
        ),
        "Conflicting predicate decisions",
    )

    groups = defaultdict(list)

    for row in gaps:
        groups[row["proposed_new_predicate"]].append(row)

    proposals = []

    for label, items in sorted(groups.items()):
        facts = sorted({
            item["fact_id"] for item in items
        })
        articles = sorted({
            item["article_id"] for item in items
        })

        proposals.append({
            "proposal_key": stable_hash({
                "version": VERSION,
                "predicate": label,
            }),
            "name": label,
            "status": "PROPOSED",
            "proposal_version": VERSION,
            "supporting_facts": len(facts),
            "distinct_articles": len(articles),
            "fact_ids": facts,
            "article_ids": articles,
            "approved": False,
        })

    return {
        "version": VERSION,
        "refinements": refinements,
        "gaps": gaps,
        "proposals": proposals,
        "decision_counts": dict(counts),
        "excluded_counts": dict(excluded),
        "total_contexts": len(contexts),
        "input_models": {
            "generation": data[
                "candidate_manifest"
            ]["model"],
            "verification": data[
                "verification_manifest"
            ]["verification_model"],
        },
    }


# ----------------------------------------------------------
# Neo4j queries
# ----------------------------------------------------------

PREFLIGHT_QUERY = """
UNWIND $rows AS x

OPTIONAL MATCH
    (s:CanonicalEntity {
        projection_key: x.subject_key
    })
    -[r:CANONICAL_FACT {
        projection_key: x.projection_key
    }]->
    (o:CanonicalEntity {
        projection_key: x.object_key
    })

WITH x, collect(r) AS relationships

RETURN
    x.fact_id AS fact_id,
    size(relationships) AS matching_relations,

    CASE WHEN size(relationships) = 1
         THEN relationships[0].fact_id
         ELSE null END AS stored_fact_id,

    CASE WHEN size(relationships) = 1
         THEN relationships[0].article_id
         ELSE null END AS stored_article_id,

    CASE WHEN size(relationships) = 1
         THEN relationships[0].source_batch_key
         ELSE null END AS stored_batch,

    CASE WHEN size(relationships) = 1
         THEN relationships[0].predicate
         ELSE null END AS original_predicate,

    CASE WHEN size(relationships) = 1
         THEN relationships[0].effective_predicate
         ELSE null END AS existing_effective,

    CASE WHEN size(relationships) = 1
         THEN relationships[0].predicate_refinement_status
         ELSE null END AS existing_status,

    CASE WHEN size(relationships) = 1
         THEN relationships[0].predicate_refinement_version
         ELSE null END AS existing_version,

    CASE WHEN size(relationships) = 1
         THEN relationships[0].proposed_new_predicate
         ELSE null END AS existing_proposal
"""


APPLY_REFINEMENTS_QUERY = """
UNWIND $rows AS x

MATCH
    (s:CanonicalEntity {
        projection_key: x.subject_key
    })
    -[r:CANONICAL_FACT {
        projection_key: x.projection_key
    }]->
    (o:CanonicalEntity {
        projection_key: x.object_key
    })

WHERE r.fact_id = x.fact_id
  AND r.article_id = x.article_id
  AND r.source_batch_key = x.source_batch_key
  AND r.predicate = 'OTHER'
  AND (
      r.predicate_refinement_version IS NULL
      OR r.predicate_refinement_version = $version
  )

SET r.effective_predicate = x.effective_predicate,
    r.predicate_refinement_status = 'REFINED',
    r.predicate_refinement_version = $version,
    r.predicate_refinement_fact_id = x.fact_id,
    r.predicate_refinement_quote_sha256 = x.quote_sha256,
    r.predicate_refinement_context_sha256 = x.context_sha256

REMOVE r.proposed_new_predicate

RETURN count(r) AS updated
"""


APPLY_GAPS_QUERY = """
UNWIND $rows AS x

MATCH
    (s:CanonicalEntity {
        projection_key: x.subject_key
    })
    -[r:CANONICAL_FACT {
        projection_key: x.projection_key
    }]->
    (o:CanonicalEntity {
        projection_key: x.object_key
    })

WHERE r.fact_id = x.fact_id
  AND r.article_id = x.article_id
  AND r.source_batch_key = x.source_batch_key
  AND r.predicate = 'OTHER'
  AND (
      r.predicate_refinement_version IS NULL
      OR r.predicate_refinement_version = $version
  )

SET r.effective_predicate = 'OTHER',
    r.predicate_refinement_status = 'PROPOSED_NEW_PREDICATE',
    r.predicate_refinement_version = $version,
    r.predicate_refinement_fact_id = x.fact_id,
    r.proposed_new_predicate = x.proposed_new_predicate,
    r.predicate_refinement_quote_sha256 = x.quote_sha256,
    r.predicate_refinement_context_sha256 = x.context_sha256

RETURN count(r) AS updated
"""


APPLY_PROPOSALS_QUERY = """
UNWIND $rows AS x

MERGE (p:PredicateProposal {
    proposal_key: x.proposal_key
})

ON CREATE SET
    p.name = x.name,
    p.status = 'PROPOSED',
    p.proposal_version = $version,
    p.approved = false

SET p.supporting_facts = x.supporting_facts,
    p.distinct_articles = x.distinct_articles,
    p.fact_ids = x.fact_ids,
    p.article_ids = x.article_ids

RETURN count(p) AS updated
"""


ROLLBACK_QUERY = """
MATCH ()-[r:CANONICAL_FACT]->()
WHERE r.predicate_refinement_version = $version

REMOVE r.effective_predicate,
       r.predicate_refinement_status,
       r.predicate_refinement_version,
       r.predicate_refinement_fact_id,
       r.predicate_refinement_quote_sha256,
       r.predicate_refinement_context_sha256,
       r.proposed_new_predicate

RETURN count(r) AS restored
"""


ROLLBACK_PROPOSALS_QUERY = """
MATCH (p:PredicateProposal {
    proposal_version: $version,
    status: 'PROPOSED',
    approved: false
})
DETACH DELETE p
RETURN count(p) AS deleted
"""


def preflight(session, rows):
    if not rows:
        return 0

    records = session.run(
        PREFLIGHT_QUERY,
        rows=rows,
    ).data()

    require(
        len(records) == len(rows),
        "Incomplete preflight query result",
    )

    expected = {
        row["fact_id"]: row
        for row in rows
    }

    seen = set()

    for record in records:
        fid = record["fact_id"]
        require(
            fid not in seen and fid in expected,
            f"Unexpected preflight fact: {fid}",
        )
        seen.add(fid)

        row = expected[fid]

        require(
            record["matching_relations"] == 1,
            f"Canonical relation cardinality error: {fid}",
        )

        require(
            record["stored_fact_id"] == fid
            and record["stored_article_id"]
                == row["article_id"]
            and record["stored_batch"]
                == row["source_batch_key"],
            f"Canonical relation provenance mismatch: {fid}",
        )

        require(
            record["original_predicate"] == "OTHER",
            f"Original predicate changed: {fid}",
        )

        existing_version = record["existing_version"]

        require(
            existing_version in (None, VERSION),
            f"Conflicting overlay version: {fid}",
        )

        if existing_version == VERSION:
            require(
                record["existing_effective"]
                    == row["effective_predicate"]
                and record["existing_status"]
                    == row["status"]
                and record["existing_proposal"]
                    == row.get("proposed_new_predicate"),
                f"Conflicting existing overlay data: {fid}",
            )
        else:
            require(
                record["existing_effective"] is None
                and record["existing_status"] is None
                and record["existing_proposal"] is None,
                f"Unversioned overlay conflict: {fid}",
            )

    return len(records)


def apply_transaction(tx, plan):
    refinements = plan["refinements"]
    gaps = plan["gaps"]
    proposals = plan["proposals"]

    # Revalidate inside the write transaction.
    preflight(tx, refinements + gaps)

    refined = 0
    proposed_facts = 0
    proposed_labels = 0

    if refinements:
        refined = tx.run(
            APPLY_REFINEMENTS_QUERY,
            rows=refinements,
            version=VERSION,
        ).single()["updated"]

    if gaps:
        proposed_facts = tx.run(
            APPLY_GAPS_QUERY,
            rows=gaps,
            version=VERSION,
        ).single()["updated"]

    if proposals:
        proposed_labels = tx.run(
            APPLY_PROPOSALS_QUERY,
            rows=proposals,
            version=VERSION,
        ).single()["updated"]

    require(
        refined == len(refinements),
        "Refinement write cardinality mismatch",
    )
    require(
        proposed_facts == len(gaps),
        "Taxonomy gap write cardinality mismatch",
    )
    require(
        proposed_labels == len(proposals),
        "Predicate proposal cardinality mismatch",
    )

    return {
        "refined_relations": refined,
        "proposed_predicate_relations": proposed_facts,
        "catalog_proposals": proposed_labels,
    }


def rollback_transaction(tx):
    restored = tx.run(
        ROLLBACK_QUERY,
        version=VERSION,
    ).single()["restored"]

    deleted = tx.run(
        ROLLBACK_PROPOSALS_QUERY,
        version=VERSION,
    ).single()["deleted"]

    return {
        "restored_relations": restored,
        "deleted_proposals": deleted,
    }


def run(ctx: StepContext) -> StepResult:
    args = ctx.args
    candidates_dir = getattr(args, "candidates_dir", str(paths.step_dir(args, "candidate_generation")))
    verification_dir = getattr(args, "verification_dir", str(paths.step_dir(args, "semantic_verification")))
    out_dir = getattr(args, "out_dir", str(paths.step_dir(args, "overlay")))
    database = getattr(args, "database", "neo4j")
    apply = getattr(args, "apply", False)
    rollback = getattr(args, "rollback", False)
    
    try:
    
        require(
            sum((apply, rollback)) <= 1,
            "Choose either --apply or --rollback",
        )

        password = os.getenv("NEO4J_PASSWORD")
        require(password, "NEO4J_PASSWORD is required")

        driver = step_driver(args)

        try:
            driver.verify_connectivity()

            if rollback:
                with driver.session(
                    database=database,
                ) as session:
                    result = session.execute_write(
                        rollback_transaction
                    )

                print(json.dumps(
                    {
                        "version": VERSION,
                        "operation": "ROLLBACK",
                        **result,
                    },
                    indent=2,
                ))
                return StepResult.from_summary(result)

            data = load_inputs(
                candidates_dir,
                verification_dir,
            )

            plan = build_plan(data)
            rows = (
                plan["refinements"] + plan["gaps"]
            )

            with driver.session(
                database=database,
                default_access_mode=READ_ACCESS,
            ) as session:
                checked = session.execute_read(
                    preflight,
                    rows,
                )

            summary = {
                "version": VERSION,
                "operation": (
                    "APPLY" if apply else "DRY_RUN"
                ),
                "source_facts": plan["total_contexts"],
                "preflight_checked": checked,
                "refinements": len(plan["refinements"]),
                "verified_taxonomy_gap_facts": len(plan["gaps"]),
                "new_predicate_proposals": len(plan["proposals"]),
                "decision_counts": plan["decision_counts"],
                "excluded_counts": plan["excluded_counts"],
                "models": plan["input_models"],
                "checksum_validation": False,
                "original_predicates_preserved": True,
                "automatic_taxonomy_promotion": False,
                "graph_writes": 0,
            }

            if apply:
                with driver.session(
                    database=database,
                ) as session:
                    applied = session.execute_write(
                        apply_transaction,
                        plan,
                    )

                summary["applied"] = applied
                summary["graph_writes"] = (
                    applied["refined_relations"]
                    + applied["proposed_predicate_relations"]
                    + applied["catalog_proposals"]
                )

            write_json_stable(
                Path(out_dir) / "summary.json",
                summary,
            )

            print(json.dumps(
                summary,
                ensure_ascii=False,
                indent=2,
            ))

        finally:
            close_step_driver(args)
            
    except Exception as exc:
        print(f"ERROR: {exc}")
        return StepResult.fail(f"{type(exc).__name__}: {exc}")
    
    return StepResult.from_summary(summary)
