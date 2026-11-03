"""Estrazione dell'intero archivio Data Stampa, a fasi riprendibili (orchestrate da esegui_archivio.sh).

  piano      legge i sommari di tutti i PDF, elenca articoli e pagine da passare all'OCR, scarica gli URL web
             -> dati/archivio/piano.json, dati/archivio/lavori_ocr_{0,1}.jsonl, dati/archivio/url/ (cache)
  articoli   costruisce gli articoli di una parte dei PDF (worker k su n) usando OCR, URL e testo nativo
             -> dati/<id>/rassegna.json, diagnostica.json, pagine/*.jpg
  resoconto  per ogni articolo: fonte (URL o PDF), completezza del corpo, affidabilità
             -> dati/archivio/resoconto.html, resoconto.csv

    env -u PYTHONPATH ../.venv/bin/python estrai_archivio.py piano
    OLLAMA_URL=http://127.0.0.1:11435/api/chat env -u PYTHONPATH ../.venv/bin/python estrai_archivio.py articoli 0 2
    env -u PYTHONPATH ../.venv/bin/python estrai_archivio.py resoconto
"""
import csv
import html
import json
import re
import sys
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pymupdf

import stampa_layout
import web
import web_url
from campione import articoli_del_pdf
from estrai_rassegna import ARCHIVIO, DATI, DPI_IMMAGINI, SCHEMA, componi, id_rassegna
from prova_ocr_stampa import PAROLA as PAROLA_ITA

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from adattatore_evidenze import dividi_frasi  # noqa: E402

pymupdf.TOOLS.mupdf_display_errors(False)

LAVORO = DATI / "archivio"
CACHE_OCR = LAVORO / "ocr"
CACHE_URL = LAVORO / "url"
VERSIONE = "archivio-1"
MIN_PAROLE_NATIVE = 30   # sotto: pagina web salvata come immagine, serve l'OCR


def parole_native(pagina: pymupdf.Page) -> int:
    return sum(len(r["testo"].split()) for r in web.righe_pagina(pagina, 0))


# ---- Fase 1: piano ----------------------------------------------------------------

