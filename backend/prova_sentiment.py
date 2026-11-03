"""Verifica dell'interfaccia sentiment sulle frasi etichettate.

Classifica tutte le frasi di test_set.csv e stampa accuratezza, accuratezza bilanciata,
macro-F1, recall per classe e matrice di confusione.
Il server Laya deve essere acceso. Si lancia dalla cartella backend con:
    python3 prova_sentiment.py
"""
import csv
import time

from sentiment import CLASSI, ClassificatoreLaya

TEST_SET = "/home/giovanni/Documenti/modelli/data/test_set.csv"

with open(TEST_SET, encoding="utf-8") as f:
    frasi = list(csv.DictReader(f))

clf = ClassificatoreLaya()
print(f"Modello: {clf.versione}")

t0 = time.perf_counter()
risultati = clf.classifica_lista([r["frase"] for r in frasi])
secondi = time.perf_counter() - t0

vere = [r["etichetta"] for r in frasi]
pred = [r.etichetta for r in risultati]

# Matrice di confusione: righe = etichetta vera, colonne = predetta.
matrice = {v: {p: 0 for p in CLASSI} for v in CLASSI}
for v, p in zip(vere, pred):
    matrice[v][p] += 1

recall, f1 = {}, {}
for c in CLASSI:
    tp = matrice[c][c]
    n_veri = sum(matrice[c].values())
    n_pred = sum(matrice[v][c] for v in CLASSI)
    recall[c] = tp / n_veri if n_veri else 0
    precisione = tp / n_pred if n_pred else 0
    f1[c] = 2 * precisione * recall[c] / (precisione + recall[c]) if precisione + recall[c] else 0

accuratezza = sum(v == p for v, p in zip(vere, pred)) / len(vere)
print(f"\n{len(frasi)} frasi in {secondi:.1f} s ({secondi / len(frasi) * 1000:.1f} ms/frase)")
print(f"Troncate: {sum(r.troncato for r in risultati)}")
print(f"\nAccuratezza:            {accuratezza:.1%}")
print(f"Accuratezza bilanciata: {sum(recall.values()) / len(CLASSI):.1%}")
print(f"Macro-F1:               {sum(f1.values()) / len(CLASSI):.3f}")
print("Recall per classe:      " + "  ".join(f"{c} {recall[c]:.0%}" for c in CLASSI))

print("\nMatrice di confusione (righe = vera, colonne = predetta)")
print(" " * 10 + "".join(f"{c:>10}" for c in CLASSI))
for v in CLASSI:
    print(f"{v:>10}" + "".join(f"{matrice[v][p]:>10}" for p in CLASSI))

print(f"\nEsempio: {frasi[0]['frase']}\n  -> {risultati[0]}")
