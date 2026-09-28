"""Lettura di commenti e post Reddit da sorgenti diverse, tutte nello stesso formato.

Ogni sorgente restituisce un DataFrame con le colonne di COLONNE_COMMENTI o COLONNE_POST: sono i nomi
dei campi dell'API di Reddit, che il dump mensile riporta identici. La pulizia
(`pulizia.py`) lavora solo su queste colonne, quindi non sa da dove arrivano i dati.

Sorgenti:
- dump Parquet mensile (`commenti_da_dump`, `post_da_dump`)
- API di Reddit: da aggiungere (stesso formato di output)
"""

from __future__ import annotations

import csv
import glob
import os
from pathlib import Path

import pandas as pd

COLONNE_COMMENTI = [
    "id",
    "subreddit",
    "author",
    "body",
    "score",
    "created_utc",
    "parent_id",  # t3_… = risposta diretta al post, t1_… = risposta a un commento
    "link_id",  # t3_<id del post>
    "distinguished",  # "moderator" / "admin" / None
    "stickied",
]


COLONNE_POST = [
    "id",
    "subreddit",
    "author",
    "title",
    "selftext",  # vuoto per i post link/immagine
    "url",
    "domain",  # "self.<sub>" per i post testuali
    "score",
    "num_comments",
    "created_utc",
    "over_18",
    "is_self",
    "distinguished",
    "stickied",
]


def subreddit_da_csv(
    path: str | Path,
    lingue: tuple[str, ...] = ("it", "misto"),
    max_nsfw: float = 0.2,
    esclusi: str | Path | None = None,
) -> list[str]:
    """Subreddit utilizzabili dalla tabella `subreddit_italiani.csv`.

    `esclusi`: CSV con colonna `subreddit` (es. `subreddit_esclusi.csv`) dei sub da togliere
    a mano: per adulti o di spam che passano la soglia NSFW, referral, annunci personali.
    """
    via = set()
    if esclusi:
        with open(esclusi, encoding="utf-8") as f:
            via = {r["subreddit"] for r in csv.DictReader(f)}
    with open(path, encoding="utf-8") as f:
        return [
            r["subreddit"]
            for r in csv.DictReader(f)
            if r["lingua"] in lingue
            and float(r["quota_nsfw"] or 0) <= max_nsfw
            and r["subreddit"] not in via
        ]


def commenti_da_dump(cartella: str | Path, subreddits: list[str]) -> pd.DataFrame:
    """Commenti dei subreddit indicati, letti dai file Parquet del dump (`<dump>/comments`)."""
    return _leggi_dump(cartella, COLONNE_COMMENTI, subreddits)


def post_da_dump(cartella: str | Path, subreddits: list[str]) -> pd.DataFrame:
    """Post dei subreddit indicati, letti dai file Parquet del dump (`<dump>/submissions`)."""
    return _leggi_dump(cartella, COLONNE_POST, subreddits)


def _leggi_dump(cartella: str | Path, colonne: list[str], subreddits: list[str]) -> pd.DataFrame:
    """Salta i file vuoti (copie incomplete), che DuckDB non riesce a leggere."""
    import duckdb

    files = [
        f
        for f in glob.glob(str(Path(cartella) / "**/*.parquet"), recursive=True)
        if os.path.getsize(f) > 0
    ]
    if not files:
        raise FileNotFoundError(f"nessun file Parquet non vuoto in {cartella}")
    con = duckdb.connect()
    con.execute("set enable_progress_bar=false")
    return con.execute(
        f"select {', '.join(colonne)} from read_parquet(?) "
        "where subreddit in (select unnest(?))",
        [files, subreddits],
    ).df()