def piano():
    LAVORO.mkdir(parents=True, exist_ok=True)
    pdfs, senza_sommario, errori, url_da_scaricare = [], [], [], set()
    tutti = sorted(ARCHIVIO.rglob("*.pdf"))
    for k, percorso in enumerate(tutti, 1):
        try:
            doc = pymupdf.open(percorso)
            articoli = articoli_del_pdf(percorso)
            if not articoli:
                senza_sommario.append(str(percorso.relative_to(ARCHIVIO)))
                continue
            pagine_ocr = set()
            for a in articoli:
                if a["tipo"] == "stampa":
                    pagine_ocr.update(a["pagine_pdf"])
                else:
                    if any(parole_native(doc[p]) < MIN_PAROLE_NATIVE for p in a["pagine_pdf"]):
                        pagine_ocr.update(a["pagine_pdf"])
                    if url := web.url_originale(doc, a["pagine_pdf"][0]):
                        url_da_scaricare.add(url)
            pdfs.append({"pdf": str(percorso), "id": id_rassegna(percorso), "pagine_ocr": sorted(pagine_ocr),
                         "n_articoli": len(articoli)})
        except Exception as e:
            errori.append({"pdf": str(percorso.relative_to(ARCHIVIO)), "errore": f"{type(e).__name__}: {e}"})
        if k % 100 == 0:
            print(f"piano: {k}/{len(tutti)} PDF", flush=True)

    # Lavori OCR divisi tra due GPU, bilanciando il numero di pagine.
    parti, carico = [[], []], [0, 0]
    for p in sorted(pdfs, key=lambda p: -len(p["pagine_ocr"])):
        i = carico.index(min(carico))
        parti[i] += [json.dumps({"pdf": p["pdf"], "id": p["id"], "pagina": n}) for n in p["pagine_ocr"]]
        carico[i] += len(p["pagine_ocr"])
    for i, righe in enumerate(parti):
        (LAVORO / f"lavori_ocr_{i}.jsonl").write_text("\n".join(righe) + "\n", encoding="utf-8")
    (LAVORO / "piano.json").write_text(json.dumps(
        {"pdf": pdfs, "senza_sommario": senza_sommario, "errori": errori}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"piano: {len(pdfs)} PDF con sommario, {sum(p['n_articoli'] for p in pdfs)} articoli, "
          f"{len(senza_sommario)} senza sommario, {len(errori)} errori; pagine OCR {carico}", flush=True)

    # Download degli URL (in parallelo, con cache): la fase articoli li trova già pronti.
    print(f"piano: download di {len(url_da_scaricare)} URL", flush=True)
    with ThreadPoolExecutor(8) as pool:
        for k, _ in enumerate(pool.map(lambda u: web_url.scarica(u, CACHE_URL), sorted(url_da_scaricare)), 1):
            if k % 200 == 0:
                print(f"piano: {k}/{len(url_da_scaricare)} URL", flush=True)
    print("piano: fatto", flush=True)


# ---- Fase 2: articoli -------------------------------------------------------------

VOCABOLARIO = None


def quota_italiano(testo: str) -> float | None:
    global VOCABOLARIO
    if VOCABOLARIO is None:
        VOCABOLARIO = set()
        for riga in open("/usr/share/dict/italian", encoding="utf-8"):
            VOCABOLARIO.update(PAROLA_ITA.findall(riga.lower()))
    p = PAROLA_ITA.findall(testo.lower())
    return sum(x in VOCABOLARIO for x in p) / len(p) if p else None


def limita(x: float) -> float:
    return max(0.0, min(1.0, x))


def affidabilita_ocr(testo: str, r: dict) -> tuple[float, list[str]]:
    """Articoli da OCR: qualità del testo, titolo ritrovato, blocchi vuoti, lunghezza."""
    note = []
    q = quota_italiano(testo) or 0
    # Riferimento: il testo nativo delle pagine web ha ~92-95% di parole nel dizionario (nomi propri esclusi).
    a = 0.5 + 0.5 * limita((q - 0.6) / 0.3)
    if q < 0.75:
        note.append(f"OCR di qualità bassa ({q:.0%} parole italiane)")
    if not r["titolo_trovato"]:
        a *= 0.85
        note.append("titolo del sommario non ritrovato nella pagina")
    if r["blocchi_vuoti"]:
        a *= 0.7
        note.append(f"{r['blocchi_vuoti']} blocchi di testo illeggibili")
    if len(testo.split()) < 40:
        a *= 0.7
        note.append("testo molto corto")
    return a, note


def articolo_web(doc, v, pagine_ocr, scaricati, llm_ok):
    """(segmenti, diagnostica, info) di un articolo web: URL verificato, altrimenti PDF."""
    pagine = v["pagine_pdf"]
    info = {"fonte": "PDF", "url": web.url_originale(doc, pagine[0]), "url_esito": None, "note": []}
    testo_pdf = " ".join(r["testo"] for p in pagine for r in web.righe_pagina(doc[p], p))

    if len(testo_pdf.split()) < MIN_PAROLE_NATIVE * len(pagine):
        # Pagina web salvata come immagine: OCR con layout, come la stampa.
        segmenti, d, r = stampa_layout.articolo([pagine_ocr[p] for p in pagine if p in pagine_ocr], v["titolo"], None)
        corpo = " ".join(s["testo"] for s in segmenti)
        info["metodo"] = "ocr-layout (pagina web come immagine)"
        a, note = affidabilita_ocr(corpo, r)
        info.update(r, affidabilita=a * 0.9, completo=r["blocchi_vuoti"] == 0)
        info["note"] += note
    else:
        e = web.euristica(doc, pagine, v["titolo"])
        m = ([], [])
        if llm_ok[0]:
            try:
                m = web.llm(doc, pagine, v["titolo"])
            except urllib.error.URLError:
                llm_ok[0] = False
                print("  ATTENZIONE: Ollama non raggiungibile, pagine web solo con l'euristica", flush=True)
        segmenti, d, scelto = web.combina(e, m)
        corpo = " ".join(s["testo"] for s in segmenti)
        testo_e, testo_m = (" ".join(s["testo"] for s in x[0]) for x in (e, m))
        accordo = (web_url.quota_contenuta(testo_m, testo_e) or 0) if testo_m else None
        info["metodo"] = "pdf-euristica" if scelto == "euristica" else f"pdf-modello ({web.MODELLO})"
        if accordo is None:
            a = 0.6
            info["note"].append("solo euristica (modello non disponibile)")
        elif scelto == "euristica":
            a = 0.6 + 0.35 * accordo
        else:
            a = 0.7
            info["note"].append("l'euristica aveva preso un'altra colonna: usato il modello")
        if len(corpo.split()) < 40:
            a *= 0.7
            info["note"].append("testo molto corto")
        info.update(affidabilita=a, completo=None, blocchi_corpo=None, blocchi_inclusi=None, blocchi_vuoti=None)

    # URL prima di tutto, se verificato.
    if info["url"]:
        scaricato = scaricati(info["url"])
        esito = web_url.valuta(scaricato, testo_pdf, corpo, v["titolo"])
        info["url_esito"] = esito["motivo"]
        if esito["usabile"]:
            segmenti = [{"pagina": None, "bbox": None, "testo": p, "nuovo_paragrafo": True}
                        for p in web_url.paragrafi(scaricato["testo"])]
            info["fonte"] = "URL"
            info["metodo"] = "url-trafilatura"
            cop = esito["copertura"] if esito["copertura"] is not None else 0.9
            coe = esito["coerenza"] if esito["coerenza"] is not None else (esito["titolo_sim"] or 0.6)
            info["affidabilita"] = 0.75 + 0.25 * min(cop, coe)
            info["completo"] = True
            info["note"] = [f"verificato sul PDF (coerenza {coe:.2f}, copertura {cop:.2f})"]
            d = d + [{"pagina": p + 1, "bbox": [0, 0, doc[p].rect.width, doc[p].rect.height], "livello": "pagina",
                      "stato": "info", "motivo": "testo preso dall'URL originale; le posizioni sono ricavate "
                                                 "confrontando le frasi con le righe del PDF", "testo": ""}
                     for p in pagine]
    else:
        info["url_esito"] = "nessun URL nella pagina"
    return segmenti, d, info, testo_pdf


NORM = re.compile(r"[^\w]+")


def posizioni_da_pdf(frasi, doc, pagine):
    """Per il testo preso dall'URL: posizioni delle frasi dalle righe del PDF che contengono lo stesso testo."""
    righe = [(r, NORM.sub(" ", r["testo"].lower()).strip()) for p in pagine for r in web.righe_pagina(doc[p], p)]
    righe = [(r, t) for r, t in righe if len(t) >= 15]
    for f in frasi:
        norm = NORM.sub(" ", f["testo"].lower())
        f["posizioni"] = [{"pagina": r["pagina"], "bbox": r["bbox"]} for r, t in righe if t in norm]


def classe(a: float) -> str:
    return "alta" if a >= 0.8 else "media" if a >= 0.6 else "bassa"


def articoli_pdf(voce: dict, scaricati, llm_ok):
    percorso = Path(voce["pdf"])
    doc = pymupdf.open(percorso)
    id_r = voce["id"]
    cartella = DATI / id_r
    (cartella / "pagine").mkdir(parents=True, exist_ok=True)
    pagine_ocr = {}
    for n in voce["pagine_ocr"]:
        f = CACHE_OCR / id_r / f"page_{n + 1:03d}.json"
        if f.exists():
            pagine_ocr[n] = json.loads(f.read_text(encoding="utf-8"))

    articoli, diagnostica, pagine_usate = [], {}, set()
    for k, v in enumerate(articoli_del_pdf(percorso)):
        id_a = f"{id_r}/{k + 1}"
        if v["tipo"] == "stampa":
            mancanti = [p for p in v["pagine_pdf"] if p not in pagine_ocr]
            segmenti, d, r = stampa_layout.articolo([pagine_ocr[p] for p in v["pagine_pdf"] if p in pagine_ocr],
                                                    v["titolo"], v["autore"])
            corpo = " ".join(s["testo"] for s in segmenti)
            a, note = affidabilita_ocr(corpo, r)
            if mancanti:
                a *= 0.5
                note.append(f"OCR mancante per {len(mancanti)} pagine")
            info = {"fonte": "PDF", "metodo": "ocr-layout (PaddleOCR-VL-1.6)", "url": None,
                    "url_esito": None, "affidabilita": a, "note": note, **r,
                    "completo": r["blocchi_vuoti"] == 0 and not mancanti}
        else:
            segmenti, d, info, _ = articolo_web(doc, v, pagine_ocr, scaricati, llm_ok)
        testo, seg = componi([s if s["pagina"] else {**s, "pagina": 0, "bbox": [0, 0, 0, 0]} for s in segmenti])
        frasi = dividi_frasi(testo, seg)
        if info["fonte"] == "URL":
            posizioni_da_pdf(frasi, doc, v["pagine_pdf"])
        if not testo:
            info["affidabilita"] = 0
            info["note"].append("nessun testo estratto")
        info["affidabilita"] = round(info["affidabilita"], 3)
        info["classe"] = classe(info["affidabilita"])
        info["parole"] = len(testo.split())
        pagine_usate.update(v["pagine_pdf"])
        diagnostica[id_a] = {"metodo": info["metodo"], "elementi": d}
        articoli.append({
            "id": id_a, "titolo": v["titolo"], "occhiello": None, "testata": v["testata"], "data": v["data"],
            "pagina_originale": v["pagina_giornale"], "autore": v["autore"], "sezione": v["sezione"],
            "tipo": v["tipo"], "url": info["url"], "pagine_rassegna": [p + 1 for p in v["pagine_pdf"]],
            "testo": testo, "frasi": frasi, "sentiment": None, "estrazione": info,
        })

    pagine = []
    for n in sorted(pagine_usate):
        nome = f"pagine/page_{n + 1:03d}.jpg"
        if not (cartella / nome).exists():
            doc[n].get_pixmap(dpi=DPI_IMMAGINI).pil_save(cartella / nome, quality=80)
        pagine.append({"n": n + 1, "larghezza": doc[n].rect.width, "altezza": doc[n].rect.height, "immagine": nome})
    date = sorted({a["data"] for a in articoli})
    rassegna = {"schema": SCHEMA, "versione_estrazione": VERSIONE, "id": id_r,
                "pdf": str(percorso.relative_to(ARCHIVIO)), "data": date[-1] if date else None,
                "pagine": pagine, "articoli": articoli}
    (cartella / "rassegna.json").write_text(json.dumps(rassegna, ensure_ascii=False, indent=1), encoding="utf-8")
    (cartella / "diagnostica.json").write_text(json.dumps(diagnostica, ensure_ascii=False), encoding="utf-8")
    return len(articoli)


def articoli(worker: int, n_worker: int):
    p = json.loads((LAVORO / "piano.json").read_text(encoding="utf-8"))
    mie = [v for i, v in enumerate(p["pdf"]) if i % n_worker == worker]
    llm_ok = [True]
    errori = LAVORO / f"errori_articoli_{worker}.jsonl"
    inizio = time.time()
    for k, voce in enumerate(mie, 1):
        file = DATI / voce["id"] / "rassegna.json"
        if file.exists() and json.loads(file.read_text(encoding="utf-8")).get("versione_estrazione") == VERSIONE:
            continue
        try:
            n = articoli_pdf(voce, lambda u: web_url.scarica(u, CACHE_URL), llm_ok)
            esito = f"{n} articoli"
        except Exception as e:
            with errori.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"pdf": voce["pdf"], "errore": f"{type(e).__name__}: {e}"}, ensure_ascii=False) + "\n")
            esito = f"ERRORE {type(e).__name__}: {e}"
        t = time.time() - inizio
        print(f"[articoli {worker}] {k}/{len(mie)} {voce['id']}: {esito} "
              f"(restano ~{t / k * (len(mie) - k) / 60:.0f} min)", flush=True)


