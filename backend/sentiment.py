"""Interfaccia unica per il sentiment: testo -> probabilità positivo / neutro / negativo.

Il resto del progetto usa solo `classifica` / `classifica_lista` e il tipo `Risultato`,
così il modello si può sostituire (checkpoint fine-tunato, Kev) senza toccare altro.

Implementazione attuale: Laya multilingual (server docker su 127.0.0.1:8010), prompt "V2 choice IT".
Solo libreria standard, nessuna dipendenza.

Uso:
    from sentiment import ClassificatoreLaya
    clf = ClassificatoreLaya()
    clf.classifica("Il fatturato è cresciuto del 10%.")
"""
import json
import urllib.request
from dataclasses import dataclass

CLASSI = ("positivo", "neutro", "negativo")

# Prompt "V2 choice IT", scelto con prova_domande.py (vedi Claude.md).
VARIANTE_PROMPT = "v2-choice-it"
DOMANDE = {
    "s": {
        "type": "choice",
        "instructions": "Classifica questa frase di una notizia. Valuta cosa implicano i fatti "
                        "(miglioramento o peggioramento), non solo il tono delle parole.",
        "criteria": {
            "positivo": "qualcosa migliora o va bene: crescita, guadagni, successi, record, soluzioni, benefici",
            "neutro":   "nessuna implicazione buona o cattiva: calendari, descrizioni, semplici annunci",
            "negativo": "qualcosa peggiora o va male: calo, perdite, crisi, reati, incidenti, rischi, critiche",
        },
    },
}

MAX_TESTI_PER_RICHIESTA = 64  # limite del server Laya (MAX_BATCH_STATES)


@dataclass
class Risultato:
    etichetta: str   # classe con probabilità più alta
    prob: dict       # {"positivo": p, "neutro": p, "negativo": p}, somma ~1
    modello: str     # versione: checkpoint, revisione e variante del prompt
    troncato: bool   # True se il testo superava il contesto del modello ed è stato tagliato


class ClassificatoreLaya:
    def __init__(self, url="http://127.0.0.1:8010", checkpoint="multilingual", timeout=120):
        self.url = url.rstrip("/")
        self.checkpoint = checkpoint
        self.timeout = timeout
        # La revisione del checkpoint caricato identifica esattamente i pesi usati.
        salute = self._richiesta("GET", "/health")
        revisione = salute["revisions"][checkpoint]
        self.versione = f"laya-{checkpoint}@{revisione[:7]}/{VARIANTE_PROMPT}"

    def classifica(self, testo: str) -> Risultato:
        return self.classifica_lista([testo])[0]

    def classifica_lista(self, testi: list[str]) -> list[Risultato]:
        risultati = []
        for i in range(0, len(testi), MAX_TESTI_PER_RICHIESTA):
            blocco = testi[i:i + MAX_TESTI_PER_RICHIESTA]
            corpo = {"model": self.checkpoint, "states": blocco, "questions": DOMANDE}
            risposta = self._richiesta("POST", "/v1/systemone/batch", corpo)
            risultati.extend(self._converti(r) for r in risposta["results"])
        return risultati

    def _converti(self, r: dict) -> Risultato:
        p = r["answers"]["s"]["probabilities"]
        prob = {c: p[c] for c in CLASSI}
        return Risultato(
            etichetta=max(prob, key=prob.get),
            prob=prob,
            modello=self.versione,
            troncato=r["usage"]["truncated"],
        )

    def _richiesta(self, metodo: str, percorso: str, corpo: dict | None = None) -> dict:
        dati = json.dumps(corpo).encode() if corpo is not None else None
        req = urllib.request.Request(self.url + percorso, data=dati, method=metodo,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.load(resp)
