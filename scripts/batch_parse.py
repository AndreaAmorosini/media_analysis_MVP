#!/usr/bin/env python3
"""Batch runner for press_reputation PDFs.

Input: INPUT/YYYY/MM/**/*.pdf (optional nested daily folders).
Output: OUTPUT/YYYY/MM/<optional day dirs>/<PDF_STEM>/ {CLI output, articles/, document_report.json}

No pipeline internals are imported: each PDF is processed by the repository CLI
in an isolated subprocess. One tqdm progress bar is the only normal console output.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tqdm import tqdm


SCHEMA_VERSION = 1
BODY_MIN_CHARS = 250  # quality warning, NEVER filter an article based on length
AUDIT_FIELDS = ("title_correct", "subtitle_correct", "date_correct",
                "body_complete", "reading_order_correct", "no_unrelated_text")
DOCUMENT_COLUMNS = [
    "year", "month", "day_path", "pdf", "relative_pdf", "status", "duration_seconds", "pdf_pages",
    "articles", "with_title", "with_date", "with_body", "short_body",
    "review_required", "incomplete", "body_chars", "pdf_body_assigned_chars",
    "pdf_body_orphan_chars", "body_assignment_rate", "index_entries",
    "index_entries_matched", "index_coverage", "web_attempts", "web_selected",
    "web_fallbacks", "warnings", "error", "ocr_requested_pages",
    "ocr_completed_pages", "ocr_failed_pages", "ocr_suspected_skipped_pages",
    "unresolved_title_anchors",
]
ARTICLE_COLUMNS = [
    "year", "month", "day_path", "pdf", "article_id", "article_file", "title", "publication_date",
    "subtitle_present", "body_chars", "body_source", "finalization_status",
    "completeness_status", "flags", "pdf_pages",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    temp.replace(path)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def safe_cli_name(pdf: Path) -> str:
    """Match the current press_reputation.cli.safe_document_dir_name() exactly."""
    return pdf.stem.replace(" ", "_")


MONTH_NUMBER = {
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4,
    "maggio": 5, "giugno": 6, "luglio": 7, "agosto": 8,
    "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
}


def month_sort_key(directory: Path) -> tuple[int, str]:
    name = directory.name.casefold()
    leading_number = re.match(r"^(0?[1-9]|1[0-2])(?:$|[_ -])", name)
    if leading_number:
        return int(leading_number.group(1)), name
    return MONTH_NUMBER.get(name, 99), name


def discover_pdfs(input_root: Path) -> list[tuple[str, str, Path]]:
    pdfs: list[tuple[str, str, Path]] = []
    for year_dir in sorted(input_root.iterdir()):
        if not year_dir.is_dir() or not re.fullmatch(r"\d{4}", year_dir.name):
            continue
        for month_dir in sorted(year_dir.iterdir(), key=month_sort_key):
            if not month_dir.is_dir() or month_dir.name.startswith("."):
                continue
            # Include PDF directly under MM and inside any day subfolders,
            # preserving their relative path in the output tree.
            nested = sorted(
                (p for p in month_dir.rglob("*")
                 if p.is_file() and p.suffix.casefold() == ".pdf"
                 and not any(part.startswith(".") for part in p.relative_to(month_dir).parts)),
                key=lambda p: (str(p.relative_to(month_dir)).casefold(), str(p)),
            )
            for pdf in nested:
                pdfs.append((year_dir.name, month_dir.name, pdf.resolve()))
    return pdfs


def fingerprint_sources(repo: Path) -> str:
    """Invalidate resume when the repository's Python code/config changes."""
    hasher = hashlib.sha256()
    for path in sorted((repo / "src" / "press_reputation").rglob("*.py")):
        hasher.update(str(path.relative_to(repo)).encode())
        hasher.update(path.read_bytes())
    config = repo / "pixi.toml"
    if config.is_file():
        hasher.update(config.read_bytes())
    return hasher.hexdigest()[:20]


