
#!/usr/bin/env python3
"""Validated Fact Projection v2.

Read-only preparation, audit and versioned Neo4j commit.

Sources:
- Validated Fact View v1
- Strict Entity Resolution v1
- Canonical Reconciliation v1
- Canonical Decision Gate v2
- Compound Fact Consistency Holds

No fact expansion, raw fact mutation, entity merge
or hold release.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json

from collections import Counter
from pathlib import Path


VERSION = "validated-fact-projection-v2"
SELECTED = "projected"

UNUSABLE_PREDICATES = {
    "",
    "OTHER",
    "UNKNOWN",
    "UNSPECIFIED",
}


# ============================================================
# Utilities
# ============================================================

def require(ok, message):
    if not ok:
        raise ValueError(message)


def sha(value):
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def read_json(path):
    return json.loads(
        Path(path).read_text(
            encoding="utf-8"
        )
    )


def read_jsonl(path):
    return [
        json.loads(line)
        for line in Path(path).read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip()
    ]


def jsonl(rows):
    return "".join(
        json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
        ) + "\n"
        for row in rows
    )


def immutable(path, content):
    path = Path(path)

    if path.exists():
        require(
            path.read_text(
                encoding="utf-8"
            ) == content,
            f"Immutable output differs: {path}",
        )
        return

    tmp = path.with_name(
        path.name + ".tmp"
    )

    tmp.write_text(
        content,
        encoding="utf-8",
    )

    tmp.replace(path)


def indexed(rows, field):
    result = {}

    for row in rows:
        key = row.get(field)

        require(
            isinstance(key, str)
            and bool(key)
            and key not in result,
            f"Missing or duplicate {field}: {key}",
        )

        result[key] = row

    return result


# ============================================================
# Source loading and provenance
# ============================================================

def load_inputs(args):
    strict = Path(args.strict_out)
    v1dir = Path(args.canonical_dir)
    v2dir = Path(args.canonical_v2_dir)
    compdir = Path(args.component_dir)

    baseline = read_json(
        strict / "summary.json"
    )

    assignments_file = (
        strict / "assignments.csv"
    )

    with assignments_file.open(
        encoding="utf-8",
        newline="",
    ) as handle:
        assignments = list(
            csv.DictReader(handle)
        )

    csv_hash = hashlib.sha256(
        assignments_file.read_bytes()
    ).hexdigest()

    v1 = read_json(
        v1dir / "summary.json"
    )

    v1_decisions = read_jsonl(
        v1dir
        / "canonical_reconciliation_decisions.jsonl"
    )

    v1_selected = [
        row for row in v1_decisions
        if row["status"]
        == "supported_surface_and_articles"
    ]

    v2 = read_json(
        v2dir / "summary.json"
    )

    v2_decisions = read_jsonl(
        v2dir / "gate_decisions.jsonl"
    )

    v2_selected = read_jsonl(
        v2dir / "selected_mappings.jsonl"
    )

    v2_manifest = read_jsonl(
        v2dir / "decision_manifest.jsonl"
    )

    hold_report = read_json(
        compdir / "summary.json"
    )

    holds = read_jsonl(
        compdir / "fact_holds.jsonl"
    )

    strict_key = (
        "validated-entity-resolution-v1:"
        f"{args.batch_id}:"
        f"{args.strict_resolution_id}"
    )

    view_key = (
        "validated-fact-view-v1:"
        f"{args.batch_id}"
    )

    compound_key = (
        "compound-verification-materialization-v1:"
        f"{args.batch_id}:"
        f"{args.strict_resolution_id}"
    )

    require(
        baseline["resolution_key"] == strict_key
        and len(assignments)
        == (
            baseline["mentions"]
            - baseline["abstained"]
        ),
        "Wrong strict baseline or assignments count",
    )

    require(
        v1["version"]
        == "canonical-reconciliation-v1"
        and v1["strict_key"] == strict_key
        and v1["strict_assignment_fingerprint"]
        == baseline["assignment_fingerprint"]
        and v1["strict_csv_sha256"] == csv_hash
        and sha(v1_decisions)
        == v1["decisions_fingerprint"]
        and sha(v1_selected)
        == v1["selected_fingerprint"]
        and len(v1_selected) == v1["selected"],
        "Canonical v1 provenance mismatch",
    )

    require(
        v2["version"]
        == "canonical-reconciliation-decision-gate-v2"
        and v2["strict_key"] == strict_key
        and v2["strict_assignment_fingerprint"]
        == baseline["assignment_fingerprint"]
        and v2["strict_csv_sha256"] == csv_hash
        and v2["canonical_v1_key"]
        == v1["batch_key"]
        and v2["canonical_v1_decisions_fingerprint"]
        == v1["decisions_fingerprint"]
        and sha(v2_decisions)
        == v2["decisions_fingerprint"]
        and sha(v2_selected)
        == v2["selected_fingerprint"]
        and sha(v2_manifest)
        == v2["manifest_fingerprint"]
        and len(v2_selected)
        == v2["selected_mappings"],
        "Canonical v2 provenance mismatch",
    )

    require(
        hold_report["hold_fingerprint"]
        == sha(holds)
        and len(holds)
        == hold_report["fact_holds"]
        and v1["hold_fingerprint"]
        == v2["hold_fingerprint"]
        == hold_report["hold_fingerprint"]
        and v2["holds_maintained"]
        == len(holds),
        "Fact holds provenance mismatch",
    )

    require(
        all(
            v2.get(key) == 0
            for key in (
                "node_merges",
                "facts_rewritten",
                "holds_released",
            )
        ),
        "Canonical v2 unexpectedly modified source facts",
    )

    by_mention = indexed(
        assignments,
        "mention_id",
    )

    by_fact = {}
    metadata = {}

    for row in assignments:
        require(
            row["role"] in (
                "subject",
                "object",
            ),
            "Invalid strict role",
        )

        require(
            row["mention_id"]
            == f"{row['fact_id']}:{row['role']}",
            "Unexpected strict mention ID convention",
        )

        roles = by_fact.setdefault(
            row["fact_id"], {}
        )

        require(
            row["role"] not in roles,
            "Duplicate strict fact role",
        )

        roles[row["role"]] = row

        gid = row["canonical_id"]

        item = (
            row["canonical_name"],
            row["kind"],
            row["scope"],
        )

        require(
            gid not in metadata
            or metadata[gid] == item,
            "Canonical ID has inconsistent metadata",
        )

        metadata[gid] = item

    v1_map = {}

    for row in v1_selected:
        source = row["source_id"]
        target = row["target_id"]

        require(
            source in metadata
            and target in metadata
            and source not in v1_map
            and source != target,
            "Invalid v1 canonical alias",
        )

        v1_map[source] = target

    v2_map = {}

    for row in v2_selected:
        source = row["source_id"]
        target = row["target_id"]

        require(
            source in metadata
            and target in metadata
            and source not in v2_map
            and source != target,
            "Invalid v2 canonical alias",
        )

        v2_map[source] = target

    require(
        not (
            set(v1_map)
            & set(v2_map)
        ),
        "Same alias source in v1 and v2",
    )

    combined = {
        **v1_map,
        **v2_map,
    }

    # Do not infer transitive identity.
    # Every selected mapping must point to a final root.

    require(
        all(
            target not in combined
            for target in combined.values()
        ),
        "Alias chain across v1/v2: "
        "explicit reconciliation required",
    )

    for source, target in combined.items():
        require(
            metadata[source][1:]
            == metadata[target][1:]
            and metadata[source][2] == "global",
            "Incompatible alias kind or scope",
        )

    held = {
        row["fact_id"]
        for row in holds
    }

    require(
        len(held) == len(holds),
        "Duplicate fact hold",
    )

    return {
        "baseline": baseline,
        "v1": v1,
        "v2": v2,
        "view_key": view_key,
        "strict_key": strict_key,
        "compound_key": compound_key,
        "hold_report": hold_report,
        "held": held,
        "by_fact": by_fact,
        "metadata": metadata,
        "v1_selected": v1_selected,
        "v2_selected": v2_selected,
        "aliases": combined,
        "csv_hash": csv_hash,
    }


# ============================================================
# Neo4j source preflight
# ============================================================

def preflight_graph(session, source):
    state = session.run(
        """
        MATCH (
            f:ValidatedFactBatch {
                id:$view,
                status:'ready'
            }
        )

        MATCH (
            s:EntityResolutionBatch {
                id:$strict,
                status:'ready'
            }
        )

        MATCH (
            h:CompoundVerificationBatch {
                id:$compound,
                status:'ready'
            }
        )

        MATCH (
            a:CanonicalReconciliationBatch {
                id:$v1,
                status:'ready'
            }
        )

        MATCH (
            b:CanonicalReconciliationV2Batch {
                id:$v2,
                status:'ready'
            }
        )

        RETURN
            properties(f) AS fact_batch,
            s.assignment_fingerprint AS strict_fp,
            s.expected_links AS strict_links,
            h.hold_fingerprint AS hold_fp,
            h.expected_holds AS holds,
            a.selected_fingerprint AS v1_fp,
            a.expected_mappings AS v1_count,
            b.selected_fingerprint AS v2_fp,
            b.expected_mappings AS v2_count
        """,
        view=source["view_key"],
        strict=source["strict_key"],
        compound=source["compound_key"],
        v1=source["v1"]["batch_key"],
        v2=source["v2"]["batch_key"],
    ).single()

    require(
        state is not None,
        "A required Neo4j batch is missing "
        "or not READY",
    )

    require(
        state["strict_fp"]
        == source["baseline"][
            "assignment_fingerprint"
        ]
        and state["strict_links"]
        == (
            source["baseline"]["mentions"]
            - source["baseline"]["abstained"]
        )
        and state["hold_fp"]
        == source["hold_report"][
            "hold_fingerprint"
        ]
        and state["holds"]
        == len(source["held"])
        and state["v1_fp"]
        == source["v1"]["selected_fingerprint"]
        and state["v1_count"]
        == len(source["v1_selected"])
        and state["v2_fp"]
        == source["v2"]["selected_fingerprint"]
        and state["v2_count"]
        == len(source["v2_selected"]),
        "Neo4j provenance/count mismatch",
    )

    # Check that the published alias edges match
    # the immutable decisions in both batches.

    for batch_key, wanted in (
        (
            source["v1"]["batch_key"],
            source["v1_selected"],
        ),
        (
            source["v2"]["batch_key"],
            source["v2_selected"],
        ),
    ):
        edges = session.run(
            """
            MATCH
            (s:ValidatedCanonicalEntity)-[
                r:CANONICAL_ALIAS_OF {
                    reconciliation_batch_id:$batch
                }
            ]->(t:ValidatedCanonicalEntity)

            RETURN
                s.id AS source,
                t.id AS target
            """,
            batch=batch_key,
        ).data()

        require(
            len(edges) == len(wanted)
            and {
                (
                    row["source"],
                    row["target"],
                )
                for row in edges
            } == {
                (
                    row["source_id"],
                    row["target_id"],
                )
                for row in wanted
            },
            f"Alias edges differ: {batch_key}",
        )

    # Verify that all held facts really remain
    # linked to the published consistency batch.

    held_nodes = session.run(
        """
        MATCH (
            h:CompoundVerificationBatch {
                id:$compound,
                status:'ready'
            }
        )-[:HAS_HOLD]->(
            :FactConsistencyHold
        )-[:HOLDS_FACT]->(
            f:ValidatedFact
        )

        RETURN collect(f.id) AS fact_ids
        """,
        compound=source["compound_key"],
    ).single()

    graph_held = (
        held_nodes["fact_ids"]
        if held_nodes
        else []
    )

    require(
        len(graph_held) == len(source["held"])
        and set(graph_held) == source["held"],
        "Neo4j hold endpoints differ "
        "from local fact holds",
    )

    return state["fact_batch"]


# ============================================================
# Validated fact retrieval
# ============================================================

def fetch_facts(session, view_key):
    rows = session.run(
        """
        MATCH (
            b:ValidatedFactBatch {
                id:$view,
                status:'ready'
            }
        )-[:HAS_FACT]->(
            f:ValidatedFact
        )

        RETURN
            f.id AS fact_id,
            f.article_id AS article_id,
            f.predicate AS predicate,
            f.subject_name AS subject_name,
            f.object_name AS object_name

        ORDER BY f.id
        """,
        view=view_key,
    ).data()

    result = indexed(
        rows,
        "fact_id",
    )

    require(
        len(rows) == len(result),
        "Duplicate fact in Validated Fact View",
    )

    for row in rows:
        require(
            isinstance(
                row["article_id"],
                str,
            )
            and bool(row["article_id"]),
            f"Missing article ID: {row['fact_id']}",
        )

    return rows


# ============================================================
# Deterministic projection
# ============================================================

def project(
    facts,
    source,
    expected_facts=34604,
):
    require(
        len(facts) == expected_facts,
        f"Validated Fact View contains "
        f"{len(facts)} facts, expected {expected_facts}",
    )

    fact_ids = {
        row["fact_id"]
        for row in facts
    }

    require(
        source["held"] <= fact_ids,
        "Fact hold references facts outside the view",
    )

    require(
        set(source["by_fact"]) <= fact_ids,
        "Strict assignments reference "
        "facts outside the view",
    )

    batch_key = (
        f"{VERSION}:"
        f"{source['strict_key'].split(':', 1)[1]}:"
        "canonical-v1-v2"
    )

    status_counts = Counter()

    decision_rows = []
    projections = []

    aliases = source["aliases"]

    for fact in facts:
        fid = fact["fact_id"]

        roles = source["by_fact"].get(
            fid, {}
        )

        subject = roles.get("subject")
        obj = roles.get("object")

        predicate = str(
            fact["predicate"] or ""
        ).strip()

        subj_id = (
            subject["canonical_id"]
            if subject
            else None
        )

        obj_id = (
            obj["canonical_id"]
            if obj
            else None
        )

        subj_final = (
            aliases.get(subj_id, subj_id)
            if subj_id
            else None
        )

        obj_final = (
            aliases.get(obj_id, obj_id)
            if obj_id
            else None
        )

        # Classification precedence is intentional.
        # Holds always override canonical mappings.

        if fid in source["held"]:
            status = "held_fact"

        elif not subject and not obj:
            status = "missing_both_strict_roles"

        elif not subject:
            status = "missing_strict_subject"

        elif not obj:
            status = "missing_strict_object"

        elif (
            predicate.upper()
            in UNUSABLE_PREDICATES
        ):
            status = "untyped_predicate"

        elif subj_final == obj_final:
            status = "self_edge_after_reconciliation"

        else:
            status = SELECTED

        # Verify assignments still belong to
        # this exact fact and source article.

        for role, assignment in (
            ("subject", subject),
            ("object", obj),
        ):
            if assignment is None:
                continue

            require(
                assignment["article_id"]
                == fact["article_id"]
                and assignment["fact_id"] == fid,
                f"Strict fact/article mismatch: "
                f"{fid} {role}",
            )

            require(
                assignment["scope"]
                in ("global", "article"),
                f"Unknown strict scope: {fid}",
            )

        decision = {
            "fact_id": fid,
            "article_id": fact["article_id"],
            "predicate": predicate,
            "original_subject_name":
                fact["subject_name"],
            "original_object_name":
                fact["object_name"],
            "strict_subject_id": subj_id,
            "strict_object_id": obj_id,
            "final_subject_id": subj_final,
            "final_object_id": obj_final,
            "status": status,
            "fact_hold_maintained":
                fid in source["held"],
            "fact_expansion_allowed": False,
        }

        decision_rows.append(decision)
        status_counts[status] += 1

        if status != SELECTED:
            continue

        projection = {
            "projection_id": (
                "pvf2-"
                + sha([
                    batch_key,
                    fid,
                ])[:32]
            ),
            "edge_id": (
                "pfe2-"
                + sha([
                    batch_key,
                    fid,
                    "edge",
                ])[:32]
            ),
            "batch_id": batch_key,
            "fact_id": fid,
            "article_id": fact["article_id"],
            "predicate": predicate,
            "subject_id": subj_final,
            "object_id": obj_final,
            "strict_subject_id": subj_id,
            "strict_object_id": obj_id,
            "subject_scope": subject["scope"],
            "object_scope": obj["scope"],
            "has_article_scoped_endpoint": (
                subject["scope"] == "article"
                or obj["scope"] == "article"
            ),
            "subject_alias_applied":
                subj_id != subj_final,
            "object_alias_applied":
                obj_id != obj_final,
        }

        projections.append(projection)

    require(
        len({
            row["projection_id"]
            for row in projections
        }) == len(projections),
        "Duplicate projection ID",
    )

    require(
        len({
            row["edge_id"]
            for row in projections
        }) == len(projections),
        "Duplicate relationship ID",
    )

    require(
        all(
            row["subject_id"]
            != row["object_id"]
            for row in projections
        ),
        "Self edge in projected facts",
    )

    report = {
        "version": VERSION,
        "batch_key": batch_key,
        "view_key": source["view_key"],
        "strict_key": source["strict_key"],
        "strict_assignment_fingerprint":
            source["baseline"][
                "assignment_fingerprint"
            ],
        "strict_csv_sha256":
            source["csv_hash"],
        "canonical_v1_key":
            source["v1"]["batch_key"],
        "canonical_v1_fingerprint":
            source["v1"][
                "selected_fingerprint"
            ],
        "canonical_v2_key":
            source["v2"]["batch_key"],
        "canonical_v2_fingerprint":
            source["v2"][
                "selected_fingerprint"
            ],
        "hold_fingerprint":
            source["hold_report"][
                "hold_fingerprint"
            ],
        "holds_maintained":
            len(source["held"]),
        "validated_facts":
            len(facts),
        "projected_facts":
            len(projections),
        "excluded_facts":
            len(facts) - len(projections),
        "status_counts": dict(sorted(
            status_counts.items()
        )),
        "projection_fingerprint":
            sha(projections),
        "decision_fingerprint":
            sha(decision_rows),
        "raw_facts_modified": 0,
        "holds_released": 0,
        "entity_merges": 0,
    }

    return (
        decision_rows,
        projections,
        report,
    )


# ============================================================
# Local audit
# ============================================================

def local_audit(args):
    folder = Path(args.out_dir)

    decisions = read_jsonl(
        folder / "projection_decisions.jsonl"
    )

    projections = read_jsonl(
        folder / "projected_facts.jsonl"
    )

    report = read_json(
        folder / "summary.json"
    )

    require(
        report["version"] == VERSION
        and report["decision_fingerprint"]
        == sha(decisions)
        and report["projection_fingerprint"]
        == sha(projections),
        "Projection output fingerprint mismatch",
    )

    require(
        report["validated_facts"]
        == len(decisions)
        and report["projected_facts"]
        == len(projections)
        and report["excluded_facts"]
        == len(decisions) - len(projections),
        "Projection counts mismatch",
    )

    require(
        report["status_counts"]
        == dict(sorted(
            Counter(
                row["status"]
                for row in decisions
            ).items()
        )),
        "Projection status counts mismatch",
    )

    require(
        len({
            row["fact_id"]
            for row in decisions
        }) == len(decisions)
        and len({
            row["fact_id"]
            for row in projections
        }) == len(projections),
        "Duplicate fact ID",
    )

    projected_ids = {
        row["fact_id"]
        for row in projections
    }

    require(
        projected_ids == {
            row["fact_id"]
            for row in decisions
            if row["status"] == SELECTED
        },
        "Projected facts differ from eligible decisions",
    )

    require(
        all(
            row["fact_id"] not in projected_ids
            for row in decisions
            if row["fact_hold_maintained"]
        ),
        "A held fact was projected",
    )

    require(
        all(
            row["subject_id"] != row["object_id"]
            and row["predicate"].upper()
            not in UNUSABLE_PREDICATES
            for row in projections
        ),
        "Unsafe projection",
    )

    require(
        all(
            report[key] == 0
            for key in (
                "raw_facts_modified",
                "holds_released",
                "entity_merges",
            )
        ),
        "Original graph mutation indicated",
    )

    return projections, report


# ============================================================
# Neo4j projection audit
# ============================================================

def graph_audit(
    session,
    projections,
    report,
    status="building",
):
    rows = session.run(
        """
        MATCH (
            b:CanonicalFactProjectionBatch {
                id:$key,
                status:$status
            }
        )-[:HAS_PROJECTED_FACT]->(
            p:ProjectedValidatedFact
        )

        OPTIONAL MATCH
        (p)-[:PROJECTION_OF]->(
            f:ValidatedFact
        )

        OPTIONAL MATCH
        (p)-[:SUBJECT_ENTITY]->(
            s:ValidatedCanonicalEntity
        )

        OPTIONAL MATCH
        (p)-[:OBJECT_ENTITY]->(
            o:ValidatedCanonicalEntity
        )

        RETURN
            properties(p) AS props,
            collect(DISTINCT f.id) AS facts,
            collect(DISTINCT s.id) AS subjects,
            collect(DISTINCT o.id) AS objects
        """,
        key=report["batch_key"],
        status=status,
    ).data()

    wanted = indexed(
        projections,
        "projection_id",
    )

    require(
        len(rows) == len(wanted),
        "Neo4j projected fact count differs",
    )

    seen = set()

    for record in rows:
        props = record["props"]

        require(
            props is not None
            and props["projection_id"] in wanted,
            "Unexpected projected fact node",
        )

        pid = props["projection_id"]
        projection = wanted[pid]

        require(
            pid not in seen,
            "Duplicate projected graph node",
        )

        seen.add(pid)

        require(
            all(
                props.get(key) == value
                for key, value
                in projection.items()
            )
            and record["facts"]
            == [projection["fact_id"]]
            and record["subjects"]
            == [projection["subject_id"]]
            and record["objects"]
            == [projection["object_id"]],
            "Projected node or links differ "
            "from manifest",
        )

    edges = session.run(
        """
        MATCH
        (s:ValidatedCanonicalEntity)-[
            r:PROJECTED_FACT {
                batch_id:$key
            }
        ]->(o:ValidatedCanonicalEntity)

        RETURN
            s.id AS source,
            o.id AS target,
            r.id AS edge_id,
            r.fact_id AS fact_id,
            r.predicate AS predicate,
            r.projection_id AS projection_id
        """,
        key=report["batch_key"],
    ).data()

    expected = indexed(
        projections,
        "edge_id",
    )

    require(
        len(edges) == len(expected),
        "Neo4j canonical edge count differs",
    )

    for edge in edges:
        projection = expected.get(
            edge["edge_id"]
        )

        require(
            projection is not None
            and edge["source"]
            == projection["subject_id"]
            and edge["target"]
            == projection["object_id"]
            and edge["fact_id"]
            == projection["fact_id"]
            and edge["predicate"]
            == projection["predicate"]
            and edge["projection_id"]
            == projection["projection_id"],
            "Projected canonical edge "
            "differs from manifest",
        )


# ============================================================
# Neo4j materialization
# ============================================================

def commit(
    args,
    source,
    projections,
    report,
):
    from neo4j import GraphDatabase
    from presskg.settings import CFG

    driver = GraphDatabase.driver(
        CFG.neo4j_uri,
        auth=(
            CFG.neo4j_user,
            CFG.neo4j_password,
        ),
    )

    try:
        with driver.session(
            database=CFG.neo4j_database
        ) as session:

            preflight_graph(
                session,
                source,
            )

            for statement in (
                """
                CREATE CONSTRAINT
                canonical_fact_projection_batch_id
                IF NOT EXISTS
                FOR (b:CanonicalFactProjectionBatch)
                REQUIRE b.id IS UNIQUE
                """,
                """
                CREATE CONSTRAINT
                projected_validated_fact_id
                IF NOT EXISTS
                FOR (p:ProjectedValidatedFact)
                REQUIRE p.projection_id IS UNIQUE
                """,
            ):
                session.run(
                    statement
                ).consume()

            props = {
                "id": report["batch_key"],
                "version": VERSION,
                "view_key": report["view_key"],
                "strict_key": report["strict_key"],
                "projection_fingerprint":
                    report["projection_fingerprint"],
                "decision_fingerprint":
                    report["decision_fingerprint"],
                "expected_facts":
                    report["projected_facts"],
                "hold_fingerprint":
                    report["hold_fingerprint"],
                "canonical_v1_key":
                    report["canonical_v1_key"],
                "canonical_v2_key":
                    report["canonical_v2_key"],
                "status": "building",
            }

            state = session.run(
                """
                MERGE (
                    b:CanonicalFactProjectionBatch {
                        id:$id
                    }
                )

                ON CREATE SET b += $props

                RETURN properties(b) AS props
                """,
                id=report["batch_key"],
                props=props,
            ).single()["props"]

            require(
                all(
                    state.get(key) == value
                    for key, value in props.items()
                    if key != "status"
                )
                and state.get("status")
                in ("building", "ready"),
                "Existing projection batch differs",
            )

            if state["status"] == "ready":
                graph_audit(
                    session,
                    projections,
                    report,
                    status="ready",
                )

                print(
                    "ALREADY READY: "
                    "projection graph audit passed"
                )
                return

            for start in range(
                0,
                len(projections),
                args.batch_size,
            ):
                batch = projections[
                    start:start + args.batch_size
                ]

                def transaction(tx):
                    existing = tx.run(
                        """
                        UNWIND $rows AS row

                        OPTIONAL MATCH (
                            p:ProjectedValidatedFact {
                                projection_id:row.projection_id
                            }
                        )

                        RETURN
                            row.projection_id AS id,
                            properties(p) AS props
                        """,
                        rows=batch,
                    ).data()

                    require(
                        len(existing) == len(batch),
                        "Projection preflight size mismatch",
                    )

                    by_id = indexed(
                        batch,
                        "projection_id",
                    )

                    for old in existing:
                        if old["props"] is not None:
                            require(
                                all(
                                    old["props"].get(key)
                                    == value
                                    for key, value
                                    in by_id[old["id"]].items()
                                ),
                                "Existing projection properties differ",
                            )

                    result = tx.run(
                        """
                        UNWIND $rows AS row

                        MATCH (
                            b:CanonicalFactProjectionBatch {
                                id:$key,
                                status:'building'
                            }
                        )

                        MATCH (
                            v:ValidatedFactBatch {
                                id:$view,
                                status:'ready'
                            }
                        )-[:HAS_FACT]->(
                            f:ValidatedFact {
                                id:row.fact_id
                            }
                        )

                        MATCH (
                            er:EntityResolutionBatch {
                                id:$strict,
                                status:'ready'
                            }
                        )-[:HAS_ENTITY]->(
                            s:ValidatedCanonicalEntity {
                                id:row.subject_id
                            }
                        )

                        MATCH (
                            er
                        )-[:HAS_ENTITY]->(
                            o:ValidatedCanonicalEntity {
                                id:row.object_id
                            }
                        )

                        MERGE (
                            p:ProjectedValidatedFact {
                                projection_id:row.projection_id
                            }
                        )

                        ON CREATE SET p += row

                        MERGE (b)-[:HAS_PROJECTED_FACT]->(p)
                        MERGE (p)-[:PROJECTION_OF]->(f)
                        MERGE (p)-[:SUBJECT_ENTITY]->(s)
                        MERGE (p)-[:OBJECT_ENTITY]->(o)

                        MERGE (
                            s
                        )-[r:PROJECTED_FACT {
                            id:row.edge_id
                        }]->(o)

                        ON CREATE SET
                            r.batch_id=$key,
                            r.fact_id=row.fact_id,
                            r.predicate=row.predicate,
                            r.projection_id=row.projection_id

                        RETURN count(p) AS n
                        """,
                        rows=batch,
                        key=report["batch_key"],
                        view=report["view_key"],
                        strict=report["strict_key"],
                    ).single()

                    require(
                        result is not None
                        and result["n"] == len(batch),
                        "Incomplete projection write, "
                        "transaction rolled back",
                    )

                session.execute_write(
                    transaction
                )

            graph_audit(
                session,
                projections,
                report,
            )

            ready = session.run(
                """
                MATCH (
                    b:CanonicalFactProjectionBatch {
                        id:$key,
                        status:'building'
                    }
                )

                WHERE b.projection_fingerprint=$fp

                SET b.status='ready'

                RETURN b.status AS status
                """,
                key=report["batch_key"],
                fp=report["projection_fingerprint"],
            ).single()

            require(
                ready is not None
                and ready["status"] == "ready",
                "Failed to mark projection ready",
            )

            print(
                f"READY: {len(projections)} projected facts. "
                "No hold releases."
            )

    finally:
        driver.close()


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description=__doc__
    )

    parser.add_argument(
        "--batch-id",
        default="qwen38-gemma31-gemma12-v1",
    )

    parser.add_argument(
        "--strict-resolution-id",
        default="strict-v1",
    )

    parser.add_argument(
        "--strict-out",
        default=(
            "output/resolution/"
            "validated-entities-v1"
        ),
    )

    parser.add_argument(
        "--component-dir",
        default=(
            "output/resolution/"
            "component-verification-v1"
        ),
    )

    parser.add_argument(
        "--canonical-dir",
        default=(
            "output/resolution/"
            "canonical-reconciliation-v1"
        ),
    )

    parser.add_argument(
        "--canonical-v2-dir",
        default=(
            "output/resolution/"
            "canonical-decision-gate-v2"
        ),
    )

    parser.add_argument(
        "--out-dir",
        default=(
            "output/resolution/"
            "validated-fact-projection-v2"
        ),
    )

    parser.add_argument(
        "--expected-facts",
        type=int,
        default=34604,
    )

    parser.add_argument(
        "--batch-size",
        type=int,
        default=200,
    )

    parser.add_argument(
        "--audit",
        action="store_true",
    )

    parser.add_argument(
        "--commit",
        action="store_true",
    )

    parser.add_argument(
        "--verify-graph",
        action="store_true",
    )

    args = parser.parse_args()

    require(
        args.expected_facts > 0
        and args.batch_size > 0,
        "Invalid expected facts or batch size",
    )

    source = load_inputs(args)

    if (
        args.audit
        or args.commit
        or args.verify_graph
    ):
        projections, report = local_audit(
            args
        )

        require(
            report["strict_key"]
            == source["strict_key"]
            and report["view_key"]
            == source["view_key"]
            and report["hold_fingerprint"]
            == source["hold_report"][
                "hold_fingerprint"
            ]
            and report["canonical_v1_fingerprint"]
            == source["v1"]["selected_fingerprint"]
            and report["canonical_v2_fingerprint"]
            == source["v2"]["selected_fingerprint"]
            and report["strict_csv_sha256"]
            == source["csv_hash"],
            "Projection audit differs from source inputs",
        )

        if args.commit:
            commit(
                args,
                source,
                projections,
                report,
            )

        elif args.verify_graph:
            from neo4j import GraphDatabase
            from presskg.settings import CFG

            driver = GraphDatabase.driver(
                CFG.neo4j_uri,
                auth=(
                    CFG.neo4j_user,
                    CFG.neo4j_password,
                ),
            )

            try:
                with driver.session(
                    database=CFG.neo4j_database
                ) as session:
                    preflight_graph(
                        session,
                        source,
                    )

                    graph_audit(
                        session,
                        projections,
                        report,
                        status="ready",
                    )

                print(
                    "GRAPH AUDIT PASSED"
                )

            finally:
                driver.close()

        else:
            print(
                "LOCAL AUDIT PASSED"
            )

        print(
            json.dumps(
                report,
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    from neo4j import GraphDatabase
    from presskg.settings import CFG

    driver = GraphDatabase.driver(
        CFG.neo4j_uri,
        auth=(
            CFG.neo4j_user,
            CFG.neo4j_password,
        ),
    )

    try:
        with driver.session(
            database=CFG.neo4j_database
        ) as session:
            batch_props = preflight_graph(
                session,
                source,
            )

            facts = fetch_facts(
                session,
                source["view_key"],
            )

            expected_from_graph = (
                batch_props.get("expected_facts")
            )

            if expected_from_graph is not None:
                require(
                    expected_from_graph == len(facts),
                    "ValidatedFactBatch count mismatch",
                )

    finally:
        driver.close()

    decisions, projections, report = project(
        facts,
        source,
        args.expected_facts,
    )

    output = Path(args.out_dir)

    output.mkdir(
        parents=True,
        exist_ok=True,
    )

    immutable(
        output / "projection_decisions.jsonl",
        jsonl(decisions),
    )

    immutable(
        output / "projected_facts.jsonl",
        jsonl(projections),
    )

    immutable(
        output / "summary.json",
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ) + "\n",
    )

    print(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
        )
    )

    print(
        "READ ONLY: no graph writes, "
        "no fact expansion, no hold releases"
    )


if __name__ == "__main__":
    main()
