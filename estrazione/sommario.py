"""Lettura del sommario delle rassegne Data Stampa (testo nativo a colonne).

Un PDF contiene di solito una rassegna con il sommario in pagina 1; alcuni PDF uniscono più rassegne,
ognuna con il proprio sommario. Ogni riga del sommario diventa una voce:
    {sezione, data, testata, pagina_giornale, titolo, autore, pagina_rassegna, pagina_pdf}
`pagina_pdf` (da 0) è la prima pagina del PDF occupata dall'articolo.

Le colonne si riconoscono dalla posizione orizzontale (x) di ogni frammento di testo e dal contenuto.
Varianti viste: data con anno a 2 o 4 cifre; colonna della pagina del giornale presente o assente
(il titolo inizia allora a x 187 invece di 203); titolo, testata e autore che vanno a capo.
"""
import re

import pymupdf

DATA_RASSEGNA = re.compile(r"Rassegna del (\d{2})/(\d{2})/(\d{4})")
DATA_RIGA = re.compile(r"^(\d{2})/(\d{2})/(\d{2}|\d{4})$")


def colonna(x: float, testo: str) -> str:
    if x < 80:
        return "data"
    if x < 165:
        return "testata"
    if x < 200 and testo.isdigit():
        return "pagina_giornale"
    if x < 430:
        return "titolo"
    if x < 540:
        return "autore"
    return "pagina_rassegna"


def frammenti(pagina: pymupdf.Page):
    """(x, y, dimensione, testo) per ogni frammento di testo non vuoto."""
    for blocco in pagina.get_text("dict")["blocks"]:
        for riga in blocco.get("lines", []):
            for s in riga["spans"]:
                if s["text"].strip():
                    yield s["bbox"][0], s["bbox"][1], s["size"], s["text"].strip()


def e_sommario(pagina: pymupdf.Page) -> bool:
    return bool(DATA_RASSEGNA.search(pagina.get_text()[:200]))


def leggi_pagina_sommario(pagina: pymupdf.Page):
    """Data della rassegna e voci di una pagina di sommario."""
    data, voci, sezione = None, [], None
    righe = {}
    for x, y, dim, testo in frammenti(pagina):
        if m := DATA_RASSEGNA.search(testo):
            data = f"{m[3]}-{m[2]}-{m[1]}"
        elif dim >= 11:  # intestazione di sezione: STAMPA LOCALE, WEB, ...
            righe.setdefault(round(y), []).append(("sezione", testo))
        else:
            righe.setdefault(round(y), []).append((colonna(x, testo), testo))

    for y in sorted(righe):
        celle = righe[y]
        if celle[0][0] == "sezione":
            sezione = " ".join(t for _, t in celle)
            continue
        valori = {}
        for c, t in celle:
            valori[c] = f"{valori[c]} {t}" if c in valori else t
        if m := DATA_RIGA.match(valori.get("data", "")):
            gg, mm, aa = m.groups()
            voci.append({
                "sezione": sezione,
                "data": f"{aa if len(aa) == 4 else '20' + aa}-{mm}-{gg}",
                "testata": valori.get("testata"),
                "pagina_giornale": valori.get("pagina_giornale"),
                "titolo": valori.get("titolo", ""),
                "autore": valori.get("autore"),
                "pagina_rassegna": valori.get("pagina_rassegna"),
            })
        elif voci:
            # Continuazione della riga precedente (testata, titolo o autore andati a capo).
            for c in ("testata", "titolo", "autore"):
                if c in valori:
                    prima = voci[-1][c]
                    # Siti web in maiuscolo spezzati a metà parola: "LAPROVINCIAONLINE.I" + "NFO".
                    sep = "" if c == "testata" and prima and prima.isupper() and valori[c].isupper() else " "
                    voci[-1][c] = f"{prima}{sep}{valori[c]}" if prima else valori[c]

    for v in voci:
        if v["autore"] in ("...", "…"):
            v["autore"] = None
        for c in ("pagina_giornale", "pagina_rassegna"):
            v[c] = int(v[c]) if v[c] and v[c].isdigit() else None
    return data, voci


def numero_a_pie_di_pagina(pagina: pymupdf.Page) -> int | None:
    """Numero di pagina della rassegna stampato in basso a destra ("STAMPA LOCALE 6", "WEB 13")."""
    for x, y, dim, testo in frammenti(pagina):
        if y > pagina.rect.height - 40 and x > pagina.rect.width * 0.8 and testo.isdigit():
            return int(testo)
    return None


def leggi_rassegne(doc: pymupdf.Document):
    """Una rassegna per ogni pagina di sommario: [{data, pagina_sommario, voci}].

    La pagina_rassegna N di una voce si cerca tra le pagine successive al sommario tramite il numero stampato a
    piè di pagina: alcuni PDF contengono pagine non elencate nel sommario, che sfaserebbero il conteggio.
    Se il numero non si trova, si usa la posizione (sommario + N).
    """
    indici = [i for i, pagina in enumerate(doc) if e_sommario(pagina)]
    rassegne = []
    for k, i in enumerate(indici):
        fine = indici[k + 1] if k + 1 < len(indici) else len(doc)
        numeri = {}
        for j in range(i + 1, fine):
            n = numero_a_pie_di_pagina(doc[j])
            if n is not None and n not in numeri:
                numeri[n] = j
        data, voci = leggi_pagina_sommario(doc[i])
        for v in voci:
            n = v["pagina_rassegna"]
            v["pagina_pdf"] = (numeri.get(n, i + n)) if n else None
        rassegne.append({"data": data, "pagina_sommario": i, "voci": voci})
    return rassegne
