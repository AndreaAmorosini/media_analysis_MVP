import logging
from pathlib import Path
from typing import Any

from docling.document_converter import DocumentConverter
from press_reputation.parsers.base import DocumentParser

logger = logging.getLogger(__name__)

class DoclingParser(DocumentParser):
    name = "docling"
    
    def __init__(self) -> None:
        self.converter = DocumentConverter()
        
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