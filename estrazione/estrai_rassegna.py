"""Estrazione: PDF di rassegna Data Stampa -> dati/<id>/rassegna.json (schema in docs/schema_articolo.md).

Per ogni voce del sommario:
  stampa  OCR delle colonne del ritaglio (stampa.py), posizione per riga
  web     testo nativo, euristica o modello generativo (web.py), posizione per riga
Poi divisione in frasi con le posizioni sulla pagina e immagini JPEG delle pagine degli articoli.
In dati/<id>/diagnostica.json i riquadri di ogni elemento considerato (tenuto o scartato, con il motivo).
Il sentiment resta a null: lo aggiunge backend/classifica_rassegna.py.

Per le pagine web serve Ollama con qwen2.5:7b su web.OLLAMA_URL; senza, si usa solo l'euristica.

    cd estrazione
    CUDA_VISIBLE_DEVICES=0 env -u PYTHONPATH ../.venv/bin/python estrai_rassegna.py "2025/Ottobre/Evidenze Rassegna 25102025.pdf" [altri...]
I percorsi sono relativi all'archivio (~/Documenti/autentica/Data) o assoluti.
"""
import json
import re
import sys
import time
import urllib.error
from pathlib import Path

import pymupdf

import stampa
import web
from campione import articoli_del_pdf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from adattatore_evidenze import dividi_frasi  # noqa: E402

pymupdf.TOOLS.mupdf_display_errors(False)

SCHEMA = 1
ARCHIVIO = Path.home() / "Documenti/autentica/Data"
DATI = Path(__file__).resolve().parent.parent / "dati"
DPI_IMMAGINI = 110
VERSIONE_STAMPA = "rapidocr-it-ppocrv6/colonne-ricucite"
FINE_PERIODO = re.compile(r"[.!?»\"”]$")


def id_rassegna(percorso: Path) -> str:
    """Identificativo stabile dal percorso nell'archivio: anno_mese_nome-file."""
    rel = percorso.relative_to(ARCHIVIO) if percorso.is_relative_to(ARCHIVIO) else Path(percorso.name)
    return re.sub(r"[^\w-]+", "_", "_".join(rel.with_suffix("").parts)).strip("_")


