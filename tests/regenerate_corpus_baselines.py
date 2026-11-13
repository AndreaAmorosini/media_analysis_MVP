"""Rigenera i baseline del corpus reale per la suite di regression.

Uso:
    PYTHONPATH=src pixi run python tests/regenerate_corpus_baselines.py \
        --data-root Data --output tests/corpus_manifest.json

Esegue la pipeline completa su ogni caso del manifest corrente (o sui PDF
passati con --extra) e aggiorna i valori attesi. E' uno strumento di
manutenzione, non un test: il manifest risultante (pagine minime, numero
di articoli, soglie) viene committato, i PDF mai.
"""
from __future__ import annotations

import argparse
import contextlib
import glob
import io
import json
import sys
import time
from pathlib import Path

import typer

from press_reputation.cli import parse

DEFAULT_MANIFEST = Path(__file__).resolve().parent / "corpus_manifest.json"


def run_document(pdf: Path, output_root: Path) -> dict:
    reason_dir = output_root / pdf.stem.replace(" ", "_")
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        try:
            parse(
                pdf_path=pdf,
                output_dir=output_root,
                debug_bbox=False,
                ocr_policy="auto",
                web_first=False,
                allow_untitled=False,
            )
        except typer.Exit as exit_call:
            if exit_call.exit_code != 0:
                raise

    records_path = reason_dir / "flow" / "article_records.json"
    records = json.loads(records_path.read_text(encoding="utf-8")) if records_path.exists() else []
    pages = glob.glob(str(reason_dir / "pages" / "page_*.json"))
    statuses = [record.get("finalization_status") for record in records]
    return {
        "pages": len(pages),
        "articles": len(records),
        "finalization_statuses": {status: statuses.count(status) for status in set(statuses) if status},
        "untitled": sum(1 for record in records if not record.get("title")),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default="Data", help="cartella dei PDF")
    parser.add_argument("--output", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--workdir", default=None, help="cartella di output temporanea (default: tmp)")
    parser.add_argument("--categories", default=None, help="solo queste categorie, separate da virgola")
    args = parser.parse_args()

    manifest_path = Path(args.output)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"cases": []}
    data_root = Path(args.data_root)
    only_categories = set(args.categories.split(",")) if args.categories else None

    workdir = Path(args.workdir) if args.workdir else Path("/") / "tmp" / "corpus_baseline_work"
    workdir.mkdir(parents=True, exist_ok=True)

    temp_root = workdir / "temp_output"
    temp_root.mkdir(exist_ok=True)

    failures = 0
    for case in manifest["cases"]:
        if only_categories and case["category"] not in only_categories:
            continue
        pdf = data_root / case["path"]
        if not pdf.exists():
            print(f"[SKIP] {case['id']}: PDF assente ({pdf})")
            continue
        reason_temp = temp_root / case["id"]
        reason_temp.mkdir(exist_ok=True)
        started = time.perf_counter()
        try:
            metrics = run_document(pdf, reason_temp)
        except Exception as exc:  # noqa: BLE001 - report intenzionale
            print(f"[ERROR] {case['id']}: {exc}")
            failures += 1
            continue
        elapsed = time.perf_counter() - started
        case["expected"] = {
            "min_pages": metrics["pages"],
            "articles": metrics["articles"],
            "finalization_statuses": metrics["finalization_statuses"],
            "untitled": metrics["untitled"],
        }
        case["measured_seconds"] = round(elapsed, 1)
        print(f"[OK] {case['id']}: {json.dumps(case['expected'])} ({elapsed:.1f}s)")

    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nmanifest aggiornato: {manifest_path} ({failures} errori)")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
