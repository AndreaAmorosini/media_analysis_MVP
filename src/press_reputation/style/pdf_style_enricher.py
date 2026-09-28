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
            if not region.bbox:
                continue

            matching = [
                span
                for span in spans
                if self.overlap_ratio(region.bbox, span["bbox"]) > 0.20
            ]

            if not matching:
                continue

            font_sizes = [span["size"] for span in matching if span.get("size")]
            font_names = [span["font"] for span in matching if span.get("font")]
            colors = [
                span.get("color")
                for span in matching
                if span.get("color") is not None
            ]
            opacities = [
                span.get("opacity")
                for span in matching
                if span.get("opacity") is not None
            ]

            region.style.update(
                {
                    "font_names": sorted(set(font_names)),
                    "dominant_font": self.most_common(font_names),
                    "median_font_size": median(font_sizes) if font_sizes else None,
                    "max_font_size": max(font_sizes) if font_sizes else None,
                    "bold_ratio": self.bold_ratio(font_names),
                    "italic_ratio": self.italic_ratio(font_names),
                    "dominant_color": self.most_common(colors),
                    "median_opacity": median(opacities) if opacities else None,
                }
            )

        return page

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