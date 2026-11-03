"""Prototipo: confronto di metodi OCR sulle pagine stampa del campione.

Metodi:
  A  blocchi + EasyOCR     immagini dell'articolo estratte dal PDF, OCR blocco per blocco nell'ordine di inserimento
  B  blocchi + RapidOCR    come A, con RapidOCR (modello italiano PP-OCRv6)
  C  docling               pagina intera con Docling (OCR EasyOCR), testo filtrato con la stessa geometria
  D  blocchi + PaddleOCR-VL-1.6 (0,9B, Transformers) sulle colonne ricucite, compito "OCR:"
  E  come D, ma con la versione GGUF ufficiale via Ollama (llama.cpp): molto più veloce su Turing

Misura automatica: quota di parole (>= 3 lettere) presenti nel vocabolario italiano
(/usr/share/dict/italian + parole dei testi nativi delle pagine web del campione), e tempo per pagina.
Testi in dati/prototipo/ocr/<metodo>/<n>.txt, riepilogo in dati/prototipo/ocr/riepilogo.json.

    CUDA_VISIBLE_DEVICES=0 env -u PYTHONPATH ../.venv/bin/python prova_ocr_stampa.py [metodi, es. A,B,C]
"""
import io
import os
import json
import re
import statistics
import sys
import time
from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image

pymupdf.TOOLS.mupdf_display_errors(False)

ARCHIVIO = Path.home() / "Documenti/autentica/Data"
PROTOTIPO = Path(__file__).resolve().parent.parent / "dati/prototipo"
USCITA = PROTOTIPO / "ocr"

# Cornice Data Stampa (punti PDF, origine in alto a sinistra): tutto ciò che sta qui non è articolo.
LIMITE_INTESTAZIONE = 55      # sopra: logo testata, data, "da pag.", tiratura, logo Data Stampa
MINIATURA = (470, 690)        # x0, y0 minimi della miniatura della pagina in basso a destra
STRISCIA_DESTRA = 560         # oltre: striscia verticale dei diritti
PIE_DI_PAGINA = 815           # sotto: sezione e numero di pagina


def nella_cornice(b) -> bool:
    x0, y0, x1, y1 = b
    return (y1 <= LIMITE_INTESTAZIONE or (x0 >= MINIATURA[0] and y0 >= MINIATURA[1])
            or x0 >= STRISCIA_DESTRA or y0 >= PIE_DI_PAGINA)


def immagine(doc: pymupdf.Document, xref: int) -> Image.Image:
    pix = pymupdf.Pixmap(doc, xref)
    if pix.n - pix.alpha >= 4:  # CMYK
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    return Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")


def blocchi_articolo(doc: pymupdf.Document, n_pagina: int, raddoppia: bool = True):
    """Immagini dell'articolo (RGB numpy) nell'ordine in cui Data Stampa le ha inserite.

    Data Stampa taglia ogni colonna in strisce di poche righe, a volte a metà riga: le strisce
    consecutive della stessa colonna (stessa x, una sotto l'altra) vengono ricucite in un'unica immagine.
    """
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
        # Blocchi a bassa risoluzione (150 dpi): raddoppiati, l'OCR legge meglio i caratteri piccoli.
        b = gruppo[0]["bbox"]
        if raddoppia and img.width / ((b[2] - b[0]) / 72) < 200:
            img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
        yield np.array(img)


def in_righe(scatole, testi) -> list[str]:
    """Riordina i frammenti riconosciuti in righe (stessa altezza) da sinistra a destra, righe dall'alto."""
    elementi = sorted(((b[0][1] + b[2][1]) / 2, b[0][0], b[2][1] - b[0][1], t) for b, t in zip(scatole, testi))
    righe = []
    for yc, x, h, t in elementi:
        if righe and abs(yc - righe[-1][0]) < h * 0.5:
            righe[-1][1].append((x, t))
        else:
            righe.append((yc, [(x, t)]))
    return [" ".join(t for _, t in sorted(pezzi)) for _, pezzi in righe]


