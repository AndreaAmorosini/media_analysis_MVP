"""Adattatore PROVVISORIO: cartella di output della pipeline di estrazione -> rassegna.json (docs/schema_articolo.md).

Costruito su un solo esempio (dati/Evidenze_rassegna_23_apr_25): le regole sono euristiche e vanno
riviste quando la pipeline produrrà gli articoli assemblati (flow/article_drafts.json).
Il sentiment non viene calcolato qui (campi a null): lo aggiunge classifica_rassegna.py.

Si lancia dalla cartella backend con:
    python3 adattatore_evidenze.py ../dati/Evidenze_rassegna_23_apr_25
"""
import json
import re
import sys
from pathlib import Path

SCHEMA = 1

# Riga del sommario: "23/04/25 Metropolis 8 <titolo> <autore> 1"
#                     data     testata  pag  resto            pagina nella rassegna
RIGA_SOMMARIO = re.compile(r"^(\d{2})/(\d{2})/(\d{2}) (.+?) (\d+) (.+) (\d+)$")
DATA_RASSEGNA = re.compile(r"Rassegna del (\d{2})/(\d{2})/(\d{4})")

# Codici della pipeline di Data Stampa che l'OCR legge dentro il ritaglio (es. "DS1667").
CODICE_DS = re.compile(r"\s*\bDS\d{3,}\b")
# Parola in maiuscolo incollata alle vicine dall'OCR: "diGORIper" -> "di GORI per".
MAIUSCOLE_INCOLLATE = re.compile(r"([a-zà-ù])([A-Z]{2,})([a-zà-ù])")

# Fine frase: . ! ? (eventualmente seguiti da » o ") poi spazio e inizio maiuscolo o virgolette.
FINE_FRASE = re.compile(r"[.!?][»\"”]?(?=\s+[A-ZÀ-Ý«\"“(])")
ABBREVIAZIONI = {"dott", "dr", "prof", "sig", "sigg", "ing", "avv", "on", "sen", "dir", "resp", "art",
                 "pag", "ecc", "n", "nr", "tel", "via", "p", "es", "s", "st"}

TIPI_ESCLUSI = {"header_metadata", "footer", "watermark", "caption", "image", "article_position_thumbnail"}


def pulisci(testo: str) -> str:
    testo = CODICE_DS.sub("", testo)
    testo = MAIUSCOLE_INCOLLATE.sub(r"\1 \2 \3", testo)
    return re.sub(r"\s+", " ", testo).strip()


def dentro(bbox, contenitore) -> bool:
    return (bbox[0] >= contenitore[0] - 1 and bbox[1] >= contenitore[1] - 1
            and bbox[2] <= contenitore[2] + 1 and bbox[3] <= contenitore[3] + 1)


def leggi_sommario(pagina: dict):
    """Data della rassegna e righe del sommario."""
    data, voci = None, []
    for r in pagina["regions"]:
        testo = (r["text"] or "").strip()
        if m := DATA_RASSEGNA.search(testo):
            data = f"{m[3]}-{m[2]}-{m[1]}"
        elif m := RIGA_SOMMARIO.match(testo):
            voci.append({
                "data": f"20{m[3]}-{m[2]}-{m[1]}",
                "testata": m[4],
                "pagina_originale": int(m[5]),
                "titolo_e_autore": m[6],
                "pagina_numerata": int(m[7]),
            })
    return data, voci


def numero_a_pie_di_pagina(pagina: dict):
    for r in pagina["regions"]:
        if r["type"] == "footer" and (r["text"] or "").strip().isdigit():
            return int(r["text"])
    return None


def regioni_ritaglio(pagina: dict):
    """Regioni OCR interne al ritaglio, nell'ordine di lettura della pipeline."""
    regioni = pagina["regions"]
    # Fascia di intestazione Data Stampa: tutto ciò che sta sopra la fine dei metadati di intestazione.
    alto = [r["bbox"][3] for r in regioni if r["type"] == "header_metadata" and r["bbox"][0] < 100]
    limite_intestazione = max(alto, default=0) + 5
    miniature = [r["bbox"] for r in regioni if r["type"] == "article_position_thumbnail"]

    scelte = []
    for r in regioni:
        if r["extraction_method"] != "ocr" or not r["text"] or r["type"] in TIPI_ESCLUSI:
            continue
        if r["bbox"][3] <= limite_intestazione:
            continue
        if any(dentro(r["bbox"], m) for m in miniature):
            continue
        scelte.append(r)
    return scelte


def separa_titolo_autore(titolo_e_autore: str, titolo_ocr: str | None):
    """Il sommario ha "titolo cognome nome" senza separatore: si usa il titolo letto nel ritaglio."""
    if titolo_ocr and titolo_e_autore.startswith(titolo_ocr):
        resto = titolo_e_autore[len(titolo_ocr):].strip()
        if resto:
            parti = resto.split()
            # Nel sommario l'autore è "Cognome Nome": si riporta a "Nome Cognome".
            autore = " ".join(parti[1:] + parti[:1]) if len(parti) == 2 else resto
            return titolo_ocr, autore
        return titolo_ocr, None
    return titolo_e_autore, None


