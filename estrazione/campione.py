"""Sceglie il campione per il prototipo e ne elenca gli articoli con le pagine occupate.

Campione: N PDF per anno (con sommario), scelti a caso con seme fisso.
Ogni articolo va dalla sua pagina_pdf fino alla pagina prima dell'articolo successivo.
Scrive dati/prototipo/campione.json.

    env -u PYTHONPATH ../.venv/bin/python campione.py
"""
import json
import random
from pathlib import Path

import pymupdf

from sommario import leggi_rassegne

pymupdf.TOOLS.mupdf_display_errors(False)

ARCHIVIO = Path.home() / "Documenti/autentica/Data"
USCITA = Path(__file__).resolve().parent.parent / "dati/prototipo/campione.json"
PDF_PER_ANNO = 5
SEME = 7


def articoli_del_pdf(percorso: Path) -> list[dict]:
    doc = pymupdf.open(percorso)
    articoli = []
    for r in leggi_rassegne(doc):
        voci = [v for v in r["voci"] if v["pagina_pdf"] is not None and v["pagina_pdf"] < len(doc)]
        for k, v in enumerate(voci):
            fine = voci[k + 1]["pagina_pdf"] if k + 1 < len(voci) else len(doc)
            # Fine al prossimo sommario, se le rassegne sono unite nello stesso PDF.
            fine = min([fine] + [p for p in range(v["pagina_pdf"] + 1, fine) if p == r["pagina_sommario"]])
            articoli.append({**v, "tipo": "web" if v["sezione"] == "WEB" else "stampa",
                             "pagine_pdf": list(range(v["pagina_pdf"], max(fine, v["pagina_pdf"] + 1)))})
    return articoli


if __name__ == "__main__":
    tutti = sorted(ARCHIVIO.rglob("*.pdf"))
    rnd = random.Random(SEME)
    campione = []
    for anno in ("2023", "2024", "2025", "2026"):
        candidati = [p for p in tutti if p.relative_to(ARCHIVIO).parts[0] == anno]
        rnd.shuffle(candidati)
        scelti = 0
        for p in candidati:
            articoli = articoli_del_pdf(p)
            if not articoli:
                continue
            campione.append({"pdf": str(p.relative_to(ARCHIVIO)), "articoli": articoli})
            scelti += 1
            if scelti == PDF_PER_ANNO:
                break

    USCITA.parent.mkdir(parents=True, exist_ok=True)
    USCITA.write_text(json.dumps(campione, ensure_ascii=False, indent=1), encoding="utf-8")
    n = [a for c in campione for a in c["articoli"]]
    print(f"{len(campione)} PDF, {len(n)} articoli "
          f"({sum(a['tipo'] == 'stampa' for a in n)} stampa, {sum(a['tipo'] == 'web' for a in n)} web) -> {USCITA}")
    for c in campione:
        print(f"  {c['pdf']}: {len(c['articoli'])} articoli")
