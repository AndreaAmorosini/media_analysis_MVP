"""Top 20 subreddit candidati per una notizia (titolo ed eventuale descrizione).

    python 02_embedding/suggerisci.py "titolo" ["descrizione o testo della notizia"] [--n 20] [--alfa 0.2]

Usa il modello scelto nella Fase 4 (e5-large-instruct, metodo del centroide) e gli embedding
dei post già calcolati da `confronta_modelli.py` (in 02_embedding/output/).
Al punteggio di similarità si aggiunge `alfa` × attività del subreddit (dimensione della
community, commenti e voti tipici, rischio di rimozione: vedi `candidati.PESI_ATTIVITA`),
calcolato su tutti i subreddit prima di prendere i primi N. `--alfa 0` = solo similarità.
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from candidati import IndicePost, combina, punteggio_attivita  # noqa: E402
from embedding import codifica  # noqa: E402

MODELLO = "e5-large-instruct"
OUTPUT = ROOT / "02_embedding" / "output"
POST = ROOT / "01_analisi" / "output" / "post_puliti.parquet"
CATALOGO = ROOT / "data" / "subreddit_puliti.csv"
# scelto sulla valutazione: migliora le prime posizioni perdendo poco sui 20 candidati
ALFA = 0.2


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("titolo")
    p.add_argument("descrizione", nargs="?", default="")
    p.add_argument("--n", type=int, default=20)
    p.add_argument("--alfa", type=float, default=ALFA, help="peso dell'attività (0 = solo similarità)")
    args = p.parse_args()

    df = pd.read_parquet(POST)
    post = df[df["motivo_scarto"].isna()].reset_index(drop=True)
    emb = np.load(OUTPUT / f"post_{MODELLO}.npy")
    ids = np.load(OUTPUT / f"post_{MODELLO}_id.npy", allow_pickle=True)
    if len(ids) != len(post) or not (ids == post["id"].to_numpy()).all():
        sys.exit("gli embedding non corrispondono ai post puliti: rilancia confronta_modelli.py")

    indice = IndicePost(emb, post["subreddit"].tolist())
    notizia = f"{args.titolo}\n\n{args.descrizione}".strip()
    similarita = indice.centroide(codifica([notizia], MODELLO, "query"))
    attivita = punteggio_attivita(pd.read_csv(CATALOGO), indice.sub_nomi)
    finale = combina(similarita, attivita, args.alfa)[0]
    similarita = similarita[0]
    # posizione che il sub avrebbe con la sola similarità
    pos_sim = np.empty(len(similarita), dtype=int)
    pos_sim[np.argsort(-similarita)] = np.arange(1, len(similarita) + 1)

    print(f"{'':4}{'subreddit':24} {'similarità':>10} {'attività':>9} {'finale':>7}  solo similarità")
    for i, s in enumerate(np.argsort(-finale)[: args.n], 1):
        spostamento = pos_sim[s] - i
        freccia = f"↑{spostamento}" if spostamento > 0 else f"↓{-spostamento}" if spostamento < 0 else "="
        print(f"{i:2}. {indice.sub_nomi[s]:24} {similarita[s]:10.3f} {attivita[s]:9.2f} "
              f"{finale[s]:7.2f}  {pos_sim[s]:3}° {freccia}")


if __name__ == "__main__":
    main()