def allocate_output_names(jobs: list[tuple[str, str, Path]]) -> dict[Path, str]:
    """Protect against 'a b.pdf' / 'a_b.pdf' and case-only collisions."""
    groups: dict[tuple[str, str, str, str], list[Path]] = defaultdict(list)
    for year, month, pdf in jobs:
        # A filename collision matters only within the SAME day folder.
        groups[(year, month, str(pdf.parent), safe_cli_name(pdf).casefold())].append(pdf)
    out = {}
    for (year, month, parent, _), paths in groups.items():
        for path in paths:
            name = safe_cli_name(path)
            if len(paths) > 1:
                suffix = hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:8]
                name = f"{name}__{suffix}"
            out[path] = name
    return out


def article_text(record: dict) -> str:
    """Use the selected canonical body, including the web-first override."""
    for key in ("body", "body_clean", "body_raw"):
        candidate = record.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def selected_body_source(record: dict) -> str:
    method = (record.get("extraction") or {}).get("method") or "unknown"
    return "web_html" if method == "web_html" else str(method)


def web_tail_diagnostic(record: dict, text: str) -> bool:
    """Flag possible unrelated short HTML tail; NEVER automatically delete it.

    Uses prior PDF body as supporting evidence when available. A real website may
    have changed since clipping, so the result is a review flag, NOT a rejection.
    """
    if selected_body_source(record) != "web_html":
        return False
    pdf = record.get("body_pdf_raw") or ""
    if len(pdf) < 800:
        return False
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n|\n", text) if part.strip()]
    if len(paragraphs) < 2 or not (70 <= len(paragraphs[-1]) <= 400):
        return False

    def shingles(value: str) -> set[tuple[str, ...]]:
        words = re.findall(r"[\wÀ-ÿ]+", value.casefold())
        return {tuple(words[i:i + 5]) for i in range(max(0, len(words) - 4))}

    reference = shingles(pdf)
    tail = shingles(paragraphs[-1])
    previous = shingles(" ".join(paragraphs[:-1]))
    if not reference or not tail or not previous:
        return False
    tail_overlap = len(tail & reference) / len(tail)
    previous_overlap = len(previous & reference) / len(previous)
    return tail_overlap < 0.02 and previous_overlap >= 0.12


def page_body_coverage(pages_dir: Path) -> tuple[int, int, int]:
    assigned = orphan = pages_count = 0
    if not pages_dir.is_dir():
        return 0, 0, 0
    for path in sorted(pages_dir.glob("page_*.json")):
        page = read_json(path, {})
        pages_count += 1
        for region in page.get("regions", []):
            if region.get("type") != "article_body" or region.get("exclude_from_article_text"):
                continue
            chars = len((region.get("text") or "").strip())
            if region.get("article_id"):
                assigned += chars
            else:
                orphan += chars
    return assigned, orphan, pages_count


def _index_stats(doc_dir: Path) -> tuple[int, int, float | None]:
    reports = read_json(doc_dir / "flow" / "extraction_completeness.json", [])
    if isinstance(reports, list) and reports:
        item = reports[0]
        total = item.get("index_entries_total")
        matched = item.get("index_entries_with_record")
        if isinstance(total, int) and isinstance(matched, int):
            return total, matched, round(matched / total, 4) if total > 0 else None
    entries = read_json(doc_dir / "flow" / "review_index_entries.json", [])
    if not isinstance(entries, list):
        return 0, 0, None
    return len(entries), 0, None  # don't fabricate matches if unavailable


