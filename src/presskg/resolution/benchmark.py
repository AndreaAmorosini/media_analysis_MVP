"""Benchmark di una fase della pipeline: tempo e RSS di picco.

Uso (con Neo4j attivo):

    python -m presskg.resolution.benchmark --phase identity

Salva ``output/benchmarks/<phase>_<timestamp>.json``. Rieseguire dopo la
migrazione Stadio B per confrontare ``elapsed_seconds`` e ``peak_rss_kb``.

Nota: un benchmark "pre-Stadio-A" della fase identity non era possibile:
l'ordinamento STEPS pre-P5 non permetteva una run end-to-end da zero. Il
primo numero utile si raccade DOPO lo Stadio A (baseline per lo Stadio B).
"""

from __future__ import annotations

import argparse
import json
import resource
import time
from datetime import datetime, timezone
from pathlib import Path

from presskg.resolution import pipeline, paths


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Benchmark di una fase della pipeline")
    parser.add_argument("--phase", required=True, choices=pipeline.PHASES)
    parser.add_argument("--out", default=paths.ROOT_DEFAULT)
    parser.add_argument("--bench-dir", default="output/benchmarks")
    parser.add_argument("--stage", default="post-stadio-A",
                        help="Etichetta registrata nel report (es. post-stadio-B)")
    args = parser.parse_args(argv)

    bench = Path(args.bench_dir)
    bench.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    pipeline.run(args, state={})
    elapsed = time.perf_counter() - started

    snapshot = {
        "phase": args.phase,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(elapsed, 3),
        "peak_rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "stage": args.stage,
    }
    target = bench / f"{args.phase}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    target.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    print(f"[bench] salvato {target}")


if __name__ == "__main__":
    main()
