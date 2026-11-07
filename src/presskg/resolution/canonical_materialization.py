
"""PressKG Phase 5B.2: Canonical Entity Materialization.

Materializes SAFE identities only.

Modes:
  --check   read-only preflight (default)
  --apply   explicit, idempotent graph writes
  --verify  read-only post-materialization audit

Never modifies ValidatedFact or Article nodes.
Never removes the existing Validated Fact View.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os

from collections import Counter
from pathlib import Path

from neo4j import GraphDatabase, READ_ACCESS


VERSION = "canonical-projection-v1"
GATE_VERSION = "materialization-safety-gate-v1"
CONFIRM = "APPLY_CANONICAL_PROJECTION"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(path):
    h = hashlib.sha256()

    with Path(path).open("rb") as f:
        for chunk in iter(
            lambda: f.read(1 << 20), b""
        ):
            h.update(chunk)

    return h.hexdigest()


def read_json(path):
    return json.loads(
        Path(path).read_text(encoding="utf-8")
    )


def read_jsonl(path):
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def projection_key(batch, iid):
    raw = f"{VERSION}\0{batch}\0{iid}"

    return "ce-" + hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:32]


def representative_name(surface_counts):
    require(
        bool(surface_counts),
        "Empty surface inventory",
    )

    return sorted(
        surface_counts,
        key=lambda name: (
            -surface_counts[name],
            -len(name),
            name.casefold(),
        ),
    )[0]


def load_plan(gate_dir, dry_run_dir):
    gate_dir = Path(gate_dir)
    dry_run_dir = Path(dry_run_dir)

    gs = read_json(gate_dir / "summary.json")
    ds = read_json(dry_run_dir / "summary.json")

    require(
        gs["version"] == GATE_VERSION,
        "Unexpected safety gate version",
    )
    require(
        ds["version"]
        == "canonical-projection-dry-run-v1",
        "Unexpected dry-run version",
    )
    require(
        gs["source_batch_key"]
        == ds["source_batch_key"],
        "Source batch mismatch",
    )
    require(
        gs["source_dry_run_sha256"]
        == ds["checksums"]["projection_plan.jsonl"],
        "Safety gate/dry-run lineage mismatch",
    )

    for root, summary in (
        (gate_dir, gs),
        (dry_run_dir, ds),
    ):
        require(
            summary["graph_writes"] == 0,
            "Mutated upstream output",
        )

        for filename, expected in (
            summary["checksums"].items()
        ):
            require(
                sha256(root / filename) == expected,
                f"Checksum mismatch: {root / filename}",
            )

    verified = {}

    for row in read_jsonl(
        dry_run_dir / "graph_verification.jsonl"
    ):
        pid = row["profile_id"]

        require(
            pid not in verified,
            f"Duplicate verification: {pid}",
        )
        verified[pid] = row

    plan = []
    seen_identities = set()
    seen_profiles = set()
    seen_mentions = set()

    for source in read_jsonl(
        gate_dir / "safe_projection_plan.jsonl"
    ):
        iid = source["proposed_identity_id"]

        require(
            iid not in seen_identities,
            f"Duplicate identity: {iid}",
        )
        seen_identities.add(iid)

        require(
            source["safety_gate_status"] == "SAFE"
            and source["execute"] is False
            and source["source_batch_key"]
                == gs["source_batch_key"],
            f"Invalid SAFE operation: {iid}",
        )

        facets = source["semantic_facet_counts"]

        require(
            len(facets) == 1,
            f"Mixed canonical facet: {iid}",
        )

        pids = source["validated_profile_ids"]

        require(
            len(pids) == len(set(pids))
            == source["validated_occurrences"],
            f"Invalid identity members: {iid}",
        )

        key = projection_key(
            gs["source_batch_key"], iid
        )

        links = []

        for pid in pids:
            require(
                pid not in seen_profiles,
                f"Duplicate projection profile: {pid}",
            )
            seen_profiles.add(pid)

            require(
                pid in verified,
                f"Missing graph verification: {pid}",
            )

            row = verified[pid]

            require(
                row["graph_status"] == "READY"
                and row["proposed_identity_id"] == iid
                and not row["graph_reasons"],
                f"Unverified identity member: {pid}",
            )

            mid = row["mention_id"]

            require(
                mid not in seen_mentions,
                f"Mention collision: {mid}",
            )
            seen_mentions.add(mid)

            require(
                row["role"] in {"subject", "object"}
                and mid == (
                    f'{row["fact_id"]}:{row["role"]}'
                ),
                f"Invalid mention identifier: {mid}",
            )

            links.append({
                "profile_id": pid,
                "mention_id": mid,
                "fact_id": row["fact_id"],
                "article_id": row["article_id"],
                "role": row["role"],
                "projection_key": key,
                "proposed_identity_id": iid,
            })

        plan.append({
            "projection_key": key,
            "proposed_identity_id": iid,
            "name": representative_name(
                source["surface_counts"]
            ),
            "semantic_facet": next(iter(facets)),
            "source_batch_key": gs["source_batch_key"],
            "projection_version": VERSION,
            "links": links,
        })

    require(
        len(plan) == gs["safe_identities"],
        "SAFE identity count mismatch",
    )

    require(
        len(seen_profiles) == gs["safe_mention_links"],
        "SAFE occurrence count mismatch",
    )

    return gs, plan


# ==========================================================
# Neo4j live validation
# ==========================================================

CHECK_QUERY = """
UNWIND $rows AS x