def make_document_report(doc_dir: Path, year: str, month: str, pdf: Path,
                         relative_pdf: str, duration: float,
                         min_body_chars: int = BODY_MIN_CHARS) -> dict:
    records = read_json(doc_dir / "flow" / "article_records.json")
    if not isinstance(records, list):
        raise ValueError("flow/article_records.json non contiene una lista di articoli")

    articles_dir = doc_dir / "articles"
    articles_dir.mkdir(exist_ok=True)
    for stale in articles_dir.glob("article_*.json"):
        stale.unlink()

    details = []
    flags_count: Counter[str] = Counter()
    counts = Counter()
    for index, record in enumerate(records, start=1):
        body = article_text(record)
        title = (record.get("title") or "").strip()
        subtitle = (record.get("subtitle") or "").strip() or None
        published = record.get("publication_date") or None
        output_file = f"article_{index:04d}.json"
        # Exact four fields requested. Source/provenance stay in original records.
        write_json(articles_dir / output_file, {
            "title": title,
            "subtitle": subtitle,
            "publication_date": published,
            "body": body,
        })
        completeness = record.get("extraction_completeness") or {}
        completeness_status = completeness.get("status") or "unknown"
        final_status = record.get("finalization_status") or "unknown"
        flags = []
        if not title:
            flags.append("missing_title")
        if not published:
            flags.append("missing_date")
        if not body:
            flags.append("empty_body")
        elif len(body) < min_body_chars:
            flags.append("short_body")
        if final_status == "review_required":
            flags.append("review_required")
        if completeness_status == "incomplete":
            flags.append("incomplete_pdf_reconstruction")
        if web_tail_diagnostic(record, body):
            flags.append("web_tail_not_supported_by_pdf")
        for flag in flags:
            flags_count[flag] += 1
        counts["articles"] += 1
        counts["with_title"] += bool(title)
        counts["with_date"] += bool(published)
        counts["with_body"] += bool(body)
        counts["short_body"] += bool(body and len(body) < min_body_chars)
        counts["review_required"] += final_status == "review_required"
        counts["incomplete"] += completeness_status == "incomplete"
        counts["body_chars"] += len(body)
        counts[f"source:{selected_body_source(record)}"] += 1
        details.append({
            "year": year, "month": month,
            "day_path": str(Path(*Path(relative_pdf).parts[2:-1])) if len(Path(relative_pdf).parts)>3 else "",
            "pdf": pdf.name,
            "article_id": record.get("id"), "article_file": f"articles/{output_file}",
            "title": title, "publication_date": published, "subtitle_present": bool(subtitle),
            "body_chars": len(body), "body_source": selected_body_source(record),
            "finalization_status": final_status, "completeness_status": completeness_status,
            "flags": flags, "pdf_pages": record.get("pdf_pages") or [],
        })

    assigned, orphan, page_count = page_body_coverage(doc_dir / "pages")
    index_count, index_matched, index_coverage = _index_stats(doc_dir)
    web = read_json(doc_dir / "flow" / "web_retrieval.json", [])
    web = web if isinstance(web, list) else []
    web_selected = sum(item.get("status") == "web_selected" for item in web)
    web_fallbacks = sum(item.get("status") in {"pdf_fallback", "web_unavailable"} for item in web)
    # OCR audit from the persisted profiler, independent of article detection.
    profiling = read_json(doc_dir / "profiling" / "document.json", {}) or {}
    profiles = list((profiling.get("pages") or {}).values())
    ocr_requested = sum(bool(p.get("run_ocr")) for p in profiles)
    ocr_completed = sum(p.get("ocr_status") == "completed" for p in profiles)
    ocr_failed = sum(p.get("ocr_status") == "failed" for p in profiles)
    # Diagnostic only: this is a heuristic, not proof that OCR is needed.
    skipped_suspect = sum(
        not p.get("run_ocr")
        and (p.get("native_body_word_count") or 0) < 30
        and (p.get("image_coverage") or 0) >= 0.15
        for p in profiles
    )
    # Article title anchors that never became ArticleRecord items.
    record_ids = {r.get("id") for r in records}
    title_anchors = set()
    for page_file in (doc_dir / "pages").glob("page_*.json"):
        parsed_page = read_json(page_file, {}) or {}
        for region in parsed_page.get("regions", []):
            if region.get("type") == "article_title" and region.get("article_id"):
                if not region.get("exclude_from_article_text"):
                    title_anchors.add(region["article_id"])
    unresolved_title_anchors = len(title_anchors - record_ids)
    summary = {
        "unresolved_title_anchors": unresolved_title_anchors,
        "ocr_requested_pages": ocr_requested,
        "ocr_completed_pages": ocr_completed,
        "ocr_failed_pages": ocr_failed,
        "ocr_suspected_skipped_pages": skipped_suspect,
        "year": year, "month": month,
        "day_path": str(Path(*Path(relative_pdf).parts[2:-1])) if len(Path(relative_pdf).parts)>3 else "",
        "pdf": pdf.name, "relative_pdf": relative_pdf,
        "status": "success", "duration_seconds": round(duration, 2),
        "pdf_pages": page_count, "articles": counts["articles"],
        "with_title": counts["with_title"], "with_date": counts["with_date"],
        "with_body": counts["with_body"], "short_body": counts["short_body"],
        "review_required": counts["review_required"], "incomplete": counts["incomplete"],
        "body_chars": counts["body_chars"],
        "pdf_body_assigned_chars": assigned, "pdf_body_orphan_chars": orphan,
        "body_assignment_rate": round(assigned / (assigned + orphan), 4) if assigned + orphan else None,
        "index_entries": index_count, "index_entries_matched": index_matched,
        "index_coverage": index_coverage,
        "web_attempts": len(web), "web_selected": web_selected,
        "web_fallbacks": web_fallbacks,
        "warnings": sum(flags_count.values()) + int(orphan > 0) + int(not records),
        "error": "",
    }
    anomalies = []
    if orphan:
        anomalies.append({"kind": "orphan_body_chars", "count": orphan})
    if skipped_suspect:
        anomalies.append({"kind": "ocr_candidate_skipped", "count": skipped_suspect})
    if unresolved_title_anchors:
        anomalies.append({"kind": "title_without_article_record", "count": unresolved_title_anchors})
    if ocr_failed:
        anomalies.append({"kind": "ocr_failed_pages", "count": ocr_failed})
    if not records:
        anomalies.append({"kind": "no_article_records", "count": 1})
    anomalies += [{"kind": key, "count": value} for key, value in sorted(flags_count.items())]
    report = {
        "schema_version": SCHEMA_VERSION, "generated_at": utc_now(),
        "document": summary, "body_sources": {k[7:]: v for k, v in counts.items() if k.startswith("source:")},
        "anomalies": anomalies, "articles": details,
        "interpretation": "Indicatori diagnostici, NON stime di precision, recall o correttezza del body."
    }
    write_json(doc_dir / "document_report.json", report)
    return report


