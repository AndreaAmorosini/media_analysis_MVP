"""Testo degli articoli stampa: OCR (RapidOCR, modello italiano) sulle immagini del ritaglio Data Stampa.

Scelto con prova_ocr_stampa.py (metodo B). Le immagini dell'articolo si estraggono dal PDF scartando la
cornice Data Stampa; le strisce consecutive della stessa colonna vengono ricucite e l'OCR si fa per colonna,
nell'ordine di inserimento (= ordine di lettura). Ogni riga riconosciuta conserva la posizione sulla pagina.
"""
import io
import re

import numpy as np
import pymupdf
from PIL import Image

# Cornice Data Stampa (punti PDF, origine in alto a sinistra): tutto ciò che sta qui non è articolo.
LIMITE_INTESTAZIONE = 55      # sopra: logo testata, data, "da pag.", tiratura, logo Data Stampa
MINIATURA = (470, 690)        # x0, y0 minimi della miniatura della pagina in basso a destra
STRISCIA_DESTRA = 560         # oltre: striscia verticale dei diritti
PIE_DI_PAGINA = 815           # sotto: sezione e numero di pagina

_motore = None


def motore():
    global _motore
    if _motore is None:
        from rapidocr import RapidOCR
        _motore = RapidOCR(params={"Rec.lang_type": "it", "Global.log_level": "error"})
    return _motore


def nella_cornice(b) -> bool:
    x0, y0, x1, y1 = b
    return (y1 <= LIMITE_INTESTAZIONE or (x0 >= MINIATURA[0] and y0 >= MINIATURA[1])
            or x0 >= STRISCIA_DESTRA or y0 >= PIE_DI_PAGINA)


def immagine(doc: pymupdf.Document, xref: int) -> Image.Image:
    pix = pymupdf.Pixmap(doc, xref)
    if pix.n - pix.alpha >= 4:  # CMYK
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    return Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")


def colonne(doc: pymupdf.Document, n_pagina: int):
    """(immagine numpy, bbox PDF) di ogni colonna dell'articolo, strisce ricucite, in ordine di lettura."""
    infos = [i for i in doc[n_pagina].get_image_info(xrefs=True)
             if i["xref"] and not nella_cornice(i["bbox"]) and min(i["width"], i["height"]) >= 30]
    gruppi = []
    for info in infos:
        x0, y0, x1, _ = info["bbox"]
        if gruppi:
            px0, _, px1, py1 = gruppi[-1][-1]["bbox"]
            if abs(x0 - px0) < 3 and abs(x1 - px1) < 3 and abs(y0 - py1) < 3:
                gruppi[-1].append(info)
                continue
        gruppi.append([info])

    for gruppo in gruppi:
        pezzi = [immagine(doc, i["xref"]) for i in gruppo]
        larghezza = pezzi[0].width
        pezzi = [p if p.width == larghezza else p.resize((larghezza, round(p.height * larghezza / p.width)))
                 for p in pezzi]
        img = Image.new("RGB", (larghezza, sum(p.height for p in pezzi)), "white")
        y = 0
        for p in pezzi:
            img.paste(p, (0, y))
            y += p.height
        bbox = (min(i["bbox"][0] for i in gruppo), gruppo[0]["bbox"][1],
                max(i["bbox"][2] for i in gruppo), gruppo[-1]["bbox"][3])
        # Colonne a bassa risoluzione (150 dpi): raddoppiate, l'OCR legge meglio i caratteri piccoli.
        if img.width / ((bbox[2] - bbox[0]) / 72) < 200:
            img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
        yield np.array(img), bbox


def righe_colonna(img: np.ndarray, bbox) -> list[dict]:
    """Righe OCR di una colonna, dall'alto in basso, con bbox in punti PDF."""
    r = motore()(img)
    if not r.txts:
        return []
    sx = (bbox[2] - bbox[0]) / img.shape[1]
    sy = (bbox[3] - bbox[1]) / img.shape[0]
    # Frammenti alla stessa altezza = stessa riga, da sinistra a destra.
    elementi = sorted((((b[0][1] + b[2][1]) / 2, b, t) for b, t in zip(r.boxes, r.txts)), key=lambda e: e[0])
    righe = []
    for yc, b, t in elementi:
        h = b[2][1] - b[0][1]
        if righe and abs(yc - righe[-1]["yc"]) < h * 0.5:
            righe[-1]["pezzi"].append((b, t))
        else:
            righe.append({"yc": yc, "pezzi": [(b, t)]})
    uscita = []
    for riga in righe:
        pezzi = sorted(riga["pezzi"], key=lambda bt: bt[0][0][0])
        xs = [p[0] for b, _ in pezzi for p in b]
        ys = [p[1] for b, _ in pezzi for p in b]
        uscita.append({"testo": " ".join(t for _, t in pezzi).strip(),
                       "bbox": [round(float(v), 1) for v in (bbox[0] + min(xs) * sx, bbox[1] + min(ys) * sy,
                                                              bbox[0] + max(xs) * sx, bbox[1] + max(ys) * sy)]})
    return [u for u in uscita if u["testo"]]


def diag(pagina, bbox, livello, stato, motivo, testo=""):
    return {"pagina": pagina, "bbox": [round(float(v), 1) for v in bbox], "livello": livello, "stato": stato,
            "motivo": motivo, "testo": testo[:120]}


def immagini_cornice(doc: pymupdf.Document, n_pagina: int) -> list[dict]:
    """Diagnostica delle immagini scartate: cornice Data Stampa e immagini minuscole."""
    d = []
    for i in doc[n_pagina].get_image_info(xrefs=True):
        if nella_cornice(i["bbox"]):
            d.append(diag(n_pagina + 1, i["bbox"], "immagine", "scartato",
                          "cornice Data Stampa (logo, intestazione, miniatura o striscia)"))
        elif min(i["width"], i["height"]) < 30:
            d.append(diag(n_pagina + 1, i["bbox"], "immagine", "scartato", "immagine troppo piccola"))
    return d


def righe_articolo(doc: pymupdf.Document, pagine: list[int]) -> tuple[list[list[dict]], list[dict]]:
    """Per ogni colonna dell'articolo (in ordine di lettura) le sue righe con pagina (da 1) e bbox, e la diagnostica."""
    risultato, d = [], []
    for n in pagine:
        d += immagini_cornice(doc, n)
        for k, (img, bbox) in enumerate(colonne(doc, n)):
            righe = righe_colonna(img, bbox)
            for r in righe:
                r["pagina"] = n + 1
                r["larghezza_colonna"] = bbox[2] - bbox[0]
            if righe:
                risultato.append(righe)
                d.append(diag(n + 1, bbox, "colonna", "info", f"colonna OCR {len(risultato)} ({len(righe)} righe)"))
            else:
                d.append(diag(n + 1, bbox, "colonna", "scartato", "immagine senza testo (foto o grafica)"))
    return risultato, d


TITOLO_PAROLA = re.compile(r"\w+")


def somiglianza_parole(a: str, b: str) -> float:
    """Quota delle parole di `a` presenti in `b`."""
    pa = TITOLO_PAROLA.findall(a.lower())
    pb = set(TITOLO_PAROLA.findall(b.lower()))
    return sum(p in pb for p in pa) / len(pa) if pa else 0