def segmenti_stampa(doc: pymupdf.Document, pagine: list[int], titolo: str):
    """(segmenti, diagnostica) del corpo di un articolo stampa."""
    colonne, d = stampa.righe_articolo(doc, pagine)
    # Le prime colonne che ripetono il titolo del sommario sono il titolo stampato: escluse.
    while colonne and stampa.somiglianza_parole(" ".join(r["testo"] for r in colonne[0]), titolo) >= 0.6:
        for r in colonne.pop(0):
            d.append(stampa.diag(r["pagina"], r["bbox"], "riga", "scartato", "titolo (già nel sommario)", r["testo"]))
    # Righe in caratteri grandi (titoli, sottotitoli, occhielli): più alte di 1,6 volte la riga tipica del corpo.
    altezze = sorted(r["bbox"][3] - r["bbox"][1] for c in colonne for r in c)
    altezza_corpo = altezze[len(altezze) // 2] if altezze else 0
    segmenti, prec = [], None
    for colonna in colonne:
        for r in colonna:
            if r["bbox"][3] - r["bbox"][1] > 1.6 * altezza_corpo:
                d.append(stampa.diag(r["pagina"], r["bbox"], "riga", "scartato",
                                     "carattere grande (titolo, sottotitolo o occhiello)", r["testo"]))
                continue
            # Nuovo paragrafo: la riga prima chiude un periodo ed è corta (ultima riga di un capoverso).
            nuovo = prec is None or (FINE_PERIODO.search(prec["testo"])
                                     and prec["bbox"][2] - prec["bbox"][0] < 0.8 * prec["larghezza_colonna"])
            segmenti.append({"pagina": r["pagina"], "bbox": r["bbox"], "testo": r["testo"], "nuovo_paragrafo": nuovo})
            d.append(stampa.diag(r["pagina"], r["bbox"], "riga", "tenuto", "corpo (riga OCR)", r["testo"]))
            prec = r
    return segmenti, d


def componi(segmenti: list[dict]) -> tuple[str, list[dict]]:
    """Testo dell'articolo dai segmenti, con le posizioni (inizio, fine) di ognuno nel testo.

    Ricuce le parole sillabate a fine riga ("allac-" + "cio") senza perdere la corrispondenza con le posizioni.
    """
    testo, uscita = "", []
    for s in segmenti:
        t = re.sub(r"\s+", " ", s["testo"]).strip()
        if not t:
            continue
        if testo:
            if re.search(r"\w[-¬]$", testo) and t[0].islower():
                testo = testo[:-1]
                uscita[-1]["fine"] = len(testo)
            elif s["nuovo_paragrafo"]:
                testo += "\n\n"
            else:
                testo += " "
        inizio = len(testo)
        testo += t
        uscita.append({"inizio": inizio, "fine": len(testo), "pagina": s["pagina"], "bbox": s["bbox"]})
    return testo, uscita


def estrai_pdf(percorso: Path) -> dict:
    doc = pymupdf.open(percorso)
    id_r = id_rassegna(percorso)
    cartella = DATI / id_r
    (cartella / "pagine").mkdir(parents=True, exist_ok=True)

    articoli, pagine_usate, saltati, diagnostica = [], set(), 0, {}
    llm_disponibile = True
    for k, v in enumerate(articoli_del_pdf(percorso)):
        t0 = time.perf_counter()
        if v["tipo"] == "stampa":
            (segmenti, d), metodo = segmenti_stampa(doc, v["pagine_pdf"], v["titolo"]), VERSIONE_STAMPA
        else:
            try:
                if not llm_disponibile:
                    raise urllib.error.URLError("non disponibile")
                segmenti, d, metodo = web.estrai(doc, v["pagine_pdf"], v["titolo"])
                metodo = f"{metodo}/{web.MODELLO}" if metodo == "llm" else metodo
            except urllib.error.URLError:
                if llm_disponibile:
                    print("  ATTENZIONE: Ollama non raggiungibile, pagine web solo con l'euristica")
                llm_disponibile = False
                (segmenti, d), metodo = web.euristica(doc, v["pagine_pdf"], v["titolo"]), "euristica"
        diagnostica[f"{id_r}/{k + 1}"] = {"metodo": metodo, "elementi": d}
        testo, segmenti = componi(segmenti)
        if not testo:
            saltati += 1
            print(f"  [{k + 1}] {v['testata']}: nessun testo estratto, saltato")
            continue
        pagine_usate.update(v["pagine_pdf"])
        articoli.append({
            "id": f"{id_r}/{k + 1}",
            "titolo": v["titolo"],
            "occhiello": None,
            "testata": v["testata"],
            "data": v["data"],
            "pagina_originale": v["pagina_giornale"],
            "autore": v["autore"],
            "sezione": v["sezione"],
            "tipo": v["tipo"],
            "url": web.url_originale(doc, v["pagine_pdf"][0]) if v["tipo"] == "web" else None,
            "pagine_rassegna": [p + 1 for p in v["pagine_pdf"]],
            "testo": testo,
            "frasi": dividi_frasi(testo, segmenti),
            "sentiment": None,
            "estrazione": {"metodo": metodo},
        })
        print(f"  [{k + 1}] {v['tipo']:6} {v['testata'][:24]:24} {len(testo.split()):5} parole  "
              f"{len(articoli[-1]['frasi']):3} frasi  {metodo}  ({time.perf_counter() - t0:.1f}s)")

    pagine = []
    for n in sorted(pagine_usate):
        nome = f"pagine/page_{n + 1:03d}.jpg"
        doc[n].get_pixmap(dpi=DPI_IMMAGINI).pil_save(cartella / nome, quality=80)
        pagine.append({"n": n + 1, "larghezza": doc[n].rect.width, "altezza": doc[n].rect.height, "immagine": nome})

    date = sorted({a["data"] for a in articoli})
    rassegna = {"schema": SCHEMA, "id": id_r, "pdf": str(percorso.relative_to(ARCHIVIO))
                if percorso.is_relative_to(ARCHIVIO) else percorso.name,
                "data": date[-1] if date else None, "pagine": pagine, "articoli": articoli}
    (cartella / "rassegna.json").write_text(json.dumps(rassegna, ensure_ascii=False, indent=1), encoding="utf-8")
    # Riquadri di tutto ciò che è stato considerato (tenuto, scartato e perché), per articolo: vista in dashboard.
    (cartella / "diagnostica.json").write_text(json.dumps(diagnostica, ensure_ascii=False), encoding="utf-8")
    print(f"  -> {cartella / 'rassegna.json'}: {len(articoli)} articoli ({saltati} senza testo)")
    return rassegna


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    for arg in sys.argv[1:]:
        p = Path(arg)
        p = p if p.is_absolute() else ARCHIVIO / p
        print(p.relative_to(ARCHIVIO) if p.is_relative_to(ARCHIVIO) else p)
        estrai_pdf(p)
