from pathlib import Path
from statistics import median

import fitz

from press_reputation.models.page import PageRecord


class PdfStyleEnricher:
    def enrich_document(
        self,
        pdf_path: Path,
        pages: list[PageRecord],
    ) -> list[PageRecord]:
        pdf_doc = fitz.open(pdf_path)

        try:
            for page in pages:
                self.enrich_page(pdf_doc, page)
        finally:
            pdf_doc.close()

        return pages

    def enrich_page(self, pdf_doc: fitz.Document, page: PageRecord) -> PageRecord:
        pdf_page = pdf_doc[page.pdf_page - 1]

        try:
            traces = pdf_page.get_texttrace()
        except Exception:
            return page

        spans = self.extract_spans(traces)

        for region in page.regions:
            if (region.extraction_method == "ocr" or not region.text or not region.bbox or len(region.bbox) != 4):
                continue

            matching = [span for span in spans if self.span_coverage(region.bbox, span["bbox"]) > 0.65]

            if not matching:
                continue

            font_sizes = [span["size"] for span in matching if span.get("size")]
            font_names = [span["font"] for span in matching if span.get("font")]
            colors = [span.get("color") for span in matching if span.get("color") is not None]
            opacities = [span.get("opacity") for span in matching if span.get("opacity") is not None]
            
            bold_ratio, bold_coverage = self.trait_ratio(matching, trait="bold")
            italic_ratio, italic_coverage = self.trait_ratio(matching, trait="italic")

            region.style.update(
                {
                    "font_names": sorted(set(font_names)),
                    "dominant_font": self.most_common(font_names),
                    "median_font_size": median(font_sizes) if font_sizes else None,
                    "max_font_size": max(font_sizes) if font_sizes else None,
                    "bold_ratio": bold_ratio,
                    "bold_evidence_fraction": bold_coverage,
                    "italic_ratio": italic_ratio,
                    "italic_evidence_fraction": italic_coverage,
                    "style_match_count": len(matching),
                    "style_source": "pdf_texttrace",
                    "dominant_color": self.most_common(colors),
                    "median_opacity": median(opacities) if opacities else None,
                }
            )

        return page
    
    @staticmethod
    def span_coverage(region_box: list[float], span_box: list[float]) -> float:
        rx0, ry0, rx1, ry1 = region_box
        sx0, sy0, sx1, sy1 = span_box
        
        intersection = max(0.0, min(rx1, sx1) - max(rx0, sx0)) * max(0.0, min(ry1, sy1) - max(ry0, sy0))
        
        span_area = max((sx1 - sx0) * (sy1 - sy0), 0.0)
        
        return (intersection / span_area if span_area > 0 else 0.0)

    @staticmethod
    def extract_spans(traces: list[dict]) -> list[dict]:
        spans = []

        for item in traces:
            bbox = item.get("bbox")

            if not bbox:
                continue

            spans.append(
                {
                    "bbox": list(bbox),
                    "font": item.get("font"),
                    "size": item.get("size"),
                    "color": item.get("color"),
                    "opacity": item.get("opacity"),
                    "flags": item.get("flags"),
                    "char_count": max(len(item.get("chars") or []), 1)
                }
            )

        return spans

    @staticmethod
    def overlap_ratio(a: list[float], b: list[float]) -> float:
        ax0, ay0, ax1, ay1 = a
        bx0, by0, bx1, by1 = b

        ix0 = max(ax0, bx0)
        iy0 = max(ay0, by0)
        ix1 = min(ax1, bx1)
        iy1 = min(ay1, by1)

        if ix1 <= ix0 or iy1 <= iy0:
            return 0.0

        intersection = (ix1 - ix0) * (iy1 - iy0)
        area_a = max((ax1 - ax0) * (ay1 - ay0), 1.0)

        return intersection / area_a

    @staticmethod
    def most_common(values):
        if not values:
            return None

        return max(set(values), key=values.count)

    @staticmethod
    def bold_ratio(font_names: list[str]) -> float | None:
        if not font_names:
            return None

        bold_count = sum(
            "bold" in font.lower()
            or "black" in font.lower()
            or "semibold" in font.lower()
            for font in font_names
        )

        return bold_count / len(font_names)

    @staticmethod
    def italic_ratio(font_names: list[str]) -> float | None:
        if not font_names:
            return None

        italic_count = sum(
            "italic" in font.lower()
            or "oblique" in font.lower()
            for font in font_names
        )

        return italic_count / len(font_names)
    
    @staticmethod
    def font_trait(span: dict, *, trait: str) -> bool | None:
        font = (span.get("font") or "").casefold()
        flags = span.get("flags")
        
        markers = {
            "bold": ("bold", "black", "semibold", "demibold"),
            "italic": ("italic", "oblique")
        }
        
        flag_bit = {"bold": 16, "italic": 2}[trait]
        
        if any(marker in font for marker in markers[trait]):
            return True
        
        if isinstance(flags, int) and flags & flag_bit:
            return True
        
        if any(marker in font for marker in ("regular", "normal", "roman", "book")):
            return False
        
        return None
    
    def trait_ratio(self, spans: list[dict], *, trait: str) -> tuple[float | None, float]:
        known_weight = 0
        positive_weight = 0
        total_weight = 0
        
        for span in spans:
            weight = span["char_count"]
            total_weight += weight
            value = self.font_trait(span, trait=trait)
            
            if value is None:
                continue
            
            known_weight += weight
            if value:
                positive_weight += weight
                
        if known_weight == 0:
            return None, 0.0
        
        return (positive_weight / known_weight, known_weight / max(total_weight, 1))
        