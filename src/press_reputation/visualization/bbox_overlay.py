from pathlib import Path
import fitz
from press_reputation.models.page import PageRecord

REGION_COLORS: dict[str, tuple[float, float, float]] = {
    "header_metadata": (0, 0, 1),
    "article_title": (1, 0, 0),
    "article_subtitle": (1, 0.5, 0),
    "article_body": (0, 0.7, 0),
    "author": (0, 0.4, 0.8),
    "image": (0.6, 0, 0.8),
    "caption": (0, 0.7, 0.7),
    "footer": (0.2, 0.2, 0.2),
    "sidebar": (0.3, 0.3, 0.3),
    "advertisement": (1, 0.8, 0),
    "navigation": (0.5, 0.5, 0.5),
    "related_content": (0.8, 0.4, 0.8),
    "unknown": (1, 0, 1),
}

def render_page_bboxes(pdf_path: Path, page_record: PageRecord, output_path: Path, zoom: float = 2.0) -> None:
    # Renderizza le bbox con i corrispondenti tag sui pdf per verifica
    pdf_path = Path(pdf_path)
    output_path = Path(output_path)
    
    if not pdf_path.exists():
        raise FileNotFoundError(f"File not found: {pdf_path}")
    
    if page_record.pdf_page < 1:
        raise ValueError(f"Invalid page number: {page_record.pdf_page}")
    
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    pdf_doc = fitz.open(pdf_path)
    
    try:
        page_index = page_record.pdf_page - 1
        
        if page_index >= len(pdf_doc):
            raise ValueError(f"Page number {page_record.pdf_page} exceeds total pages in PDF.")
        
        page = pdf_doc[page_index]
        
        matrix = fitz.Matrix(zoom, zoom)
        pixmap = page.get_pixmap(matrix=matrix)
        
        overlay_doc = fitz.open()
        overlay_page = overlay_doc.new_page(width=page.rect.width * zoom, height=page.rect.height * zoom)
        
        overlay_page.insert_image(overlay_page.rect, pixmap=pixmap)
        
        for region in page_record.regions:
            if not region.bbox:
                continue
            
            if len(region.bbox) != 4:
                raise ValueError(f"Invalid bbox for region: {region.bbox}")
            
            x0, y0, x1, y1 = region.bbox
            
            rect = fitz.Rect(x0 * zoom, y0 * zoom, x1 * zoom, y1 * zoom)
            
            label = region.type.value
            color = REGION_COLORS.get(label, REGION_COLORS["unknown"])
            
            overlay_page.draw_rect(rect, color=color, width=1.2)
            
            overlay_page.insert_text(
                fitz.Point(rect.x0, max(rect.y0 - 4, 8)),
                label,
                fontsize=7,
                color=color
            )
            
        overlay_page.get_pixmap().save(str(output_path))
        overlay_doc.close()
    
    finally:
        pdf_doc.close()