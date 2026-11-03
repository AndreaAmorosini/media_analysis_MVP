"""Prototipo: estrazione del testo degli articoli dalle pagine WEB del campione.

Metodi:
  W1  euristica sul testo nativo del PDF: colonna principale + font dominante + ancora sul titolo
      del sommario + marcatori di fine articolo
  W2  URL originale (scritto nella pagina) + trafilatura sulla pagina web scaricata
  W3  modello generativo (qwen2.5:7b via Ollama) che sceglie quali blocchi numerati sono il corpo
      dell'articolo; il testo è composto dai blocchi originali, il modello non lo riscrive

Dove W2 riesce, il suo testo fa da riferimento per misurare W1 e W3 (precisione, richiamo, F1 sulle parole).
W3 richiede Ollama attivo su OLLAMA_URL con il modello scaricato.
Testi in dati/prototipo/web/<metodo>/<n>.txt, riepilogo in dati/prototipo/web/riepilogo.json.

    env -u PYTHONPATH ../.venv/bin/python prova_web.py
"""
import json
import re
import statistics
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

import time
import urllib.request

import pymupdf
import trafilatura

pymupdf.TOOLS.mupdf_display_errors(False)

ARCHIVIO = Path.home() / "Documenti/autentica/Data"
PROTOTIPO = Path(__file__).resolve().parent.parent / "dati/prototipo"
USCITA = PROTOTIPO / "web"

URL = re.compile(r"https?://\S+")
OLLAMA_URL = "http://127.0.0.1:11435/api/chat"
MODELLO = "qwen2.5:7b"
# Righe dopo le quali l'articolo è finito.
FINE = re.compile(r"^(leggi anche|articoli correlati|potrebbe interessarti|ti potrebbe interessare|"
                  r"tags?:?$|commenti|lascia un commento|iscriviti|condividi|© riproduzione|"
                  r"riproduzione riservata|correlati|altri articoli|ultime notizie|"
                  r"tutte le notizie|post più letti|articolo precedente|articolo successivo)", re.I)
# Righe da saltare senza chiudere l'articolo.
SALTA = re.compile(r"^(versione accessibile|di redazione$|redazione$|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|"
                   r"tempo di lettura|pubblicità|advertisement|foto:?|immagine di repertorio)", re.I)


def righe_pagina(pagina: pymupdf.Page, n: int):
    """Righe di testo con posizione e stile, esclusa la cornice Data Stampa."""
    for blocco in pagina.get_text("dict")["blocks"]:
        for riga in blocco.get("lines", []):
            spans = [s for s in riga["spans"] if s["text"].strip()]
            if not spans:
                continue
            x0, y0, x1, y1 = riga["bbox"]
            if y1 < 70 or y0 > 812 or x0 > 570:  # intestazione, piè di pagina, striscia dei diritti
                continue
            testo = " ".join(s["text"].strip() for s in spans)
            dim = round(max(spans, key=lambda s: len(s["text"]))["size"], 1)
            grassetto = all("Bold" in s["font"] or s["flags"] & 16 for s in spans)
            yield {"pagina": n, "x0": x0, "x1": x1, "y0": y0, "testo": testo, "stile": (dim, grassetto)}


