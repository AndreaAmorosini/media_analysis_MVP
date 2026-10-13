from pathlib import Path

import fitz

from press_reputation.config import DocumentProfilingConfig
from press_reputation.profiling.models import DocumentProfile, PageExtractionProfile

class DocumentProfiler:
    def __init__(self, config: DocumentProfilingConfig | None = None) -> None:
        self.config = config or DocumentProfilingConfig()

    def profile(self, pdf_path: Path, *, ocr_policy: str = "auto") -> DocumentProfile:
        """Choose pages for OCR using PDF text density and image coverage.

        auto sparse central native text + moderate image coverage
        aggressive OCR pages with a moderate amount of native text when images
        suggest other editorial content may still be rasterized
        all diagnostic full-page OCR on every page (slow, may duplicate text)
        """
        if ocr_policy not in {"auto", "aggressive", "all"}:
            raise ValueError(f"Invalid OCR policy: {ocr_policy}")
        result = DocumentProfile()

        with fitz.open(pdf_path) as document:
            for page_number, page in enumerate(document, start=1):
                words = page.get_text("words")
                height = page.rect.height

                body_words = [word for word in words if (word[1] >= height * self.config.header_fraction and
                                                         word[3] <= height * (1 - self.config.footer_fraction))]

                image_coverage = self._image_coverage(page)
                profile = PageExtractionProfile(
                    pdf_page = page_number,
                    native_word_count = len(words),
                    native_body_word_count = len(body_words),
                    image_coverage = image_coverage,
                )

                if ocr_policy == "all":
                    profile.run_ocr = True
                    profile.decision_reasons.append("ocr_policy_all")
                elif not words and image_coverage >= self.config.min_image_coverage_without_text:
                    profile.run_ocr = True
                    profile.decision_reasons.append("no_pdf_text_with_significant_image")
                elif (len(body_words) < self.config.min_native_body_words and
                        image_coverage >= self.config.min_image_coverage_with_sparse_text):
                    profile.run_ocr = True
                    profile.decision_reasons.append("sparse_body_text_with_significant_image")
                elif (ocr_policy == "aggressive" and
                        len(body_words) < self.config.aggressive_ocr_body_word_limit and
                        image_coverage >= self.config.aggressive_ocr_image_coverage):
                    profile.run_ocr = True
                    profile.decision_reasons.append("aggressive_ocr_low_native_body_coverage")
                else:
                    profile.run_ocr = False
                    profile.decision_reasons.append("native_text_sufficient_or_no_ocr_evidence")

                result.pages[page_number] = profile

        return result

    @staticmethod
    def _image_coverage(page: fitz.Page) -> float:
        grid_size = 32
        covered: set[tuple[int, int]] = set()
        page_rect = page.rect

        if page_rect.is_empty:
            return 0.0

        for image in page.get_image_info():
            rect = fitz.Rect(image["bbox"]) & page_rect
            if rect.is_empty:
                continue

            x0 = max(0, int(grid_size * rect.x0 / page_rect.width))
            x1 = min(grid_size - 1, int(grid_size * rect.x1 / page_rect.width))
            y0 = max(0, int(grid_size * rect.y0 / page_rect.height))
            y1 = min(grid_size - 1, int(grid_size * rect.y1 / page_rect.height))

            for x in range(x0, x1 + 1):
                for y in range(y0, y1 + 1):
                    covered.add((x, y))

        return len(covered) / (grid_size * grid_size)