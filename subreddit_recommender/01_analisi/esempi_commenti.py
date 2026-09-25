"""Mostra esempi reali per ogni regola di pulizia, per controllare falsi positivi e sporcizia residua.

    python 01_analisi/esempi_commenti.py
"""

from pathlib import Path

import pandas as pd

OUTPUT = Path(__file__).resolve().parent / "output"


def mostra(titolo: str, testi: pd.Series, n: int = 10) -> None:
    print(f"\n== {titolo} ==")
    for t in testi.head(n):
        print("  -", str(t)[:160].replace("\n", " "))


def main() -> None:
    df = pd.read_parquet(OUTPUT / "commenti_puliti.parquet")
    tenuti = df[df["motivo_scarto"].isna()]

    print("== autori scartati come bot (nome) ==")
    print(df[df["motivo_scarto"] == "bot_nome"]["author"].value_counts().head(20).to_string())

    for motivo in ["bot_frase", "moderazione", "autore_ripetitivo"]:
        scarti = df[df["motivo_scarto"] == motivo]
        mostra(motivo, scarti.sample(min(8, len(scarti)), random_state=1).apply(
            lambda r: f"[{r.author}] {r.testo_pulito}", axis=1))

    print("\n== ripetuto: testi più frequenti ==")
    print(df[df["motivo_scarto"] == "ripetuto"]["testo_pulito"].str[:100].value_counts().head(15).to_string())

    corti = df[(df["motivo_scarto"] == "corto") & (df["n_parole"] >= 3)]
    mostra("corto (3-4 parole)", corti["testo_pulito"].sample(10, random_state=3))

    mostra("TENUTI, casuali", tenuti.sample(15, random_state=2).apply(
        lambda r: f"[{r.subreddit}] {r.testo_pulito}", axis=1), 15)

    print("\n== TENUTI: autori più attivi (controllare che non siano bot) ==")
    print(tenuti["author"].value_counts().head(15).to_string())

    print("\n== subreddit: quota di commenti tenuti e di spam ==")
    per_sub = df.groupby("subreddit").agg(
        commenti=("id", "size"),
        tenuti=("motivo_scarto", lambda s: s.isna().mean()),
        spam=("motivo_scarto", lambda s: s.isin(["ripetuto", "autore_ripetitivo"]).mean()),
    )
    print(per_sub[per_sub["commenti"] >= 200].sort_values("spam", ascending=False).head(15).round(3).to_string())


if __name__ == "__main__":
    main()
