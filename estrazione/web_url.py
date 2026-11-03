"""Articoli web dall'URL originale (scritto nella pagina Data Stampa), con verifica che il testo sia valido.

Il testo scaricato (trafilatura) si usa solo se:
  - non è bloccato: nessun segno di paywall/accesso riservato e almeno MIN_PAROLE parole;
  - è lo stesso articolo: le sue parole compaiono nel testo nativo della pagina catturata nel PDF
    (coerenza >= SOGLIA_COERENZA); se la pagina non ha testo nativo, si confronta il titolo;
  - è completo: contiene il corpo letto dal PDF (copertura >= SOGLIA_COPERTURA), così un articolo troncato
    da un paywall o modificato non passa.
Altrimenti si restituisce il motivo, e l'articolo si estrae dal PDF.
I download sono in cache (dati/archivio/url/<sha1>.json): rilanciando non si contattano di nuovo i siti.
"""
import hashlib
import json
import re
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

import trafilatura
from trafilatura.settings import use_config

MIN_PAROLE = 40
SOGLIA_COERENZA = 0.75
SOGLIA_COPERTURA = 0.8
SOGLIA_TITOLO = 0.6

PAYWALL = re.compile(r"(abbonati per (continuare|leggere)|per continuare a leggere|contenuto (riservato|esclusivo) "
                     r"(agli|per gli) abbonati|accedi per (leggere|continuare)|articolo riservato|"
                     r"sei già abbonato|registrati per (leggere|continuare)|leggi l'articolo completo|"
                     r"subscribe to (continue|read)|attiva l'abbonamento)", re.I)
PAROLA = re.compile(r"\w+")

_config = use_config()
_config.set("DEFAULT", "DOWNLOAD_TIMEOUT", "20")
_config.set("DEFAULT", "MAX_REDIRECTS", "5")


def parole(testo: str) -> Counter:
    return Counter(PAROLA.findall(testo.lower()))


def quota_contenuta(a: str, b: str) -> float | None:
    """Quota delle parole di `a` presenti in `b` (None se `a` è vuoto)."""
    pa, pb = parole(a), parole(b)
    n = sum(pa.values())
    return sum((pa & pb).values()) / n if n else None


def scarica(url: str, cache: Path) -> dict:
    """Testo e titolo della pagina originale, con cache su disco."""
    file = cache / f"{hashlib.sha1(url.encode()).hexdigest()}.json"
    if file.exists():
        return json.loads(file.read_text(encoding="utf-8"))
    risultato = {"url": url, "testo": None, "titolo": None, "errore": None}
    try:
        html = trafilatura.fetch_url(url, config=_config)
        if not html:
            risultato["errore"] = "pagina non raggiungibile"
        else:
            risultato["testo"] = trafilatura.extract(html, include_comments=False, include_tables=False,
                                                     favor_precision=True, config=_config)
            meta = trafilatura.extract_metadata(html)
            risultato["titolo"] = meta.title if meta else None
            if not risultato["testo"]:
                risultato["errore"] = "nessun testo estraibile"
    except Exception as e:  # siti con certificati scaduti, codifiche rotte, ecc.
        risultato["errore"] = f"errore di download ({type(e).__name__})"
    cache.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(risultato, ensure_ascii=False), encoding="utf-8")
    return risultato


def valuta(scaricato: dict, testo_pdf: str, corpo_pdf: str, titolo: str) -> dict:
    """Decide se il testo dall'URL è utilizzabile. Restituisce {usabile, motivo, coerenza, copertura, titolo_sim}."""
    esito = {"usabile": False, "motivo": None, "coerenza": None, "copertura": None, "titolo_sim": None,
             "parole": 0}
    if scaricato.get("errore"):
        esito["motivo"] = scaricato["errore"]
        return esito
    testo = scaricato["testo"]
    esito["parole"] = len(testo.split())
    if scaricato.get("titolo") and titolo:
        esito["titolo_sim"] = round(SequenceMatcher(None, scaricato["titolo"].lower()[:200],
                                                    re.split(r" - | \| ", titolo)[0].lower()).ratio(), 3)
    if PAYWALL.search(testo) or esito["parole"] < MIN_PAROLE:
        esito["motivo"] = "bloccato o troppo corto (paywall, accesso riservato)"
        return esito
    if len(testo_pdf.split()) >= 50:
        esito["coerenza"] = round(quota_contenuta(testo, testo_pdf), 3)
        if esito["coerenza"] < SOGLIA_COERENZA:
            esito["motivo"] = f"non corrisponde alla pagina catturata (coerenza {esito['coerenza']:.2f})"
            return esito
    elif (esito["titolo_sim"] or 0) < SOGLIA_TITOLO:
        esito["motivo"] = "pagina senza testo nativo e titolo diverso"
        return esito
    if len(corpo_pdf.split()) >= 30:
        esito["copertura"] = round(quota_contenuta(corpo_pdf, testo), 3)
        if esito["copertura"] < SOGLIA_COPERTURA:
            esito["motivo"] = f"incompleto rispetto al PDF (copertura {esito['copertura']:.2f})"
            return esito
    esito["usabile"] = True
    esito["motivo"] = "verificato"
    return esito


def paragrafi(testo: str) -> list[str]:
    return [p.strip() for p in testo.split("\n") if p.strip()]
