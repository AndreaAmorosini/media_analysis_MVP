
"""Canonical Fact Projection Dry-run.

Projects validated subject-predicate-object facts between
already materialized canonical entities.

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
from presskg.common.hashing import sha256_file
from presskg.common.io import iter_jsonl, read_json, write_jsonl
from presskg.resolution import paths
from presskg.resolution._common import close_step_driver, step_driver
from presskg.resolution.context import StepContext, StepResult



VERSION = "canonical-fact-projection-v1"
ENTITY_VERSION = "canonical-projection-v1"

IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*$")


def stable_key(batch, fact_id):
    payload = f"{VERSION}\0{batch}\0{fact_id}"
    return "cf-" + hashlib.sha256(
        payload.encode("utf-8")
    ).hexdigest()[:32]


def cypher_identifier(value):
    require(
        IDENTIFIER_RE.fullmatch(value) is not None,
        f"Invalid property name: {value}",
    )
    return f"`{value}`"


# -----------------------------------------------------------
# Source validation
# -----------------------------------------------------------

def load_inputs(gate_dir, dry_run_dir):
    gate_dir = Path(gate_dir)
    dry_run_dir = Path(dry_run_dir)

    gs = read_json(gate_dir / "summary.json")
    ds = read_json(dry_run_dir / "summary.json")

    require(
        gs["version"] == "materialization-safety-gate-v1",
        "Unexpected safety gate version",
    )
    require(
        ds["version"] == "canonical-projection-dry-run-v1",
        "Unexpected dry-run version",
    )
    require(
        gs["source_batch_key"] == ds["source_batch_key"],
        "Batch mismatch",
    )
    require(
        gs["source_dry_run_sha256"]
        == ds["checksums"]["projection_plan.jsonl"],
        "Safety gate/dry-run lineage mismatch",
    )
    require(
        gs["graph_writes"] == 0
        and ds["graph_writes"] == 0,
        "Unexpected source graph writes",
    )

    for root, summary in (
        (gate_dir, gs),
        (dry_run_dir, ds),
    ):
        for filename, digest in summary["checksums"].items():
            require(
                sha256_file(root / filename) == digest,
                f"Checksum mismatch: {root / filename}",
            )

    verified = {}

    for row in iter_jsonl(
        dry_run_dir / "graph_verification.jsonl"
    ):
        pid = row["profile_id"]
        require(pid not in verified, f"Duplicate profile: {pid}")
        verified[pid] = row

    selected_profiles = set()
    expected_keys = {}
    safe_identities = set()

    for item in iter_jsonl(
        gate_dir / "safe_projection_plan.jsonl"
    ):
        iid = item["proposed_identity_id"]
        require(iid not in safe_identities, "Duplicate identity")
        safe_identities.add(iid)

        require(
            item["safety_gate_status"] == "SAFE"
            and item["execute"] is False
            and item["source_batch_key"] == gs["source_batch_key"],
            f"Invalid SAFE identity: {iid}",
        )

        payload = (
            f"{ENTITY_VERSION}\0"
            f"{gs['source_batch_key']}\0{iid}"
        )
        canonical_key = "ce-" + hashlib.sha256(
            payload.encode("utf-8")
        ).hexdigest()[:32]

        for pid in item["validated_profile_ids"]:
            require(
                pid not in selected_profiles,
                f"Profile assigned twice: {pid}",
            )
            require(pid in verified, f"Unknown profile: {pid}")

            v = verified[pid]

            require(
                v["graph_status"] == "READY"
                and v["proposed_identity_id"] == iid
                and v["execute"] is False
                and not v["graph_reasons"],
                f"Unverified profile: {pid}",
            )

            selected_profiles.add(pid)
            expected_keys[pid] = canonical_key

    require(
        len(safe_identities) == gs["safe_identities"],
        "SAFE identity count mismatch",
    )
    require(
        len(selected_profiles) == gs["safe_mention_links"],
        "SAFE link count mismatch",
    )

    # A fact can be projected only if BOTH roles are approved.
    by_fact = defaultdict(dict)

    for pid in sorted(selected_profiles):
        row = verified[pid]
        role = row["role"]
        fid = row["fact_id"]

        require(
            role in {"subject", "object"},
            f"Unexpected role: {role}",
        )
        require(
            row["mention_id"] == f"{fid}:{role}",
            f"Invalid mention ID: {pid}",
        )
        require(
            role not in by_fact[fid],
            f"Duplicate {role} for fact {fid}",
        )

        by_fact[fid][role] = {
            "profile_id": pid,
            "mention_id": row["mention_id"],
            "canonical_key": expected_keys[pid],
            "article_id": row["article_id"],
        }

    pairs = []
    incomplete = []

    for fid, roles in sorted(by_fact.items()):
        if set(roles) != {"subject", "object"}:
            incomplete.append({
                "fact_id": fid,
                "approved_roles": sorted(roles),
                "reason": "ONLY_ONE_CANONICAL_ROLE",
            })
            continue

        subject = roles["subject"]
        obj = roles["object"]

        require(
            subject["article_id"] == obj["article_id"],
            f"Cross-article fact roles: {fid}",
        )

        pairs.append({
            "fact_id": fid,
            "article_id": subject["article_id"],
            "subject_mention_id": subject["mention_id"],
            "object_mention_id": obj["mention_id"],
            "subject_profile_id": subject["profile_id"],
            "object_profile_id": obj["profile_id"],
            "subject_key": subject["canonical_key"],
            "object_key": obj["canonical_key"],
        })

    require(
        len(pairs) + len(incomplete) == len(by_fact),
        "Fact coverage mismatch",
    )

    return gs, pairs, incomplete


# -----------------------------------------------------------
# Neo4j live fact validation
# -----------------------------------------------------------

def build_query(predicate_property):
    prop = cypher_identifier(predicate_property)

    return f"""
    UNWIND $rows AS x

    OPTIONAL MATCH (f:ValidatedFact {{id: x.fact_id}})
    WITH x, collect(DISTINCT f) AS fs

    OPTIONAL MATCH (
        sm:ValidatedMention {{id: x.subject_mention_id}}
    )
    WITH x, fs, collect(DISTINCT sm) AS sms

    OPTIONAL MATCH (
        om:ValidatedMention {{id: x.object_mention_id}}
    )
    WITH x, fs, sms, collect(DISTINCT om) AS oms

    WITH x, fs, sms, oms,
         fs[0] AS f, sms[0] AS sm, oms[0] AS om

    OPTIONAL MATCH (sm)-[:RESOLVED_AS]->(
        sc:CanonicalEntity
    )
    WITH x, fs, sms, oms, f, sm, om,
         collect(DISTINCT sc.projection_key) AS subject_keys

    OPTIONAL MATCH (om)-[:RESOLVED_AS]->(
        oc:CanonicalEntity
    )
    WITH x, fs, sms, oms, f, sm, om, subject_keys,
         collect(DISTINCT oc.projection_key) AS object_keys

    RETURN
        x.fact_id AS fact_id,
        size(fs) AS fact_count,
        size(sms) AS subject_mention_count,
        size(oms) AS object_mention_count,
        subject_keys,
        object_keys,

        CASE WHEN size(fs) = 1 AND size(sms) = 1
        THEN EXISTS {{ MATCH (sm)--(f) }}
        ELSE false END AS subject_fact_linked,

        CASE WHEN size(fs) = 1 AND size(oms) = 1
        THEN EXISTS {{ MATCH (om)--(f) }}
        ELSE false END AS object_fact_linked,

        sm.role AS subject_role,
        om.role AS object_role,
        sm.fact_id AS subject_fact_id,
        om.fact_id AS object_fact_id,

        f.article_id AS fact_article_id,
        f.{prop} AS predicate
    """


def query_batch(tx, query, rows):
    return [
        dict(record)
        for record in tx.run(query, rows=rows)
    ]


def validate_fact(expected, actual, check_graph_identity=True):
    """Verifica un fact proiettabile contro lo stato osservato nel grafo.

    ``check_graph_identity=False`` (dry-run): i percorsi
    ``(ValidatedMention)-[:RESOLVED_AS]->(CanonicalEntity)`` esistono solo
    dopo la materializzazione (apply): senza quest'ultima il controllo
    identita' sarebbe negativo per costruzione e va saltato.
    """
    problems = []

    for field in (
        "fact_count",
        "subject_mention_count",
        "object_mention_count",
    ):
        if actual[field] != 1:
            problems.append(f"INVALID_{field.upper()}")

    if not actual["subject_fact_linked"]:
        problems.append("SUBJECT_FACT_LINK_MISSING")

    if not actual["object_fact_linked"]:
        problems.append("OBJECT_FACT_LINK_MISSING")

    if check_graph_identity:
        if actual["subject_keys"] != [expected["subject_key"]]:
            problems.append("SUBJECT_IDENTITY_MISMATCH")

        if actual["object_keys"] != [expected["object_key"]]:
            problems.append("OBJECT_IDENTITY_MISMATCH")

    if (
        actual["subject_role"] is not None
        and str(actual["subject_role"]).lower() != "subject"
    ):
        problems.append("SUBJECT_ROLE_MISMATCH")

    if (
        actual["object_role"] is not None
        and str(actual["object_role"]).lower() != "object"
    ):
        problems.append("OBJECT_ROLE_MISMATCH")

    for role in ("subject", "object"):
        observed_id = actual[f"{role}_fact_id"]
        if (
            observed_id is not None
            and observed_id != expected["fact_id"]
        ):
            problems.append(
                f"{role.upper()}_FACT_ID_MISMATCH"
            )

    if (
        actual["fact_article_id"] is not None
        and actual["fact_article_id"] != expected["article_id"]
    ):
        problems.append("ARTICLE_ID_MISMATCH")

    predicate = actual["predicate"]

    if not isinstance(predicate, str) or not predicate.strip():
        problems.append("PREDICATE_MISSING")

    return sorted(set(problems))


# -----------------------------------------------------------
# Runner
# -----------------------------------------------------------

def run(ctx: StepContext) -> StepResult:
    args = ctx.args
    gate_dir = getattr(args, "gate_dir", str(paths.step_dir(args, "safety_gate")))
    dry_run_dir = getattr(args, "dry_run_dir", str(paths.step_dir(args, "dry_run")))
    out_dir = getattr(args, "out_dir", str(paths.step_dir(args, "fact_projection")))
    database = getattr(args, "database", "neo4j")
    batch_size = getattr(args, "batch_size", 250)
    predicate_property = getattr(args, "predicate_property", "predicate")
    
    try:
    
        require(batch_size > 0, "Invalid batch size")

        gs, pairs, incomplete = load_inputs(
            gate_dir,
            dry_run_dir,
        )

        query = build_query(predicate_property)

        password = os.environ.get("NEO4J_PASSWORD")
        require(password, "NEO4J_PASSWORD is required")

        driver = step_driver(args)

        projected = []
        rejected = []
        warning_counts = Counter()
        failure_counts = Counter()
        predicate_counts = Counter()

        try:
            driver.verify_connectivity()

            with driver.session(
                database=database,
                default_access_mode=READ_ACCESS,
            ) as session:
                for start in range(
                    0, len(pairs), batch_size
                ):
                    batch = pairs[start:start + batch_size]

                    results = session.execute_read(
                        query_batch, query, batch
                    )

                    lookup = {}
                    for result in results:
                        fid = result["fact_id"]
                        require(
                            fid not in lookup,
                            f"Duplicate result: {fid}",
                        )
                        lookup[fid] = result

                    require(
                        len(lookup) == len(batch),
                        "Incomplete Neo4j result",
                    )

                    for expected in batch:
                        fid = expected["fact_id"]

                        require(
                            fid in lookup,
                            f"Missing fact result: {fid}",
                        )

                        actual = lookup[fid]
                        problems = validate_fact(
                            expected, actual,
                            check_graph_identity=ctx.apply,
                        )

                        if problems:
                            failure_counts.update(problems)
                            rejected.append({
                                **expected,
                                "reasons": problems,
                            })
                            continue

                        predicate = actual["predicate"].strip()

                        warnings = []

                        if predicate.upper() == "OTHER":
                            warnings.append("GENERIC_PREDICATE")

                        if (
                            expected["subject_key"]
                            == expected["object_key"]
                        ):
                            warnings.append("CANONICAL_SELF_LOOP")

                        warning_counts.update(warnings)
                        predicate_counts[predicate] += 1

                        projected.append({
                            "operation": "PROPOSE_CANONICAL_FACT",
                            "projection_key": stable_key(
                                gs["source_batch_key"], fid
                            ),
                            "fact_id": fid,
                            "article_id": expected["article_id"],
                            "predicate": predicate,
                            "subject_key": expected["subject_key"],
                            "object_key": expected["object_key"],
                            "subject_mention_id":
                                expected["subject_mention_id"],
                            "object_mention_id":
                                expected["object_mention_id"],
                            "subject_profile_id":
                                expected["subject_profile_id"],
                            "object_profile_id":
                                expected["object_profile_id"],
                            "warnings": warnings,
                            "source_batch_key":
                                gs["source_batch_key"],
                            "projection_version": VERSION,
                            "execute": False,
                        })
        finally:
            close_step_driver(args)

        require(
            len(projected) + len(rejected) == len(pairs),
            "Incomplete fact validation",
        )

        require(
            len({
                row["projection_key"]
                for row in projected
            }) == len(projected),
            "Projection key collision",
        )

        out = Path(out_dir)
        require(not out.exists(), "Output already exists")
        out.mkdir(parents=True)

        plan_path = out / "canonical_fact_plan.jsonl"
        rejection_path = out / "rejected_facts.jsonl"
        incomplete_path = out / "incomplete_facts.jsonl"

        write_jsonl(plan_path, projected)
        write_jsonl(rejection_path, rejected)
        write_jsonl(incomplete_path, incomplete)

        summary = {
            "version": VERSION,
            "source_batch_key": gs["source_batch_key"],
            "source_safety_gate_sha256": sha256_file(
                Path(gate_dir)
                / "safe_projection_plan.jsonl"
            ),
            "safe_identities": gs["safe_identities"],
            "safe_mentions": gs["safe_mention_links"],
            "facts_with_at_least_one_safe_role":
                len(pairs) + len(incomplete),
            "facts_with_both_safe_roles": len(pairs),
            "facts_with_one_safe_role": len(incomplete),
            "projection_ready_facts": len(projected),
            "rejected_facts": len(rejected),
            "graph_identity_checks": (
                "ENABLED" if ctx.apply else "SKIPPED_DRY_RUN"
            ),
            "warning_counts":
                dict(sorted(warning_counts.items())),
            "failure_counts":
                dict(sorted(failure_counts.items())),
            "predicate_counts":
                dict(sorted(predicate_counts.items())),
            "graph_writes": 0,
            "llm_calls": 0,
            "checksums": {
                path.name: sha256_file(path)
                for path in (
                    plan_path,
                    rejection_path,
                    incomplete_path,
                )
            },
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
        
    except Exception as e:
        print(f"Unexpected error: {e}")
        return StepResult.fail(f"{type(e).__name__}: {e}")
    
    return StepResult.from_summary(summary)
    