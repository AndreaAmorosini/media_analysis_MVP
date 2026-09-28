"""Pulizia di commenti e post Reddit, indipendente dalla sorgente (dump o API).

`pulisci(df)` (commenti) e `pulisci_post(df)` non eliminano nulla: aggiungono
- `testo_pulito`: testo normalizzato (senza citazioni, link, markdown)
- `n_parole`
- `motivo_scarto`: il primo motivo per cui la riga va scartata, o None se si tiene

Così si può misurare quanto pesa ogni regola prima di decidere le soglie.
Le regole "di gruppo" (testi ripetuti, autori ripetitivi) guardano tutto il DataFrame:
funzionano meglio su lotti grandi, come il dump.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from functools import cache
from urllib.parse import urlsplit

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
# messaggio che Reddit mette al posto dei contenuti che il vecchio sito non sa mostrare
_AVVISO_OLD_REDDIT = re.compile(
    r"This post contains content not supported on old Reddit\.\s*Click here to view the full post\.?"
)


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
    testo = _AVVISO_OLD_REDDIT.sub(" ", testo)
    testo = testo.replace("​", " ")
    return _SPAZI.sub(" ", testo).strip()


def _e_bot(autori: pd.Series, bot: set[str]) -> pd.Series:
    return autori.isin(bot) | autori.str.lower().str.endswith("bot")


def _applica_motivi(df: pd.DataFrame, motivi: list[tuple[str, pd.Series]]) -> None:
    """Scrive in `motivo_scarto` il primo motivo che si applica a ogni riga."""
    df["motivo_scarto"] = None
    for motivo, maschera in motivi:
        df.loc[maschera.fillna(False) & df["motivo_scarto"].isna(), "motivo_scarto"] = motivo


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
        ("bot_nome", _e_bot(df["author"], r.autori_bot)),
        ("bot_frase", df["testo_pulito"].str.contains(FRASI_BOT)),
        (
            "moderazione",
            (df["distinguished"] == "moderator") | df["stickied"].fillna(False).astype(bool),
        ),
        ("corto", df["n_parole"] < r.min_parole),
        ("ripetuto", ripetizioni >= r.min_ripetizioni),
        ("autore_ripetitivo", df["author"].isin(autori_ripetitivi)),
    ]
    _applica_motivi(df, motivi)
    return df


# ---------------------------------------------------------------------------- post

# titolo o testo sostituiti da Reddit o dai moderatori: il contenuto originale non c'è più
_RIMOSSO_DA_REDDIT = re.compile(r"(?i)^\[ ?removed by (reddit|moderator)")
TESTI_ASSENTI = {"[removed]", "[deleted]"}
DOMINI_IMMAGINE = {"i.redd.it", "i.imgur.com", "imgur.com"}
DOMINI_VIDEO = {"v.redd.it", "youtube.com", "m.youtube.com", "youtu.be"}
_CROSSPOST = re.compile(r"^(/r/|https?://(www\.|old\.)?reddit\.com/r/)")

# lingue da cui distinguere l'italiano: quelle che compaiono nei sub italiani
LINGUE_CONFRONTO = ["ITALIAN", "ENGLISH", "SPANISH", "PORTUGUESE", "FRENCH", "GERMAN", "ROMANIAN"]


@dataclass
class RegolePost:
    # parole di titolo + testo: i titoli di notizie ne hanno quasi sempre più di 4
    min_parole: int = 4
    # stesso autore, stesso post (titolo + testo) almeno N volte nel lotto → spam o
    # autopromozione pubblicata in molti sub
    min_ripetizioni: int = 5
    # `selftext == "[removed]"`: post tolto dai moderatori, quindi giudicato non adatto al sub
    tieni_rimossi_dai_mod: bool = False
    # confidenza minima che il testo sia italiano (0-1, rispetto a LINGUE_CONFRONTO): 0,2 tiene
    # i titoli misti ("Che ne pensate di Steam Machine?"), sotto è quasi tutto inglese
    min_conf_italiano: float = 0.2
    # subreddit con meno di N post tenuti nel lotto: troppo pochi per descriverlo
    min_post_sub: int = 20
    # "[deleted]" non c'è: per i post l'autore cancellato lascia un titolo valido
    autori_bot: set[str] = field(default_factory=lambda: AUTORI_BOT - {"[deleted]"})


def dominio_url(url: str) -> str:
    """Dominio del link, senza `www.` ("" per i link relativi come `/r/...`).

    Si ricava dall'url perché il campo `domain` del dump a volte contiene il permalink del post
    (succede con i video v.redd.it).
    """
    try:
        return urlsplit(url or "").hostname.removeprefix("www.")
    except (AttributeError, ValueError):
        return ""


def tipo_post(df: pd.DataFrame) -> pd.Series:
    """testo / link / immagine / video / galleria / crosspost / altro."""
    url = df["url"].fillna("")
    dominio = url.map(dominio_url)
    tipo = pd.Series("link", index=df.index)
    tipo[url == ""] = "altro"
    tipo[url.str.match(_CROSSPOST)] = "crosspost"
    tipo[dominio.isin(DOMINI_VIDEO)] = "video"
    tipo[dominio.isin(DOMINI_IMMAGINE)] = "immagine"
    tipo[url.str.contains("reddit.com/gallery/", regex=False)] = "galleria"
    tipo[df["is_self"].fillna(False).astype(bool)] = "testo"
    return tipo


@cache
def _rilevatore():
    from lingua import Language, LanguageDetectorBuilder

    lingue = [getattr(Language, nome) for nome in LINGUE_CONFRONTO]
    return LanguageDetectorBuilder.from_languages(*lingue).with_preloaded_language_models().build()


def confidenza_italiano(testi: pd.Series) -> pd.Series:
    """Probabilità (0-1) che ogni testo sia italiano, con il rilevatore `lingua`."""
    from lingua import Language

    # i primi 1000 caratteri bastano e tengono basso il tempo sui testi lunghi
    valori = _rilevatore().compute_language_confidence_in_parallel(
        testi.str[:1000].tolist(), Language.ITALIAN
    )
    return pd.Series(valori, index=testi.index)


def pulisci_post(df: pd.DataFrame, regole: RegolePost | None = None) -> pd.DataFrame:
    """Aggiunge ai post `titolo_pulito`, `corpo_pulito`, `testo_pulito` (titolo + corpo),
    `n_parole`, `tipo`, `dominio` (solo per link e video esterni), `conf_italiano` e
    `motivo_scarto`.

    Il titolo è il nucleo: il corpo si aggiunge quando c'è (post testuali non rimossi).
    """
    r = regole or RegolePost()
    df = df.copy()
    corpo = df["selftext"].fillna("")
    corpo_assente = corpo.isin(TESTI_ASSENTI) | corpo.str.match(_RIMOSSO_DA_REDDIT)
    df["titolo_pulito"] = df["title"].fillna("").map(normalizza)
    df["corpo_pulito"] = corpo.where(~corpo_assente, "").map(normalizza)
    df["testo_pulito"] = (df["titolo_pulito"] + "\n\n" + df["corpo_pulito"]).str.strip()
    df["n_parole"] = df["testo_pulito"].str.split().str.len().fillna(0).astype(int)
    df["tipo"] = tipo_post(df)
    dominio = df["url"].map(dominio_url)
    esterno = df["tipo"].isin(["link", "video"]) & ~dominio.str.endswith("redd.it")
    df["dominio"] = dominio.where(esterno, "")
    df["conf_italiano"] = confidenza_italiano(df["testo_pulito"])

    chiave = df["testo_pulito"].str.lower()
    # per autore: la stessa notizia condivisa da utenti diversi in sub diversi non è spam
    ripetizioni = df.assign(k=chiave).groupby(["author", "k"])["id"].transform("size")
    # stesso post nello stesso sub (ripubblicato): si tiene il primo
    duplicato = (
        df.assign(k=chiave).sort_values("created_utc").duplicated(["subreddit", "k"]).reindex(df.index)
    )

    motivi = [
        (
            "rimosso",
            df["title"].fillna("").str.match(_RIMOSSO_DA_REDDIT)
            | corpo.str.match(_RIMOSSO_DA_REDDIT),
        ),
        ("rimosso_mod", (corpo == "[removed]") & (not r.tieni_rimossi_dai_mod)),
        ("nsfw", df["over_18"].fillna(False).astype(bool)),
        ("bot_nome", _e_bot(df["author"], r.autori_bot)),
        ("bot_frase", df["testo_pulito"].str.contains(FRASI_BOT)),
        (
            "moderazione",
            (df["distinguished"] == "moderator") | df["stickied"].fillna(False).astype(bool),
        ),
        ("corto", df["n_parole"] < r.min_parole),
        ("non_italiano", df["conf_italiano"] < r.min_conf_italiano),
        ("ripetuto", ripetizioni >= r.min_ripetizioni),
        ("duplicato", duplicato),
    ]
    _applica_motivi(df, motivi)

    # ultima regola, sui post rimasti: i subreddit con troppo pochi post non si usano
    tenuti = df["motivo_scarto"].isna()
    per_sub = df.loc[tenuti, "subreddit"].value_counts()
    piccoli = per_sub[per_sub < r.min_post_sub].index
    df.loc[tenuti & df["subreddit"].isin(piccoli), "motivo_scarto"] = "sub_piccolo"
    return df


def riepilogo(df: pd.DataFrame) -> pd.DataFrame:
    """Quante righe per motivo di scarto (None = tenute)."""
    conteggi = df["motivo_scarto"].fillna("TENUTO").value_counts()
    return pd.DataFrame(
        {"righe": conteggi, "quota": (conteggi / len(df)).round(3)}
    )