# ---- Fase 3: resoconto -------------------------------------------------------------

def resoconto():
    p = json.loads((LAVORO / "piano.json").read_text(encoding="utf-8"))
    righe = []
    for voce in p["pdf"]:
        file = DATI / voce["id"] / "rassegna.json"
        if not file.exists():
            righe.append({"pdf": voce["pdf"].replace(str(ARCHIVIO) + "/", ""), "id": "", "data": "", "testata": "",
                          "titolo": "(rassegna non elaborata)", "tipo": "", "fonte": "", "metodo": "", "url": "",
                          "url_esito": "", "completo": "", "blocchi": "", "parole": 0, "affidabilita": 0,
                          "classe": "bassa", "sentiment": "", "note": "elaborazione non riuscita: vedi errori"})
            continue
        r = json.loads(file.read_text(encoding="utf-8"))
        for a in r["articoli"]:
            e = a["estrazione"]
            blocchi = (f"{e['blocchi_inclusi']}/{e['blocchi_corpo']}" + (f" ({e['blocchi_vuoti']} illeggibili)"
                       if e.get("blocchi_vuoti") else "")) if e.get("blocchi_corpo") is not None else ""
            completo = {True: "sì", False: "no", None: "n.d."}[e.get("completo")]
            righe.append({"pdf": r["pdf"], "id": a["id"], "cartella": r["id"], "pagine": a["pagine_rassegna"],
                          "data": a["data"], "testata": a["testata"],
                          "titolo": a["titolo"], "tipo": a["tipo"], "fonte": e["fonte"], "metodo": e["metodo"],
                          "url": a["url"] or "", "url_esito": e.get("url_esito") or "", "completo": completo,
                          "blocchi": blocchi, "parole": e["parole"], "affidabilita": round(e["affidabilita"] * 100),
                          "classe": e["classe"], "sentiment": (a["sentiment"] or {}).get("etichetta", ""),
                          "note": "; ".join(e.get("note", []))})
    for pdf in p["senza_sommario"]:
        righe.append({"pdf": pdf, "id": "", "data": "", "testata": "", "titolo": "(PDF senza sommario: non elaborato)",
                      "tipo": "", "fonte": "", "metodo": "", "url": "", "url_esito": "", "completo": "", "blocchi": "",
                      "parole": 0, "affidabilita": 0, "classe": "", "sentiment": "",
                      "note": "manca il sommario: articoli non individuati (da gestire)"})
    for err in p["errori"]:
        righe.append({"pdf": err["pdf"], "id": "", "data": "", "testata": "", "titolo": "(errore di lettura del PDF)",
                      "tipo": "", "fonte": "", "metodo": "", "url": "", "url_esito": "", "completo": "", "blocchi": "",
                      "parole": 0, "affidabilita": 0, "classe": "", "sentiment": "", "note": err["errore"]})

    campi = [c for c in righe[-1].keys()] + ["cartella", "pagine"]
    campi = list(dict.fromkeys(c for c in campi))
    with (LAVORO / "resoconto.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campi, restval="")
        w.writeheader()
        w.writerows(righe)
    scrivi_html(righe, p)
    print(f"resoconto: {len(righe)} righe -> {LAVORO / 'resoconto.html'}, resoconto.csv", flush=True)


def scrivi_html(righe, p):
    art = [r for r in righe if r["id"]]
    n = len(art) or 1
    def conta(f):
        return sum(1 for r in art if f(r))
    web_ = [r for r in art if r["tipo"] == "web"]
    stampa_ = [r for r in art if r["tipo"] == "stampa"]
    riepilogo = [
        ("Articoli elaborati", f"{len(art)}"),
        ("Affidabilità alta / media / bassa",
         f"{conta(lambda r: r['classe'] == 'alta')} / {conta(lambda r: r['classe'] == 'media')} / "
         f"{conta(lambda r: r['classe'] == 'bassa')}"),
        ("Web recuperati dall'URL", f"{conta(lambda r: r['fonte'] == 'URL')} su {len(web_)}"),
        ("Stampa con tutti i blocchi del corpo", f"{sum(1 for r in stampa_ if r['completo'] == 'sì')} su {len(stampa_)}"),
        ("PDF senza sommario (non elaborati)", f"{len(p['senza_sommario'])}"),
        ("PDF con errori", f"{len(p['errori'])}"),
    ]
    intest = ["", "Data", "Testata", "Titolo", "Tipo", "Fonte", "Metodo", "Esito URL", "Corpo completo", "Blocchi",
              "Parole", "Affidabilità", "Sentiment", "Riquadri", "Note", "PDF"]
    def cella(r):
        url = (f'<a href="{html.escape(r["url"])}" target="_blank" rel="noopener">{html.escape(r["fonte"])}</a>'
               if r["url"] and r["fonte"] == "URL" else html.escape(r["fonte"]))
        # Apre l'articolo nella dashboard con i riquadri di estrazione visibili.
        apri = (f'<a href="../../frontend/#articolo={html.escape(r["id"])}&riquadri=1" target="_blank">Apri</a>'
                if r["id"] else "")
        # Immagini delle pagine con i riquadri disegnati (disegna_riquadri.py).
        riquadri = " ".join(f'<a href="../{html.escape(r["cartella"])}/riquadri/page_{n:03d}.jpg" target="_blank">p{n}</a>'
                            for n in (r.get("pagine") or []))
        return (f'<tr class="c-{r["classe"]}"><td>{apri}</td><td>{r["data"]}</td><td>{html.escape(r["testata"])}</td>'
                f'<td>{html.escape(r["titolo"])}</td><td>{r["tipo"]}</td><td>{url}</td><td>{html.escape(r["metodo"])}</td>'
                f'<td>{html.escape(r["url_esito"])}</td><td>{r["completo"]}</td><td>{r["blocchi"]}</td>'
                f'<td class="n">{r["parole"]}</td><td class="n aff">{r["affidabilita"]}%</td><td>{r["sentiment"]}</td>'
                f'<td>{riquadri}</td>'
                f'<td>{html.escape(r["note"])}</td><td class="pdf">{html.escape(r["pdf"])}</td></tr>')
    pagina = f"""<!doctype html><html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Resoconto estrazione</title>
<style>
:root {{ --sfondo:#f5f6f8; --sup:#fff; --bordo:#e2e4e9; --testo:#1f2329; --tenue:#6b7280;
  --alta:#2e9e5b; --media:#c98a12; --bassa:#d64545; }}
@media (prefers-color-scheme: dark) {{ :root {{ --sfondo:#15171b; --sup:#1e2126; --bordo:#30343b; --testo:#e6e8eb; --tenue:#9aa1ab; }} }}
body {{ margin:0; padding:16px; font:13px system-ui, sans-serif; background:var(--sfondo); color:var(--testo); }}
h1 {{ font-size:20px; margin:0 0 4px; }} p.sotto {{ color:var(--tenue); margin:0 0 16px; }}
.riepilogo {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:10px; margin-bottom:16px; }}
.voce {{ background:var(--sup); border:1px solid var(--bordo); border-radius:8px; padding:10px 12px; }}
.voce b {{ display:block; font-size:18px; margin-top:2px; }}
.filtri {{ display:flex; flex-wrap:wrap; gap:8px; margin-bottom:10px; }}
.filtri input, .filtri select {{ padding:5px 8px; border:1px solid var(--bordo); border-radius:6px; background:var(--sup); color:var(--testo); }}
.tab {{ overflow-x:auto; background:var(--sup); border:1px solid var(--bordo); border-radius:8px; }}
table {{ border-collapse:collapse; width:100%; }} th, td {{ padding:6px 8px; border-bottom:1px solid var(--bordo); text-align:left; vertical-align:top; }}
th {{ position:sticky; top:0; background:var(--sup); cursor:pointer; white-space:nowrap; }}
td.n {{ text-align:right; white-space:nowrap; }} td.pdf {{ color:var(--tenue); font-size:11px; }}
tr.c-alta td.aff {{ color:var(--alta); font-weight:600; }} tr.c-media td.aff {{ color:var(--media); font-weight:600; }}
tr.c-bassa td.aff {{ color:var(--bassa); font-weight:600; }}
.legenda {{ color:var(--tenue); margin:12px 0; line-height:1.5; }}
</style></head><body>
<h1>Resoconto estrazione articoli</h1>
<p class="sotto">Generato il {time.strftime('%d/%m/%Y %H:%M')} · versione {VERSIONE}</p>
<div class="riepilogo">{''.join(f'<div class="voce">{html.escape(k)}<b>{html.escape(v)}</b></div>' for k, v in riepilogo)}</div>
<div class="legenda"><b>Fonte</b>: URL = testo dell'articolo originale scaricato dal sito e verificato sul PDF (stesso articolo,
non bloccato, completo); PDF = ricostruito dal PDF. <b>Corpo completo</b>: per gli articoli da OCR, tutti i blocchi di testo
del corpo riconosciuti dal layout sono stati inclusi e letti (colonna Blocchi: inclusi/rilevati). <b>Affidabilità</b>: stima
di quanto la ricostruzione è corretta (alta ≥80%, media 60–79%, bassa &lt;60%), calcolata da qualità dell'OCR, titolo
ritrovato, blocchi illeggibili, accordo tra euristica e modello, verifica dell'URL. <b>Apri</b>: mostra l'articolo nella
dashboard con i riquadri di ciò che l'estrazione ha tenuto (verde) e scartato (rosso); passando il mouse su un riquadro
compare il motivo. <b>Riquadri</b>: le stesse informazioni disegnate sulle immagini delle pagine (una per pagina).
Serve il server della dashboard attivo.</div>
<div class="filtri"><input id="cerca" placeholder="Cerca testata, titolo, note…" size="40">
<select id="fclasse"><option value="">Tutte le affidabilità</option><option>alta</option><option>media</option><option>bassa</option></select>
<select id="ffonte"><option value="">Tutte le fonti</option><option>URL</option><option>PDF</option></select>
<select id="ftipo"><option value="">Stampa e web</option><option>stampa</option><option>web</option></select></div>
<div class="tab"><table id="t"><thead><tr>{''.join(f'<th>{h}</th>' for h in intest)}</tr></thead>
<tbody>{''.join(cella(r) for r in righe)}</tbody></table></div>
<script>
const righe=[...document.querySelectorAll('#t tbody tr')];
function filtra(){{const q=cerca.value.toLowerCase(), c=fclasse.value, f=ffonte.value, t=ftipo.value;
 righe.forEach(r=>{{const td=r.children; const ok=(!q||r.textContent.toLowerCase().includes(q))&&(!c||r.className==='c-'+c)
  &&(!f||td[5].textContent===f)&&(!t||td[4].textContent===t); r.hidden=!ok;}});}}
[cerca,fclasse,ffonte,ftipo].forEach(e=>e.addEventListener('input',filtra));
document.querySelectorAll('#t th').forEach((th,i)=>th.addEventListener('click',()=>{{const tb=document.querySelector('#t tbody');
 const asc=th.dataset.asc!=='1'; th.dataset.asc=asc?'1':'0';
 const v=r=>{{const s=r.children[i].textContent; const n=parseFloat(s); return isNaN(n)?s:n;}};
 [...tb.rows].sort((a,b)=>(v(a)>v(b)?1:v(a)<v(b)?-1:0)*(asc?1:-1)).forEach(r=>tb.appendChild(r));}}));
</script></body></html>"""
    (LAVORO / "resoconto.html").write_text(pagina, encoding="utf-8")


if __name__ == "__main__":
    fase = sys.argv[1] if len(sys.argv) > 1 else ""
    if fase == "piano":
        piano()
    elif fase == "articoli":
        articoli(int(sys.argv[2]), int(sys.argv[3]))
    elif fase == "resoconto":
        resoconto()
    else:
        sys.exit(__doc__)
