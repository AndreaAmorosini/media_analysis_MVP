"""Mostra esempi reali per ogni regola di pulizia dei post, per controllare falsi positivi e
sporcizia residua.

    python 01_analisi/esempi_post.py
"""

from pathlib import Path

import pandas as pd

OUTPUT = Path(__file__).resolve().parent / "output"


def mostra(titolo: str, testi: pd.Series, n: int = 10) -> None:
    print(f"\n== {titolo} ==")
    for t in testi.head(n):
        print("  -", str(t)[:160].replace("\n", " "))


def campione(df: pd.DataFrame, n: int, seed: int = 1) -> pd.DataFrame:
    return df.sample(min(n, len(df)), random_state=seed)


def main() -> None:
    df = pd.read_parquet(OUTPUT / "post_puliti.parquet")
    tenuti = df[df["motivo_scarto"].isna()]
    etichetta = lambda r: f"[{r.subreddit}] {r.testo_pulito}"  # noqa: E731

    for motivo in ["rimosso_mod", "bot_nome", "moderazione", "duplicato"]:
        mostra(motivo, campione(df[df["motivo_scarto"] == motivo], 8).apply(etichetta, axis=1))

    print("\n== ripetuto: titoli più frequenti ==")
    print(df[df["motivo_scarto"] == "ripetuto"]["titolo_pulito"].str[:100].value_counts().head(15).to_string())

    corti = df[(df["motivo_scarto"] == "corto") & (df["n_parole"] == 3)]
    mostra("corto (3 parole)", campione(corti, 10, 3).apply(etichetta, axis=1))

    mostra("TENUTI, casuali", campione(tenuti, 15, 2).apply(etichetta, axis=1), 15)
    mostra("TENUTI, link esterni", campione(tenuti[tenuti["tipo"] == "link"], 10, 2).apply(
        lambda r: f"[{r.subreddit}] ({r.dominio}) {r.titolo_pulito}", axis=1))
    non_it = df[df["motivo_scarto"] == "non_italiano"]
    mostra("non_italiano, i più vicini alla soglia", non_it.nlargest(10, "conf_italiano").apply(
        lambda r: f"({r.conf_italiano:.2f}) {etichetta(r)}", axis=1))
    mostra("TENUTI, i meno sicuramente italiani", tenuti.nsmallest(10, "conf_italiano").apply(
        lambda r: f"({r.conf_italiano:.2f}) {etichetta(r)}", axis=1))

    print("\n== sub_piccolo: subreddit esclusi per pochi post ==")
    piccoli = df[df["motivo_scarto"] == "sub_piccolo"]["subreddit"].value_counts()
    print(f"{len(piccoli)} subreddit, {piccoli.sum()} post: " + ", ".join(
        f"{s} ({n})" for s, n in piccoli.items()))

    print("\n== TENUTI: autori più attivi (controllare che non siano bot) ==")
    print(tenuti.groupby("author").agg(
        post=("id", "size"), sub=("subreddit", lambda s: ", ".join(s.value_counts().index[:3]))
    ).sort_values("post", ascending=False).head(15).to_string())

    print("\n== TENUTI: domini esterni più linkati ==")
    print(tenuti[tenuti["dominio"] != ""]["dominio"].value_counts().head(20).to_string())

    print("\n== subreddit: post tenuti ==")
    per_sub = df.groupby("subreddit").agg(
        post=("id", "size"),
        tenuti=("motivo_scarto", lambda s: s.isna().sum()),
        quota=("motivo_scarto", lambda s: s.isna().mean()),
    )
    print(per_sub.sort_values("tenuti", ascending=False).head(25).round(2).to_string())
    print(f"\nsubreddit con meno di 20 post tenuti: {(per_sub['tenuti'] < 20).sum()} su {len(per_sub)}")


if __name__ == "__main__":
    main()