def error_report(year: str, month: str, pdf: Path, relative_pdf: str, duration: float,
                 error: str, status: str = "failed") -> dict:
    summary = dict.fromkeys(DOCUMENT_COLUMNS, "")
    day_path = str(Path(*Path(relative_pdf).parts[2:-1])) if len(Path(relative_pdf).parts)>3 else ""
    summary.update(year=year, month=month, day_path=day_path,
                   pdf=pdf.name, relative_pdf=relative_pdf,
                   status=status, duration_seconds=round(duration, 2), error=error[:1000])
    return {"schema_version": SCHEMA_VERSION, "document": summary, "articles": [],
            "anomalies": [{"kind": status, "count": 1}]}


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            normalized = {key: ("; ".join(map(str, value)) if isinstance(value, list) else value)
                          for key, value in row.items()}
            writer.writerow(normalized)


def aggregate(reports: list[dict]) -> dict:
    docs = [r["document"] for r in reports]
    successes = [d for d in docs if d.get("status") == "success"]
    articles = [a for r in reports for a in r.get("articles", [])]
    summed = lambda name: sum(int(d.get(name) or 0) for d in successes)
    article_count = summed("articles")
    total_chars = summed("pdf_body_assigned_chars") + summed("pdf_body_orphan_chars")
    indexed = summed("index_entries")
    issue_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    for report in reports:
        for anomaly in report.get("anomalies", []):
            issue_counts[anomaly["kind"]] += int(anomaly.get("count") or 0)
        source_counts.update(report.get("body_sources", {}))
    return {
        "documents_total": len(docs), "documents_success": len(successes),
        "documents_failed": len(docs) - len(successes),
        "document_success_rate": round(len(successes) / len(docs), 4) if docs else None,
        "articles_total": article_count,
        "articles_per_successful_document": round(article_count / len(successes), 2) if successes else None,
        "title_presence_rate": round(summed("with_title") / article_count, 4) if article_count else None,
        "publication_date_presence_rate": round(summed("with_date") / article_count, 4) if article_count else None,
        "nonempty_body_rate": round(summed("with_body") / article_count, 4) if article_count else None,
        "short_body_count": summed("short_body"),
        "review_required_articles": summed("review_required"),
        "pdf_incomplete_articles": summed("incomplete"),
        "body_char_assignment_rate": round(summed("pdf_body_assigned_chars") / total_chars, 4) if total_chars else None,
        "pdf_body_orphan_chars": summed("pdf_body_orphan_chars"),
        "index_entries_total": indexed,
        "index_entries_with_record": summed("index_entries_matched"),
        "index_linkage_rate": round(summed("index_entries_matched") / indexed, 4) if indexed else None,
        "unresolved_title_anchors": summed("unresolved_title_anchors"),
        "ocr_requested_pages": summed("ocr_requested_pages"),
        "ocr_completed_pages": summed("ocr_completed_pages"),
        "ocr_failed_pages": summed("ocr_failed_pages"),
        "ocr_suspected_skipped_pages": summed("ocr_suspected_skipped_pages"),
        "web_attempts": summed("web_attempts"),
        "web_selected": summed("web_selected"),
        "web_fallbacks_or_unavailable": summed("web_fallbacks"),
        "body_sources": dict(sorted(source_counts.items())),
        "issue_counts": dict(sorted(issue_counts.items())),
        "notes": [
            "Sono indicatori automatici di copertura e consistenza, non accuracy/precision/recall.",
            "body_char_assignment_rate include solo regioni già riconosciute ARTICLE_BODY; esclude testo UNKNOWN o scartato.",
            "index_linkage_rate è disponibile solo quando l'indice viene estratto; non misura la recall effettiva.",
            "Per misurare correttezza e completezza reale, annotare gli articoli nel CSV audit_template.csv.",
        ],
    }