OPTIONAL MATCH (m:ValidatedMention {id: x.mention_id})
WITH x, collect(DISTINCT m) AS ms

OPTIONAL MATCH (f:ValidatedFact {id: x.fact_id})
WITH x, ms, collect(DISTINCT f) AS fs

WITH x, ms, fs, ms[0] AS m, fs[0] AS f

OPTIONAL MATCH (m)-[r:RESOLVED_AS]->(c:CanonicalEntity)
WITH x, ms, fs, m, f,
     collect(DISTINCT CASE
       WHEN r IS NULL THEN null
       ELSE {
         key: c.projection_key,
         version: r.projection_version
       }
     END) AS existing

RETURN
    x.profile_id AS profile_id,
    size(ms) AS mention_count,
    size(fs) AS fact_count,

    CASE
      WHEN size(ms) = 1 AND size(fs) = 1
      THEN EXISTS { MATCH (m)--(f) }
      ELSE false
    END AS fact_linked,

    m.fact_id AS mention_fact_id,
    m.role AS mention_role,
    m.article_id AS mention_article_id,
    f.article_id AS fact_article_id,

    CASE
      WHEN size(ms) = 1 AND size(fs) = 1
      THEN (
        EXISTS {
          MATCH (m)--(a:Article)
          WHERE a.id = x.article_id
        }
        OR EXISTS {
          MATCH (f)--(a:Article)
          WHERE a.id = x.article_id
        }
      )
      ELSE false
    END AS article_linked,

    existing AS existing_resolutions
"""


def validate_observation(source, obs):
    errors = []

    if obs["mention_count"] != 1:
        errors.append("MENTION_CARDINALITY")

    if obs["fact_count"] != 1:
        errors.append("FACT_CARDINALITY")

    if errors:
        return errors

    if not obs["fact_linked"]:
        errors.append("FACT_LINK_MISSING")

    if (
        obs["mention_fact_id"] is not None
        and obs["mention_fact_id"] != source["fact_id"]
    ):
        errors.append("FACT_ID_CONFLICT")

    if (
        obs["mention_role"] is not None
        and str(obs["mention_role"]).lower()
        != source["role"]
    ):
        errors.append("ROLE_CONFLICT")

    article_values = [
        value for value in (
            obs["mention_article_id"],
            obs["fact_article_id"],
        )
        if value is not None
    ]

    if any(
        value != source["article_id"]
        for value in article_values
    ):
        errors.append("ARTICLE_CONFLICT")

    if not (
        obs["article_linked"] or article_values
    ):
        errors.append("ARTICLE_NOT_VERIFIED")

    for existing in obs["existing_resolutions"]:
        if (
            existing["key"]
            != source["projection_key"]
            or existing["version"] != VERSION
        ):
            errors.append("EXISTING_RESOLUTION_CONFLICT")

    return sorted(set(errors))


def check_batch(tx, links, require_materialized=False):
    records = [
        dict(row)
        for row in tx.run(CHECK_QUERY, rows=links)
    ]

    require(
        len(records) == len(links),
        "Incomplete Neo4j response",
    )

    observed = {
        row["profile_id"]: row
        for row in records
    }

    require(
        len(observed) == len(links),
        "Duplicate preflight result",
    )

    problems = []

    for source in links:
        pid = source["profile_id"]

        require(
            pid in observed,
            f"Missing preflight result: {pid}",
        )

        obs = observed[pid]
        errors = validate_observation(source, obs)

        if require_materialized:
            expected = {
                "key": source["projection_key"],
                "version": VERSION,
            }

            if obs["existing_resolutions"] != [expected]:
                errors.append(
                    "EXPECTED_CANONICAL_LINK_MISSING"
                )

        if errors:
            problems.append({
                "profile_id": pid,
                "mention_id": source["mention_id"],
                "errors": sorted(set(errors)),
            })

    return problems


# ==========================================================
# Materialization
# ==========================================================

CREATE_NODES_QUERY = """
UNWIND $nodes AS x