def pulisci(testo: str) -> str:
    testo = re.sub(r"(\w)[-¬] (\w)", r"\1\2", testo)  # sillabazione a fine riga: "allac- cio"
    return re.sub(r"\s+", " ", testo).strip()


# ---- Metodi ------------------------------------------------------------------

def metodo_easyocr():
    import easyocr
    lettore = easyocr.Reader(["it"], gpu=True, verbose=False)

    def ocr(doc, n_pagina):
        parti = []
        for img in blocchi_articolo(doc, n_pagina):
            parti += lettore.readtext(img, detail=0, paragraph=True)
        return pulisci(" ".join(parti))
    return ocr


def metodo_rapidocr():
    from rapidocr import RapidOCR
    motore = RapidOCR(params={"Rec.lang_type": "it"})

    def ocr(doc, n_pagina):
        parti = []
        for img in blocchi_articolo(doc, n_pagina):
            r = motore(img)
            if r.txts:
                parti += in_righe(r.boxes, r.txts)
        return pulisci(" ".join(parti))
    return ocr


def metodo_docling():
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import EasyOcrOptions, PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption
    opzioni = PdfPipelineOptions(do_ocr=True, ocr_options=EasyOcrOptions(lang=["it"], force_full_page_ocr=True))
    conv = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opzioni)})

    def ocr(doc, n_pagina):
        r = conv.convert(doc.name, page_range=(n_pagina + 1, n_pagina + 1))
        altezza = doc[n_pagina].rect.height
        parti = []
        for t in r.document.texts:
            for p in t.prov:
                b = p.bbox  # origine in basso a sinistra
                if not nella_cornice((b.l, altezza - b.t, b.r, altezza - b.b)):
                    parti.append(t.text)
                    break
        return pulisci(" ".join(parti))
    return ocr


def metodo_paddlevl():
    import os
    import torch
    from transformers import AutoModelForImageTextToText, AutoProcessor
    percorso = "PaddlePaddle/PaddleOCR-VL-1.6"
    # Turing non supporta bf16 (pesi originali): fp16, oppure fp32 con PADDLEVL_FP32=1.
    dtype = torch.float32 if os.environ.get("PADDLEVL_FP32") else torch.float16
    modello = AutoModelForImageTextToText.from_pretrained(percorso, dtype=dtype).to("cuda").eval()
    proc = AutoProcessor.from_pretrained(percorso)
    lato_min = proc.image_processor.size["shortest_edge"]

    def ocr(doc, n_pagina):
        parti = []
        # Risoluzione originale: nel test era 7 volte più veloce del raddoppio, con la stessa qualità.
        for img in blocchi_articolo(doc, n_pagina, raddoppia=False):
            msg = [{"role": "user", "content": [{"type": "image", "image": Image.fromarray(img)},
                                                {"type": "text", "text": "OCR:"}]}]
            inp = proc.apply_chat_template(
                msg, add_generation_prompt=True, tokenize=True, return_dict=True, return_tensors="pt",
                images_kwargs={"size": {"shortest_edge": lato_min, "longest_edge": 2048 * 28 * 28}}).to("cuda")
            with torch.no_grad():
                out = modello.generate(**inp, max_new_tokens=2048, do_sample=False)
            parti.append(proc.decode(out[0][inp["input_ids"].shape[-1]:-1]))
        return pulisci(" ".join(parti))
    return ocr


def metodo_paddlevl_ollama():
    import base64
    import urllib.request
    url = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11435/api/chat")

    def ocr(doc, n_pagina):
        parti = []
        for img in blocchi_articolo(doc, n_pagina, raddoppia=False):
            buf = io.BytesIO()
            Image.fromarray(img).save(buf, format="PNG")
            richiesta = {"model": "hf.co/PaddlePaddle/PaddleOCR-VL-1.6-GGUF", "stream": False,
                         "options": {"temperature": 0, "num_predict": 2048},
                         "messages": [{"role": "user", "content": "OCR:",
                                       "images": [base64.b64encode(buf.getvalue()).decode()]}]}
            req = urllib.request.Request(url, data=json.dumps(richiesta).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=600) as resp:
                parti.append(json.load(resp)["message"]["content"])
        return pulisci(" ".join(parti))
    return ocr


