
"""Entity Resolution conservativa e indipendente dal dominio."""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
import unicodedata

from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

from neo4j import GraphDatabase

from presskg.entity_ontology import FAMILIES
from presskg.settings import CFG


VERSION = "entity-resolution-v2-strict"

# Classi che possono rappresentare identità
# concrete persistenti.
IDENTITY_KINDS = (
    set(FAMILIES["actor"])
    - {"PopulationGroup"}
)

# Sono suffissi giuridici generici.
# Servono SOLO a proporre candidati.
LEGAL_SUFFIXES = (
    ("s", "p", "a"),
    ("s", "r", "l"),
    ("spa",),
    ("srl",),
    ("srls",),
    ("ltd",),
    ("llc",),
    ("inc",),
    ("plc",),
)


def norm(name):
    text = unicodedata.normalize(
        "NFKD",
        str(name or "").casefold(),
    )

    text = "".join(
        c for c in text
        if not unicodedata.combining(c)
    )

    return " ".join(
        re.sub(r"[^\w]+", " ", text).split()
    )


def without_legal_suffix(key, kind):
    if kind != "Company":
        return key

    tokens = key.split()

    for suffix in LEGAL_SUFFIXES:
        if (
            len(tokens) > len(suffix)
            and tuple(tokens[-len(suffix):]) == suffix
        ):
            return " ".join(
                tokens[:-len(suffix)]
            )

    return key


def stable_cid(kind, key):
    digest = hashlib.sha256(
        f"{kind}|{key}".encode("utf-8")
    ).hexdigest()[:24]

    return f"canonical-entity:{digest}"


def csv_write(path, rows, columns):
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with path.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=columns,
        )

        writer.writeheader()
        writer.writerows(rows)


def build_candidates(entities):
    safe = []

    by_kind_name = defaultdict(list)
    by_name = defaultdict(list)

    for entity in entities:
        key = norm(entity.get("name"))

        if len(key) < 3:
            continue

        by_name[key].append(entity)

        if entity.get("kind") in IDENTITY_KINDS:
            by_kind_name[
                (entity["kind"], key)
            ].append(entity)

    # FASE A:
    # Soltanto nomi ortograficamente equivalenti,
    # stessa classe, almeno due entità.
    for (kind, key), members in sorted(
        by_kind_name.items()
    ):
        if len(members) < 2:
            continue

        # Non selezioniamo automaticamente
        # la denominazione OCR più lunga.
        representative = min(
            members,
            key=lambda e: (
                len(e["name"] or ""),
                (e["name"] or "").casefold(),
            ),
        )

        cid = stable_cid(kind, key)

        for entity in members:
            safe.append({
                "entity_id": entity["id"],
                "original_name": entity["name"],
                "kind": kind,
                "normalized_name": key,
                "canonical_id": cid,
                "canonical_name": (
                    representative["name"]
                ),
                "existing": (
                    entity.get("existing") or ""
                ),
                "method": "strict_orthographic",
                "status": "safe_candidate",
            })

    # FASE B:
    # Nomi uguali con classi differenti.
    # Soltanto diagnostica.
    conflicts = []

    for key, members in sorted(
        by_name.items()
    ):
        kinds = sorted({
            str(e.get("kind") or "")
            for e in members
        })

        if len(kinds) > 1:
            conflicts.append({
                "normalized_name": key,
                "kinds": " | ".join(kinds),
                "examples": " | ".join(
                    sorted({
                        e["name"]
                        for e in members
                    })
                )[:400],
            })

    # FASE C:
    # Varianti societarie e fuzzy.
    # Mai automaticamente applicate.
    suggestions = {}

    legal_blocks = defaultdict(list)
    fuzzy_blocks = defaultdict(set)

    for kind, key in by_kind_name:
        legal_root = without_legal_suffix(
            key,
            kind,
        )

        if kind == "Company":
            legal_blocks[
                (kind, legal_root)
            ].append(key)

        if len(key) >= 7:
            fuzzy_blocks[
                (kind, key[:4])
            ].add(key)

    # Differenze come:
    # ACEA / ACEA S.p.A.
    for (kind, _), keys in legal_blocks.items():
        distinct = sorted(set(keys))

        if len(distinct) > 15:
            continue

        for i, left in enumerate(distinct):
            for right in distinct[i + 1:]:
                if left != right:
                    suggestions[
                        (kind, left, right)
                    ] = (
                        "legal_suffix_variant",
                        1.0,
                    )

    # Varianti molto simili.
    for (kind, _), block in fuzzy_blocks.items():
        keys = sorted(block)

        if len(keys) > 80:
            continue

        for i, left in enumerate(keys):
            for right in keys[i + 1:]:
                # Non considerare equivalenti
                # importi, anni o numeri differenti.
                if (
                    re.findall(r"\d+", left)
                    != re.findall(r"\d+", right)
                ):
                    continue

                similarity = (
                    SequenceMatcher(
                        None,
                        left,
                        right,
                    ).ratio()
                )

                if similarity >= 0.94:
                    suggestions.setdefault(
                        (kind, left, right),
                        (
                            "fuzzy_candidate",
                            similarity,
                        ),
                    )

    review = []

    for (
        kind,
        left,
        right,
    ), (
        method,
        similarity,
    ) in sorted(suggestions.items()):
        review.append({
            "kind": kind,
            "name_a": left,
            "name_b": right,
            "method": method,
            "similarity": round(
                similarity,
                4,
            ),
            "approved": "",
        })

    return safe, conflicts, review


