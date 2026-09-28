"""Top 20 subreddit candidati per una notizia (titolo ed eventuale descrizione).

    python 02_embedding/suggerisci.py "titolo" ["descrizione o testo della notizia"] [--n 20]

Usa il modello scelto nella Fase 4 (e5-large-instruct, metodo del centroide) e gli embedding
dei post già calcolati da `confronta_modelli.py` (in 02_embedding/output/).
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from candidati import IndicePost  # noqa: E402
from embedding import codifica  # noqa: E402

MODELLO = "e5-large-instruct"
OUTPUT = ROOT / "02_embedding" / "output"
POST = ROOT / "01_analisi" / "output" / "post_puliti.parquet"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("titolo")
    p.add_argument("descrizione", nargs="?", default="")
    p.add_argument("--n", type=int, default=20)
    args = p.parse_args()

    df = pd.read_parquet(POST)
    post = df[df["motivo_scarto"].isna()].reset_index(drop=True)
    emb = np.load(OUTPUT / f"post_{MODELLO}.npy")
    ids = np.load(OUTPUT / f"post_{MODELLO}_id.npy", allow_pickle=True)
    if len(ids) != len(post) or not (ids == post["id"].to_numpy()).all():
        sys.exit("gli embedding non corrispondono ai post puliti: rilancia confronta_modelli.py")

    indice = IndicePost(emb, post["subreddit"].tolist())
    notizia = f"{args.titolo}\n\n{args.descrizione}".strip()
    punteggi = indice.centroide(codifica([notizia], MODELLO, "query"))[0]
    for i, s in enumerate(np.argsort(-punteggi)[: args.n], 1):
        print(f"{i:2}. {indice.sub_nomi[s]:24} {punteggi[s]:.3f}")


if __name__ == "__main__":
    main()
