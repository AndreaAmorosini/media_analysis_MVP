"""IO su file JSON/JSONL per tutta la pipeline."""

import json
from pathlib import Path


def read_json(path: str | Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_jsonl(path: str | Path) -> list:
    """Legge un file JSONL e restituisce la lista dei record."""
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def iter_jsonl(path: str | Path):
    """Generatore sui record di un JSONL, con errore contestualizzato.

    Salta le righe vuote; una riga malformata solleva ValueError con
    il percorso e il numero di riga.
    """
    with Path(path).open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{number}: {exc}") from exc


def write_json(path: str | Path, data) -> None:
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_json_stable(path: str | Path, data) -> None:
    """Scrittura atomica con chiavi ordinate e newline finale.

    Le directory genitore vengono create se mancano; il file temporaneo
    sostituisce il destinatario solo dopo la scrittura completa.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def write_jsonl(path: str | Path, rows: list, *, sort_keys: bool = True) -> None:
    """Scrive un JSONL in streaming, una riga per record."""
    with Path(path).open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=sort_keys) + "\n")


def latest_records(path: str | Path, key: str = "fact_id") -> dict:
    """Restituisce un dizionario {key: record} con l'ultimo valore per chiave."""
    latest = {}
    for record in iter_jsonl(path):
        latest[record[key]] = record
    return latest


def batches(items: list, size: int = 300):
    for start in range(0, len(items), size):
        yield items[start:start + size]