def costruisci_articolo(id_articolo: str, voce: dict, pagine: list[dict]) -> dict:
    regioni = [(p["pdf_page"], r) for p in pagine for r in regioni_ritaglio(p)]

    titoli = [r for _, r in regioni if r["raw_label"] == "section_header"]
    titolo_ocr = pulisci(titoli[0]["text"]) if titoli else None
    titolo, autore = separa_titolo_autore(voce["titolo_e_autore"], titolo_ocr)

    occhiello = None
    corpo = []
    for n_pag, r in regioni:
        testo = pulisci(r["text"])
        if r["raw_label"] == "section_header" or not testo:
            continue
        if autore and testo.lower() == autore.lower():
            continue  # firma
        if occhiello is None and not corpo and len(testo.split()) <= 3:
            occhiello = testo  # breve testo sopra il titolo, es. località
            continue
        corpo.append((n_pag, r["bbox"], testo))

    # Unione dei blocchi: un blocco che non termina con punteggiatura continua nel successivo
    # (paragrafo spezzato tra colonne); altrimenti inizia un nuovo paragrafo.
    testo_articolo, segmenti = "", []
    for n_pag, bbox, testo in corpo:
        if testo_articolo:
            testo_articolo += "\n\n" if re.search(r"[.!?»\"”]$", testo_articolo) else " "
        inizio = len(testo_articolo)
        testo_articolo += testo
        segmenti.append({"inizio": inizio, "fine": len(testo_articolo), "pagina": n_pag,
                         "bbox": [round(x, 1) for x in bbox]})

    return {
        "id": id_articolo,
        "titolo": titolo,
        "occhiello": occhiello,
        "testata": voce["testata"],
        "data": voce["data"],
        "pagina_originale": voce["pagina_originale"],
        "autore": autore,
        "pagine_rassegna": sorted({p["pdf_page"] for p in pagine}),
        "testo": testo_articolo,
        "frasi": dividi_frasi(testo_articolo, segmenti),
        "sentiment": None,
    }


def dividi_frasi(testo: str, segmenti: list[dict]) -> list[dict]:
    tagli = [0]
    for m in FINE_FRASE.finditer(testo):
        parola = re.search(r"(\w+)$", testo[:m.start()])
        if parola and parola[1].lower() in ABBREVIAZIONI:
            continue
        # Frammenti di meno di 3 parole (es. località in apertura, "Gragnano.") restano uniti alla frase dopo.
        if len(testo[tagli[-1]:m.end()].split()) < 3:
            continue
        tagli.append(m.end())
    tagli.append(len(testo))

    frasi = []
    for a, b in zip(tagli, tagli[1:]):
        # Escludi gli spazi ai bordi, mantenendo le posizioni corrette.
        while a < b and testo[a].isspace():
            a += 1
        while b > a and testo[b - 1].isspace():
            b -= 1
        if a == b:
            continue
        posizioni = [{"pagina": s["pagina"], "bbox": s["bbox"]}
                     for s in segmenti if s["inizio"] < b and s["fine"] > a]
        frasi.append({"n": len(frasi), "testo": testo[a:b], "inizio": a, "fine": b,
                      "posizioni": posizioni, "sentiment": None})
    return frasi


def converti(cartella: Path) -> dict:
    pagine = [json.loads(f.read_text(encoding="utf-8")) for f in sorted((cartella / "pages").glob("page_*.json"))]
    sommario = next(p for p in pagine if p["page_type"] != "clipping")
    ritagli = [p for p in pagine if p["page_type"] == "clipping"]
    data, voci = leggi_sommario(sommario)

    # Ogni articolo inizia alla pagina col numero indicato nel sommario e continua fino al successivo.
    inizio_per_numero = {numero_a_pie_di_pagina(p): i for i, p in enumerate(ritagli)}
    articoli = []
    for k, voce in enumerate(voci):
        inizio = inizio_per_numero.get(voce["pagina_numerata"])
        if inizio is None:
            print(f"ATTENZIONE: pagina {voce['pagina_numerata']} del sommario non trovata, articolo saltato")
            continue
        fine = inizio_per_numero.get(voci[k + 1]["pagina_numerata"], len(ritagli)) if k + 1 < len(voci) else len(ritagli)
        articoli.append(costruisci_articolo(f"{cartella.name}/{k + 1}", voce, ritagli[inizio:fine]))

    return {
        "schema": SCHEMA,
        "id": cartella.name,
        "data": data,
        "pagine": [{"n": p["pdf_page"], "larghezza": p["page_width"], "altezza": p["page_height"],
                    "immagine": f"debug/page_{p['pdf_page']:03d}.png"} for p in ritagli],
        "articoli": articoli,
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Uso: python3 adattatore_evidenze.py <cartella rassegna>")
    cartella = Path(sys.argv[1]).resolve()
    rassegna = converti(cartella)
    uscita = cartella / "rassegna.json"
    uscita.write_text(json.dumps(rassegna, ensure_ascii=False, indent=2), encoding="utf-8")
    n_frasi = sum(len(a["frasi"]) for a in rassegna["articoli"])
    print(f"{len(rassegna['articoli'])} articoli, {n_frasi} frasi -> {uscita}")
