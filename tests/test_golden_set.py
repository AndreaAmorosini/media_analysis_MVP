"""Test offline per scripts/build_golden_set.py: parsing NAF, GT schema, manifest.

Nessun test qui usa rete o PyMuPDF: la sintesi PDF e' coperta solo via
write_case_gt/validate (livelli puri). La CLI gira con pixi.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_golden_set as bgs  # noqa: E402

NAF_SAMPLE = """<NAF xml:lang="it" version="v3">
<raw>Il Governo approva il decreto legge.</raw>
<text>
<wf id="w1" sent="1" offset="0" length="2">Il</wf>
<wf id="w2" sent="1" offset="3" length="7">Governo</wf>
<wf id="w3" sent="1" offset="11" length="7">approva</wf>
<wf id="w4" sent="1" offset="19" length="2">il</wf>
<wf id="w5" sent="1" offset="22" length="7">decreto</wf>
<wf id="w6" sent="1" offset="30" length="6">legge.</wf>
</text>
<entities>
<entity id="e1" type="ORG">
<mentions><mention id="m1"><span><target id="w2"/></span></mention></mentions>
<externalReferences><externalRef resource="Wikidata" reference="http://www.wikidata.org/entity/Q123"/></externalReferences>
</entity>
</entities>
<events>
<event id="ev1" type="Decision">
<mentions><mention id="m2"><span><target id="w3"/></span></mention></mentions>
<externalReferences><externalRef resource="DBpedia" reference="http://dbpedia.org/resource/Approve"/></externalReferences>
</event>
</events>
</NAF>
"""


class TestParseNaf:
    def test_entity_mention_span(self):
        parsed = bgs.parse_naf(NAF_SAMPLE)
        assert parsed["raw"].startswith("Il Governo")
        ent = parsed["entities"][0]
        assert ent["id"] == "e1" and ent["type"] == "ORG"
        assert ent["mentions"][0]["text"] == "Governo"
        assert ent["mentions"][0]["start"] == 3
        assert ent["mentions"][0]["end"] == 10
        assert ent["refs"][0]["reference"].endswith("Q123")

    def test_event_parsed_with_refs(self):
        parsed = bgs.parse_naf(NAF_SAMPLE)
        ev = parsed["events"][0]
        assert ev["type"] == "Decision"
        assert ev["mentions"][0]["text"] == "approva"
        assert ev["refs"][0]["resource"] == "DBpedia"


class TestTextHelpers:
    def test_normalize_text(self):
        assert bgs.normalize_text("  Il  Governo\u00a0 approva ") == "il governo approva"

    def test_token_f1(self):
        assert bgs.token_f1("a b c", "a b c") == 1.0
        assert bgs.token_f1("a b", "c d") == 0.0
        score = bgs.token_f1("a b c", "a b")
        assert 0.6 < score < 1.0

    def test_slugify(self):
        assert bgs.slugify("Il Governo approva il DL!") == "il-governo-approva-il-dl"
        assert bgs.slugify("!!!") == "articolo"


class TestGazetteer:
    def test_from_bindings(self):
        bindings = [
            {"item": {"value": "http://www.wikidata.org/entity/Q100"},
             "itemLabel": {"value": "La Repubblica"},
             "alias": {"value": "la Repubblica"}},
            {"item": {"value": "http://www.wikidata.org/entity/Q100"},
             "itemLabel": {"value": "La Repubblica"},
             "alias": {"value": "Repubblica"}},
            {"item": {"value": "http://www.wikidata.org/entity/Q200"},
             "itemLabel": {"value": "Il Corriere della Sera"}},
        ]
        gaz = bgs.gazetteer_from_bindings(bindings)
        assert gaz["metadata"]["licenza"] == "CC0-1.0"
        by_qid = {t["qid"]: t for t in gaz["testate"]}
        assert by_qid["Q100"]["nome"] == "La Repubblica"
        assert "Repubblica" in by_qid["Q100"]["aliases"]
        assert by_qid["Q200"]["aliases"] == []


def _sample_articles(case_id: str) -> list[dict]:
    return [
        {
            "article_id": f"{case_id}_a1", "title": "Titolo uno", "subtitle": "Sottotitolo",
            "authors": ["Mario Rossi"], "publication_date": "2024-05-01",
            "source": "ilpost", "url": "https://example.it/1", "pages": [2], "body": "Corpo uno.",
        },
        {
            "article_id": f"{case_id}_a2", "title": "Titolo due", "subtitle": None,
            "authors": [], "publication_date": None,
            "source": "ilpost", "url": None, "pages": [3, 4], "body": "Corpo due.",
        },
    ]


def _sample_regions() -> list[dict]:
    return [
        {"page": 1, "bbox": [42, 42, 553, 66], "role": "header", "text": "RASSEGNA STAMPA"},
        {"page": 1, "bbox": [42, 120, 553, 134], "role": "index_row", "text": "a1. ilpost - Titolo uno - pag. 2"},
        {"page": 2, "bbox": [42, 76, 553, 110], "role": "title", "text": "Titolo uno"},
        {"page": 2, "bbox": [42, 300, 553, 700], "role": "body", "text": "Corpo uno."},
        {"page": 2, "bbox": [42, 400, 553, 420], "role": "watermark", "text": "DS1667 - RIPRODUZIONE RISERVATA"},
    ]


class TestCaseRoundtrip:
    def test_write_and_validate(self, tmp_path: Path):
        case_dir = tmp_path / "cases" / "c1"
        entry = bgs.write_case_gt(
            case_dir, "c1", _sample_articles("c1"), _sample_regions(), "ilpost",
            page_count=4, pdf_name="input.pdf", ocr_pages=[2],
            entities_gt=[{"entity_id": "e1", "type": "ORG",
                          "mentions": [{"text": "Governo", "start": 3, "end": 10}],
                          "refs": [{"resource": "Wikidata", "reference": ".../Q123"}]}],
            events_gt=[{"event_id": "ev1", "type": "Decision",
                        "mentions": [{"text": "approva", "start": 11, "end": 18}], "refs": []}],
        )
        assert entry["expected"] == {"pages": 4, "articles": 2}
        assert entry["gt"]["entities"].endswith("entities.json")
        assert bgs.validate_case_dir(case_dir) == []

        articles = json.loads((case_dir / "gt" / "articles.json").read_text(encoding="utf-8"))
        assert articles["schema_version"] == bgs.SCHEMA_VERSION
        assert len(articles["articles"]) == 2

        csv_text = (case_dir / "audit_template.csv").read_text(encoding="utf-8-sig")
        header = csv_text.splitlines()[0]
        for field in ("title_correct", "body_complete", "reading_order_correct"):
            assert field in header
        assert "article_id" in header

    def test_validate_detects_bad_role(self, tmp_path: Path):
        regions = _sample_regions()
        regions[0]["role"] = "not-a-role"
        errors = bgs.validate_regions_gt(regions)
        assert errors and "not-a-role" in errors[0]

    def test_validate_detects_missing_keys(self):
        errors = bgs.validate_articles_gt([{"article_id": "x"}])
        assert errors and "title" in errors[0]

    def test_validate_golden_root(self, tmp_path: Path):
        root = tmp_path
        case_dir = root / "cases" / "c1"
        bgs.write_case_gt(case_dir, "c1", _sample_articles("c1"), _sample_regions(),
                          "ilpost", 4, "input.pdf", [])
        (root / "cases" / "c1" / "input.pdf").write_bytes(b"%PDF-1.4 demo")
        manifest = {"schema_version": 1, "cases": [{
            "id": "c1", "source": "ilpost", "pdf": "cases/c1/input.pdf",
            "ocr_pages": [], "expected": {"pages": 4, "articles": 2},
            "gt": {"articles": "cases/c1/gt/articles.json", "regions": "cases/c1/gt/regions.json"},
        }]}
        bgs.write_json(root / "manifest.json", manifest)
        assert bgs.validate_golden_root(root) == []
        (root / "cases" / "c1" / "input.pdf").unlink()
        errors = bgs.validate_golden_root(root)
        assert any("missing pdf" in e for e in errors)


class TestAuditColumns:
    def test_audit_fields_present(self):
        article_columns, audit_fields = bgs.audit_columns()
        assert set(audit_fields) == set(bgs.AUDIT_FIELDS_FALLBACK)
        assert "article_id" in article_columns and "title" in article_columns


class TestMeantimeGt:
    def test_meantime_gt_shapes(self):
        items = [{
            "file": "topic_a.naf.xml",
            "raw": "Testo notícia.",
            "entities": [{"id": "e1", "type": "PER", "mentions": [{"text": "Mario", "start": 0, "end": 5}],
                          "refs": [{"resource": "Wikidata", "reference": "http://www.wikidata.org/entity/Q9"}]}],
            "events": [{"id": "ev1", "type": "Meeting", "mentions": [{"text": "incontro", "start": 6, "end": 14}], "refs": []}],
        }]
        articles, entities, events = bgs._meantime_gt(items, "meantime_it")
        assert articles[0]["article_id"] == "meantime_it_a1"
        assert articles[0]["body"] == "Testo notícia."
        assert entities[0]["entity_id"] == "meantime_it_e1"
        assert entities[0]["article_id"] == "meantime_it_a1"
        assert events[0]["type"] == "Meeting"
        assert bgs.validate_entities_gt(entities) == []
        assert bgs.validate_articles_gt(articles) == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