def simile(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def estrai_euristica(doc: pymupdf.Document, pagine: list[int], titolo: str) -> str:
    # Ordine di lettura: pagina, poi dall'alto in basso (PyMuPDF non lo garantisce).
    righe = sorted((r for n in pagine for r in righe_pagina(doc[n], n)),
                   key=lambda r: (r["pagina"], round(r["y0"]), r["x0"]))
    if not righe:
        return ""
    # Font e colonna del corpo: quelli che portano più testo nelle righe lunghe della pagina.
    lunghe = [r for r in righe if len(r["testo"].split()) >= 6]
    peso_dim, peso_x = Counter(), Counter()
    for r in lunghe:
        peso_dim[r["stile"][0]] += len(r["testo"])
        peso_x[round(r["x0"] / 10)] += len(r["testo"])
    # Si confronta solo la dimensione del font: nel corpo ci sono anche righe in grassetto.
    dim_corpo = peso_dim.most_common(1)[0][0] if peso_dim else righe[0]["stile"][0]
    x_corpo = peso_x.most_common(1)[0][0] * 10 if peso_x else righe[0]["x0"]

    # Inizio: dopo la riga in cui compare il titolo (confronto con il titolo del sommario,
    # senza sottotitoli dopo " - " e nome del sito dopo " | ").
    inizio = 0
    if titolo:
        titolo_norm = re.split(r" - | \| ", titolo)[0].strip()
        finestra = [(i, " ".join(x["testo"] for x in righe[i:i + 4])) for i in range(len(righe))]
        punteggio, i = max((simile(t[:len(titolo_norm) + 20], titolo_norm), i) for i, t in finestra)
        if punteggio > 0.6:
            inizio = i + 1

    corpo = []
    for r in righe[inizio:]:
        t = r["testo"]
        # Rientro ammesso a destra (elenchi puntati, citazioni), non a sinistra.
        in_colonna = x_corpo - 12 <= r["x0"] <= x_corpo + 45
        if FINE.match(t) and corpo and in_colonna:
            break
        if SALTA.match(t) or URL.search(t):
            continue
        if abs(r["stile"][0] - dim_corpo) > 0.3 or not in_colonna:
            continue
        corpo.append(t)
    testo = " ".join(corpo)
    testo = re.sub(r"(\w)- (\w)", r"\1\2", testo)
    return re.sub(r"\s+", " ", testo).strip()


def blocchi_testo(doc: pymupdf.Document, pagine: list[int]) -> list[dict]:
    """Righe raggruppate in blocchi (paragrafi): stessa pagina, stesso inizio x, stessa dimensione, righe vicine."""
    righe = sorted((r for n in pagine for r in righe_pagina(doc[n], n)),
                   key=lambda r: (r["pagina"], round(r["y0"]), r["x0"]))
    blocchi = []
    for r in righe:
        b = blocchi[-1] if blocchi else None
        if (b and b["pagina"] == r["pagina"] and abs(b["x0"] - r["x0"]) < 3 and b["dim"] == r["stile"][0]
                and 0 <= r["y0"] - b["y1"] < b["dim"] * 1.2):
            b["testo"] += " " + r["testo"]
            b["y1"] = r["y0"] + r["stile"][0]
            b["x1"] = max(b["x1"], r["x1"])
        else:
            blocchi.append({"pagina": r["pagina"], "x0": r["x0"], "x1": r["x1"], "y0": r["y0"],
                            "y1": r["y0"] + r["stile"][0], "dim": r["stile"][0], "testo": r["testo"]})
    return blocchi


ISTRUZIONI = """Ricevi i blocchi di testo di una pagina web di un giornale online, salvata in PDF.
Ogni blocco ha un numero, la pagina, la posizione orizzontale x, la larghezza w e la dimensione del font.
La pagina contiene un solo articolo, ma anche menu, pubblicità, link ad altri articoli, colonne laterali,
date, firme, tag, commenti, inviti a iscriversi o condividere.

Indica SOLO i blocchi che formano il corpo dell'articolo indicato dal titolo: i paragrafi del testo,
nell'ordine. Escludi il titolo stesso, il sommario sotto il titolo se ripete il titolo, e tutto il resto.
I paragrafi del corpo hanno di solito la stessa x, la stessa dimensione di font e larghezza simile.

Rispondi solo con JSON: {"blocchi": [numeri]}"""


def estrai_llm(doc: pymupdf.Document, pagine: list[int], titolo: str) -> str:
    blocchi = blocchi_testo(doc, pagine)
    if not blocchi:
        return ""
    elenco = "\n".join(
        f"[{i}] p{b['pagina']} x={b['x0']:.0f} w={b['x1'] - b['x0']:.0f} font={b['dim']}: "
        f"{b['testo'][:220]}{'…' if len(b['testo']) > 220 else ''}"
        for i, b in enumerate(blocchi))
    richiesta = {
        "model": MODELLO, "stream": False, "format": "json",
        "options": {"temperature": 0, "num_ctx": 16384},
        "messages": [{"role": "system", "content": ISTRUZIONI},
                     {"role": "user", "content": f"Titolo: {titolo}\n\nBlocchi:\n{elenco}"}],
    }
    req = urllib.request.Request(OLLAMA_URL, data=json.dumps(richiesta).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        risposta = json.load(resp)["message"]["content"]
    try:
        scelti = [int(i) for i in json.loads(risposta).get("blocchi", [])]
    except (ValueError, TypeError, AttributeError):
        return ""
    testo = " ".join(blocchi[i]["testo"] for i in sorted(set(scelti)) if 0 <= i < len(blocchi))
    testo = re.sub(r"(\w)- (\w)", r"\1\2", testo)
    return re.sub(r"\s+", " ", testo).strip()


def url_originale(doc: pymupdf.Document, pagina: int) -> str | None:
    testo = doc[pagina].get_text()[:1500]
    m = URL.search(testo)
    return m[0].rstrip(".,)") if m else None


def estrai_trafilatura(url: str) -> str | None:
    html = trafilatura.fetch_url(url)
    if not html:
        return None
    testo = trafilatura.extract(html, include_comments=False, include_tables=False, favor_precision=True)
    return re.sub(r"\s+", " ", testo).strip() if testo else None


PAROLA = re.compile(r"\w+")


def confronto(estratto: str, riferimento: str):
    """Precisione, richiamo e F1 sulle parole (multinsiemi)."""
    a, b = Counter(PAROLA.findall(estratto.lower())), Counter(PAROLA.findall(riferimento.lower()))
    comuni = sum((a & b).values())
    p = comuni / sum(a.values()) if a else 0
    r = comuni / sum(b.values()) if b else 0
    return p, r, 2 * p * r / (p + r) if p + r else 0


def leggi_w2_salvato(file: Path):
    """Testo W2 già scaricato in una prova precedente (None se assente o non riuscito)."""
    if not file.exists():
        return None, False
    righe = file.read_text(encoding="utf-8").split("\n")
    testo = "\n".join(righe[4:]).strip()
    return (None if testo.startswith("[") else testo), True


def media(valori):
    return round(statistics.mean(valori), 3) if valori else None


if __name__ == "__main__":
    campione = json.loads((PROTOTIPO / "campione.json").read_text(encoding="utf-8"))
    articoli = [(c["pdf"], a) for c in campione for a in c["articoli"] if a["tipo"] == "web"]
    for m in ("W1", "W2", "W3"):
        (USCITA / m).mkdir(parents=True, exist_ok=True)

    misure = {"W1": [], "W3": []}
    parole, tempi_w3, n_w2 = {"W1": [], "W3": []}, [], 0
    for i, (pdf, a) in enumerate(articoli):
        doc = pymupdf.open(ARCHIVIO / pdf)
        intest = f"{pdf} | {a['testata']} | {a['titolo']}\n\n"

        w1 = estrai_euristica(doc, a["pagine_pdf"], a["titolo"])
        t0 = time.perf_counter()
        w3 = estrai_llm(doc, a["pagine_pdf"], a["titolo"])
        tempi_w3.append(time.perf_counter() - t0)
        for m, testo in (("W1", w1), ("W3", w3)):
            parole[m].append(len(testo.split()))
            (USCITA / m / f"{i:03d}.txt").write_text(intest + testo + "\n", encoding="utf-8")

        file_w2 = USCITA / "W2" / f"{i:03d}.txt"
        w2, gia_provato = leggi_w2_salvato(file_w2)
        if not gia_provato:
            url = url_originale(doc, a["pagine_pdf"][0])
            w2 = estrai_trafilatura(url) if url else None
            stato = "ok" if w2 else ("senza url" if not url else "non scaricato")
            file_w2.write_text(f"{intest}{url}\n\n{w2 or '[' + stato + ']'}\n", encoding="utf-8")

        riga = f"{i:03d} {a['testata'][:22]:22} W1 {len(w1.split()):4}  W3 {len(w3.split()):4} ({tempi_w3[-1]:4.1f}s)"
        if w2:
            n_w2 += 1
            for m, testo in (("W1", w1), ("W3", w3)):
                misure[m].append(confronto(testo, w2))
            riga += (f"  W2 {len(w2.split()):4}  F1 W1 {misure['W1'][-1][2]:.2f}  W3 {misure['W3'][-1][2]:.2f}")
        print(riga, flush=True)

    riepilogo = {"articoli": len(articoli), "W2_riusciti": n_w2,
                 "W3_secondi_medi": round(statistics.mean(tempi_w3), 1)}
    for m in ("W1", "W3"):
        f1 = [x[2] for x in misure[m]]
        riepilogo[m] = {"parole_medie": round(statistics.mean(parole[m])),
                        "vuoti": sum(n == 0 for n in parole[m]),
                        "precisione": media([x[0] for x in misure[m]]),
                        "richiamo": media([x[1] for x in misure[m]]),
                        "F1": media(f1), "F1_mediana": round(statistics.median(f1), 3) if f1 else None,
                        "F1_almeno_085": sum(x >= 0.85 for x in f1), "F1_sotto_05": sum(x < 0.5 for x in f1)}
    (USCITA / "riepilogo.json").write_text(json.dumps(riepilogo, indent=2))
    print(json.dumps(riepilogo, indent=2))
