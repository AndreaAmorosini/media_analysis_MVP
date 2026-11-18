from dataclasses import dataclass
from pathlib import Path

import hashlib
import json


def short_hash(value: str) -> str:
    return hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()[:24]


@dataclass(frozen=True)
class Article:
    id: str
    rel_path: str
    title: str
    subtitle: str
    publication_date: str | None
    source_name: str
    source_type: str
    body: str
    content_hash: str


def read_article(path: Path, data_dir: Path) -> Article | None:

    rel_path = path.relative_to(data_dir).as_posix()

    data = json.loads(path.read_text(encoding="utf-8"))

    if not isinstance(data, dict):
        raise ValueError(f"JSON non è un oggetto articolo: {rel_path}")

    raw_body = data.get("body") or ""

    if (isinstance(raw_body, list) and all(isinstance(x, str) for x in raw_body)):
        raw_body = "\n".join(raw_body)

    if not isinstance(raw_body, str):
        raise ValueError(f"Body non valido: {rel_path}")

    if not raw_body.strip():
        return None


    date = (data.get("publication_date") or data.get("date") or "")
    title = str(data.get("title") or "")
    subtitle = str(data.get("subtitle") or "")
    source_name = str(data.get("source") or "undefined")
    source_type = str("unknown")
    content_hash = hashlib.sha256(json.dumps([title, subtitle, str(date), source_name, raw_body], ensure_ascii=False).encode("utf-8")).hexdigest()

    return Article(
        id="article-" + short_hash(rel_path),
        rel_path=rel_path,
        title=title,
        subtitle=subtitle,
        publication_date=str(date)[:10] or None,
        source_name=source_name,
        source_type=source_type,
        body=raw_body,
        content_hash=content_hash,
    )


def iter_articles(data_dir: Path):
    if not data_dir.is_dir():
        raise FileNotFoundError(data_dir)

    for path in sorted(data_dir.rglob("*.json")):
        yield path, read_article(path, data_dir)