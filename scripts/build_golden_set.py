#!/usr/bin/env python3
"""Build a public/open golden set for press_reputation evaluation.

Three layers per case under the output root:

1. Extraction/tagging: synthetic "rassegna stampa" PDFs (index page, clippings,
   watermark/register code, optional rasterized pages to force the OCR path)
   built from openly licensed Italian news articles, with per-region ground
   truth (gt/regions.json: page, bbox, role, text) and per-article ground truth
   (gt/articles.json), plus an audit_template.csv compatible with
   scripts/evaluate_audit.py.
2. Knowledge graph: entity/event annotations with canonical references
   (gt/entities.json, gt/events.json) from the CC-BY NewsReader MEANTIME Italian
   corpus, and a CC0 gazetteer built from Wikidata (entities/gazetteer.json).
3. Manifest: manifest.json listing cases, sources, licenses and expectations.

Sources (public/open; details in docs/golden_set.md):
  meantime  NewsReader MEANTIME / WItaC Italian NAF  CC BY
  ilpost    ARTeLab/ilpost on Hugging Face           CC BY 4.0
  fanpage   ARTeLab/fanpage on Hugging Face          CC BY 4.0
  wikidata  Wikidata SPARQL / wbsearchentities       CC0 1.0
  scans     Internet Archive italian newspapers      public domain (optional)
  gdelt     GDELT 2.0 daily events                   open, citation required (optional)

Usage:
    PYTHONPATH=src pixi run python scripts/build_golden_set.py \
        --sources meantime wikidata ilpost --limit 60 --out golden_set
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import random
import re
import unicodedata
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

GOLDEN_ROLES = (
    "header", "index_row", "title", "subtitle", "byline", "body",
    "continuation", "watermark", "rights_notice",
)

ROLE_TO_REGION_TYPE = {
    "header": "header_metadata",
    "index_row": None,
    "title": "article_title",
    "subtitle": "article_subtitle",
    "byline": "author",
    "body": "article_body",
    "continuation": "article_body",
    "watermark": "watermark",
    "rights_notice": "rights_notice",
}

ARTICLE_COLUMNS_FALLBACK = [
    "year", "month", "day_path", "pdf", "article_id", "article_file", "title", "publication_date",
    "subtitle_present", "body_chars", "body_source", "finalization_status",
    "completeness_status", "flags", "pdf_pages",
]
AUDIT_FIELDS_FALLBACK = (
    "title_correct", "subtitle_correct", "date_correct",
    "body_complete", "reading_order_correct", "no_unrelated_text",
)

SOURCE_INFO = {
    "meantime": {
        "license": "CC BY",
        "url": "http://www.newsreader-project.eu/results/data/wikinews/",
        "note": "NewsReader MEANTIME corpus, Italian section (WItaC), NAF annotations",
    },
    "ilpost": {
        "license": "CC BY 4.0",
        "url": "https://huggingface.co/datasets/ARTeLab/ilpost",
        "note": "Il Post articles via Hugging Face datasets-server",
    },
    "fanpage": {
        "license": "CC BY 4.0",
        "url": "https://huggingface.co/datasets/ARTeLab/fanpage",
        "note": "Fanpage.it articles via Hugging Face datasets-server",
    },
    "wikidata": {
        "license": "CC0 1.0",
        "url": "https://www.wikidata.org/",
        "note": "SPARQL gazetteer of Italian newspapers + wbsearchentities",
    },
    "scans": {
        "license": "public domain items",
        "url": "https://archive.org/details/italian-newspapers",
        "note": "Internet Archive Italian newspapers, djvu.xml as OCR/layout GT",
    },
    "gdelt": {
        "license": "open, citation required",
        "url": "https://gdeltproject.org/",
        "note": "GDELT 2.0 events for Italy (ActionGeo_CountryCode=IT)",
    },
}

WDQS_URL = "https://query.wikidata.org/sparql"
WB_API_URL = "https://www.wikidata.org/w/api.php"
DS_SERVER = "https://datasets-server.huggingface.co"
IA_SEARCH_URL = "https://archive.org/advancedsearch.php"
MEANTIME_IT_ZIP = "https://www.newsreader-project.eu/results/data/wikinews/meantime_newsreader_italian.zip"
USER_AGENT = "press-reputation-golden-set/1.0 (https://github.com/AndreaAmorosini/media_analysis_MVP)"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    temp.replace(path)


def write_csv(path: Path, header: list[str], rows: list[list[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(header)
        writer.writerows(rows)


def audit_columns() -> tuple[list[str], list[str]]:
    try:
        from batch_parse import ARTICLE_COLUMNS, AUDIT_FIELDS
        return list(ARTICLE_COLUMNS), list(AUDIT_FIELDS)
    except Exception:
        return list(ARTICLE_COLUMNS_FALLBACK), list(AUDIT_FIELDS_FALLBACK)


def slugify(text: str, maxlen: int = 48) -> str:
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return (text[:maxlen].strip("-") or "articolo")


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    value = unicodedata.normalize("NFKC", str(value)).casefold()
    return " ".join(value.split())


def token_f1(a: str, b: str) -> float:
    ta, tb = normalize_text(a).split(), normalize_text(b).split()
    if not ta or not tb:
        return 1.0 if not ta and not tb else 0.0
    from collections import Counter
    ca, cb = Counter(ta), Counter(tb)
    common = sum((ca & cb).values())
    if common == 0:
        return 0.0
    precision = common / len(ta)
    recall = common / len(tb)
    return 2 * precision * recall / (precision + recall)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_naf(xml_text: str) -> dict:
    root = ET.fromstring(xml_text)
    raw_el = next((e for e in root.iter() if _local(e.tag) == "raw"), None)
    raw = (raw_el.text or "") if raw_el is not None else ""
    words: dict[str, tuple[int, int, str]] = {}
    for wf in (e for e in root.iter() if _local(e.tag) == "wf"):
        wid = wf.get("id")
        if wid:
            words[wid] = (int(wf.get("offset", "0")), int(wf.get("length", "0")), wf.text or "")

    def mention_text(target_ids: list[str]) -> tuple[str, int | None, int | None]:
        spans = [words[t] for t in target_ids if t in words]
        if not spans:
            return "", None, None
        start = min(s[0] for s in spans)
        end = max(s[0] + s[1] for s in spans)
        text = raw[start:end].strip() if raw else " ".join(s[2] for s in sorted(spans))
        return text, start, end

    def parse_items(container: str) -> list[dict]:
        items = []
        for el in root.iter():
            if _local(el.tag) != container:
                continue
            mentions = []
            for m in (c for c in el.iter() if _local(c.tag) == "mention"):
                targets = [t.get("id") for t in (c2 for c2 in m.iter() if _local(c2.tag) == "target")]
                text, start, end = mention_text(targets)
                if text:
                    mentions.append({"text": text, "start": start, "end": end})
            refs = [
                {"resource": r.get("resource"), "reference": r.get("reference")}
                for r in (c for c in el.iter() if _local(c.tag) == "externalRef")
            ]
            items.append({
                "id": el.get("id"),
                "type": el.get("type") or el.get("pred") or el.get("lemma"),
                "mentions": mentions,
                "refs": refs,
            })
        return items

    return {"raw": raw, "entities": parse_items("entity"), "events": parse_items("event")}


def _http_get_json(url: str, params: dict | None = None) -> Any:
    import httpx
    with httpx.Client(timeout=90, follow_redirects=True, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}) as client:
        resp = client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()


def _http_get_bytes(url: str) -> bytes:
    import httpx
    with httpx.Client(timeout=300, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.content


def sparql(query: str) -> list[dict]:
    data = _http_get_json(WDQS_URL, {"query": query, "format": "json"})
    return data["results"]["bindings"]


def italian_newspapers_query(limit: int = 500) -> str:
    return f"""
