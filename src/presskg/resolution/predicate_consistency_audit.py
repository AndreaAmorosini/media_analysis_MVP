
"""PressKG 6B.3.2: deterministic semantic consistency audit.

Read-only audit of predicate candidates and semantic
verifications. No LLM calls, no Neo4j writes.

Flags possible semantic risks, without approving,
rejecting or changing any candidate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re

from collections import Counter, defaultdict
from pathlib import Path


VERSION = "predicate-consistency-audit-v1"

POSITIVE_DECISIONS = {"RECLASSIFY", "TAXONOMY_GAP"}
ABSTENTIONS = {"KEEP_OTHER", "INSUFFICIENT_EVIDENCE"}

# Diagnostic linguistic signals, not validation rules.
# These expressions never change a semantic verdict.
ACTION_ONLY_SIGNALS = (
    r"\b(?:azione|azioni)\s+(?:attiva|attive)\b",
    r"\b(?:agire|agisce|agiscono)\b",
    r"\b(?:azione|attività)\s+(?:operativa|concreta)\b",
    r"\bnon\s+(?:compie|svolge|esercita)\b",
)

STATIC_RELATION_SIGNALS = (
    r"\b(?:geografico|geografica|spaziale|territoriale)\b",
    r"\b(?:ubicazione|localizzazione|collocazione)\b",
    r"\b(?:normativo|normativa|giuridico|istituzionale)\b",
    r"\b(?:appartenenza|proprietà|titolarità)\b",
)

DIRECTION_SIGNALS = (
    r"\b(?:invers[ao]|invertit[ao]|direzione opposta)\b",
    r"\b(?:soggetto|oggetto)\s+(?:invertito|invertita)\b",
)

TAXONOMY_SIGNALS = (
    r"\b(?:predicato|categoria|tassonomia)\b"
    r".{0,90}\b(?:mancante|assente|non presente)\b",
    r"\b(?:nessun predicato|nuovo predicato)\b",
    r"\b(?:non rappresentat[ao]|non copert[ao])\b",
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    h = hashlib.sha256()

    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)

    return h.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_jsonl(path):
    with Path(path).open(encoding="utf-8") as f:
        for line_number, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid JSONL: {path}:{line_number}"
                ) from exc


def write_json(path, data):
    Path(path).write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path, rows):
    with Path(path).open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(
                json.dumps(
                    row,
                    ensure_ascii=False,
                    sort_keys=True,
                ) + "\n"
            )


def validate_checksums(root):
    summary = read_json(root / "summary.json")

    require(
        summary.get("graph_writes") == 0,
        f"Unexpected graph writes in {root}",
    )

    for filename, expected in summary["checksums"].items():
        path = root / filename

        require(
            path.is_file(),
            f"Missing input: {path}",
        )

        require(
            digest(path) == expected,
            f"Checksum mismatch: {path}",
        )

    return summary


def latest_records(path):
    latest = {}
    history = Counter()

    for record in read_jsonl(path):
        fid = record["fact_id"]
        latest[fid] = record
        history[fid] += 1

    return latest, history


def matches_any(text, patterns):
    if not isinstance(text, str):
        return False

    return any(
        re.search(pattern, text, flags=re.IGNORECASE)
        for pattern in patterns
    )


def load_data(candidate_dir, verification_dir):
    candidate_root = Path(candidate_dir)
    verification_root = Path(verification_dir)

    cs = validate_checksums(candidate_root)
    vs = validate_checksums(verification_root)

    cm = read_json(
        candidate_root / "selection_manifest.json"
    )
    vm = read_json(
        verification_root / "verification_manifest.json"
    )

    require(
        cm["version"] == "predicate-candidates-v1",
        "Unsupported candidate version",
    )
    require(
        vm["version"] == "predicate-semantic-verification-v1",
        "Unsupported verification version",
    )

    require(
        vm["source_version"] == cm["version"],
        "Verification source version mismatch",
    )

    require(
        vm["taxonomy_sha256"] == cm["taxonomy_sha256"],
        "Taxonomy version mismatch",
    )

    for filename, expected in vm["input_checksums"].items():
        require(
            digest(candidate_root / filename) == expected,
            f"Verification input changed: {filename}",
        )

    taxonomy = read_json(candidate_root / "taxonomy.json")

    require(
        isinstance(taxonomy, list)
        and all(isinstance(x, str) for x in taxonomy),
        "Invalid taxonomy",
    )

    contexts = {}

    for row in read_jsonl(
        candidate_root / "selected_contexts.jsonl"
    ):
        fid = row["fact_id"]

        require(
            fid not in contexts,
            f"Duplicate context: {fid}",
        )
        contexts[fid] = row

    candidates, candidate_history = latest_records(
        candidate_root / "candidate_attempts.jsonl"
    )

    verifications, verification_history = latest_records(
        verification_root / "verification_attempts.jsonl"
    )

    require(
        set(candidates).issubset(contexts),
        "Unknown facts in candidate checkpoint",
    )

    require(
        set(verifications).issubset(candidates),
        "Unknown facts in verification checkpoint",
    )

    for fid, record in verifications.items():
        candidate = candidates[fid]
        context = contexts[fid]

        require(
            candidate["status"] == "CANDIDATE",
            f"Verified non-candidate: {fid}",
        )

        for field in (
            "article_id",
            "projection_key",
            "subject_key",
            "object_key",
            "context_sha256",
            "quote_sha256",
        ):
            require(
                record[field] == context[field],
                f"Provenance mismatch: {fid}/{field}",
            )

        require(
            record["candidate"] == candidate["proposal"],
            f"Candidate snapshot mismatch: {fid}",
        )

        require(
            record["approved"] is False,
            f"Unexpected approval: {fid}",
        )

    return {
        "candidate_summary": cs,
        "verification_summary": vs,
        "candidate_manifest": cm,
        "verification_manifest": vm,
        "contexts": contexts,
        "candidates": candidates,
        "verifications": verifications,
        "candidate_history": candidate_history,
        "verification_history": verification_history,
        "taxonomy": taxonomy,
    }


def predicate_statistics(data):
    groups = defaultdict(list)

    for fid, record in data["verifications"].items():
        if record["status"] != "VERIFIED":
            continue

        proposal = record["candidate"]
        verification = record["verification"]

        if (
            proposal["decision"] == "RECLASSIFY"
            and verification["verdict"] == "SUPPORTED"
        ):
            groups[proposal["proposed_predicate"]].append(
                record
            )

    total = sum(len(items) for items in groups.values())

    stats = {}

    for predicate, items in sorted(groups.items()):
        articles = {x["article_id"] for x in items}
        subjects = {x["subject_key"] for x in items}
        objects = {x["object_key"] for x in items}

        stats[predicate] = {
            "facts": len(items),
            "share": len(items) / total if total else 0,
            "articles": len(articles),
            "subjects": len(subjects),
            "objects": len(objects),
        }

    return stats


def audit_fact(
    fid,
    context,
    candidate,
    verification,
    predicate_stats,
    args,
):
    flags = []

    def flag(code, severity, explanation):
        flags.append({
            "code": code,
            "severity": severity,
            "explanation": explanation,
        })

    if candidate is None:
        flag(
            "MISSING_CANDIDATE",
            "HIGH",
            "No generation result for this fact",
        )

    elif candidate["status"] == "MODEL_FAILURE":
        flag(
            "GENERATION_FAILURE",
            "HIGH",
            "Predicate generation did not produce "
            "a valid candidate",
        )

    elif verification is None:
        flag(
            "MISSING_VERIFICATION",
            "HIGH",
            "Candidate has not been verified",
        )

    elif verification["status"] == "MODEL_FAILURE":
        flag(
            "VERIFICATION_FAILURE",
            "HIGH",
            "Semantic verification failed technically",
        )

    else:
        proposal = verification["candidate"]
        result = verification["verification"]

        decision = proposal["decision"]
        verdict = result["verdict"]

        rationale = str(result.get("rationale", ""))
        original_rationale = str(
            proposal.get("rationale", "")
        )

        if verdict == "REJECTED":
            flag(
                "REJECTED_BY_VERIFIER",
                "HIGH",
                "The verifier rejected the original decision",
            )

        elif verdict == "UNCERTAIN":
            flag(
                "UNCERTAIN_VERIFICATION",
                "MEDIUM",
                "The verifier could not reach a conclusion",
            )

        if decision == "RECLASSIFY":
            predicate = proposal["proposed_predicate"]

            if verdict == "SUPPORTED":
                stats = predicate_stats.get(predicate, {})

                if (
                    stats.get("facts", 0)
                    >= args.min_predicate_facts
                    and stats.get("share", 0)
                    >= args.min_predicate_share
                ):
                    flag(
                        "PREDICATE_CONCENTRATION",
                        "MEDIUM",
                        "This predicate occurs frequently "
                        "among supported reclassifications. "
                        "Check for semantic overgeneralization",
                    )

            if result.get("direction_ok") is False:
                flag(
                    "DIRECTION_CONFLICT",
                    "HIGH",
                    "The verifier detected a direction issue",
                )

            if result.get("predicate_semantics_ok") is False:
                flag(
                    "PREDICATE_SEMANTICS_CONFLICT",
                    "HIGH",
                    "Predicate meaning is not supported",
                )

        if decision in ABSTENTIONS:
            if verdict == "REJECTED":
                flag(
                    "ABSTENTION_REVIEW",
                    "HIGH",
                    "The abstention was rejected. "
                    "The available rationale does not "
                    "automatically establish an alternative",
                )

            if (
                matches_any(
                    original_rationale,
                    ACTION_ONLY_SIGNALS,
                )
                or matches_any(
                    rationale,
                    ACTION_ONLY_SIGNALS,
                )
            ):
                flag(
                    "POSSIBLE_ACTION_ONLY_BIAS",
                    "MEDIUM",
                    "An action-based assumption may have "
                    "influenced the abstention",
                )

            if matches_any(
                original_rationale + " " + rationale,
                STATIC_RELATION_SIGNALS,
            ):
                flag(
                    "STATIC_RELATION_REVIEW",
                    "LOW",
                    "The explanation mentions a static, "
                    "spatial, legal or institutional relation",
                )

        if matches_any(
            rationale,
            DIRECTION_SIGNALS,
        ):
            flag(
                "DIRECTION_LANGUAGE_SIGNAL",
                "MEDIUM",
                "The verifier's rationale mentions "
                "a possible inversion",
            )

        if (
            verdict == "REJECTED"
            and matches_any(
                rationale,
                TAXONOMY_SIGNALS,
            )
        ):
            flag(
                "POSSIBLE_TAXONOMY_GAP",
                "MEDIUM",
                "The rejection may involve an unrepresented "
                "predicate. Requires independent assessment",
            )

        if decision == "TAXONOMY_GAP":
            flag(
                "TAXONOMY_GAP_CANDIDATE",
                "MEDIUM",
                "Potential new predicate. Must undergo "
                "independent semantic consolidation",
            )

        if (
            decision in POSITIVE_DECISIONS
            and verdict == "SUPPORTED"
        ):
            evidence = proposal.get("evidence_quote")

            if (
                not isinstance(evidence, str)
                or evidence not in context["quote"]
            ):
                flag(
                    "EVIDENCE_INTEGRITY_FAILURE",
                    "HIGH",
                    "The candidate evidence is not a "
                    "literal substring of the verified quote",
                )

    priority = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}

    highest = max(
        (priority[x["severity"]] for x in flags),
        default=0,
    )

    return {
        "fact_id": fid,
        "article_id": context["article_id"],
        "projection_key": context["projection_key"],
        "subject_key": context["subject_key"],
        "object_key": context["object_key"],
        "subject_name": context["subject_name"],
        "object_name": context["object_name"],
        "context_sha256": context["context_sha256"],
        "decision": (
            verification["candidate"]["decision"]
            if verification and verification["status"] == "VERIFIED"
            else None
        ),
        "predicate": (
            verification["candidate"].get(
                "proposed_predicate"
            )
            if verification and verification["status"] == "VERIFIED"
            else None
        ),
        "verdict": (
            verification["verification"]["verdict"]
            if verification and verification["status"] == "VERIFIED"
            else None
        ),
        "risk_level": (
            "HIGH" if highest == 3
            else "MEDIUM" if highest == 2
            else "LOW" if highest == 1
            else "NONE"
        ),
        "flags": flags,
        "requires_review": bool(flags),
        "semantic_correctness_established": False,
        "approved": False,
    }


def run(args):
    require(
        args.min_predicate_facts >= 1,
        "Invalid minimum predicate facts",
    )
    require(
        0 <= args.min_predicate_share <= 1,
        "Invalid predicate share",
    )

    data = load_data(
        args.candidates_dir,
        args.verification_dir,
    )

    stats = predicate_statistics(data)

    reports = []

    for fid, context in data["contexts"].items():
        reports.append(
            audit_fact(
                fid,
                context,
                data["candidates"].get(fid),
                data["verifications"].get(fid),
                stats,
                args,
            )
        )

    reports.sort(key=lambda x: x["fact_id"])

    flagged = [
        row for row in reports
        if row["requires_review"]
    ]

    priority_order = {
        "HIGH": 0,
        "MEDIUM": 1,
        "LOW": 2,
        "NONE": 3,
    }

    flagged.sort(
        key=lambda row: (
            priority_order[row["risk_level"]],
            row["fact_id"],
        )
    )

    flags = Counter(
        flag["code"]
        for row in reports
        for flag in row["flags"]
    )

    risk_counts = Counter(
        row["risk_level"] for row in reports
    )

    verdict_counts = Counter(
        row["verdict"]
        for row in reports
        if row["verdict"] is not None
    )

    generation_model = data[
        "candidate_manifest"
    ]["model"]

    verification_model = data[
        "verification_manifest"
    ]["verification_model"]

    same_model = (
        generation_model == verification_model
    )

    summary = {
        "version": VERSION,
        "input_facts": len(reports),
        "flagged_facts": len(flagged),
        "risk_counts": dict(sorted(risk_counts.items())),
        "flag_counts": dict(sorted(flags.items())),
        "verdict_counts": dict(sorted(verdict_counts.items())),
        "predicate_statistics": stats,
        "generation_model": generation_model,
        "verification_model": verification_model,
        "same_generation_and_verification_model":
            same_model,
        "model_independent_verification":
            not same_model,
        "independent_ground_truth_available": False,
        "automatic_semantic_approval": False,
        "approved_corrections": 0,
        "approved_new_predicates": 0,
        "llm_calls": 0,
        "graph_writes": 0,
        "source_checksums": {
            "candidate_summary": digest(
                Path(args.candidates_dir) / "summary.json"
            ),
            "verification_summary": digest(
                Path(args.verification_dir) / "summary.json"
            ),
        },
        "audit_parameters": {
            "min_predicate_facts":
                args.min_predicate_facts,
            "min_predicate_share":
                args.min_predicate_share,
        },
    }

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    write_jsonl(
        out / "fact_consistency.jsonl",
        reports,
    )
    write_jsonl(
        out / "review_queue.jsonl",
        flagged,
    )

    predicate_rows = [
        {"predicate": key, **value}
        for key, value in sorted(
            stats.items(),
            key=lambda item: (
                -item[1]["facts"],
                item[0],
            ),
        )
    ]

    write_jsonl(
        out / "predicate_risk_distribution.jsonl",
        predicate_rows,
    )

    summary["checksums"] = {
        filename: digest(out / filename)
        for filename in (
            "fact_consistency.jsonl",
            "review_queue.jsonl",
            "predicate_risk_distribution.jsonl",
        )
    }

    write_json(out / "summary.json", summary)

    print(
        json.dumps(
            {
                "version": VERSION,
                "input_facts": summary["input_facts"],
                "flagged_facts": summary["flagged_facts"],
                "risk_counts": summary["risk_counts"],
                "flag_counts": summary["flag_counts"],
                "same_model": same_model,
                "graph_writes": 0,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def main():
    parser = argparse.ArgumentParser(
        description=__doc__
    )

    parser.add_argument(
        "--candidates-dir",
        default=(
            "output/resolution/"
            "predicate-candidates-v1-sample"
        ),
    )

    parser.add_argument(
        "--verification-dir",
        default=(
            "output/resolution/"
            "predicate-verification-v1-sample-full"
        ),
    )

    parser.add_argument(
        "--out-dir",
        default=(
            "output/resolution/"
            "predicate-consistency-audit-v1-sample"
        ),
    )

    parser.add_argument(
        "--min-predicate-facts",
        type=int,
        default=5,
    )

    parser.add_argument(
        "--min-predicate-share",
        type=float,
        default=0.08,
    )

    run(parser.parse_args())


if __name__ == "__main__":
    main()
