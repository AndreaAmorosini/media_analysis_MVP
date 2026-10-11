import re
from pathlib import Path

import fitz

from press_reputation.config import WatermarkTextCleaningConfig
from press_reputation.models.page import PageRecord, Region, RegionType


# Match sulla stringa originale: gli indici devono rimanere validi
# rispetto a Region.text, non a una copia Unicode normalizzata.
WATERMARK_TEXT_PATTERN = re.compile(
    r"\bdata\s*stamp(?:a)?\s*(?P<number>0*\d{3,8})\b",
    flags=re.IGNORECASE,
)

AUDIENCE_MARKERS = ("tiratura", "diffusione", "lettori", "dir. resp")


class WatermarkTextCleaner:
    def __init__(self, config: WatermarkTextCleaningConfig | None = None) -> None:
        self.config = config or WatermarkTextCleaningConfig()

    @staticmethod
    def marker_number(match: re.Match) -> int:
        return int(match.group("number"))

    @staticmethod
    def protected_metadata(region: Region) -> bool:
        if region.type in {
            RegionType.HEADER_METADATA, RegionType.SOURCE_NAME,
            RegionType.PRESS_REVIEW_PROVIDER, RegionType.RIGHTS_NOTICE,
        }:
            return True

        text = (region.text or "").casefold()
        return any(marker in text for marker in AUDIENCE_MARKERS)

    @staticmethod
    def overlap_fraction(inner: list[float], outer: list[float]) -> float:
        x0, y0 = max(inner[0], outer[0]), max(inner[1], outer[1])
        x1, y1 = min(inner[2], outer[2]), min(inner[3], outer[3])
        intersection = max(0.0, x1 - x0) * max(0.0, y1 - y0)
        area = max((inner[2] - inner[0]) * (inner[3] - inner[1]), 1.0)
        return intersection / area

    @staticmethod
    def native_trace_text(trace: dict) -> str:
        characters = []
        for char in trace.get("chars") or []:
            if char and isinstance(char[0], int) and 0 <= char[0] <= 0x10FFFF:
                characters.append(chr(char[0]))
        return "".join(characters)

    def native_marker_evidence(self, pdf_path: Path | None, pages: list[PageRecord]) -> dict[int, list[dict]]:
        if pdf_path is None:
            return {}

        evidence: dict[int, list[dict]] = {}
        with fitz.open(pdf_path) as document:
            for page in pages:
                try:
                    traces = document[page.pdf_page - 1].get_texttrace()
                except (IndexError, ValueError):
                    continue

                markers = []
                for trace in traces:
                    text = self.native_trace_text(trace)
                    if not WATERMARK_TEXT_PATTERN.search(text) or not trace.get("bbox"):
                        continue

                    markers.append({
                        "text": text,
                        "bbox": list(trace["bbox"]),
                        "opacity": trace.get("opacity"),
                        "color": trace.get("color"),
                    })

                evidence[page.pdf_page] = markers

        return evidence

    def native_overlap(self, region: Region, marker: re.Match, traces: list[dict]) -> tuple[bool, bool]:
        if not region.bbox:
            return False, False

        expected_number = self.marker_number(marker)
        for trace in traces:
            native_match = WATERMARK_TEXT_PATTERN.search(trace["text"])
            if not native_match or self.marker_number(native_match) != expected_number:
                continue

            if self.overlap_fraction(trace["bbox"], region.bbox) < self.config.min_native_span_overlap:
                continue

            opacity = trace.get("opacity")
            pale = isinstance(opacity, (int, float)) and opacity < 0.55
            return True, pale

        return False, False

    @staticmethod
    def same_page_header_marker(page: PageRecord, region: Region, number: int) -> bool:
        for other in page.regions:
            if other is region or not other.text or not other.bbox:
                continue

            if not any(marker in other.text.casefold() for marker in AUDIENCE_MARKERS):
                continue

            if page.page_height and other.bbox[3] > page.page_height * 0.18:
                continue

            if any(int(match.group("number")) == number for match in WATERMARK_TEXT_PATTERN.finditer(other.text)):
                return True

        return False

    @staticmethod
    def trailing_markers(text: str) -> tuple[int, list[re.Match]] | None:
        matches = list(WATERMARK_TEXT_PATTERN.finditer(text))
        if not matches:
            return None

        # Rimuove solo una catena che termina la regione. Un marker citato
        # nel mezzo della prosa resta intatto.
        cursor = len(text)
        selected: list[re.Match] = []

        for match in reversed(matches):
            if text[match.end():cursor].strip(" \t\r\n-–—") != "":
                break
            selected.append(match)
            cursor = match.start()

        if not selected:
            return None

        start = cursor
        if len(selected) == 1:
            number = int(selected[0].group("number"))
            prefix = text[:start]
            orphan = re.search(r"(?<!\w)(?P<number>0*\d{3,8})\s*-\s*$", prefix)
            if orphan and int(orphan.group("number")) == number:
                start = orphan.start()

        # Il separatore prima della catena è parte dell'intrusione,
        # non del titolo o della dateline.
        while start > 0 and text[start - 1].isspace():
            start -= 1

        return start, list(reversed(selected))

    def evaluate(
        self, region: Region, page: PageRecord, traces: list[dict],
    ) -> tuple[int, float, list[str], list[re.Match]] | None:
        text = region.text or ""
        if not text or len(text) > self.config.max_candidate_chars:
            return None

        trailing = self.trailing_markers(text)
        if trailing is None:
            return None

        start, markers = trailing
        repeated = (region.metadata.get("embedded_watermark_candidate") or {}).get("supporting_pdf_pages", 0)
        same_header = any(self.same_page_header_marker(page, region, self.marker_number(match)) for match in markers)
        native, pale = self.native_overlap(region, markers[0], traces)

        near_header = bool(
            region.bbox and page.page_height and region.bbox[1] / page.page_height <= 0.18
        )
        score = 0.60
        evidence = ["bounded_data_stampa_marker"]

        if repeated >= self.config.min_repeated_pages:
            score += 0.15
            evidence.append("repeated_at_compatible_position")
        if same_header:
            score += 0.10
            evidence.append("same_page_audience_header_marker")
        if near_header:
            score += 0.10
            evidence.append("upper_page_band")
        if native:
            score += 0.20
            evidence.append("matching_native_pdf_span")
        if pale:
            score += 0.05
            evidence.append("pale_native_span")

        # Non è sufficiente la sola stringa OCR: serve almeno
        # un corroborante non derivato dalla stessa regex.
        corroborated = repeated >= self.config.min_repeated_pages or same_header or native
        if not corroborated or score < self.config.min_cleaning_score:
            return None

        return start, round(min(score, 1.0), 4), evidence, markers

    def enrich(self, pages: list[PageRecord], pdf_path: Path | None = None) -> list[PageRecord]:
        native = self.native_marker_evidence(pdf_path, pages)

        for page in pages:
            for region in page.regions:
                if region.extraction_method != "ocr" or not region.text or self.protected_metadata(region):
                    continue

                if region.type in {
                    RegionType.IMAGE, RegionType.TABLE, RegionType.INFOGRAPHIC,
                    RegionType.RIGHTS_NOTICE, RegionType.ADVERTISEMENT,
                }:
                    continue
                
                if region.metadata.get("watermark_text_cleanup", {}).get("status") == "applied":
                    continue

                original = region.text
                decision = self.evaluate(region, page, native.get(page.pdf_page, []))

                if decision is None:
                    if WATERMARK_TEXT_PATTERN.search(original):
                        region.metadata["watermark_text_cleanup"] = {
                            "status": "candidate", "method": "conservative_suffix_v1",
                            "reason": "insufficient_or_non_trailing_evidence",
                            "original_text": original,
                        }
                    continue

                start, score, evidence, matches = decision
                removed = original[start:]
                cleaned = original[:start]

                region.metadata["watermark_text_cleanup"] = {
                    "status": "applied",
                    "method": "conservative_suffix_v1",
                    "original_text": original,
                    "cleaned_text": cleaned,
                    "removed_text": removed,
                    "removed_raw_span": [start, len(original)],
                    "kept_raw_spans": [[0, start]] if start else [],
                    "matched_raw_spans": [[match.start(), match.end()] for match in matches],
                    "score": score,
                    "evidence": evidence,
                    "original_provenance": list(region.provenance),
                    "offset_scope": "region_text_before_watermark_cleanup",
                    "source_text_field": "text",
                    "provenance_charspan_scope": "docling_original_item",
                    "bbox_scope": "original_mixed_region",
                }
                region.text = cleaned
                
                if cleaned and region.raw_label == "section_header" and len(cleaned.split()) <=3:
                    region.metadata["watermark_text_cleanup"]["residual_role_hint"] = "short_header_candidate"
                    

                if not cleaned.strip():
                    region.type = RegionType.WATERMARK
                    region.exclude_from_article_text = True
                    region.metadata["watermark_text_cleanup"]["result"] = "standalone_watermark"
                else:
                    # Il tipo editoriale viene deciso dopo la pulizia.
                    region.metadata["watermark_text_cleanup"]["result"] = "mixed_region_cleaned"

        return pages