MERGE (c:CanonicalEntity {
    projection_key: x.projection_key
})

ON CREATE SET
    c.proposed_identity_id = x.proposed_identity_id,
    c.name = x.name,
    c.semantic_facet = x.semantic_facet,
    c.source_batch_key = x.source_batch_key,
    c.projection_version = x.projection_version

WITH x, c
WHERE
    c.proposed_identity_id = x.proposed_identity_id
    AND c.source_batch_key = x.source_batch_key
    AND c.projection_version = x.projection_version
    AND c.semantic_facet = x.semantic_facet

RETURN count(c) AS matched
"""


CREATE_LINKS_QUERY = """
UNWIND $rows AS x

MATCH (m:ValidatedMention {id: x.mention_id})
MATCH (c:CanonicalEntity {
    projection_key: x.projection_key
})

MERGE (m)-[r:RESOLVED_AS]->(c)

ON CREATE SET
    r.projection_version = $version,
    r.profile_id = x.profile_id,
    r.source_batch_key = $batch

WITH r, x
WHERE
    r.projection_version = $version
    AND r.profile_id = x.profile_id
    AND r.source_batch_key = $batch

RETURN count(r) AS matched
"""


def materialize_batch(tx, nodes):
    links = [
        link
        for node in nodes
        for link in node["links"]
    ]

    # Revalidate within the same transaction.
    problems = check_batch(tx, links)

    require(
        not problems,
        "Neo4j preflight changed: "
        + json.dumps(problems[:5]),
    )

    result = tx.run(
        CREATE_NODES_QUERY,
        nodes=[
            {
                key: value
                for key, value in node.items()
                if key != "links"
            }
            for node in nodes
        ],
    ).single()

    require(
        result is not None
        and result["matched"] == len(nodes),
        "Canonical node conflict",
    )

    result = tx.run(
        CREATE_LINKS_QUERY,
        rows=links,
        version=VERSION,
        batch=nodes[0]["source_batch_key"],
    ).single()

    require(
        result is not None
        and result["matched"] == len(links),
        "Canonical relation conflict",
    )

    return len(nodes), len(links)


def plan_batches(plan, max_links):
    batch = []
    count = 0

    for node in plan:
        links = len(node["links"])

        if batch and count + links > max_links:
            yield batch
            batch = []
            count = 0

        batch.append(node)
        count += links

    if batch:
        yield batch


def check_constraint(session):
    result = session.run("""
        SHOW CONSTRAINTS
        YIELD type, labelsOrTypes, properties
        RETURN type, labelsOrTypes, properties
    """)

    return any(
        "CanonicalEntity" in row["labelsOrTypes"]
        and row["properties"] == ["projection_key"]
        and row["type"] in {
            "UNIQUENESS",
            "NODE_PROPERTY_UNIQUENESS",
            "NODE_KEY",
            "NODE_KEY_CONSTRAINT",
        }
        for row in result
    )


def validate_live(
    session, plan, batch_size,
    require_materialized=False,
):
    problems = []
    checked = 0

    for nodes in plan_batches(plan, batch_size):
        links = [
            link
            for node in nodes
            for link in node["links"]
        ]

        problems.extend(
            session.execute_read(
                check_batch,
                links,
                require_materialized,
            )
        )
        checked += len(links)

    return checked, problems

def count_materialized(session, plan):
    keys = [
        node["projection_key"] for node in plan
    ]

    row = session.run("""
        UNWIND $keys AS key
        OPTIONAL MATCH (
            c:CanonicalEntity {projection_key: key}
        )
        RETURN count(DISTINCT c) AS nodes
    """, keys=keys).single()

    node_count = row["nodes"]

    row = session.run("""
        MATCH (m:ValidatedMention)
              -[r:RESOLVED_AS]->
              (c:CanonicalEntity)
        WHERE c.projection_key IN $keys
          AND r.projection_version = $version
        RETURN count(r) AS links
    """, keys=keys, version=VERSION).single()

    return node_count, row["links"]


# ==========================================================
# CLI
# ==========================================================

def run(args):
    gs, plan = load_plan(
        args.gate_dir,
        args.dry_run_dir,
    )

    require(
        args.batch_size > 0,
        "Invalid batch size",
    )

    require(
        not (args.apply and args.verify),
        "Choose only one mode",
    )

    if args.apply:
        require(
            args.confirm == CONFIRM,
            "Explicit confirmation required",
        )

    password = os.environ.get("NEO4J_PASSWORD")
    require(password, "NEO4J_PASSWORD is required")

    driver = GraphDatabase.driver(
        os.environ.get(
            "NEO4J_URI",
            "bolt://localhost:7687",
        ),
        auth=(
            os.environ.get(
                "NEO4J_USER", "neo4j"
            ),
            password,
        ),
    )

    expected_nodes = len(plan)
    expected_links = sum(
        len(node["links"])
        for node in plan
    )

    report = {
        "version": VERSION,
        "source_batch_key": gs["source_batch_key"],
        "mode": (
            "APPLY" if args.apply
            else "VERIFY" if args.verify
            else "CHECK"
        ),
        "expected_nodes": expected_nodes,
        "expected_links": expected_links,
        "graph_writes": 0,
    }

    try:
        driver.verify_connectivity()

        with driver.session(
            database=args.database,
            default_access_mode=READ_ACCESS,
        ) as session:
            constraint_present = check_constraint(
                session
            )

            report["uniqueness_constraint_present"] = (
                constraint_present
            )

            checked, problems = validate_live(
                session,
                plan,
                args.batch_size,
                require_materialized=args.verify,
            )

            report["preflight_checked"] = checked
            report["preflight_errors"] = len(
                problems
            )
            report["preflight_examples"] = (
                problems[:20]
            )

            require(
                checked == expected_links,
                "Incomplete live validation",
            )
            require(
                not problems,
                "Live preflight failed",
            )

            if args.apply:
                require(
                    constraint_present,
                    "CanonicalEntity uniqueness "
                    "constraint is missing",
                )

        if args.apply:
            written_nodes = 0
            written_links = 0

            with driver.session(
                database=args.database
            ) as session:
                for batch in plan_batches(
                    plan, args.batch_size
                ):
                    n, l = session.execute_write(
                        materialize_batch, batch
                    )

                    written_nodes += n
                    written_links += l

            report["attempted_nodes"] = written_nodes
            report["attempted_links"] = written_links
            report["graph_writes"] = (
                written_nodes + written_links
            )

        with driver.session(
            database=args.database,
            default_access_mode=READ_ACCESS,
        ) as session:
            nodes, links = count_materialized(
                session, plan
            )

        report["actual_nodes"] = nodes
        report["actual_links"] = links
        report["complete"] = (
            nodes == expected_nodes
            and links == expected_links
        )

        if args.apply or args.verify:
            require(
                report["complete"],
                "Post-materialization count mismatch",
            )

    finally:
        driver.close()

        if args.out_dir:
            out = Path(args.out_dir)
            out.mkdir(parents=True, exist_ok=True)

            path = out / (
                f"{report['mode'].lower()}_summary.json"
            )
            path.write_text(
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
    parser = argparse.ArgumentParser(
        description=__doc__
    )

    parser.add_argument(
        "--gate-dir",
        default=(
            "output/resolution/"
            "materialization-safety-gate-v1"
        ),
    )
    parser.add_argument(
        "--dry-run-dir",
        default=(
            "output/resolution/"
            "canonical-projection-dry-run-v1"
        ),
    )
    parser.add_argument(
        "--out-dir",
        default=(
            "output/resolution/"
            "canonical-materialization-v1"
        ),
    )
    parser.add_argument(
        "--database", default="neo4j"
    )
    parser.add_argument(
        "--batch-size", type=int, default=250
    )
    parser.add_argument(
        "--apply", action="store_true"
    )
    parser.add_argument(
        "--verify", action="store_true"
    )
    parser.add_argument("--confirm")

    run(parser.parse_args())


if __name__ == "__main__":
    main()
