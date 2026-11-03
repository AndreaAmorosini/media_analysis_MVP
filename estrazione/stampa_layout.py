"""Articoli stampa dai blocchi del layout (ocr_layout.py): quali blocchi sono il corpo, con motivo per ognuno.

Restituisce segmenti {pagina, bbox, testo, nuovo_paragrafo}, diagnostica {pagina, bbox, livello, stato, motivo,
testo} e un riepilogo per il resoconto: blocchi di corpo rilevati / inclusi / vuoti, titolo ritrovato.
"""
import re
from difflib import SequenceMatcher

from stampa import nella_cornice

CORNICE = {"header", "footer", "header_image", "footer_image", "aside_text", "number"}
IMMAGINI = {"image", "chart", "seal", "table"}
DIDASCALIE = {"vision_footnote", "figure_title", "chart_title", "table_title"}
TITOLI = {"doc_title", "paragraph_title"}
CORPO = {"text", "content", "abstract", "reference_content", "footnote", "reference"}

CODICE_DS = re.compile(r"\s*(Data Stampa|Data Stamp|DS)\s*\d{3,}(\s*-\s*(Data Stampa|Data Stamp|DS)\s*\d{3,})*", re.I)
COPYRIGHT = re.compile(r"^©?\s*riproduzione riservata\.?$", re.I)
RIMANDO = re.compile(r"^(a|segue a|continua a|servizio a)\s*pag(ina)?\.?\s*\d+", re.I)


def diag(pagina, bbox, stato, motivo, testo=""):
    return {"pagina": pagina, "bbox": bbox, "livello": "blocco", "stato": stato, "motivo": motivo, "testo": testo[:120]}


def simile(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


_DIZIONARIO = None


def dizionario() -> set[str]:
    global _DIZIONARIO
    if _DIZIONARIO is None:
        _DIZIONARIO = {r.strip().lower() for r in open("/usr/share/dict/italian", encoding="utf-8")}
    return _DIZIONARIO


def ricuci(m: re.Match) -> str:
    """Sillabazione rimasta nel testo ("titani-ca"): si unisce, tranne quando entrambe le parti sono parole
    (composti come "centro-destra"). Il dizionario non ha tutte le forme, quindi non si controlla la parola unita."""
    a, b = m[1], m[2]
    if a.lower() in dizionario() and b.lower() in dizionario():
        return m[0]
    return a + b


def pulisci(testo: str) -> str:
    testo = CODICE_DS.sub(" ", testo)
    testo = re.sub(r"(\w)-\n(\w)", r"\1\2", testo)  # sillabazione a fine riga
    testo = re.sub(r"\b([a-zà-ù]+)-\s?([a-zà-ù]+)\b", ricuci, testo)
    return re.sub(r"\s+", " ", testo).strip()


def articolo(pagine_ocr: list[dict], titolo: str, autore: str | None):
    segmenti, d = [], []
    rilevati = inclusi = vuoti = 0
    titolo_trovato = False
    titolo_norm = re.split(r" - ", titolo)[0] if titolo else ""
    for p in pagine_ocr:
        n = p["pagina"]
        if p.get("errore"):
            d.append(diag(n, [0, 0, p.get("larghezza", 595), p.get("altezza", 842)], "scartato",
                          f"errore OCR: {p['errore']}"))
            continue
        # Ordine di lettura del layout; i blocchi senza ordine (immagini, intestazioni) non sono testo dell'articolo.
        blocchi = sorted(p["blocchi"], key=lambda b: (b["ordine"] is None, b["ordine"] or 0))
        for b in blocchi:
            et, bbox, testo = b["etichetta"], b["bbox"], pulisci(b["testo"])
            if et in CORNICE or nella_cornice(bbox):
                d.append(diag(n, bbox, "scartato", f"cornice o intestazione ({et})", testo))
                continue
            if et in IMMAGINI:
                d.append(diag(n, bbox, "scartato", f"immagine ({et})"))
                continue
            if et in DIDASCALIE:
                d.append(diag(n, bbox, "scartato", "didascalia", testo))
                continue
            if et in TITOLI:
                if titolo_norm and (simile(testo[:len(titolo_norm) + 20], titolo_norm) > 0.5
                                    or titolo_norm.lower()[:40] in testo.lower()):
                    titolo_trovato = True
                    d.append(diag(n, bbox, "scartato", "titolo (già nel sommario)", testo))
                else:
                    d.append(diag(n, bbox, "scartato", "titolo, sottotitolo o titoletto", testo))
                continue
            if et not in CORPO:
                d.append(diag(n, bbox, "scartato", f"tipo di blocco non testuale ({et})", testo))
                continue
            # Blocchi di testo: candidati corpo.
            if not testo:
                rilevati += 1
                vuoti += 1
                d.append(diag(n, bbox, "scartato", "blocco di testo senza testo riconosciuto"))
                continue
            if COPYRIGHT.match(testo):
                d.append(diag(n, bbox, "scartato", "nota di copyright", testo))
                continue
            if RIMANDO.match(testo) and len(testo.split()) <= 6:
                d.append(diag(n, bbox, "scartato", "rimando ad altra pagina", testo))
                continue
            if autore and len(testo.split()) <= 4 and set(testo.lower().split()) == set(autore.lower().split()):
                d.append(diag(n, bbox, "scartato", "firma", testo))
                continue
            if titolo_norm and simile(testo[:len(titolo_norm) + 20], titolo_norm) > 0.75:
                titolo_trovato = True
                d.append(diag(n, bbox, "scartato", "titolo (già nel sommario)", testo))
                continue
            rilevati += 1
            inclusi += 1
            segmenti.append({"pagina": n, "bbox": bbox, "testo": testo, "nuovo_paragrafo": True})
            d.append(diag(n, bbox, "tenuto", "corpo" + (" (riletto)" if b.get("riletto") else ""), testo))
    # Un blocco che non chiude il periodo continua nel successivo (colonna spezzata): stesso paragrafo.
    for prec, s in zip(segmenti, segmenti[1:]):
        if not re.search(r"[.!?»\"”:]$", prec["testo"]):
            s["nuovo_paragrafo"] = False
    riepilogo = {"blocchi_corpo": rilevati, "blocchi_inclusi": inclusi, "blocchi_vuoti": vuoti,
                 "titolo_trovato": titolo_trovato}
    return segmenti, d, riepilogo
