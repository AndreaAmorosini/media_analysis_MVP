"""Testo degli articoli web dalle pagine Data Stampa (testo nativo del PDF, con menu, banner e colonne laterali).

Due metodi, scelti con prova_web.py, combinati:
  euristica  colonna e font dominanti, inizio dopo il titolo del sommario, marcatori di fine
  llm        un modello generativo (Ollama) sceglie quali blocchi numerati sono il corpo; il testo resta
             quello originale dei blocchi, il modello non lo riscrive
Regola: se il testo scelto dal modello è contenuto per almeno l'80% in quello dell'euristica, si tiene
l'euristica (più completa); altrimenti il modello (l'euristica ha preso una colonna laterale).

Ogni metodo restituisce (segmenti, diagnostica):
  segmenti     {pagina (da 1), bbox, testo, nuovo_paragrafo} del corpo
  diagnostica  {pagina, bbox, livello, stato, motivo, testo} per ogni riga/blocco considerato
"""
import json
import os
import re
import urllib.request
from collections import Counter
from difflib import SequenceMatcher

import pymupdf

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11435/api/chat")
# Scelto con valuta_web.py tra qwen2.5:7b, qwen3.5:4b/9b, gemma4:e2b/e4b (2026-10-13): vedi Claude.md.
MODELLO = os.environ.get("MODELLO_WEB", "gemma4:e4b-it-qat")
SOGLIA_CONTENUTO = 0.8

URL = re.compile(r"https?://\S+")
FINE = re.compile(r"^(leggi anche|articoli correlati|potrebbe interessarti|ti potrebbe interessare|"
                  r"tags?:?$|commenti|lascia un commento|iscriviti|condividi|© riproduzione|"
                  r"riproduzione riservata|correlati|altri articoli|ultime notizie|"
                  r"tutte le notizie|post più letti|articolo precedente|articolo successivo)", re.I)
SALTA = re.compile(r"^(versione accessibile|di redazione$|redazione$|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|"
                   r"tempo di lettura|pubblicità|advertisement|foto:?|immagine di repertorio)", re.I)
PAROLA = re.compile(r"\w+")


def diag(pagina, bbox, livello, stato, motivo, testo=""):
    return {"pagina": pagina, "bbox": bbox, "livello": livello, "stato": stato, "motivo": motivo,
            "testo": testo[:120]}


def righe_pagina(pagina: pymupdf.Page, n: int, scartate: list | None = None):
    """Righe di testo con posizione e stile; quelle nella cornice Data Stampa finiscono in `scartate`."""
    for blocco in pagina.get_text("dict")["blocks"]:
        for riga in blocco.get("lines", []):
            spans = [s for s in riga["spans"] if s["text"].strip()]
            if not spans:
                continue
            bbox = [round(v, 1) for v in riga["bbox"]]
            testo = " ".join(s["text"].strip() for s in spans)
            if bbox[3] < 70 or bbox[1] > 812 or bbox[0] > 570:  # intestazione, piè di pagina, striscia dei diritti
                if scartate is not None:
                    scartate.append(diag(n + 1, bbox, "riga", "scartato", "cornice Data Stampa", testo))
                continue
            dim = round(max(spans, key=lambda s: len(s["text"]))["size"], 1)
            yield {"pagina": n + 1, "bbox": bbox, "testo": testo, "dim": dim}


def righe_ordinate(doc: pymupdf.Document, pagine: list[int], scartate: list | None = None) -> list[dict]:
    # Ordine di lettura: pagina, poi dall'alto in basso (PyMuPDF non lo garantisce).
    return sorted((r for n in pagine for r in righe_pagina(doc[n], n, scartate)),
                  key=lambda r: (r["pagina"], round(r["bbox"][1]), r["bbox"][0]))


