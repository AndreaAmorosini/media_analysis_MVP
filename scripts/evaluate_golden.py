#!/usr/bin/env python3
"""Evaluate press_reputation output against the public golden set.

For each case in golden_set/manifest.json the pipeline CLI is run on the case
PDF (subprocess, PYTHONPATH=src) and compared with the ground truth:

- extraction/reconstruction: gt/articles.json vs flow/article_records.json
  (article recall, title/date/subtitle match, body token-F1);
- region tagging: gt/regions.json vs pages/page_*.json (per-role accuracy by
  bbox IoU matching, watermark detection, index rows kept out of article text);
- entity resolution (KG layer): gt/entities.json mentions vs
  lookup.resolve_entity and vs the Wikidata gazetteer (entities/gazetteer.json);
- events: gt/events.json and events_gdelt_*.json are shipped as ingestion GT
  (no event extraction in the pipeline yet; only coverage is reported).

Usage:
    PYTHONPATH=src pixi run python scripts/evaluate_golden.py \
        --golden-root golden_set --output golden_eval_report.json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_golden_set import (  # noqa: E402
    ROLE_TO_REGION_TYPE,
    normalize_text,
    token_f1,
    utc_now,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTICLE_REGION_TYPES = {"article_title", "article_subtitle", "article_body", "author"}


def iou(a: list[float], b: list[float]) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    if inter <= 0:
        return 0.0
    area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    area_b = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    return inter / (area_a + area_b - inter)


def run_pipeline(pdf: Path, out_dir: Path) -> Path:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    out_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, "-m", "press_reputation.cli", str(pdf), "--output-dir", str(out_dir)],
        env=env, check=True, cwd=REPO_ROOT,
    )
    doc_dir = out_dir / pdf.stem.replace(" ", "_")
    return doc_dir


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def match_articles(gt_articles: list[dict], records: list[dict]) -> list[tuple[dict, dict | None]]:
    pairs = []
    for gt in gt_articles:
        best, best_score = None, 0.0
        for rec in records:
            score = SequenceMatcher(None, normalize_text(gt.get("title")), normalize_text(rec.get("title"))).ratio()
            if score > best_score:
                best, best_score = rec, score
        pairs.append((gt, best if best_score >= 0.5 else None))
    return pairs


def evaluate_case(case: dict, golden_root: Path, workdir: Path, run: bool) -> dict:
    case_dir = golden_root / "cases" / case["id"]
    gt = load_json(case_dir / "gt" / "articles.json", {})
    gt_regions = load_json(case_dir / "gt" / "regions.json", {}).get("regions", [])
    gt_articles = gt.get("articles", [])

    result: dict[str, Any] = {
        "case": case["id"], "source": case.get("source"), "pdf": case.get("pdf"),
        "gt_articles": len(gt_articles), "gt_regions": len(gt_regions),
    }

    if case.get("pdf") and run:
        doc_dir = run_pipeline(golden_root / case["pdf"], workdir / case["id"])
    elif case.get("pdf"):
        doc_dir = workdir / case["id"] / Path(case["pdf"]).stem.replace(" ", "_")
    else:
        doc_dir = None

    if doc_dir is not None:
        records = load_json(doc_dir / "flow" / "article_records.json", []) or []
        pairs = match_articles(gt_articles, records)
        found = [p for p in pairs if p[1] is not None]
        result["records"] = len(records)
        result["article_recall"] = round(len(found) / len(gt_articles), 4) if gt_articles else None
        if found:
            result["title_exact_rate"] = round(sum(
                1 for gt, rec in found if normalize_text(gt.get("title")) == normalize_text(rec.get("title"))
            ) / len(found), 4)
            result["date_match_rate"] = round(sum(
                1 for gt, rec in found
                if (gt.get("publication_date") or None) == (str(rec.get("publication_date")) or None)
            ) / len(found), 4)
            result["subtitle_match_rate"] = round(sum(
                1 for gt, rec in found
                if bool(gt.get("subtitle")) == bool(rec.get("subtitle"))
            ) / len(found), 4)
            result["body_f1_mean"] = round(sum(
                token_f1(gt.get("body") or "", rec.get("body") or "") for gt, rec in found
            ) / len(found), 4)
        result["statuses"] = {}
        for rec in records:
            status = rec.get("finalization_status")
            result["statuses"][status] = result["statuses"].get(status, 0) + 1

        page_regions: list[dict] = []
        pages_dir = doc_dir / "pages"
        if pages_dir.exists():
            for page_file in sorted(pages_dir.glob("page_*.json")):
                page = load_json(page_file, {})
                for region in page.get("regions", []):
                    if region.get("bbox"):
                        page_regions.append({"page": page.get("pdf_page"), "region": region})
        per_role: dict[str, dict[str, int]] = {}
        matched = 0
        for gtr in gt_regions:
            candidates = [p for p in page_regions if p["page"] == gtr["page"]]
            best, best_iou = None, 0.0
            for cand in candidates:
                score = iou(gtr["bbox"], cand["region"]["bbox"])
                if score > best_iou:
                    best, best_iou = cand, score
            stats = per_role.setdefault(gtr["role"], {"total": 0, "matched": 0, "role_ok": 0})
            stats["total"] += 1
            if best is not None and best_iou >= 0.3:
                stats["matched"] += 1
                matched += 1
                expected = ROLE_TO_REGION_TYPE.get(gtr["role"])
                rtype = best["region"].get("type")
                if expected is None:
                    ok = best["region"].get("exclude_from_article_text") or rtype not in ARTICLE_REGION_TYPES
                else:
                    ok = rtype == expected
                if ok:
                    stats["role_ok"] += 1
        result["region_iou_match_rate"] = round(matched / len(gt_regions), 4) if gt_regions else None
        result["region_tagging"] = {
            role: {
                **stats,
                "role_accuracy": round(stats["role_ok"] / stats["matched"], 4) if stats["matched"] else None,
            }
            for role, stats in sorted(per_role.items())
        }

    entities_gt = load_json(case_dir / "gt" / "entities.json", {}).get("entities")
    if entities_gt is not None:
        result["kg"] = evaluate_entities(entities_gt, golden_root)
    events_gt = load_json(case_dir / "gt" / "events.json", {}).get("events")
    if events_gt is not None:
        result["kg_events_gt"] = len(events_gt)
    return result


def _canonical_from_refs(refs: list[dict]) -> str | None:
    for ref in refs or []:
        resource = (ref.get("resource") or "").lower()
        reference = ref.get("reference") or ""
        if resource in ("wikidata", "dbpedia") and reference:
            tail = reference.rstrip("/").rsplit("/", 1)[-1]
            return tail.replace("_", " ")
    return None


def evaluate_entities(entities_gt: list[dict], golden_root: Path) -> dict:
    from press_reputation.lookup import resolve_entity

    gazetteer = load_json(golden_root / "entities" / "gazetteer.json", {}).get("testate", [])
    gazetteer_index: dict[str, str] = {}
    for entry in gazetteer:
        if entry.get("nome"):
            gazetteer_index[normalize_text(entry["nome"])] = entry["nome"]
        for alias in entry.get("aliases") or []:
            gazetteer_index[normalize_text(alias)] = entry["nome"]

    mentions = [(ent, m) for ent in entities_gt for m in ent.get("mentions", [])]
    lookup_hits = canonical_ok = gazetteer_hits = 0
    for ent, mention in mentions:
        text = mention.get("text", "")
        match = resolve_entity(text)
        if match is not None:
            lookup_hits += 1
            expected = _canonical_from_refs(ent.get("refs"))
            if expected and normalize_text(match.canonical_name) == normalize_text(expected):
                canonical_ok += 1
        if normalize_text(text) in gazetteer_index:
            gazetteer_hits += 1
    return {
        "mentions": len(mentions),
        "entities": len(entities_gt),
        "lookup_hit_rate": round(lookup_hits / len(mentions), 4) if mentions else None,
        "canonical_accuracy": round(canonical_ok / len(mentions), 4) if mentions else None,
        "gazetteer_hit_rate": round(gazetteer_hits / len(mentions), 4) if mentions else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden-root", type=Path, default=Path("golden_set"))
    parser.add_argument("--workdir", type=Path, default=Path("/tmp/golden_eval"))
    parser.add_argument("--output", type=Path, default=None, help="write the JSON report here")
    parser.add_argument("--case", action="append", default=None, help="restrict to case id (repeatable)")
    parser.add_argument("--skip-run", action="store_true", help="reuse existing outputs in workdir")
    args = parser.parse_args(argv)

    manifest = load_json(args.golden_root / "manifest.json", {})
    cases = [c for c in manifest.get("cases", []) if not args.case or c["id"] in args.case]
    results = [evaluate_case(case, args.golden_root, args.workdir, run=not args.skip_run) for case in cases]

    gdelt_files = sorted(args.golden_root.glob("events_gdelt_*.json"))
    report = {
        "generated_at": utc_now(),
        "golden_root": str(args.golden_root),
        "cases": results,
        "gdelt_event_files": [str(p.name) for p in gdelt_files],
        "interpretation": (
            "article_recall counts GT articles matched to an ArticleRecord by title similarity >= 0.5; "
            "region_tagging.role_accuracy is computed only on GT regions matched by bbox IoU >= 0.3; "
            "canonical_accuracy compares lookup canonical names with NAF external references; "
            "gdelt files are event GT for the future KG ingestion layer."
        ),
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
