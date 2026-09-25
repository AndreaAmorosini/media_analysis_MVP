"""Legge i commenti dei subreddit utili, applica la pulizia e stampa quanti ne scarta ogni regola.

    python 01_analisi/analisi_commenti.py [--rileggi]

Subreddit utili: lingua "it" o "misto" nel CSV, al massimo il 20% di post NSFW.
La prima volta legge il dump (~25 s) e salva i commenti grezzi in 01_analisi/output/;
le volte successive riparte da lì (`--rileggi` per rileggere il dump).
Salva anche i commenti con il motivo di scarto, per `esempi_commenti.py`.
"""

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from pulizia import pulisci, riepilogo  # noqa: E402
from sorgenti import commenti_da_dump, subreddit_da_csv  # noqa: E402

OUTPUT = ROOT / "01_analisi" / "output"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--rileggi", action="store_true", help="rilegge il dump anche se c'è la cache")
    args = p.parse_args()

    OUTPUT.mkdir(exist_ok=True)
    grezzi = OUTPUT / "commenti_grezzi.parquet"
    if grezzi.exists() and not args.rileggi:
        raw = pd.read_parquet(grezzi)
    else:
        t = time.time()
        subs = subreddit_da_csv(ROOT / "data" / "subreddit_italiani.csv")
        raw = commenti_da_dump(ROOT / "reddit" / "reddit_parquet" / "comments", subs)
        raw.to_parquet(grezzi)
        print(f"letti {len(raw)} commenti di {len(subs)} subreddit in {time.time() - t:.0f}s")

    df = pulisci(raw)
    print(f"{len(df)} commenti, {df['subreddit'].nunique()} subreddit\n")
    print(riepilogo(df).to_string())
    df.to_parquet(OUTPUT / "commenti_puliti.parquet")


if __name__ == "__main__":
    main()
