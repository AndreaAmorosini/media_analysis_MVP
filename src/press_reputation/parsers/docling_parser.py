import logging
from pathlib import Path
from typing import Any
from dataclasses import dataclass, field
from tempfile import TemporaryDirectory
import fitz

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
from docling.document_converter import PdfFormatOption
from docling.document_converter import DocumentConverter

from press_reputation.config import DocumentProfilingConfig
from press_reputation.profiling.models import DocumentProfile
from press_reputation.parsers.base import DocumentParser

logger = logging.getLogger(__name__)

@dataclass
class ProfiledExtraction:
    native_document: Any
    ocr_documents: dict[int, Any] = field(default_factory=dict)

class DoclingParser(DocumentParser):
    name = "docling"
    
    def __init__(self) -> None:
        native_options = PdfPipelineOptions(do_ocr=False)
        self.converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=native_options)
            }
        )
        
        ocr_options = PdfPipelineOptions(do_ocr=True, ocr_options=RapidOcrOptions(force_full_page_ocr=True))
        self.ocr_converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=ocr_options)
            }
        )
    
    def extract_with_profile(self, path: Path, profile: DocumentProfile, config: DocumentProfilingConfig | None = None) -> ProfiledExtraction:
        config = config or DocumentProfilingConfig()
        extracted = ProfiledExtraction(native_document=self.extract(path))
        
        selected_pages = [
            page_number for page_number, page_profile in profile.pages.items() if page_profile.run_ocr
        ]
        
        if not selected_pages:
            return extracted
        
        with fitz.open(path) as source, TemporaryDirectory() as directory:
            for page_number in selected_pages:
                page_profile = profile.pages[page_number]
                temporary_pdf = (Path(directory) / f"ocr_{page_number:04d}.pdf")
                
                try:
                    self._rasterized_page_pdf(source[page_number - 1], temporary_pdf, config.ocr_dpi)
                    result = self.ocr_converter.convert(temporary_pdf)
                    extracted.ocr_documents[page_number] = result.document
                    page_profile.ocr_status = "completed"
                except Exception as e:
                    page_profile.ocr_status = "failed"
                    page_profile.warnings.append(f"ocr_conversion_failed: {e}")
                    logger.exception(f"OCR conversion failed for page {page_number} of {path}: {e}")
        
        return extracted
    
    @staticmethod
    def _rasterized_page_pdf(source_page: fitz.Page, destination: Path, dpi: int) -> None:
        scale = dpi / 72
        pixmap = source_page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        
        with fitz.open() as temporary:
            page = temporary.new_page(width=source_page.rect.width, height=source_page.rect.height)
            page.insert_image(page.rect, pixmap=pixmap)
            temporary.save(destination)
        

    def extract(self, path: Path) -> Any:
        pdf_path = Path(path)
        
        if not pdf_path.exists():
            raise FileNotFoundError(f"File not found: {pdf_path}")
        
        if not pdf_path.is_file():
            raise ValueError(f"Path is not a file: {pdf_path}")
        
        logger.info(f"Extracting data from PDF: {pdf_path}")
        
        result = self.converter.convert(pdf_path)
        
        logger.info(f"Extraction completed for PDF: {pdf_path}")
        return result.document
    
    def save_raw_json(self, document: Any, output_path: Path) -> None:
        # Salva il DoclingDocument estratto in formato JSON
        
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"Saving raw JSON to: {output_path}")
        
        document.save_as_json(
            filename=output_path,
            ensure_ascii=False,
            indent=2
        )