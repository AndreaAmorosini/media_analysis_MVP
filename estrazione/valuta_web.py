"""Valutazione dell'estrazione web di produzione (web.py) sul campione, contro i testi W2 di riferimento.

I riferimenti sono le pagine originali scaricate da prova_web.py (dati/prototipo/web/W2): niente nuovi download.
Serve Ollama attivo (web.OLLAMA_URL) con i modelli indicati.

    env -u PYTHONPATH ../.venv/bin/python valuta_web.py [modello ...]
"""
import json
import statistics
import sys
import time
from pathlib import Path

import pymupdf

import web
from prova_web import confronto, leggi_w2_salvato

pymupdf.TOOLS.mupdf_display_errors(False)

ARCHIVIO = Path.home() / "Documenti/autentica/Data"
PROTOTIPO = Path(__file__).resolve().parent.parent / "dati/prototipo"


def testo(segmenti):
    return " ".join(s["testo"] for s in segmenti)


campione = json.loads((PROTOTIPO / "campione.json").read_text(encoding="utf-8"))
articoli = []
for i, (pdf, a) in enumerate((c["pdf"], a) for c in campione for a in c["articoli"] if a["tipo"] == "web"):
    rif, _ = leggi_w2_salvato(PROTOTIPO / "web/W2" / f"{i:03d}.txt")
    if rif:
        articoli.append((i, pdf, a, rif))

modelli = sys.argv[1:] or [web.MODELLO]
f1_euristica = []
for i, pdf, a, rif in articoli:
    doc = pymupdf.open(ARCHIVIO / pdf)
    a["_euristica"] = web.euristica(doc, a["pagine_pdf"], a["titolo"])
    f1_euristica.append(confronto(testo(a["_euristica"][0]), rif)[2])

riepilogo = {"euristica": {"F1": round(statistics.mean(f1_euristica), 3)}}
for modello in modelli:
    f1_m, f1_c, tempi = [], [], []
    for i, pdf, a, rif in articoli:
        doc = pymupdf.open(ARCHIVIO / pdf)
        t0 = time.perf_counter()
        m = web.llm(doc, a["pagine_pdf"], a["titolo"], modello)
        tempi.append(time.perf_counter() - t0)
        seg, _, scelto = web.combina(a["_euristica"], m)
        f1_m.append(confronto(testo(m[0]), rif)[2])
        f1_c.append(confronto(testo(seg), rif)[2])
        print(f"{modello:20} {i:03d} {a['testata'][:22]:22} euristica {f1_euristica[len(f1_m) - 1]:.2f}  "
              f"modello {f1_m[-1]:.2f}  combinato {f1_c[-1]:.2f} ({scelto})  {tempi[-1]:.1f}s", flush=True)
    riepilogo[modello] = {
        "modello_F1": round(statistics.mean(f1_m), 3), "modello_buoni": sum(x >= 0.85 for x in f1_m),
        "combinato_F1": round(statistics.mean(f1_c), 3),
        "combinato_F1_mediana": round(statistics.median(f1_c), 3),
        "combinato_buoni": sum(x >= 0.85 for x in f1_c), "combinato_sbagliati": sum(x < 0.5 for x in f1_c),
        "secondi_medi": round(statistics.mean(tempi[1:] or tempi), 1)}

print(f"\n{len(articoli)} articoli con riferimento (buoni: F1 >= 0,85; sbagliati: F1 < 0,5)")
print(json.dumps(riepilogo, indent=2))