def save_reports(output_root: Path, reports: list[dict]) -> None:
    reports.sort(key=lambda r: (str(r["document"].get("year")),
                                str(r["document"].get("month")),
                                str(r["document"].get("pdf"))))
    summaries = [r["document"] for r in reports]
    article_rows = [a for r in reports for a in r.get("articles", [])]
    output_root.mkdir(parents=True, exist_ok=True)
    _write_csv(output_root / "documents.csv", DOCUMENT_COLUMNS, summaries)
    _write_csv(output_root / "articles.csv", ARTICLE_COLUMNS, article_rows)
    _write_csv(output_root / "audit_template.csv", ARTICLE_COLUMNS + list(AUDIT_FIELDS), article_rows)
    write_json(output_root / "batch_summary.json", {
        "generated_at": utc_now(), "metrics": aggregate(reports),
        "documents": summaries,
    })
    groups_month: dict[tuple[str, str], list[dict]] = defaultdict(list)
    groups_year: dict[str, list[dict]] = defaultdict(list)
    for report in reports:
        doc = report["document"]
        year, month = doc["year"], doc["month"]
        groups_month[(year, month)].append(report)
        groups_year[year].append(report)
    for (year, month), subreports in groups_month.items():
        write_json(output_root / year / month / "month_summary.json", {
            "year": year, "month": month, "metrics": aggregate(subreports),
            "documents": [r["document"] for r in subreports],
        })
    for year, subreports in groups_year.items():
        write_json(output_root / year / "year_summary.json", {
            "year": year, "metrics": aggregate(subreports),
            "months": {month: aggregate(group) for (y, month), group in groups_month.items() if y == year},
        })


