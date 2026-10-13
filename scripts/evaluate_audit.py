#!/usr/bin/env python3
"""Optional evaluation of manually labeled articles from audit_template.csv.

Only measures correctness within reviewed articles, not recall of articles the
pipeline never found. All six audit columns accept 1/0, yes/no, sì/no, true/false.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

COLUMNS = ("title_correct", "subtitle_correct", "date_correct", "body_complete",
           "reading_order_correct", "no_unrelated_text")
TRUE = {"1", "y", "yes", "si", "sì", "true", "ok", "corretto"}
FALSE = {"0", "n", "no", "false", "errato", "non corretto"}


def evaluate(rows):
    counts = defaultdict(lambda: {"correct": 0, "assessed": 0})
    by_month = defaultdict(lambda: defaultdict(lambda: {"correct": 0, "assessed": 0}))
    reviewed = 0
    for row in rows:
        this_row = 0
        for key in COLUMNS:
            value = (row.get(key) or "").strip().casefold()
            if value not in TRUE | FALSE:
                continue
            ok = value in TRUE
            counts[key]["assessed"] += 1
            counts[key]["correct"] += ok
            per_month = by_month[(row.get("year"), row.get("month"))][key]
            per_month["assessed"] += 1
            per_month["correct"] += ok
            this_row += 1
        reviewed += this_row > 0

    def finalize(measures):
        return {name: {**vals,
                       "correct_rate": round(vals["correct"] / vals["assessed"], 4)
                       if vals["assessed"] else None}
                for name, vals in measures.items()}

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "articles_in_audit": len(rows),
        "articles_reviewed_at_least_one_field": reviewed,
        "reviewed_fraction": round(reviewed / len(rows), 4) if rows else None,
        "metrics_among_reviewed_articles": finalize(counts),
        "months": {f"{year}/{month}": finalize(metrics)
                   for (year, month), metrics in sorted(by_month.items())},
        "interpretation": (
            "Frazioni calcolate sui soli articoli/attributi manualmente valutati. "
            "Non misurano il recall rispetto ad articoli mai individuati. "
            "Se il campione è selezionato a mano senza randomizzazione, non è "
            "necessariamente rappresentativo dell'intero corpus."
        ),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with args.audit.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    result = evaluate(rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
