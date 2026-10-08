import logging
import time
from pathlib import Path
import json

import typer
from rich.console import Console

from press_reputation.normalization import PageNormalizer
from press_reputation.parsers import DoclingParser
from press_reputation.image_analysis import enrich_article_position_thumbnails
from press_reputation.style import PdfStyleEnricher
from press_reputation.pipeline import PageProcessingPipeline
from press_reputation.reconstruction.article_draft_assembler import ArticleDraftAssembler
from press_reputation.config import DocumentProfilingConfig
from press_reputation.profiling.document_profiler import DocumentProfiler
from press_reputation.profiling.merge import merge_extraction
from press_reputation.review_index.parser import ReviewIndexParser

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
    output_dir: Path = typer.Option(Path("results"), "--output-dir", "-o", help="Directory to save the parsed data"),
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
    
    
    profiling_config = DocumentProfilingConfig()
    profile = DocumentProfiler(profiling_config).profile(pdf_path)
    
    parser = DoclingParser()
    console.print(f"[bold]Parsing document:[/bold] {pdf_path.name}")
    console.print(f"[bold]Parser:[/bold] {parser.name}")
    
    extracted = parser.extract_with_profile(pdf_path, profile, profiling_config)
    
    raw_output_path = raw_dir / "document.json"
    parser.save_raw_json(extracted.native_document, raw_output_path)
    
    normalizer = PageNormalizer()
    pages = normalizer.normalize(extracted.native_document, document_id=pdf_path.name, extraction_method="pdf_text")
    
    ocr_pages = {}
    for original_page_number, ocr_document in extracted.ocr_documents.items():
        ocr_raw_path = (raw_dir / f"ocr_page_{original_page_number:03d}.json")
        parser.save_raw_json(ocr_document, ocr_raw_path)
        
        temporary_pages = normalizer.normalize(ocr_document, document_id=pdf_path.name, extraction_method="ocr")
        
        if not temporary_pages:
            profile.pages[original_page_number].warnings.append("ocr_produced_no_page")
            continue
        
        ocr_page = temporary_pages[0]
        ocr_page.pdf_page = original_page_number
        
        for region in ocr_page.regions:
            for provenance in region.provenance:
                provenance["extraction_pass"] = "ocr_image_page"
                provenance["temporary_pdf_page"] = provenance.get("page")
                provenance["page"] = original_page_number
                
            region_id = region.metadata.get("region_id")
            if region_id:
                region.metadata["region_id"] = (f"ocr:pdf_page={original_page_number}:{region_id}")
                
        ocr_pages[original_page_number] = ocr_page
        
    pages = merge_extraction(native_pages=pages, ocr_pages=ocr_pages, profile=profile, config=profiling_config)
    
    index_entries = ReviewIndexParser().parse(extracted.native_document, document_id=pdf_path.name)
    
    profiling_dir = document_dir / "profiling"
    profiling_dir.mkdir(parents=True, exist_ok=True)
    (profiling_dir / "document.json").write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    
    pipeline = PageProcessingPipeline()
    pages = pipeline.process(pages=pages, pdf_path=pdf_path, review_index_entries=index_entries)
    
    flow_dir = document_dir / "flow"
    flow_dir.mkdir(parents=True, exist_ok=True)
    
    (flow_dir / "reading_order.json").write_text(
        json.dumps([item.model_dump(mode="json") for item in (pipeline.reading_orders.values())], indent=2, ensure_ascii=False), encoding="utf-8"
    )
    
    (flow_dir / "newspaper_continuation_decisions.json").write_text(
        json.dumps(pipeline.newspaper_continuation_resolver.decisions, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    
    (flow_dir / "links.json").write_text(
        json.dumps([link.model_dump(mode="json") for link in pipeline.flow_links], indent=2, ensure_ascii=False), encoding="utf-8"
    )
    
    drafts = ArticleDraftAssembler().assemble(pages=pages, links=pipeline.flow_links, reading_orders=pipeline.reading_orders)
    
    (flow_dir / "article_drafts.json").write_text(
        json.dumps([draft.model_dump(mode="json") for draft in drafts], indent=2, ensure_ascii=False), encoding="utf-8"
    )
    
    (flow_dir / "review_index_entries.json").write_text(
        json.dumps([entry.model_dump(mode="json") for entry in index_entries], indent=2, ensure_ascii=False), encoding="utf-8"
    )
    
    (flow_dir / "review_index_matches.json").write_text(
        json.dumps([match.model_dump(mode="json") for match in pipeline.review_index_matches], indent=2, ensure_ascii=False), encoding="utf-8"
    )
        
    for page in pages:
        enrich_article_position_thumbnails(pdf_path=pdf_path, page=page)
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
    