"""Esporta gli articoli dai risultati della pipeline in una struttura pulita.

Per ogni cartella `Evidenze_Rassegna_*` (o qualsiasi cartella contenente
`flow/article_records.json`) dentro `--input-root`, legge i record completi
e scrive un JSON per articolo dentro `--output-root/<nome_cartella>/`:

    <output-root>/Evidenze_Rassegna_24042026/article_0001.json
    <output-root>/Evidenze_Rassegna_24042026/article_0002.json
    ...

Ogni JSON contiene solo: title, subtitle, publication_date, body, source.
I campi mancanti sono stringhe vuote (""), come già fatto per subtitle.
`source` è il nome del giornale o sito (`record["source"]["source"]`);
se assente, vale "".
"""

import argparse
import json
from pathlib import Path


def article_body(record: dict) -> str:
    """Stessa selezione del body usata da batch_parse: body, poi body_clean, poi body_raw."""
    for key in ("body", "body_clean", "body_raw"):
        candidate = record.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def article_source(record: dict) -> str:
    """Nome della testata/sito dal record; "" se non disponibile."""
    source = record.get("source")
    if isinstance(source, dict):
        name = source.get("source")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return ""


def export_document(doc_dir: Path, out_dir: Path) -> int:
    records_path = doc_dir / "flow" / "article_records.json"
    records = json.loads(records_path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError(f"{records_path} non contiene una lista di articoli")

    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("article_*.json"):
        stale.unlink()
    # Rimuovi anche gli article_*.json esportati in una run precedente
    # dentro la stessa cartella documento (output batch annidato).
    for stale in doc_dir.glob("article_*.json"):
        stale.unlink()

    for index, record in enumerate(records, start=1):
        article = {
            "title": (record.get("title") or "").strip(),
            "subtitle": (record.get("subtitle") or "").strip() or "",
            "publication_date": record.get("publication_date") or "",
            "body": article_body(record),
            "source": article_source(record),
        }
        (out_dir / f"article_{index:04d}.json").write_text(
            json.dumps(article, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return len(records)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", type=Path, default=Path("results"),
                        help="Cartella con i risultati (default: results)")
    parser.add_argument("--output-root", type=Path, required=True,
                        help="Cartella di destinazione degli articoli esportati")
    args = parser.parse_args()

    # Trova ricorsivamente ogni cartella documento con flow/article_records.json
    # (funziona sia con results/Evidenze_Rassegna_* sia con output batch
    # annidati come results/test/2026/Settembre/<documento>/).
    records_files = sorted(args.input_root.rglob("flow/article_records.json"))
    if not records_files:
        raise SystemExit(f"Nessun flow/article_records.json trovato in {args.input_root}")

    total = 0
    for records_path in records_files:
        doc_dir = records_path.parent.parent          # cartella del documento
        rel = doc_dir.relative_to(args.input_root)    # percorso relativo preservato
        out_dir = args.output_root / rel
        count = export_document(doc_dir, out_dir)
        total += count
        print(f"{rel}: {count} articoli -> {out_dir}/")
    print(f"Totale: {total} articoli esportati in {args.output_root}")


if __name__ == "__main__":
    main()
