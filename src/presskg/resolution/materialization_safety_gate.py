
"""PressKG Phase 5B.1: Materialization Safety Gate.

Produces a conservative whitelist from a verified Phase 5A
projection plan. Fully read-only, deterministic and auditable.

No new entity resolution, LLM calls or Neo4j writes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path


VERSION = "materialization-safety-gate-v1"

SAFE = "SAFE"
REVIEW = "REVIEW_REQUIRED"

# Linguistic categories, never names of corpus entities.
ROLE_HEADS = {
    "sindaco", "vicesindaco", "assessore",
    "consigliere", "presidente", "vicepresidente",
    "ministro", "viceministro", "prefetto",
    "questore", "commissario", "direttore",
    "responsabile", "amministratore",
    "segretario", "governatore",
}

RELATIVE_QUALIFIERS = {
    "precedente", "precedenti", "successivo",
    "successiva", "attuale", "attuali",
    "passato", "passata", "uscente",
    "uscenti", "ex",
}

GENERIC_ACTIVITY_HEADS = {
    "lavori", "interventi", "opere",
    "attivita", "operazioni",
}

# Event classes potentially repeated at different dates.
REPEATABLE_EVENT_HEADS = {
    "inaugurazione", "incontro",
    "riunione", "conferenza",
    "cerimonia", "manifestazione",
    "assemblea", "sopralluogo",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(
            lambda: f.read(1024 * 1024), b""
        ):
            h.update(block)
    return h.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(
        encoding="utf-8"
    ))


def read_jsonl(path):
    with Path(path).open(encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path}:{n}: {exc}"
                ) from exc


def write_jsonl(path, rows):
    with Path(path).open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(
                row, ensure_ascii=False, sort_keys=True
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


def tokens(value):
    return normalize(value).split()


def load_inputs(dry_run_dir):
    root = Path(dry_run_dir)
    summary = read_json(root / "summary.json")

    require(
        summary["version"]
        == "canonical-projection-dry-run-v1",
        "Unsupported dry-run version",
    )
    require(
        summary["graph_writes"] == 0
        and summary["identity_merges"] == 0,
        "Upstream graph mutation detected",
    )

    for filename, digest in summary["checksums"].items():
        require(
            sha256(root / filename) == digest,
            f"Checksum mismatch: {filename}",
        )

    require(
        summary["graph_verification_status_counts"]
        .get("READY", 0)
        == summary["approved_input_occurrences"],
        "Not all input occurrences are READY",
    )
    require(
        summary["planned_mention_links"]
        == summary["approved_input_occurrences"],
        "Partial upstream projection",
    )
    require(
        summary["planned_canonical_entities"]
        == summary["approved_input_identities"],
        "Partial upstream identity coverage",
    )
    require(
        summary["incomplete_identities"] == 0,
        "Incomplete upstream identities",
    )

    verification = {}

    for row in read_jsonl(
        root / "graph_verification.jsonl"
    ):
        pid = row["profile_id"]
        require(
            pid not in verification,
            f"Duplicate graph verification: {pid}",
        )
        require(
            row["graph_status"] == "READY"
            and row["execute"] is False
            and not row["graph_reasons"],
            f"Unverified graph occurrence: {pid}",
        )

        evidence = row["graph_evidence"]

        require(
            evidence["mention_count"] == 1
            and evidence["fact_count"] == 1
            and evidence["fact_linked"] is True,
            f"Invalid graph evidence: {pid}",
        )

        verification[pid] = row

    plan = {}
    all_planned_profiles = set()

    for row in read_jsonl(
        root / "projection_plan.jsonl"
    ):
        iid = row["proposed_identity_id"]

        require(
            iid not in plan,
            f"Duplicate planned identity: {iid}",
        )
        require(
            row["operation"] == "PROPOSE_CANONICAL_ENTITY"
            and row["execute"] is False
            and row["source_batch_key"]
                == summary["source_batch_key"],
            f"Invalid projection operation: {iid}",
        )

        ids = row["validated_profile_ids"]
        require(
            len(ids) == len(set(ids))
            == row["validated_occurrences"],
            f"Duplicate identity membership: {iid}",
        )
        require(
            bool(ids),
            f"Empty planned identity: {iid}",
        )

        for pid in ids:
            require(
                pid not in all_planned_profiles,
                f"Profile planned twice: {pid}",
            )
            require(
                pid in verification
                and verification[pid][
                    "proposed_identity_id"
                ] == iid,
                f"Projection mismatch: {pid}",
            )
            all_planned_profiles.add(pid)

        plan[iid] = row

    require(
        all_planned_profiles == set(verification),
        "Plan/verification coverage mismatch",
    )
    require(
        len(plan) == summary[
            "planned_canonical_entities"
        ],
        "Planned identity count mismatch",
    )
    require(
        len(verification) == summary[
            "planned_mention_links"
        ],
        "Planned link count mismatch",
    )

    return summary, plan, verification


def family(plan_row):
    facets = plan_row["semantic_facet_counts"]
    require(
        len(facets) == 1,
        "Mixed semantic families in projection plan",
    )
    return next(iter(facets))


def main_surface(plan_row):
    counts = plan_row["surface_counts"]
    require(bool(counts), "Missing surface names")
    return sorted(
        counts,
        key=lambda value: (
            -counts[value],
            -len(value),
            normalize(value),
        ),
    )[0]


def classed_as_role_description(surface, facet):
    if facet not in {"PERSON", "ORGANIZATION"}:
        return False

    t = tokens(surface)

    return (
        len(t) >= 3
        and t[0] in ROLE_HEADS
        and t[1] in {
            "di", "del", "della", "dei",
            "degli", "delle",
        }
    )


def relative_institutional_reference(surface, facet):
    if facet != "ORGANIZATION":
        return False

    t = tokens(surface)
    return bool(
        set(t) & RELATIVE_QUALIFIERS
    )


def generic_project_reference(surface, source_kinds):
    if "Project" not in source_kinds:
        return False

    t = tokens(surface)
    if not t or t[0] not in GENERIC_ACTIVITY_HEADS:
        return False

    # Conservative indicator: no independently capitalized
    # named component following a generic activity head.
    raw = re.findall(
        r"[^\W\d_]+",
        str(surface or ""),
        flags=re.UNICODE,
    )

    return not any(
        word[0].isupper()
        for word in raw[1:]
    )


def repeatable_event_reference(surface, source_kinds):
    if "Event" not in source_kinds:
        return False

    t = tokens(surface)

    return bool(t and t[0] in REPEATABLE_EVENT_HEADS)


def evaluate_identity(plan_row, occurrences):
    facet = family(plan_row)
    surfaces = list(plan_row["surface_counts"])

    kinds = {
        row["source_kind"]
        for row in occurrences
    }
    articles = {
        row["article_id"]
        for row in occurrences
    }

    holds = set()
    advisories = set()
    
    if "Event" in kinds and "Project" in kinds:
        holds.add("MIXED_EVENT_PROJECT_IDENTITY")

    office_surfaces = [
        s for s in surfaces
        if classed_as_role_description(s, facet)
    ]

    if office_surfaces:
        stable_institutional_office = (
            facet == "ORGANIZATION"
            and kinds == {"PublicAuthority"}
            and len(articles) >= 2
            and not any(
                relative_institutional_reference(s, facet)
                for s in surfaces
            )
        )

        if stable_institutional_office:
            advisories.add(
                "INSTITUTIONAL_OFFICE_NOT_PERSON"
            )
        else:
            holds.add(
                "ROLE_OR_OFFICE_REFERENCE"
            )
            
    if any(
        relative_institutional_reference(s, facet)
        for s in surfaces
    ):
        holds.add("RELATIVE_INSTITUTIONAL_REFERENCE")

    if any(
        generic_project_reference(s, kinds)
        for s in surfaces
    ):
        holds.add("GENERIC_PROJECT_REFERENCE")

    if any(
        repeatable_event_reference(s, kinds)
        for s in surfaces
    ):
        # The dry-run has no validated event-instance identity
        # or event date. Do not infer one from article dates.
        holds.add("EVENT_INSTANCE_NOT_VERIFIED")

    for row in occurrences:
        advisories.update(row["semantic_warnings"])

    if "Project" in kinds and "descriptive_project_name" in advisories:
        advisories.add("PROJECT_NAME_REQUIRES_PERIODIC_AUDIT")

    if len(articles) < 2:
        advisories.add("SINGLE_ARTICLE_SUPPORT")

    return {
        "proposed_identity_id":
            plan_row["proposed_identity_id"],
        "status": REVIEW if holds else SAFE,
        "hold_reasons": sorted(holds),
        "advisory_warnings": sorted(advisories),
        "semantic_facet": facet,
        "source_kinds": sorted(kinds),
        "representative_surface":
            main_surface(plan_row),
        "surface_counts":
            plan_row["surface_counts"],
        "validated_articles": len(articles),
        "validated_occurrences": len(occurrences),
        "validated_profile_ids": sorted(
            row["profile_id"]
            for row in occurrences
        ),
        "execute": False,
    }


def run(args):
    summary, plan, verification = load_inputs(
        args.dry_run_dir
    )

    decisions = []
    safe_plan = []
    held = []
    status_counts = Counter()
    hold_counts = Counter()
    warning_counts = Counter()

    for iid, row in sorted(plan.items()):
        occurrences = [
            verification[pid]
            for pid in row["validated_profile_ids"]
        ]

        decision = evaluate_identity(
            row, occurrences
        )

        decisions.append(decision)
        status_counts[decision["status"]] += 1
        hold_counts.update(
            decision["hold_reasons"]
        )
        warning_counts.update(
            decision["advisory_warnings"]
        )

        if decision["status"] == SAFE:
            safe_plan.append({
                **row,
                "safety_gate_status": SAFE,
                "safety_gate_version": VERSION,
                "execute": False,
            })
        else:
            held.append(decision)

    safe_ids = {
        pid
        for row in safe_plan
        for pid in row["validated_profile_ids"]
    }
    held_ids = {
        pid
        for row in held
        for pid in row["validated_profile_ids"]
    }

    require(
        safe_ids.isdisjoint(held_ids),
        "Safe/held identity membership collision",
    )
    require(
        safe_ids | held_ids == set(verification),
        "Incomplete safety gate coverage",
    )
    require(
        len(safe_plan) + len(held) == len(plan),
        "Incomplete identity decision coverage",
    )

    out = Path(args.out_dir)
    require(
        not out.exists(),
        "Output directory already exists",
    )
    out.mkdir(parents=True)

    decisions_path = out / "identity_decisions.jsonl"
    safe_path = out / "safe_projection_plan.jsonl"
    held_path = out / "held_identities.jsonl"

    write_jsonl(decisions_path, decisions)
    write_jsonl(safe_path, safe_plan)
    write_jsonl(held_path, held)

    final_summary = {
        "version": VERSION,
        "source_batch_key":
            summary["source_batch_key"],
        "source_dry_run_sha256": sha256(
            Path(args.dry_run_dir)
            / "projection_plan.jsonl"
        ),
        "input_identities": len(plan),
        "input_occurrences": len(verification),
        "status_counts":
            dict(sorted(status_counts.items())),
        "hold_reason_counts":
            dict(sorted(hold_counts.items())),
        "advisory_warning_counts":
            dict(sorted(warning_counts.items())),
        "safe_identities": len(safe_plan),
        "safe_mention_links": len(safe_ids),
        "held_identities": len(held),
        "held_mention_links": len(held_ids),
        "graph_writes": 0,
        "identity_merges": 0,
        "canonical_entities_created": 0,
        "llm_calls": 0,
        "materialization_authorized": False,
        "checksums": {
            path.name: sha256(path)
            for path in (
                decisions_path,
                safe_path,
                held_path,
            )
        },
    }

    (out / "summary.json").write_text(
        json.dumps(
            final_summary,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )

    print(json.dumps(
        final_summary,
        ensure_ascii=False,
        indent=2,
    ))


def main():
    parser = argparse.ArgumentParser(
        description=__doc__
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
            "materialization-safety-gate-v1"
        ),
    )
    run(parser.parse_args())


if __name__ == "__main__":
    main()
