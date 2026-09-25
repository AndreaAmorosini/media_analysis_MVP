"""Pulizia dei commenti Reddit, indipendente dalla sorgente (dump o API).

`pulisci(df)` non elimina nulla: aggiunge
- `testo_pulito`: testo normalizzato (senza citazioni, link, markdown)
- `n_parole`
- `motivo_scarto`: il primo motivo per cui il commento va scartato, o None se si tiene

Così si può misurare quanto pesa ogni regola prima di decidere le soglie.
Le regole "di gruppo" (testi ripetuti, autori ripetitivi) guardano tutto il DataFrame:
funzionano meglio su lotti grandi, come il dump.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

import pandas as pd

TESTI_RIMOSSI = {"[deleted]", "[removed]", "[ Removed by Reddit ]", "[removed by reddit]"}
AUTORI_BOT = {"AutoModerator", "[deleted]", "profanitycounter"}
# frasi con cui i bot si presentano, per quelli che non hanno "bot" nel nome
FRASI_BOT = r"(?i)\bi am a bot\b|\bi'm a bot\b|\bsono un bot\b|performed automatically|beep boop"

_CITAZIONE = re.compile(r"^\s*(>|&gt;).*$", re.MULTILINE)
_LINK_MARKDOWN = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"https?://\S+|www\.\S+")
_UTENTE = re.compile(r"(?<!\w)/?u/[\w-]+")
_MARKDOWN = re.compile(r"[*_~`#|]+")
_SPAZI = re.compile(r"\s+")


@dataclass
class Regole:
    min_parole: int = 5
    # stesso testo (normalizzato) ripetuto almeno N volte nel lotto → messaggio automatico
    min_ripetizioni: int = 5
    # autore con almeno N commenti, di cui questa quota sono testi che ha già scritto
    autore_min_commenti: int = 20
    autore_quota_ripetuti: float = 0.5
    autori_bot: set[str] = field(default_factory=lambda: set(AUTORI_BOT))


def normalizza(testo: str) -> str:
    testo = html.unescape(testo or "")
    testo = _CITAZIONE.sub(" ", testo)
    testo = _LINK_MARKDOWN.sub(r"\1", testo)
    testo = _URL.sub(" ", testo)
    testo = _UTENTE.sub(" ", testo)
    testo = _MARKDOWN.sub(" ", testo)
    testo = testo.replace("​", " ")
    return _SPAZI.sub(" ", testo).strip()


def pulisci(df: pd.DataFrame, regole: Regole | None = None) -> pd.DataFrame:
    r = regole or Regole()
    df = df.copy()
    df["testo_pulito"] = df["body"].fillna("").map(normalizza)
    df["n_parole"] = df["testo_pulito"].str.split().str.len().fillna(0).astype(int)
    chiave = df["testo_pulito"].str.lower()

    ripetizioni = chiave.map(chiave.value_counts())
    # testi (non corti) che lo stesso autore ha scritto più di una volta
    lunghi = df["n_parole"] >= r.min_parole
    gia_scritto = df[lunghi].assign(k=chiave[lunghi]).duplicated(["author", "k"], keep=False)
    per_autore = gia_scritto.groupby(df.loc[lunghi, "author"]).agg(["size", "mean"])
    autori_ripetitivi = per_autore[
        (per_autore["size"] >= r.autore_min_commenti)
        & (per_autore["mean"] >= r.autore_quota_ripetuti)
    ].index

    # in ordine: vale il primo motivo che si applica
    motivi = [
        ("rimosso", df["body"].isin(TESTI_RIMOSSI)),
        (
            "bot_nome",
            df["author"].isin(r.autori_bot) | df["author"].str.lower().str.endswith("bot"),
        ),
        ("bot_frase", df["testo_pulito"].str.contains(FRASI_BOT)),
        (
            "moderazione",
            (df["distinguished"] == "moderator") | df["stickied"].fillna(False).astype(bool),
        ),
        ("corto", df["n_parole"] < r.min_parole),
        ("ripetuto", ripetizioni >= r.min_ripetizioni),
        ("autore_ripetitivo", df["author"].isin(autori_ripetitivi)),
    ]
    df["motivo_scarto"] = None
    for motivo, maschera in motivi:
        df.loc[maschera.fillna(False) & df["motivo_scarto"].isna(), "motivo_scarto"] = motivo
    return df


def riepilogo(df: pd.DataFrame) -> pd.DataFrame:
    """Quanti commenti per motivo di scarto (None = tenuti)."""
    conteggi = df["motivo_scarto"].fillna("TENUTO").value_counts()
    return pd.DataFrame(
        {"commenti": conteggi, "quota": (conteggi / len(df)).round(3)}
    )