SELECT ?item ?itemLabel ?alias WHERE {{
  ?item wdt:P31/wdt:P279* wd:Q11032 .
  ?item wdt:P17 wd:Q38 .
  OPTIONAL {{ ?item skos:altLabel ?alias . FILTER(LANG(?alias) IN ("it", "en")) }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "it,en". }}
}}
LIMIT {int(limit)}
"""


def gazetteer_from_bindings(bindings: list[dict]) -> dict:
    testate: dict[str, dict] = {}
    for b in bindings:
        qid = b["item"]["value"].rsplit("/", 1)[-1]
        label = b.get("itemLabel", {}).get("value")
        alias = b.get("alias", {}).get("value")
        entry = testate.setdefault(qid, {
            "nome": label, "aliases": [], "qid": qid,
            "tipo": "testata", "categoria": None, "fonte_dataset": ["wikidata"],
        })
        if label and not entry["nome"]:
            entry["nome"] = label
        if alias and alias != entry["nome"] and alias not in entry["aliases"]:
            entry["aliases"].append(alias)
    entries = sorted(testate.values(), key=lambda e: normalize_text(e["nome"]))
    return {
        "metadata": {
            "licenza": "CC0-1.0",
            "fonte": "Wikidata SPARQL (query.wikidata.org)",
            "generato_da": "scripts/build_golden_set.py",
            "generato_at": utc_now(),
        },
        "testate": entries,
    }


def search_entities(label: str, limit: int = 5, lang: str = "it") -> list[dict]:
    data = _http_get_json(WB_API_URL, {
        "action": "wbsearchentities", "search": label, "language": lang,
        "format": "json", "limit": str(limit), "type": "item",
    })
    return data.get("search", [])


def _pick(row: dict, *names: str) -> Any:
    for name in names:
        if row.get(name):
            return row[name]
    return None


def normalize_hf_article(row: dict, source: str) -> dict:
    return {
        "title": _pick(row, "title", "headline", "titolo"),
        "text": _pick(row, "text", "article", "body", "content", "testo"),
        "summary": _pick(row, "summary", "sommario"),
        "date": _pick(row, "date", "published", "data"),
        "url": _pick(row, "url", "link"),
        "author": _pick(row, "author", "autore"),
        "source": source,
    }


def fetch_hf_dataset(dataset: str, limit: int) -> list[dict]:
    splits = _http_get_json(f"{DS_SERVER}/splits", {"dataset": dataset}).get("splits", [])
    if not splits:
        return []
    config, split = splits[0]["config"], splits[0]["split"]
    articles = []
    offset = 0
    while len(articles) < limit:
        batch = _http_get_json(f"{DS_SERVER}/rows", {
            "dataset": dataset, "config": config, "split": split,
            "offset": str(offset), "length": "100",
        }).get("rows", [])
        if not batch:
            break
        for item in batch:
            row = item.get("row", {})
            article = normalize_hf_article(row, dataset)
            if article["title"] and article["text"]:
                articles.append(article)
        offset += len(batch)
        if len(batch) < 100:
            break
    return articles[:limit]


def fetch_meantime(limit: int) -> list[dict]:
    blob = _http_get_bytes(MEANTIME_IT_ZIP)
    archive = zipfile.ZipFile(io.BytesIO(blob))
    items = []
    for name in archive.namelist():
        if not name.lower().endswith((".naf", ".xml")):
            continue
        parsed = parse_naf(archive.read(name).decode("utf-8", "replace"))
        if parsed["raw"] or parsed["entities"] or parsed["events"]:
            items.append({
                "file": name,
                "raw": parsed["raw"],
                "entities": parsed["entities"],
                "events": parsed["events"],
            })
        if len(items) >= limit:
            break
    return items


def fetch_ia_newspaper_gt(identifier: str) -> dict | None:
    try:
        blob = _http_get_bytes(f"https://archive.org/download/{identifier}/{identifier}_djvu.xml")
    except Exception:
        return None
    words = []
    for el in ET.fromstring(blob).iter():
        if _local(el.tag) == "WORD" and el.text:
            coords = (el.get("coords") or "").split(",")
            if len(coords) == 4:
                words.append({
                    "text": el.text,
                    "bbox": [float(c) for c in coords],
                })
    return {
        "identifier": identifier,
        "pdf_url": f"https://archive.org/download/{identifier}/{identifier}.pdf",
        "djvu_words": words,
    }


def ia_italian_newspaper_ids(limit: int) -> list[str]:
    data = _http_get_json(IA_SEARCH_URL, {
        "q": "collection:italian-newspapers AND mediatype:texts",
        "fl[]": "identifier", "rows": str(limit), "page": "1", "output": "json",
    })
    return [doc["identifier"] for doc in data["response"]["docs"] if doc.get("identifier")]


def fetch_gdelt_events_it(date_str: str) -> list[dict]:
    blob = _http_get_bytes(f"https://data.gdeltproject.org/events/{date_str}.export.CSV.zip")
    archive = zipfile.ZipFile(io.BytesIO(blob))
    name = next(n for n in archive.namelist() if n.endswith(".CSV"))
    raw = archive.read(name).decode("utf-8", "replace").splitlines()
    reader = csv.DictReader(raw, delimiter="\t")
    events = []
    for row in reader:
        if row.get("ActionGeo_CountryCode") != "IT":
            continue
        events.append({
            "event_id": row.get("GlobalEventID"),
            "date": row.get("SQLDATE"),
            "actor1": row.get("Actor1Name"),
            "actor2": row.get("Actor2Name"),
            "event_code": row.get("EventCode"),
            "goldstein": row.get("GoldsteinScale"),
            "lat": row.get("ActionGeo_Lat"),
            "lon": row.get("ActionGeo_Long"),
            "source_url": row.get("SOURCEURL"),
        })
    return events


def _wrap_lines(text: str, fontname: str, fontsize: float, width: float) -> list[str]:
    import fitz
    lines: list[str] = []
    for para in str(text or "").split("\n"):
        current = ""
        for word in para.split(" "):
            trial = (current + " " + word).strip()
            if not current or fitz.get_text_length(trial, fontname=fontname, fontsize=fontsize) <= width:
                current = trial
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def render_rassegna_pdf(
    articles: list[dict],
    out_path: Path,
    case_id: str,
    ocr_pages: set[int] | None = None,
    watermark_code: str = "DS1667",
) -> tuple[int, list[dict], list[dict]]:
    import fitz

    ocr_pages = ocr_pages or set()
    margin = 42.0
    width, height = fitz.paper_rect("a4").width, fitz.paper_rect("a4").height
    body_font, body_size, body_lh = "helv", 9.5, 1.3
    col_gap = 18.0
    col_width = (width - 2 * margin - col_gap) / 2

    def add_watermark(page: Any, page_no: int, regions: list[dict]) -> None:
        text = f"{watermark_code} - RIPRODUZIONE RISERVATA"
        page.insert_text(
            (margin, height / 2), text,
            fontsize=22, fontname="hebo", fill=(0.8, 0.8, 0.8), fill_opacity=0.12,
        )
        regions.append({
            "page": page_no, "bbox": [margin, height / 2 - 22, width - margin, height / 2 + 6],
            "role": "watermark", "text": text,
        })

    def add_rights(page: Any, page_no: int, regions: list[dict]) -> None:
        text = "Riproduzione riservata - tutti i diritti riservati"
        page.insert_text((margin, height - margin / 2), text, fontsize=7, fontname="helv", fill=(0.4, 0.4, 0.4))
        regions.append({
            "page": page_no, "bbox": [margin, height - margin / 2 - 7, width - margin, height - margin / 2 + 2],
            "role": "rights_notice", "text": text,
        })

    def draw_lines(page: Any, lines: list[str], x: float, y: float, fontsize: float, fontname: str, lh: float) -> float:
        for line in lines:
            page.insert_text((x, y), line, fontsize=fontsize, fontname=fontname)
            y += fontsize * lh
        return y

    articles_doc = fitz.open()
    regions: list[dict] = []
    articles_gt: list[dict] = []

    for index, art in enumerate(articles):
        article_id = f"{case_id}_a{index + 1}"
        start_zero = len(articles_doc)
        title = str(art.get("title") or "Senza titolo")
        subtitle = art.get("subtitle") or art.get("summary")
        author = art.get("author")
        source = str(art.get("source") or "Testata sconosciuta")
        date_str = str(art.get("date") or "")
        body = str(art.get("text") or "")

        page = articles_doc.new_page()
        page_no = len(articles_doc)
        add_watermark(page, page_no, regions)
        header = f"{source}  -  {date_str}".strip(" -")
        page.insert_textbox(
            fitz.Rect(margin, margin, width - margin, margin + 16), header,
            fontsize=9, fontname="hebo",
        )
        regions.append({"page": page_no, "bbox": [margin, margin, width - margin, margin + 16], "role": "header", "text": header})
        y = margin + 34
        title_lines = _wrap_lines(title, "hebo", 15, width - 2 * margin)[:3]
        y_end = draw_lines(page, title_lines, margin, y, 15, "hebo", 1.15)
        regions.append({"page": page_no, "bbox": [margin, y - 15, width - margin, y_end], "role": "title", "text": "\n".join(title_lines)})
        y = y_end + 6
        if subtitle:
            sub_lines = _wrap_lines(str(subtitle), "heit", 10.5, width - 2 * margin)[:2]
            y_end = draw_lines(page, sub_lines, margin, y, 10.5, "heit", 1.2)
            regions.append({"page": page_no, "bbox": [margin, y - 10.5, width - margin, y_end], "role": "subtitle", "text": "\n".join(sub_lines)})
            y = y_end + 6
        if author:
            byline = f"di {author}"
            page.insert_text((margin, y), byline, fontsize=9, fontname="helv")
            regions.append({"page": page_no, "bbox": [margin, y - 9, width - margin, y + 2], "role": "byline", "text": byline})
            y += 16

        body_lines = _wrap_lines(body, body_font, body_size, col_width)
        line_h = body_size * body_lh
        top = y + 4
        capacity = max(1, int((height - margin - top) // line_h))
        pos = 0
        page_span = [page_no]
        while pos < len(body_lines):
            if pos > 0:
                page = articles_doc.new_page()
                page_no = len(articles_doc)
                add_watermark(page, page_no, regions)
                cont_header = f"(continua) {source}  -  {date_str}".strip(" -")
                page.insert_textbox(
                    fitz.Rect(margin, margin, width - margin, margin + 16), cont_header,
                    fontsize=9, fontname="hebo",
                )
                regions.append({"page": page_no, "bbox": [margin, margin, width - margin, margin + 16], "role": "header", "text": cont_header})
                page_span.append(page_no)
                top = margin + 28
                capacity = max(1, int((height - margin - top) // line_h))
                footer = f"continua a pagina {page_no + 1}"
                prev = articles_doc[page_no - 2]
                prev.insert_text((margin, height - margin / 2 - 12), footer, fontsize=8, fontname="heit")
                regions.append({
                    "page": page_no - 1, "bbox": [margin, height - margin / 2 - 20, width - margin, height - margin / 2 - 8],
                    "role": "continuation", "text": footer,
                })
            col1 = body_lines[pos:pos + capacity]
            pos += capacity
            col2 = body_lines[pos:pos + capacity] if pos < len(body_lines) else []
            if col2:
                pos += capacity
            if col1:
                y_end = draw_lines(page, col1, margin, top + body_size, body_size, body_font, body_lh)
                regions.append({"page": page_no, "bbox": [margin, top, margin + col_width, y_end], "role": "body", "text": "\n".join(col1)})
            if col2:
                y_end = draw_lines(page, col2, margin + col_width + col_gap, top + body_size, body_size, body_font, body_lh)
                regions.append({
                    "page": page_no, "bbox": [margin + col_width + col_gap, top, width - margin, y_end],
                    "role": "body", "text": "\n".join(col2),
                })
        add_rights(articles_doc[-1], len(articles_doc), regions)
        articles_gt.append({
            "article_id": article_id,
            "title": title,
            "subtitle": subtitle,
            "authors": [author] if author else [],
            "publication_date": date_str or None,
            "source": source,
            "url": art.get("url"),
            "pages": [p + 1 for p in page_span],
            "body": body,
        })

    index_doc = fitz.open()
    index_page = index_doc.new_page()
    index_no = 1
    add_watermark(index_page, index_no, regions)
    index_page.insert_textbox(
        fitz.Rect(margin, margin, width - margin, margin + 24), "RASSEGNA STAMPA",
        fontsize=18, fontname="hebo",
    )
    regions.append({"page": index_no, "bbox": [margin, margin, width - margin, margin + 24], "role": "header", "text": "RASSEGNA STAMPA"})
    y = margin + 44
    index_page.insert_text((margin, y), "INDICE", fontsize=11, fontname="hebo")
    regions.append({"page": index_no, "bbox": [margin, y - 11, width - margin, y + 2], "role": "header", "text": "INDICE"})
    y += 20
    for art in articles_gt:
        row = f"{art['article_id'].split('_')[-1]}. {art['source']} - {art['title']} - pag. {art['pages'][0]}"
        index_page.insert_text((margin, y), row[:110], fontsize=9, fontname="helv")
        regions.append({"page": index_no, "bbox": [margin, y - 9, width - margin, y + 2], "role": "index_row", "text": row})
        y += 14
    add_rights(index_page, index_no, regions)

    final = fitz.open()
    final.insert_pdf(index_doc)
    final.insert_pdf(articles_doc)
    regions = [dict(r, page=r["page"] + 1) if r["page"] > 1 else r for r in regions]
    articles_gt = [dict(a, pages=[p + 1 for p in a["pages"]]) for a in articles_gt]

    if ocr_pages:
        raster = fitz.open()
        for pno in range(len(final)):
            if pno + 1 in ocr_pages:
                pix = final[pno].get_pixmap(dpi=200)
                page = raster.new_page(width=width, height=height)
                page.insert_image(fitz.Rect(0, 0, width, height), pixmap=pix)
            else:
                raster.insert_pdf(final, from_page=pno, to_page=pno)
        final = raster

    out_path.parent.mkdir(parents=True, exist_ok=True)
    final.save(str(out_path), deflate=True)
    return len(final), regions, articles_gt


def write_case_gt(
    case_dir: Path,
    case_id: str,
    articles_gt: list[dict],
    regions_gt: list[dict],
    source_name: str,
    page_count: int | None,
    pdf_name: str | None,
    ocr_pages: list[int],
    entities_gt: list[dict] | None = None,
    events_gt: list[dict] | None = None,
) -> dict:
    gt_dir = case_dir / "gt"
    gt_dir.mkdir(parents=True, exist_ok=True)
    write_json(gt_dir / "articles.json", {"schema_version": SCHEMA_VERSION, "case_id": case_id, "articles": articles_gt})
    write_json(gt_dir / "regions.json", {"schema_version": SCHEMA_VERSION, "case_id": case_id, "regions": regions_gt})
    if entities_gt is not None:
        write_json(gt_dir / "entities.json", {"schema_version": SCHEMA_VERSION, "case_id": case_id, "entities": entities_gt})
    if events_gt is not None:
        write_json(gt_dir / "events.json", {"schema_version": SCHEMA_VERSION, "case_id": case_id, "events": events_gt})

    article_columns, audit_fields = audit_columns()
    rows = []
    for art in articles_gt:
        date_str = art.get("publication_date") or ""
        year = date_str[:4] if date_str else ""
        month = date_str[5:7] if len(date_str) >= 7 else ""
        rows.append([
            year, month, "", pdf_name or "", art["article_id"], "",
            art.get("title") or "", date_str,
            "1" if art.get("subtitle") else "0", len(art.get("body") or ""),
            "", "", "", "", ",".join(str(p) for p in art.get("pages", [])),
            *([""] * len(audit_fields)),
        ])
    write_csv(case_dir / "audit_template.csv", article_columns + audit_fields, rows)

    return {
        "id": case_id,
        "source": source_name,
        "pdf": f"cases/{case_id}/{pdf_name}" if pdf_name else None,
        "ocr_pages": sorted(ocr_pages),
        "expected": {"pages": page_count, "articles": len(articles_gt)},
        "gt": {
            "articles": f"cases/{case_id}/gt/articles.json",
            "regions": f"cases/{case_id}/gt/regions.json",
            "entities": f"cases/{case_id}/gt/entities.json" if entities_gt is not None else None,
            "events": f"cases/{case_id}/gt/events.json" if events_gt is not None else None,
        },
    }


def validate_articles_gt(articles: list[dict]) -> list[str]:
    errors = []
    required = {"article_id", "title", "body", "pages", "source"}
    for i, art in enumerate(articles):
        missing = required - set(art)
        if missing:
            errors.append(f"articles[{i}] missing keys: {sorted(missing)}")
        if art.get("pages") is not None and not all(isinstance(p, int) and p >= 1 for p in art["pages"]):
            errors.append(f"articles[{i}] pages must be positive ints")
    return errors


def validate_regions_gt(regions: list[dict]) -> list[str]:
    errors = []
    for i, region in enumerate(regions):
        if region.get("role") not in GOLDEN_ROLES:
            errors.append(f"regions[{i}] invalid role: {region.get('role')!r}")
        bbox = region.get("bbox")
        if not (isinstance(bbox, list) and len(bbox) == 4):
            errors.append(f"regions[{i}] bbox must be [x0,y0,x1,y1]")
        if not (isinstance(region.get("page"), int) and region["page"] >= 1):
            errors.append(f"regions[{i}] page must be a positive int")
    return errors


def validate_entities_gt(entities: list[dict]) -> list[str]:
    errors = []
    for i, ent in enumerate(entities):
        if not ent.get("mentions"):
            errors.append(f"entities[{i}] has no mentions")
        for j, m in enumerate(ent.get("mentions", [])):
            if not m.get("text"):
                errors.append(f"entities[{i}].mentions[{j}] empty text")
    return errors


def validate_case_dir(case_dir: Path) -> list[str]:
    errors: list[str] = []
    gt_dir = case_dir / "gt"
    articles_path = gt_dir / "articles.json"
    regions_path = gt_dir / "regions.json"
    if not articles_path.exists():
        return [f"{case_dir}: missing gt/articles.json"]
    if not regions_path.exists():
        errors.append(f"{case_dir}: missing gt/regions.json")
    try:
        articles = json.loads(articles_path.read_text(encoding="utf-8"))["articles"]
        errors.extend(validate_articles_gt(articles))
    except Exception as exc:
        errors.append(f"{case_dir}: articles.json unreadable: {exc}")
    if regions_path.exists():
        try:
            regions = json.loads(regions_path.read_text(encoding="utf-8"))["regions"]
            errors.extend(validate_regions_gt(regions))
        except Exception as exc:
            errors.append(f"{case_dir}: regions.json unreadable: {exc}")
    entities_path = gt_dir / "entities.json"
    if entities_path.exists():
        try:
            entities = json.loads(entities_path.read_text(encoding="utf-8"))["entities"]
            errors.extend(validate_entities_gt(entities))
        except Exception as exc:
            errors.append(f"{case_dir}: entities.json unreadable: {exc}")
    if not (case_dir / "audit_template.csv").exists():
        errors.append(f"{case_dir}: missing audit_template.csv")
    return errors


def validate_golden_root(root: Path) -> list[str]:
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        return [f"{root}: missing manifest.json"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    for case in manifest.get("cases", []):
        case_dir = root / "cases" / case["id"]
        errors.extend(validate_case_dir(case_dir))
        if case.get("pdf") and not (root / case["pdf"]).exists():
            errors.append(f"{case['id']}: missing pdf {case['pdf']}")
    return errors


def _meantime_gt(items: list[dict], case_id: str) -> tuple[list[dict], list[dict], list[dict]]:
    articles, entities, events = [], [], []
    for i, item in enumerate(items):
        articles.append({
            "article_id": f"{case_id}_a{i + 1}",
            "title": item["file"].rsplit("/", 1)[-1].replace(".naf.xml", "").replace(".xml", "").replace("_", " "),
            "subtitle": None,
            "authors": [],
            "publication_date": None,
            "source": "Wikinotizie",
            "url": None,
            "pages": [],
            "body": item["raw"],
        })
        for ent in item["entities"]:
            entities.append({
                "entity_id": f"{case_id}_{ent['id']}",
                "type": ent.get("type"),
                "mentions": ent["mentions"],
                "refs": ent["refs"],
                "article_id": f"{case_id}_a{i + 1}",
            })
        for ev in item["events"]:
            events.append({
                "event_id": f"{case_id}_{ev['id']}",
                "type": ev.get("type"),
                "mentions": ev["mentions"],
                "refs": ev["refs"],
                "article_id": f"{case_id}_a{i + 1}",
            })
    return articles, entities, events


def build(
    out_root: Path,
    sources: list[str],
    limit: int,
    articles_per_pdf: int,
    ocr_every: int,
    seed: int,
    render: bool,
    gdelt_date: str | None,
) -> dict:
    rng = random.Random(seed)
    out_root.mkdir(parents=True, exist_ok=True)
    raw_dir = out_root / "raw"
    raw_dir.mkdir(exist_ok=True)
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "generator": "scripts/build_golden_set.py",
        "sources": {},
        "cases": [],
    }

    pool: list[dict] = []
    if "meantime" in sources:
        items = fetch_meantime(limit)
        manifest["sources"]["meantime"] = {**SOURCE_INFO["meantime"], "retrieved_at": utc_now(), "items": len(items)}
        articles, entities, events = _meantime_gt(items, "meantime_it")
        case_dir = out_root / "cases" / "meantime_it"
        page_count, regions, articles = (None, [], articles)
        if render:
            page_count, regions, articles = render_rassegna_pdf(articles, case_dir / "input.pdf", "meantime_it")
        manifest["cases"].append(write_case_gt(
            case_dir, "meantime_it", articles, regions, "meantime", page_count,
            "input.pdf" if render else None, [], entities_gt=entities, events_gt=events,
        ))
        (raw_dir / "meantime_it.json").write_text(
            json.dumps(items, ensure_ascii=False, default=str), encoding="utf-8")

    for hf_name in ("ilpost", "fanpage"):
        if hf_name in sources:
            articles = fetch_hf_dataset(f"ARTeLab/{hf_name}", limit)
            manifest["sources"][hf_name] = {**SOURCE_INFO[hf_name], "retrieved_at": utc_now(), "articles": len(articles)}
            (raw_dir / f"{hf_name}.json").write_text(
                json.dumps(articles, ensure_ascii=False, default=str), encoding="utf-8")
            pool.extend(articles)

    if pool:
        rng.shuffle(pool)
        for chunk_no, start in enumerate(range(0, len(pool), articles_per_pdf)):
            chunk = pool[start:start + articles_per_pdf]
            case_id = f"rassegna_{chunk_no + 1:03d}"
            case_dir = out_root / "cases" / case_id
            page_count, regions, articles_gt = (None, [], [])
            ocr_pages: list[int] = []
            if render:
                page_count, regions, articles_gt = render_rassegna_pdf(chunk, case_dir / "input.pdf", case_id)
                if ocr_every and page_count:
                    ocr_pages = list(range(1, page_count + 1, ocr_every))
                    if ocr_pages:
                        page_count, regions, articles_gt = render_rassegna_pdf(
                            chunk, case_dir / "input.pdf", case_id, ocr_pages=set(ocr_pages))
            else:
                articles_gt = [
                    {
                        "article_id": f"{case_id}_a{i + 1}", "title": a.get("title"),
                        "subtitle": a.get("summary"), "authors": [a["author"]] if a.get("author") else [],
                        "publication_date": a.get("date"), "source": a.get("source"), "url": a.get("url"),
                        "pages": [], "body": a.get("text") or "",
                    }
                    for i, a in enumerate(chunk)
                ]
            manifest["cases"].append(write_case_gt(
                case_dir, case_id, articles_gt, regions, "ilpost+fanpage", page_count,
                "input.pdf" if render else None, ocr_pages,
            ))

    if "wikidata" in sources:
        gazetteer = gazetteer_from_bindings(sparql(italian_newspapers_query(limit)))
        entities_dir = out_root / "entities"
        entities_dir.mkdir(exist_ok=True)
        write_json(entities_dir / "gazetteer.json", gazetteer)
        manifest["sources"]["wikidata"] = {
            **SOURCE_INFO["wikidata"], "retrieved_at": utc_now(),
            "gazetteer": "entities/gazetteer.json", "testate": len(gazetteer["testate"]),
        }

    if "scans" in sources:
        ids = ia_italian_newspaper_ids(min(limit, 20))
        scans_dir = out_root / "scans"
        scans_dir.mkdir(exist_ok=True)
        kept = []
        for ident in ids:
            gt = fetch_ia_newspaper_gt(ident)
            if gt:
                write_json(scans_dir / f"{slugify(ident, 60)}.json", gt)
                kept.append({"identifier": ident, "pdf_url": gt["pdf_url"], "gt": f"scans/{slugify(ident, 60)}.json"})
        manifest["sources"]["scans"] = {**SOURCE_INFO["scans"], "retrieved_at": utc_now(), "items": kept}

    if "gdelt" in sources and gdelt_date:
        events = fetch_gdelt_events_it(gdelt_date)
        write_json(out_root / f"events_gdelt_{gdelt_date}.json", {
            "schema_version": SCHEMA_VERSION, "date": gdelt_date, "country": "IT",
            "events": events,
        })
        manifest["sources"]["gdelt"] = {
            **SOURCE_INFO["gdelt"], "retrieved_at": utc_now(),
            "date": gdelt_date, "events_it": len(events),
            "file": f"events_gdelt_{gdelt_date}.json",
        }

    write_json(out_root / "manifest.json", manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("golden_set"), help="output root (default: golden_set)")
    parser.add_argument("--sources", nargs="+", default=["meantime", "wikidata", "ilpost"],
                        choices=sorted(SOURCE_INFO), help="sources to fetch")
    parser.add_argument("--limit", type=int, default=60, help="max items per source")
    parser.add_argument("--articles-per-pdf", type=int, default=6, help="articles per synthetic press review PDF")
    parser.add_argument("--ocr-every", type=int, default=0,
                        help="if >0, rasterize every Nth page to image-only to force the OCR path")
    parser.add_argument("--seed", type=int, default=13)
    parser.add_argument("--no-render", action="store_true",
                        help="write GT only, skip PDF synthesis (no PyMuPDF needed)")
    parser.add_argument("--gdelt-date", default=None, help="YYYYMMDD for the optional gdelt source")
    parser.add_argument("--validate-only", action="store_true", help="only validate an existing golden set root")
    args = parser.parse_args(argv)

    if args.validate_only:
        errors = validate_golden_root(args.out)
        for err in errors:
            print(f"ERROR {err}")
        print(f"{args.out}: {'OK' if not errors else f'{len(errors)} errors'}")
        return 0 if not errors else 1

    manifest = build(
        out_root=args.out,
        sources=args.sources,
        limit=args.limit,
        articles_per_pdf=args.articles_per_pdf,
        ocr_every=args.ocr_every,
        seed=args.seed,
        render=not args.no_render,
        gdelt_date=args.gdelt_date,
    )
    errors = validate_golden_root(args.out)
    for err in errors:
        print(f"ERROR {err}")
    print(f"golden set in {args.out}: {len(manifest['cases'])} casi, sorgenti: {sorted(manifest['sources'])}")
    for case in manifest["cases"]:
        print(f"  {case['id']}: articoli attesi={case['expected']['articles']} pagine={case['expected']['pages']} pdf={case['pdf']}")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