def _manifest_matches(doc_dir: Path, pdf: Path, repo_hash: str, web_first: bool,
                      debug_bbox: bool, min_body_chars: int, ocr_policy: str = "auto") -> bool:
    manifest = read_json(doc_dir / "batch_manifest.json", {})
    stat = pdf.stat()
    return (
        manifest.get("status") == "success"
        and manifest.get("pdf_size") == stat.st_size
        and manifest.get("pdf_mtime_ns") == stat.st_mtime_ns
        and manifest.get("pipeline_fingerprint") == repo_hash
        and manifest.get("ocr_policy", "auto") == ocr_policy
        and manifest.get("web_first") == web_first
        and manifest.get("debug_bbox") == debug_bbox
        and manifest.get("min_body_chars") == min_body_chars
        and (doc_dir / "flow" / "article_records.json").is_file()
        and (doc_dir / "document_report.json").is_file()
    )


def _replace_dir(source_dir: Path, target: Path) -> None:
    """Keep the last successful run if replacement fails."""
    backup = None
    if target.exists():
        backup = target.with_name(target.name + ".backup_" + uuid.uuid4().hex[:8])
        target.rename(backup)
    try:
        source_dir.rename(target)
    except BaseException:
        if backup:
            backup.rename(target)
        raise
    if backup:
        shutil.rmtree(backup)


