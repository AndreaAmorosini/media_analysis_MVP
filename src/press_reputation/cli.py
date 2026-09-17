import logging
import time
from pathlib import Path

import typer
from rich.console import Console

from press_reputation.normalization import PageNormalizer
from press_reputation.parsers import DoclingParser

app = typer.Typer()
console = Console()

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s:%(name)s:%(message)s",
)

logger = logging.getLogger(__name__)

def safe_document_dir_name(pdf_path: Path) -> str:
    return pdf_path.stem.replace(" ", "_")

@app.command()
def parse(
    pdf_path: Path = typer.Argument(..., help="Path to the PDF file to parse"),
    output_dir: Path = typer.Option(Path("data"), "--output-dir", "-o", help="Directory to save the parsed data"),
    debug_bbox: bool = typer.Option(False, "--debug-bbox", help="Enable debug mode for bounding boxes"),
) -> None:
    #Elabora un PDF con Docling e genera PageRecord JSON per pagina.
    
    started_at = time.perf_counter()
    
    if not pdf_path.exists():
        raise typer.BadParameter(f"File '{pdf_path}' does not exist.")
    
    if not pdf_path.is_file():
        raise typer.BadParameter(f"Path '{pdf_path}' is not a file.")
    
    document_dir = output_dir / safe_document_dir_name(pdf_path)
    raw_dir = document_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    
    pages_dir = document_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    
    debug_dir = document_dir / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    
    parser = DoclingParser()
    
    console.print(f"[bold]Parsing document:[/bold] {pdf_path.name}")
    console.print(f"[bold]Parser:[/bold] {parser.name}")
    
    document = parser.extract(pdf_path)
    
    raw_output_path = raw_dir / "document.json"
    parser.save_raw_json(document, raw_output_path)
    
    normalizer = PageNormalizer()
    pages = normalizer.normalize(document, document_id=pdf_path.name)
    
    for page in pages:
        page_output_path = pages_dir / f"page_{page.pdf_page:03d}.json"
        page_output_path.write_text(page.model_dump_json(indent=2), encoding="utf-8")
        
    if debug_bbox:
        from press_reputation.visualization import render_page_bboxes
        
        debug_dir.mkdir(parents=True, exist_ok=True)
        
        for page in pages:
            debug_output_path = debug_dir / f"page_{page.pdf_page:03d}.png"
            render_page_bboxes(pdf_path=pdf_path, page_record=page, output_path=debug_output_path)
            
    elapsed = time.perf_counter() - started_at
    console.print(f"[bold]Pages:[/bold] {len(pages)}")
    console.print(f"[bold]Output directory:[/bold] {document_dir}")
    # console.print(f"[bold]Raw JSON:[/bold] {raw_output_path}")
    console.print(f"[bold]Elapsed time:[/bold] {elapsed:.2f} seconds")
    
    logger.info("Finished parsing document '%s' in %.2f seconds", pdf_path.name, elapsed)
    
if __name__ == "__main__":
    app()
    