METODI = {"A": ("blocchi + EasyOCR", metodo_easyocr), "B": ("blocchi + RapidOCR", metodo_rapidocr),
          "C": ("Docling pagina intera", metodo_docling), "D": ("blocchi + PaddleOCR-VL-1.6", metodo_paddlevl),
          "E": ("blocchi + PaddleOCR-VL-1.6 GGUF (Ollama)", metodo_paddlevl_ollama)}


# ---- Vocabolario e misura ----------------------------------------------------

PAROLA = re.compile(r"[a-zàáèéìíòóùú]{3,}")


def vocabolario(campione) -> set[str]:
    voc = set()
    for riga in open("/usr/share/dict/italian", encoding="utf-8"):
        voc.update(PAROLA.findall(riga.lower()))
    # Testi nativi delle pagine web del campione: italiano giornalistico, nomi propri inclusi.
    for c in campione:
        doc = pymupdf.open(ARCHIVIO / c["pdf"])
        for a in c["articoli"]:
            if a["tipo"] == "web":
                for p in a["pagine_pdf"]:
                    voc.update(PAROLA.findall(doc[p].get_text().lower()))
    return voc


def quota_italiano(testo: str, voc: set[str]) -> float | None:
    parole = PAROLA.findall(testo.lower())
    return sum(p in voc for p in parole) / len(parole) if parole else None


# ---- Esecuzione --------------------------------------------------------------

if __name__ == "__main__":
    scelti = sys.argv[1].split(",") if len(sys.argv) > 1 else list(METODI)
    campione = json.loads((PROTOTIPO / "campione.json").read_text(encoding="utf-8"))
    articoli = [(c["pdf"], a) for c in campione for a in c["articoli"] if a["tipo"] == "stampa"]
    # OGNI=n: solo un articolo ogni n (per i metodi lenti); i file mantengono il numero dell'articolo.
    passo = int(os.environ.get("OGNI", "1"))
    indici = list(range(0, len(articoli), passo))
    voc = vocabolario(campione)
    print(f"{len(articoli)} articoli stampa, vocabolario di {len(voc)} parole")

    riepilogo_file = USCITA / "riepilogo.json"
    riepilogo = json.loads(riepilogo_file.read_text()) if riepilogo_file.exists() else {}
    for m in scelti:
        nome, crea = METODI[m]
        ocr = crea()
        cartella = USCITA / m
        cartella.mkdir(parents=True, exist_ok=True)
        quote, tempi, n_parole = [], [], []
        for i in indici:
            pdf, a = articoli[i]
            doc = pymupdf.open(ARCHIVIO / pdf)
            t0 = time.perf_counter()
            testo = " ".join(ocr(doc, p) for p in a["pagine_pdf"])
            tempi.append((time.perf_counter() - t0) / len(a["pagine_pdf"]))
            (cartella / f"{i:03d}.txt").write_text(f"{pdf} | {a['testata']} | {a['titolo']}\n\n{testo}\n",
                                                   encoding="utf-8")
            print(f"  {i:03d} {len(testo.split())} parole {tempi[-1]:.1f} s/pagina", flush=True)
            q = quota_italiano(testo, voc)
            n_parole.append(len(testo.split()))
            if q is not None:
                quote.append(q)
        riepilogo[m] = {"metodo": nome, "quota_italiano_media": round(statistics.mean(quote), 4),
                        "quota_italiano_mediana": round(statistics.median(quote), 4),
                        "parole_medie": round(statistics.mean(n_parole)),
                        "secondi_per_pagina": round(statistics.mean(tempi[1:] or tempi), 2)}
        riepilogo[m]["articoli"] = len(indici)
        print(f"{m} {nome:24} italiano {riepilogo[m]['quota_italiano_media']:.1%} (mediana "
              f"{riepilogo[m]['quota_italiano_mediana']:.1%})  parole {riepilogo[m]['parole_medie']}  "
              f"{riepilogo[m]['secondi_per_pagina']} s/pagina")
        riepilogo_file.write_text(json.dumps(riepilogo, ensure_ascii=False, indent=2))
