"""Legge i post dei subreddit utili, applica la pulizia e stampa quanti ne scarta ogni regola.

    python 01_analisi/analisi_post.py [--rileggi]

Subreddit utili: lingua "it" o "misto" nel CSV, al massimo il 20% di post NSFW, esclusi quelli
in data/subreddit_esclusi.csv. La prima volta legge il dump (~5 s) e salva i post grezzi in
01_analisi/output/; le volte successive riparte da lì (`--rileggi` per rileggere il dump).
Salva anche i post con il motivo di scarto, per `esempi_post.py`, e il catalogo dei subreddit
rimasti in data/subreddit_puliti.csv (una riga per sub, da usare nella Fase 4).

Il dump si cerca in $REDDIT_DUMP, altrimenti in reddit/reddit_parquet dentro il progetto.
"""

import argparse
import os
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from pulizia import pulisci_post, riepilogo  # noqa: E402
from sorgenti import post_da_dump, subreddit_da_csv  # noqa: E402

OUTPUT = ROOT / "01_analisi" / "output"
DUMP = Path(os.environ.get("REDDIT_DUMP", ROOT / "reddit" / "reddit_parquet"))
CATALOGO = ROOT / "data" / "subreddit_puliti.csv"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--rileggi", action="store_true", help="rilegge il dump anche se c'è la cache")
    args = p.parse_args()

    OUTPUT.mkdir(exist_ok=True)
    grezzi = OUTPUT / "post_grezzi.parquet"
    if grezzi.exists() and not args.rileggi:
        raw = pd.read_parquet(grezzi)
    else:
        t = time.time()
        subs = subreddit_da_csv(
            ROOT / "data" / "subreddit_italiani.csv", esclusi=ROOT / "data" / "subreddit_esclusi.csv"
        )
        raw = post_da_dump(DUMP / "submissions", subs)
        raw.to_parquet(grezzi)
        print(f"letti {len(raw)} post di {len(subs)} subreddit in {time.time() - t:.0f}s")

    df = pulisci_post(raw)
    print(f"{len(df)} post, {df['subreddit'].nunique()} subreddit\n")
    print(riepilogo(df).to_string())

    tenuti = df[df["motivo_scarto"].isna()]
    print(f"\ntenuti: {len(tenuti)} post in {tenuti['subreddit'].nunique()} subreddit")
    print(tenuti["tipo"].value_counts().to_string())
    df.to_parquet(OUTPUT / "post_puliti.parquet")

    cat = catalogo(df)
    cat.to_csv(CATALOGO, index=False)
    print(f"\ncatalogo: {len(cat)} subreddit in {CATALOGO.relative_to(ROOT)}")


def catalogo(df: pd.DataFrame) -> pd.DataFrame:
    """Una riga per subreddit con post tenuti: è l'elenco dei sub usati nella Fase 4.

    `df`: tutti i post con `motivo_scarto` (serve per la quota di post rimossi dai moderatori).
    """
    tenuti = df[df["motivo_scarto"].isna()]
    media = tenuti["tipo"].isin(["immagine", "galleria", "video"])
    esterni = tenuti[tenuti["dominio"] != ""]
    top_domini = esterni.groupby("subreddit")["dominio"].agg(
        lambda s: ", ".join(f"{d} ({n})" for d, n in s.value_counts().head(5).items())
    )
    cat = tenuti.assign(media=media).groupby("subreddit").agg(
        n_post=("id", "size"),
        n_autori=("author", "nunique"),
        quota_testuali=("tipo", lambda s: (s == "testo").mean()),
        quota_link=("dominio", lambda s: (s != "").mean()),
        quota_media=("media", "mean"),
        mediana_commenti=("num_comments", "median"),
        mediana_score=("score", "median"),
    )
    # rischio che un post venga tolto: rimossi dai moderatori / (tenuti + rimossi dai moderatori)
    rimossi = df[df["motivo_scarto"] == "rimosso_mod"]["subreddit"].value_counts()
    rimossi = rimossi.reindex(cat.index, fill_value=0)
    cat["quota_rimossi"] = rimossi / (rimossi + cat["n_post"])
    cat["top_domini"] = top_domini.reindex(cat.index).fillna("")
    return cat.round(2).sort_values("n_post", ascending=False).reset_index()


if __name__ == "__main__":
    main()