WRITE = """
UNWIND $rows AS row

MATCH (e:GlobalEntity {
    id:row.entity_id
})

MERGE (c:GraphNode:CanonicalEntity {
    id:row.canonical_id
})

ON CREATE SET
    c.name = row.canonical_name,
    c.kind = row.kind,
    c.normalized_name = row.normalized_name,
    c.resolution_version = $version

MERGE (e)-[r:RESOLVES_TO]->(c)

SET
    r.method = row.method,
    r.status = 'normalized_group',
    r.resolution_version = $version,
    e.canonical_id = row.canonical_id
"""


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--out-dir",
        default="output/resolution/entities-v2",
    )

    parser.add_argument(
        "--apply-safe",
        action="store_true",
    )

    args = parser.parse_args()

    output = Path(args.out_dir)
    output.mkdir(
        parents=True,
        exist_ok=True,
    )

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
            entities = session.run(
                """
                MATCH (e:GlobalEntity)

                OPTIONAL MATCH
                    (e)-[:RESOLVES_TO]->
                    (c:CanonicalEntity)

                RETURN
                    e.id AS id,
                    e.name AS name,
                    e.kind AS kind,
                    coalesce(
                        c.id,
                        e.canonical_id
                    ) AS existing
                """
            ).data()

            if (
                len({
                    e["id"]
                    for e in entities
                })
                != len(entities)
            ):
                raise RuntimeError(
                    "Una GlobalEntity possiede "
                    "più RESOLVES_TO"
                )

            safe, conflicts, review = (
                build_candidates(entities)
            )
            
            actor_kinds = set(FAMILIES["actor"])

            identity_conflicts = []
            typing_conflicts = []
            
            for conflict in conflicts:
                kinds = {value.strip() for value in conflict["kinds"].split("|")}
                
                if kinds.issubset(actor_kinds):
                    identity_conflicts.append(conflict)
                else:
                    typing_conflicts.append(conflict)

            csv_write(
                output / "safe_assignments.csv",
                safe,
                [
                    "entity_id",
                    "original_name",
                    "kind",
                    "normalized_name",
                    "canonical_id",
                    "canonical_name",
                    "existing",
                    "method",
                    "status",
                ],
            )

            csv_write(
                output / "type_conflicts.csv",
                conflicts,
                [
                    "normalized_name",
                    "kinds",
                    "examples",
                ],
            )
            
            csv_write(output / "identity_type_conflicts.csv", identity_conflicts, ["normalized_name", "kinds", "examples"])
            csv_write(output / "ontology_typing_conflicts.csv", typing_conflicts, ["normalized_name", "kinds", "examples"])

            csv_write(
                output / "review_candidates.csv",
                review,
                [
                    "kind",
                    "name_a",
                    "name_b",
                    "method",
                    "similarity",
                    "approved",
                ],
            )

            groups = {
                row["canonical_id"]
                for row in safe
            }

            print("GlobalEntity:", len(entities))
            print("Gruppi automatici:", len(groups))
            print("Nodi collegabili:", len(safe))
            print("Conflitti di tipo:", len(conflicts))
            print("Candidati manuali:", len(review))

            if not args.apply_safe:
                print("DRY RUN: nessuna scrittura")
                return

            apply_rows = [row for row in safe if row["kind"] != "Person"]
            print("Applicando solo entità non Person:", len(apply_rows))

            bad = [row for row in apply_rows if (row["existing"] and row["existing"] != row["canonical_id"])]
            

            if bad:
                raise RuntimeError(f"Risoluzioni già esistenti in conflitto: {len(bad)}")

            for start in range(0, len(apply_rows), 250):
                rows = apply_rows[start:start + 250]

                session.execute_write(
                    lambda tx: tx.run(
                        WRITE,
                        rows=rows,
                        version=VERSION,
                    ).consume()
                )

            print(
                "Collegamenti RESOLVES_TO:",
                len(apply_rows),
            )

    finally:
        driver.close()


if __name__ == "__main__":
    main()
