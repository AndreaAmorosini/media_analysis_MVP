"""OCR con analisi del layout: PaddleOCR-VL-1.6 (layout PP-DocLayoutV3 + riconoscimento del testo).

Gira nell'ambiente .venv-paddle (Python 3.12, PaddlePaddle). Il layout gira su questa GPU; il riconoscimento
del testo lo fa il modello GGUF servito da Ollama (il motore Paddle nativo non entra in 8 GB su Turing).
I blocchi di testo che la pipeline restituisce vuoti vengono riletti con una chiamata diretta a Ollama.

Per ogni pagina scrive un JSON in <cache>/<pdf-id>/page_NNN.json (si salta se esiste già):
  {pdf, pagina (da 1), larghezza, altezza (punti PDF), sorgente, secondi,
   blocchi: [{ordine, etichetta, bbox (punti PDF, origine in alto a sinistra), testo, riletto}]}

    CUDA_VISIBLE_DEVICES=1 env -u PYTHONPATH ../.venv-paddle/bin/python ocr_layout.py <lavori.jsonl> <cache> [ollama_url]
<lavori.jsonl>: una riga {"pdf": percorso assoluto, "id": id rassegna, "pagina": indice da 0} per pagina.
"""
import base64
import io
import json
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image

pymupdf.TOOLS.mupdf_display_errors(False)

MODELLO_OCR = "hf.co/PaddlePaddle/PaddleOCR-VL-1.6-GGUF"
DPI_RENDER = 200
ETICHETTE_TESTO = {"text", "doc_title", "paragraph_title", "abstract", "content", "vision_footnote",
                   "figure_title", "reference", "reference_content", "footnote", "aside_text"}


def immagine_pagina(pagina: pymupdf.Page):
    """Immagine da analizzare e fattore pixel -> punti PDF.

    Se la pagina è un'unica grande immagine (formato Data Stampa da aprile 2026) si usa quella alla risoluzione
    originale; altrimenti si rende la pagina a DPI_RENDER.
    """
    area_pagina = pagina.rect.width * pagina.rect.height
    for info in pagina.get_image_info(xrefs=True):
        b = info["bbox"]
        if info["xref"] and (b[2] - b[0]) * (b[3] - b[1]) > 0.9 * area_pagina:
            pix = pymupdf.Pixmap(pagina.parent, info["xref"])
            if pix.n - pix.alpha >= 4:
                pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
            img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
            return img, (b[2] - b[0]) / img.width, (b[0], b[1]), "immagine incorporata"
    pix = pagina.get_pixmap(dpi=DPI_RENDER)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    return img, 72 / DPI_RENDER, (0, 0), f"render {DPI_RENDER} dpi"


def rileggi(img: Image.Image, bbox, url: str) -> str:
    buf = io.BytesIO()
    img.crop([int(v) for v in bbox]).save(buf, format="PNG")
    richiesta = {"model": MODELLO_OCR, "stream": False, "options": {"temperature": 0, "num_predict": 2048},
                 "messages": [{"role": "user", "content": "OCR:",
                               "images": [base64.b64encode(buf.getvalue()).decode()]}]}
    req = urllib.request.Request(url, data=json.dumps(richiesta).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.load(resp)["message"]["content"].strip()


def main():
    lavori_file, cache = Path(sys.argv[1]), Path(sys.argv[2])
    ollama = sys.argv[3] if len(sys.argv) > 3 else "http://127.0.0.1:11435"
    lavori = [json.loads(r) for r in lavori_file.read_text().splitlines() if r.strip()]
    da_fare = [l for l in lavori if not (cache / l["id"] / f"page_{l['pagina'] + 1:03d}.json").exists()]
    print(f"{len(lavori)} pagine, {len(lavori) - len(da_fare)} già fatte, {len(da_fare)} da fare", flush=True)
    if not da_fare:
        return

    from paddleocr import PaddleOCRVL
    pipe = PaddleOCRVL(pipeline_version="v1.6", device="gpu:0", vl_rec_backend="llama-cpp-server",
                       vl_rec_server_url=f"{ollama}/v1", vl_rec_api_model_name=MODELLO_OCR, vl_rec_max_concurrency=2)
    inizio_tutto = time.time()
    for k, l in enumerate(da_fare, 1):
        t0 = time.time()
        uscita = cache / l["id"] / f"page_{l['pagina'] + 1:03d}.json"
        uscita.parent.mkdir(parents=True, exist_ok=True)
        try:
            doc = pymupdf.open(l["pdf"])
            pagina = doc[l["pagina"]]
            img, scala, origine, sorgente = immagine_pagina(pagina)
            res = list(pipe.predict(np.array(img)[:, :, ::-1]))[0]  # la pipeline vuole BGR
            blocchi = []
            for b in res.json.get("res", res.json).get("parsing_res_list", []):
                testo = (b.get("block_content") or "").strip()
                riletto = False
                if not testo and b["block_label"] in ETICHETTE_TESTO:
                    testo, riletto = rileggi(img, b["block_bbox"], f"{ollama}/api/chat"), True
                x0, y0, x1, y1 = b["block_bbox"]
                blocchi.append({"ordine": b.get("block_order"), "etichetta": b["block_label"],
                                "bbox": [round(origine[0] + x0 * scala, 1), round(origine[1] + y0 * scala, 1),
                                         round(origine[0] + x1 * scala, 1), round(origine[1] + y1 * scala, 1)],
                                "testo": testo, "riletto": riletto})
            dati = {"pdf": l["pdf"], "pagina": l["pagina"] + 1, "larghezza": pagina.rect.width,
                    "altezza": pagina.rect.height, "sorgente": sorgente, "secondi": round(time.time() - t0, 1),
                    "blocchi": blocchi}
        except Exception as e:  # una pagina difettosa non deve fermare l'elaborazione dell'archivio
            dati = {"pdf": l["pdf"], "pagina": l["pagina"] + 1, "errore": f"{type(e).__name__}: {e}", "blocchi": []}
        uscita.write_text(json.dumps(dati, ensure_ascii=False), encoding="utf-8")
        trascorso = time.time() - inizio_tutto
        print(f"[{k}/{len(da_fare)}] {l['id']} p{l['pagina'] + 1}: {len(dati['blocchi'])} blocchi, "
              f"{time.time() - t0:.1f}s{' ERRORE ' + dati['errore'] if 'errore' in dati else ''} "
              f"(restano ~{trascorso / k * (len(da_fare) - k) / 60:.0f} min)", flush=True)


if __name__ == "__main__":
    main()