def simile(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def inizio_dopo_titolo(righe: list[dict], titolo: str) -> int:
    """Indice della prima riga dopo il titolo (confronto con il titolo del sommario), 0 se non trovato."""
    if not titolo or not righe:
        return 0
    titolo_norm = re.split(r" - | \| ", titolo)[0].strip()
    finestra = [(i, " ".join(x["testo"] for x in righe[i:i + 4])) for i in range(len(righe))]
    punteggio, i = max((simile(t[:len(titolo_norm) + 20], titolo_norm), i) for i, t in finestra)
    return i + 1 if punteggio > 0.6 else 0


def euristica(doc: pymupdf.Document, pagine: list[int], titolo: str):
    d = []
    righe = righe_ordinate(doc, pagine, d)
    if not righe:
        return [], d
    # Font e colonna del corpo: quelli che portano più testo nelle righe lunghe della pagina.
    peso_dim, peso_x = Counter(), Counter()
    for r in righe:
        if len(r["testo"].split()) >= 6:
            peso_dim[r["dim"]] += len(r["testo"])
            peso_x[round(r["bbox"][0] / 10)] += len(r["testo"])
    dim_corpo = peso_dim.most_common(1)[0][0] if peso_dim else righe[0]["dim"]
    x_corpo = peso_x.most_common(1)[0][0] * 10 if peso_x else righe[0]["bbox"][0]

    inizio = inizio_dopo_titolo(righe, titolo)
    segmenti, prec, finito = [], None, False
    for i, r in enumerate(righe):
        t = r["testo"]
        # Rientro ammesso a destra (elenchi puntati, citazioni), non a sinistra.
        in_colonna = x_corpo - 12 <= r["bbox"][0] <= x_corpo + 45
        if i < inizio:
            motivo = "prima dell'inizio del corpo (titolo e intestazione del sito)"
        elif finito:
            motivo = "dopo il marcatore di fine"
        elif FINE.match(t) and segmenti and in_colonna:
            finito, motivo = True, "marcatore di fine"
        elif SALTA.match(t) or URL.search(t):
            motivo = "data, firma, link o etichetta"
        elif not in_colonna:
            motivo = f"fuori dalla colonna del corpo (x {r['bbox'][0]:.0f}, corpo {x_corpo:.0f})"
        elif abs(r["dim"] - dim_corpo) > 0.3:
            motivo = f"font diverso ({r['dim']} invece di {dim_corpo})"
        else:
            motivo = None
        if motivo:
            d.append(diag(r["pagina"], r["bbox"], "riga", "scartato", motivo, t))
            continue
        # Nuovo paragrafo se c'è uno spazio verticale più ampio di una riga e mezza o si cambia pagina.
        nuovo = (prec is None or r["pagina"] != prec["pagina"]
                 or r["bbox"][1] - prec["bbox"][3] > r["dim"] * 0.9)
        segmenti.append({"pagina": r["pagina"], "bbox": r["bbox"], "testo": t, "nuovo_paragrafo": nuovo})
        d.append(diag(r["pagina"], r["bbox"], "riga", "tenuto", "corpo (colonna e font dominanti)", t))
        prec = r
    return segmenti, d


def blocchi(doc: pymupdf.Document, pagine: list[int], scartate: list | None = None) -> list[dict]:
    """Righe raggruppate in blocchi (paragrafi): stessa pagina, stesso inizio x, stessa dimensione, righe vicine."""
    uscita = []
    for r in righe_ordinate(doc, pagine, scartate):
        # La riga continua il blocco della sua colonna, anche se nel frattempo ne sono iniziati altri
        # alla stessa altezza (colonne laterali): si cerca all'indietro tra i blocchi della stessa pagina.
        blocco = None
        for b in reversed(uscita):
            if b["pagina"] != r["pagina"]:
                break
            if (abs(b["bbox"][0] - r["bbox"][0]) < 3 and b["dim"] == r["dim"]
                    and 0 <= r["bbox"][1] - b["bbox"][3] < b["dim"] * 1.2):
                blocco = b
                break
        if blocco:
            blocco["righe"].append(r)
            blocco["bbox"] = [blocco["bbox"][0], blocco["bbox"][1],
                              max(blocco["bbox"][2], r["bbox"][2]), r["bbox"][3]]
        else:
            uscita.append({"pagina": r["pagina"], "bbox": list(r["bbox"]), "dim": r["dim"], "righe": [r]})
    return uscita


ISTRUZIONI = """Ricevi i blocchi di testo di una pagina web di un giornale online, salvata in PDF.
Ogni blocco ha un numero, la pagina, la posizione orizzontale x, la larghezza w e la dimensione del font.
La pagina contiene un solo articolo, ma anche menu, pubblicità, link ad altri articoli, colonne laterali,
date, firme, tag, commenti, inviti a iscriversi o condividere.

Indica SOLO i blocchi che formano il corpo dell'articolo indicato dal titolo: i paragrafi del testo,
nell'ordine. Escludi il titolo stesso, il sommario sotto il titolo se ripete il titolo, e tutto il resto.
I paragrafi del corpo hanno di solito la stessa x, la stessa dimensione di font e larghezza simile.

Rispondi solo con JSON: {"blocchi": [numeri]}"""


def chiedi_modello(modello: str, titolo: str, elenco: str) -> str:
    richiesta = {
        "model": modello, "stream": False, "format": "json",
        "options": {"temperature": 0, "num_ctx": 16384},
        "messages": [{"role": "system", "content": ISTRUZIONI},
                     {"role": "user", "content": f"Titolo: {titolo}\n\nBlocchi:\n{elenco}"}],
    }
    if not modello.startswith("qwen2.5"):
        richiesta["think"] = False  # modelli con ragionamento: qui non serve e rallenta molto
    req = urllib.request.Request(OLLAMA_URL, data=json.dumps(richiesta).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.load(resp)["message"]["content"]


def llm(doc: pymupdf.Document, pagine: list[int], titolo: str, modello: str = MODELLO):
    d = []
    bb = blocchi(doc, pagine, d)
    if not bb:
        return [], d
    elenco = "\n".join(
        f"[{i}] p{b['pagina']} x={b['bbox'][0]:.0f} w={b['bbox'][2] - b['bbox'][0]:.0f} font={b['dim']}: "
        + (t[:220] + "…" if len(t := " ".join(r["testo"] for r in b["righe"])) > 220 else t)
        for i, b in enumerate(bb))
    try:
        scelti = {int(i) for i in json.loads(chiedi_modello(modello, titolo, elenco)).get("blocchi", [])}
    except (ValueError, TypeError, AttributeError):
        scelti = set()
    segmenti = []
    for i, b in enumerate(bb):
        testo = " ".join(r["testo"] for r in b["righe"])
        if i in scelti:
            d.append(diag(b["pagina"], b["bbox"], "blocco", "tenuto", f"blocco {i} scelto dal modello", testo))
            for k, r in enumerate(b["righe"]):
                segmenti.append({"pagina": r["pagina"], "bbox": r["bbox"], "testo": r["testo"],
                                 "nuovo_paragrafo": k == 0})
        else:
            d.append(diag(b["pagina"], b["bbox"], "blocco", "scartato", f"blocco {i} non scelto dal modello", testo))
    return segmenti, d


def contenuto(a: list[dict], b: list[dict]) -> float:
    """Quota delle parole di `a` presenti anche in `b`."""
    pa = Counter(PAROLA.findall(" ".join(s["testo"] for s in a).lower()))
    pb = Counter(PAROLA.findall(" ".join(s["testo"] for s in b).lower()))
    return sum((pa & pb).values()) / max(1, sum(pa.values()))


def combina(e, m) -> tuple[list[dict], list[dict], str]:
    """Sceglie tra euristica e modello, ognuno come (segmenti, diagnostica): vedi la regola in testa al modulo."""
    (se, de), (sm, dm) = e, m
    if se and contenuto(sm, se) >= SOGLIA_CONTENUTO:
        return se, de, "euristica"
    return (sm, dm, "llm") if sm else (se, de, "euristica")


def estrai(doc: pymupdf.Document, pagine: list[int], titolo: str, modello: str = MODELLO):
    """(segmenti, diagnostica, metodo) del corpo dell'articolo; metodo "euristica" o "llm"."""
    return combina(euristica(doc, pagine, titolo), llm(doc, pagine, titolo, modello))


def url_originale(doc: pymupdf.Document, pagina: int) -> str | None:
    m = URL.search(doc[pagina].get_text()[:1500])
    return m[0].rstrip(".,)") if m else None