def run_document(pdf: Path, year: str, month: str, relative_pdf: str,
                 doc_dir: Path, repo: Path, repo_hash: str,
                 web_first: bool, debug_bbox: bool, min_body_chars: int,
                 python_bin: str, log_dir: Path, ocr_policy: str = "auto") -> dict:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / (doc_dir.name + ".log")
    started = time.monotonic()
    month_out = doc_dir.parent
    try:
        with tempfile.TemporaryDirectory(prefix=".working_", dir=month_out) as work:
            stage_root = Path(work)
            cmd = [python_bin, "-m", "press_reputation.cli", str(pdf),
                   "--output-dir", str(stage_root)]
            if ocr_policy != "auto":
                cmd.extend(["--ocr-policy", ocr_policy])
            if web_first:
                cmd.append("--web-first")
            if debug_bbox:
                cmd.append("--debug-bbox")
            env = os.environ.copy()
            env["PYTHONPATH"] = str(repo / "src") + os.pathsep + env.get("PYTHONPATH", "")
            env["PYTHONUNBUFFERED"] = "1"
            env["TERM"] = "dumb"
            env["NO_COLOR"] = "1"
            env["RICH_NO_COLOR"] = "1"
            env["TQDM_DISABLE"] = "1"
            with log_path.open("w", encoding="utf-8") as log:
                log.write("Command: " + repr(cmd) + "\n\n")
                log.flush()
                result = subprocess.run(cmd, cwd=repo, env=env, stdout=log,
                                        stderr=subprocess.STDOUT, check=False)
                if result.returncode != 0:
                    raise RuntimeError(f"Pipeline exit code {result.returncode}; log: {log_path}")
            produced = stage_root / safe_cli_name(pdf)
            if not produced.is_dir():
                raise RuntimeError(f"Pipeline completed but output directory is missing: {produced}")
            duration = time.monotonic() - started
            report = make_document_report(produced, year, month, pdf, relative_pdf,
                                          duration, min_body_chars)
            stat = pdf.stat()
            write_json(produced / "batch_manifest.json", {
                "schema_version": SCHEMA_VERSION, "status": "success", "generated_at": utc_now(),
                "source_pdf": str(pdf), "pdf_size": stat.st_size,
                "pdf_mtime_ns": stat.st_mtime_ns, "pipeline_fingerprint": repo_hash,
                "ocr_policy": ocr_policy,
                "web_first": web_first, "debug_bbox": debug_bbox,
                "min_body_chars": min_body_chars, "pipeline_log": str(log_path),
            })
            _replace_dir(produced, doc_dir)
            return report
    except Exception as exc:
        # Failed runs never overwrite the last successful document directory.
        return error_report(year, month, pdf, relative_pdf,
                            time.monotonic() - started, f"{type(exc).__name__}: {exc}")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Extract articles from PDFs under YEAR/MONTH with one progress bar."
    )
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path,
                        default=Path(__file__).resolve().parents[1],
                        help="Root containing src/press_reputation and pixi.toml")
    parser.add_argument("--web-first", action="store_true", help="Pass --web-first to the pipeline")
    parser.add_argument("--ocr-policy", choices=("auto", "aggressive", "all"), default="auto",
                        help="OCR profile: auto (default), aggressive or all (slow)")
    parser.add_argument("--debug-bbox", action="store_true", help="Produce current page debug PNGs")
    parser.add_argument("--force", action="store_true", help="Reprocess PDFs instead of reusing successful results")
    parser.add_argument("--report-only", action="store_true", help="Rebuild reports from already-produced folders")
    parser.add_argument("--min-body-chars", type=int, default=BODY_MIN_CHARS)
    parser.add_argument("--python-bin", default=sys.executable,
                        help="Python interpreter from the same environment as press_reputation")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    input_root = args.input_root.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()
    repo = args.repo_root.expanduser().resolve()
    if not input_root.is_dir():
        raise SystemExit("Invalid --input-root: directory does not exist")
    if not (repo / "src" / "press_reputation" / "cli.py").is_file():
        raise SystemExit("Invalid --repo-root: src/press_reputation/cli.py missing")
    if output_root == input_root or input_root in output_root.parents:
        raise SystemExit("Output directory must not be inside the input directory")
    if args.min_body_chars < 0:
        raise SystemExit("--min-body-chars must be >= 0")
    jobs = discover_pdfs(input_root)
    names = allocate_output_names(jobs)
    repo_hash = fingerprint_sources(repo)
    reports = []
    with tqdm(total=len(jobs), desc="Rassegne", unit="PDF", dynamic_ncols=True,
              leave=True, file=sys.stderr) as progress:
        for year, month, pdf in jobs:
            rel = str(pdf.relative_to(input_root))
            # Preserve all subdirectories between month and PDF.
            rel_inside_month = pdf.relative_to(input_root / year / month)
            day_parts = rel_inside_month.parts[:-1]
            target = output_root / year / month / Path(*day_parts) / names[pdf]
            if args.report_only:
                if (target / "flow" / "article_records.json").exists():
                    old = read_json(target / "document_report.json", {})
                    duration = float((old.get("document") or {}).get("duration_seconds") or 0)
                    try:
                        report = make_document_report(target, year, month, pdf, rel,
                                                      duration, args.min_body_chars)
                    except Exception as exc:
                        report = error_report(year, month, pdf, rel, duration,
                                              f"Report rebuild: {exc}")
                else:
                    report = error_report(year, month, pdf, rel, 0,
                                          "No existing output", status="missing_output")
            elif not args.force and _manifest_matches(target, pdf, repo_hash, args.web_first,
                                                      args.debug_bbox, args.min_body_chars, args.ocr_policy):
                report = read_json(target / "document_report.json", {})
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                report = run_document(
                    pdf, year, month, rel, target, repo, repo_hash,
                    args.web_first, args.debug_bbox, args.min_body_chars,
                    args.python_bin, output_root / "_batch_logs" / year / month / Path(*day_parts), args.ocr_policy,
                )
            reports.append(report)
            progress.update(1)
    save_reports(output_root, reports)
    return 0 if all(r["document"].get("status") == "success" for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())