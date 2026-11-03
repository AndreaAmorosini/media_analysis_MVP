"""Immagini delle pagine con i riquadri dell'estrazione disegnati sopra (da diagnostica.json).

Per ogni rassegna: dati/<id>/riquadri/page_NNN.jpg, a partire da dati/<id>/pagine/page_NNN.jpg.
Verde = tenuto (corpo), rosso = scartato, blu = informazione (colonna OCR, pagina usata per verificare l'URL).
Su ogni riquadro un'etichetta breve con il motivo.

    env -u PYTHONPATH ../.venv/bin/python disegna_riquadri.py            # tutte le rassegne
    env -u PYTHONPATH ../.venv/bin/python disegna_riquadri.py <id> ...   # solo alcune
"""
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

DATI = Path(__file__).resolve().parent.parent / "dati"
COLORI = {"tenuto": (46, 158, 91), "scartato": (214, 69, 69), "info": (47, 111, 214)}
ORDINE = {"info": 0, "scartato": 1, "tenuto": 2}  # le righe tenute sopra a tutto


def font(dim: int):
    for nome in ("DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(nome, dim)
        except OSError:
            continue
    return ImageFont.load_default()


def etichetta(e: dict) -> str:
    """Motivo abbreviato per l'etichetta sul riquadro."""
    m = e["motivo"].split(" (")[0]
    sostituzioni = {"cornice o intestazione": "cornice", "cornice Data Stampa": "cornice", "titolo": "titolo",
                    "titolo, sottotitolo o titoletto": "titolo/sottotitolo", "blocco di testo senza testo riconosciuto":
                    "illeggibile", "testo preso dall'URL originale; le posizioni sono ricavate confrontando le frasi "
                    "con le righe del PDF": "testo da URL"}
    return sostituzioni.get(m, m)[:40]


def tratteggio(disegno, bbox, colore, larghezza=2, passo=8):
    x0, y0, x1, y1 = bbox
    for lato in ((x0, y0, x1, y0), (x1, y0, x1, y1), (x0, y1, x1, y1), (x0, y0, x0, y1)):
        a0, b0, a1, b1 = lato
        lunghezza = max(abs(a1 - a0), abs(b1 - b0))
        for t in range(0, int(lunghezza), passo * 2):
            f = min(t + passo, lunghezza) / lunghezza if lunghezza else 1
            s = t / lunghezza if lunghezza else 0
            disegno.line((a0 + (a1 - a0) * s, b0 + (b1 - b0) * s, a0 + (a1 - a0) * f, b0 + (b1 - b0) * f),
                         fill=colore, width=larghezza)


def disegna_rassegna(cartella: Path) -> int:
    r = json.loads((cartella / "rassegna.json").read_text(encoding="utf-8"))
    d_file = cartella / "diagnostica.json"
    if not d_file.exists():
        return 0
    diagnostica = json.loads(d_file.read_text(encoding="utf-8"))
    per_pagina = {}
    for a in diagnostica.values():
        for e in a["elementi"]:
            if e.get("pagina") and e.get("bbox"):
                per_pagina.setdefault(e["pagina"], []).append(e)
    (cartella / "riquadri").mkdir(exist_ok=True)
    fatte = 0
    for p in r["pagine"]:
        sorgente = cartella / p["immagine"]
        if not sorgente.exists():
            continue
        img = Image.open(sorgente).convert("RGB")
        scala = img.width / p["larghezza"]
        velo = Image.new("RGBA", img.size, (0, 0, 0, 0))
        disegno_velo = ImageDraw.Draw(velo)
        elementi = sorted(per_pagina.get(p["n"], []), key=lambda e: ORDINE.get(e["stato"], 0))
        for e in elementi:
            colore = COLORI.get(e["stato"], COLORI["info"])
            b = [v * scala for v in e["bbox"]]
            if e["stato"] != "info":
                disegno_velo.rectangle(b, fill=colore + (40,))
        img = Image.alpha_composite(img.convert("RGBA"), velo).convert("RGB")
        disegno = ImageDraw.Draw(img)
        f = font(max(9, round(img.width / 110)))
        for e in elementi:
            colore = COLORI.get(e["stato"], COLORI["info"])
            b = [v * scala for v in e["bbox"]]
            if e["stato"] == "info":
                tratteggio(disegno, b, colore)
            else:
                disegno.rectangle(b, outline=colore, width=2)
            testo = etichetta(e)
            l, t, rr, bb = disegno.textbbox((0, 0), testo, font=f)
            x, y = b[0], max(0, b[1] - (bb - t) - 3)
            disegno.rectangle((x, y, x + (rr - l) + 4, y + (bb - t) + 3), fill=colore)
            disegno.text((x + 2, y + 1 - t), testo, fill="white", font=f)
        img.save(cartella / "riquadri" / Path(p["immagine"]).name, quality=85)
        fatte += 1
    return fatte


if __name__ == "__main__":
    cartelle = [DATI / i for i in sys.argv[1:]] or sorted(p.parent for p in DATI.glob("*/diagnostica.json"))
    totale = 0
    for k, c in enumerate(cartelle, 1):
        totale += disegna_rassegna(c)
        if k % 100 == 0:
            print(f"{k}/{len(cartelle)} rassegne, {totale} pagine", flush=True)
    print(f"{len(cartelle)} rassegne, {totale} pagine con riquadri", flush=True)
