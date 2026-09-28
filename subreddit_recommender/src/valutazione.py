"""Dataset di valutazione e metriche.

Dataset: i post puliti con un link a un articolo esterno (non a piattaforme come YouTube o
Instagram). Ogni articolo è una notizia vera, e i subreddit in cui è stato pubblicato sono la
risposta giusta. Lo stesso articolo può essere in più sub: vale come risposta giusta ognuno.

La query è il titolo del post (non abbiamo il testo dell'articolo): in produzione la notizia
avrà anche il testo, quindi questi numeri sono prudenti.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# domini che non sono articoli: piattaforme di video, social, negozi, moduli, raccolte fondi
PIATTAFORME = (
    r"(?:^|\.)(?:youtube\.com|youtu\.be|instagram\.com|tiktok\.com|spotify\.com|"
    r"steampowered\.com|github\.com|forms\.gle|google\.com|facebook\.com|x\.com|twitter\.com|"
    r"t\.me|linkedin\.com|amazon\.[a-z.]+|amzn\.[a-z]+|twitch\.tv|imgur\.com|discord\.gg|"
    r"discord\.com|substack\.com|apple\.com|ebay\.[a-z.]+|ebay\.io|tenor\.com|giphy\.com|"
    r"reddit\.com|redd\.it|bsky\.app|threads\.net|vimeo\.com|dailymotion\.com|wikipedia\.org|"
    r"c\.org|change\.org|patreon\.com|gofundme\.com|kickstarter\.com|itch\.io)$"
)


def dataset_valutazione(post: pd.DataFrame) -> pd.DataFrame:
    """Una riga per articolo: `url`, `testo` (titolo del primo post: il testo scritto da chi
    condivide non fa parte della notizia), `subreddit_veri`
    (tutti i sub in cui è stato pubblicato), `posizioni` (righe di `post` da escludere
    dall'indice quando si valuta questo articolo).

    `post`: i post tenuti dalla pulizia, con indice 0..n-1 allineato agli embedding.
    """
    link = post[(post["dominio"] != "") & ~post["dominio"].str.contains(PIATTAFORME)]
    righe = []
    for url, g in link.sort_values("created_utc").groupby("url", sort=False):
        # si escludono anche i post con lo stesso titolo (stesso articolo con url diverso)
        stesso_titolo = post.index[
            post["titolo_pulito"].str.lower().isin(set(g["titolo_pulito"].str.lower()))
        ]
        righe.append({
            "url": url,
            "testo": g["titolo_pulito"].iloc[0],
            "subreddit_veri": sorted(set(g["subreddit"])),
            "posizioni": np.union1d(g.index.to_numpy(), stesso_titolo.to_numpy()),
        })
    return pd.DataFrame(righe)


def metriche(classifiche: list[list[str]], veri: list[list[str]], ks=(1, 3, 5, 10, 20)) -> dict:
    """recall@k (almeno un sub giusto nei primi k), MRR, e recall@20 medio per subreddit
    (`macro_r@20`: ogni sub pesa uguale, non vincono solo i sub con tanti articoli)."""
    rango = []
    for cl, v in zip(classifiche, veri):
        pos = [cl.index(s) + 1 for s in v if s in cl]
        rango.append(min(pos) if pos else np.inf)
    rango = np.array(rango)
    out = {f"r@{k}": float(np.mean(rango <= k)) for k in ks}
    out["mrr"] = float(np.mean(np.where(np.isfinite(rango), 1 / rango, 0)))
    per_sub: dict[str, list[bool]] = {}
    for cl, v in zip(classifiche, veri):
        for s in v:
            per_sub.setdefault(s, []).append(s in cl[:20])
    out["macro_r@20"] = float(np.mean([np.mean(x) for x in per_sub.values()]))
    return out
