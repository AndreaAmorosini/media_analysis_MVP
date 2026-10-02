"""Confronta i modelli di embedding e i metodi di punteggio sul dataset di valutazione.

    python 02_embedding/confronta_modelli.py [--modelli bge-m3 qwen3-0.6b ...] [--ricalcola]

Richiede 01_analisi/output/post_puliti.parquet (da `analisi_post.py`).
Per ogni modello calcola gli embedding dei post tenuti (salvati in 02_embedding/output/, le volte
successive si rileggono; `--ricalcola` per rifarli), poi quelli dei titoli del dataset di
valutazione, e misura recall@k e MRR per ogni metodo. Una baseline senza testo (sempre i sub con
più articoli linkati) mostra quanto aggiungono gli embedding.
Risultati in 02_embedding/output/risultati.csv.
"""

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from candidati import IndicePost, combina, punteggio_attivita  # noqa: E402
from embedding import MODELLI, carica, codifica  # noqa: E402
from valutazione import dataset_valutazione, metriche  # noqa: E402

OUTPUT = ROOT / "02_embedding" / "output"
POST = ROOT / "01_analisi" / "output" / "post_puliti.parquet"
CATALOGO = ROOT / "data" / "subreddit_puliti.csv"


def embedding_post(nome: str, post: pd.DataFrame, ricalcola: bool) -> np.ndarray:
    file_emb, file_id = OUTPUT / f"post_{nome}.npy", OUTPUT / f"post_{nome}_id.npy"
    if file_emb.exists() and not ricalcola:
        ids = np.load(file_id, allow_pickle=True)
        if len(ids) == len(post) and (ids == post["id"].to_numpy()).all():
            return np.load(file_emb)
        print("  post cambiati dall'ultima volta: ricalcolo")
    t = time.time()
    emb = codifica(post["testo_pulito"].tolist(), nome, "doc")
    print(f"  embedding di {len(post)} post in {time.time() - t:.0f}s")
    np.save(file_emb, emb)
    np.save(file_id, post["id"].to_numpy())
    return emb


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--modelli", nargs="+", default=list(MODELLI), choices=list(MODELLI))
    p.add_argument("--ricalcola", action="store_true")
    p.add_argument("--alfa", nargs="*", type=float, default=[0.1, 0.2, 0.3, 0.5, 1.0],
                   help="pesi dell'attività da provare sul metodo del centroide")
    args = p.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)

    df = pd.read_parquet(POST)
    post = df[df["motivo_scarto"].isna()].reset_index(drop=True)
    val = dataset_valutazione(post)
    veri = val["subreddit_veri"].tolist()
    escludi = val["posizioni"].tolist()
    print(f"{len(post)} post, {post['subreddit'].nunique()} subreddit; "
          f"valutazione su {len(val)} articoli in {len({s for v in veri for s in v})} subreddit\n")

    catalogo = pd.read_csv(CATALOGO)
    risultati = []
    # baseline: sempre gli stessi sub, quelli con più post con un articolo linkato
    articoli = post.loc[np.unique(np.concatenate(escludi))]
    popolari = articoli["subreddit"].value_counts().index.tolist()
    popolari += [s for s in post["subreddit"].value_counts().index if s not in popolari]
    risultati.append({"modello": "baseline", "metodo": "sub più attivi",
                      **metriche([popolari[:20]] * len(val), veri)})

    for nome in args.modelli:
        print(f"== {nome} ({MODELLI[nome].repo})")
        emb = embedding_post(nome, post, args.ricalcola)
        q = codifica(val["testo"].tolist(), nome, "query")
        indice = IndicePost(emb, post["subreddit"].tolist())
        attivita = punteggio_attivita(catalogo, indice.sub_nomi)
        att_sub = dict(zip(indice.sub_nomi, attivita))
        metodi = {"centroide": indice.centroide(q, escludi)}
        for k in (1, 3, 5):
            metodi[f"vicini k={k}"] = indice.vicini(q, k, escludi)
        for alfa in args.alfa:
            metodi[f"centroide + attività α={alfa}"] = combina(metodi["centroide"], attivita, alfa)
        for metodo, punteggi in metodi.items():
            cl = indice.classifica(punteggi)
            # quanto sono attivi i primi 5 sub proposti (0 = come la media dei sub)
            att5 = float(np.mean([att_sub[s] for c in cl for s in c[:5]]))
            risultati.append({"modello": nome, "metodo": metodo, **metriche(cl, veri),
                              "attività_top5": att5})
        # libera la GPU prima del modello successivo
        carica.cache_clear()
        import torch
        torch.cuda.empty_cache()

    ris = pd.DataFrame(risultati)
    print("\n" + ris.round(3).to_string(index=False))
    file_ris = OUTPUT / "risultati.csv"
    if file_ris.exists():  # tiene i risultati dei modelli non rilanciati questa volta
        vecchi = pd.read_csv(file_ris)
        ris = pd.concat([vecchi[~vecchi["modello"].isin(ris["modello"])], ris])
    ris.round(4).to_csv(file_ris, index=False)


if __name__ == "__main__":
    main()
