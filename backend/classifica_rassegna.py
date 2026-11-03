"""Aggiunge il sentiment a una rassegna.json: per ogni frase e, aggregato, per ogni articolo.

Aggiorna anche dati/indice.json, l'elenco delle rassegne che il frontend carica.
Il server Laya deve essere acceso. Si lancia dalla cartella backend con:
    python3 classifica_rassegna.py ../dati/Evidenze_rassegna_23_apr_25/rassegna.json
"""
import json
import sys
from dataclasses import asdict
from pathlib import Path

from sentiment import CLASSI, ClassificatoreLaya

DATI = Path(__file__).resolve().parent.parent / "dati"


def aggrega(sentimenti: list[dict], versione: str) -> dict | None:
    """Sentiment dell'articolo: media delle probabilità delle frasi (regola provvisoria)."""
    if not sentimenti:
        return None
    prob = {c: round(sum(s["prob"][c] for s in sentimenti) / len(sentimenti), 4) for c in CLASSI}
    return {
        "etichetta": max(prob, key=prob.get),
        "prob": prob,
        "modello": versione,
        "troncato": any(s["troncato"] for s in sentimenti),
        "metodo": "media_frasi",
    }


def classifica_rassegna(percorso: Path, clf: ClassificatoreLaya) -> dict:
    rassegna = json.loads(percorso.read_text(encoding="utf-8"))
    frasi = [f for a in rassegna["articoli"] for f in a["frasi"]]
    risultati = clf.classifica_lista([f["testo"] for f in frasi])
    for f, r in zip(frasi, risultati):
        f["sentiment"] = {**asdict(r), "metodo": "frase"}
    for a in rassegna["articoli"]:
        a["sentiment"] = aggrega([f["sentiment"] for f in a["frasi"]], clf.versione)
    percorso.write_text(json.dumps(rassegna, ensure_ascii=False, indent=2), encoding="utf-8")
    return rassegna


def aggiorna_indice():
    """Elenco delle rassegne (indice.json), riepilogo leggero degli articoli (articoli.json) e testi (testi.json).

    La dashboard all'avvio carica solo articoli.json; il testo completo di un articolo lo prende dalla sua
    rassegna.json quando lo si apre.
    """
    rassegne = sorted(str(p.relative_to(DATI)) for p in DATI.glob("*/rassegna.json"))
    (DATI / "indice.json").write_text(json.dumps({"rassegne": rassegne}, indent=2), encoding="utf-8")
    riepilogo, testi = [], {}
    for percorso in rassegne:
        r = json.loads((DATI / percorso).read_text(encoding="utf-8"))
        for a in r["articoli"]:
            testi[a["id"]] = " ".join((a.get("testo") or "").split())
            s, e = a.get("sentiment"), a.get("estrazione") or {}
            riepilogo.append({
                "id": a["id"], "file": percorso, "titolo": a["titolo"], "testata": a["testata"], "data": a["data"],
                "tipo": a.get("tipo"), "sezione": a.get("sezione"),
                "sentiment": {"etichetta": s["etichetta"], "prob": s["prob"]} if s else None,
                "affidabilita": e.get("affidabilita"), "classe": e.get("classe"),
            })
    (DATI / "articoli.json").write_text(json.dumps(riepilogo, ensure_ascii=False, separators=(",", ":")),
                                        encoding="utf-8")
    # Testi completi per la ricerca per parola chiave: la dashboard li carica solo alla prima ricerca.
    (DATI / "testi.json").write_text(json.dumps(testi, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return rassegne


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Uso: python3 classifica_rassegna.py <rassegna.json> [altre...]")
    clf = ClassificatoreLaya()
    print(f"Modello: {clf.versione}")
    for p in sys.argv[1:]:
        r = classifica_rassegna(Path(p), clf)
        for a in r["articoli"]:
            s = a["sentiment"]
            dettaglio = f"{s['etichetta']:9}  " + "  ".join(f"{c[:3]} {s['prob'][c]:.2f}" for c in CLASSI) if s else "-"
            print(f"  {a['titolo'][:60]:60}  {dettaglio}")
    print(f"Indice: {len(aggiorna_indice())} rassegne in {DATI / 'indice.json'}")
