import logging
from typing import Any

from press_reputation.models.page import (PageRecord, Region, RegionType)
from press_reputation.classification.boilerplate_detector import DocumentBoilerplateDetector
from press_reputation.classification.header_metadata_zone import HeaderMetadataZoneDetector
from press_reputation.reconstruction import BodyContinuationResolver
from press_reputation.normalization.text_fragment import TextFragmentRecovery
from press_reputation.profiling.models import ExtractionMethod

logger = logging.getLogger(__name__)

class PageNormalizer:
    #Converte DoclingDocument in un formato interno PageRecord (in models)
    
    def __init__(self) -> None:
        self.text_recovery = TextFragmentRecovery()
    
    def normalize(self, document: Any, document_id: str, extraction_method: ExtractionMethod = "unknown") -> list[PageRecord]:
        doc_dict = self._to_dict(document)
        pages = self._init_pages(doc_dict, document_id)
        
        self._normalize_collection(
            doc_dict=doc_dict,
            collection_name="texts",
            pages=pages,
            extraction_method=extraction_method,
        )
        
        self._normalize_collection(
            doc_dict=doc_dict,
            collection_name="pictures",
            pages=pages,
            extraction_method=extraction_method,
        )
        
        self._normalize_collection(
            doc_dict=doc_dict,
            collection_name="tables",
            pages=pages,
            extraction_method=extraction_method,
        )
        
        self._normalize_collection(
            doc_dict=doc_dict,
            collection_name="key_value_items",
            pages=pages,
            extraction_method=extraction_method,
        )

        self._normalize_collection(
            doc_dict=doc_dict,
            collection_name="form_items",
            pages=pages,
            extraction_method=extraction_method,
        )
        
        return [pages[page_no] for page_no in sorted(pages)]

    
    def _to_dict(self, document: Any) -> dict[str, Any]:
        #Converte docling document in un dizionario
        
        if isinstance(document, dict):
            return document
        
        if hasattr(document, "export_to_dict"):
            return document.export_to_dict()
        
        if hasattr(document, "model_dump"):
            return document.model_dump(mode="json", by_alias=True, exclude_none=True)
        
        raise TypeError(f"Unsupported document type {type(document)!r}. Expected a dict or an object with 'export_to_dict' or 'model_dump' method.")
    
    def _init_pages(self, doc_dict: dict[str, Any], document_id: str) -> dict[int, PageRecord]:
        #Inizializza le pagine del documento come PageRecord
        pages: dict[int, PageRecord] = {}
        
        raw_pages = doc_dict.get("pages", {})
        
        for page_key, page_data in raw_pages.items():
            page_no = int(page_data.get("page_no") or page_key)
            size = page_data.get("size") or {}
            
            pages[page_no] = PageRecord(document_id=document_id, pdf_page=page_no, page_width=size.get("width"), page_height=size.get("height"))
            
        return pages
    
    def _normalize_collection(self, doc_dict: dict[str, Any], collection_name: str, pages: dict[int, PageRecord], extraction_method: ExtractionMethod) -> None:
        
        items = doc_dict.get(collection_name, [])
        
        for item in items:
            regions = self._normalize_item(item=item, collection_name=collection_name, doc_dict=doc_dict, extraction_method=extraction_method)
            
            for page_no, region in regions:
                if page_no not in pages:
                    logger.warning("Item references missing page %s in collection %s", page_no, collection_name)
                    continue
                pages[page_no].regions.append(region)
                
    def _normalize_item(self, item: dict[str, Any], collection_name: str, doc_dict: dict[str, Any],
                        extraction_method: ExtractionMethod) -> list[tuple[int, Region]]:
        
        label, self_ref = item.get("label"), item.get("self_ref")
        provenances = item.get("prov") or []
        region_type = self._map_region_type(label=label, collection_name=collection_name)

        if not provenances:
            logger.warning("Item without page provenance: retained in raw output: %s", self_ref)
            return []

        recovery = self.text_recovery.recover(item)
        for warning in recovery.warnings:
            logger.warning("Text recovery warning for item %s: %s", self_ref, warning)

        regions: list[tuple[int, Region]] = []
        source_text_saved = False

        for fragment_index, provenance in enumerate(provenances):
            page_value = provenance.get("page_no")
            if page_value is None:
                logger.warning("Provenance without page number: item=%s fragment=%s", self_ref, fragment_index)
                continue

            try:
                page_no = int(page_value)
            except (TypeError, ValueError):
                logger.warning("Invalid page number: item=%s fragment=%s page=%r", self_ref, fragment_index, page_value)
                continue

            fragment = recovery.fragments[fragment_index]
            bbox = self._normalize_bbox(provenance.get("bbox"), page_no, doc_dict)
            metadata: dict[str, Any] = {"source_fragment_index": fragment_index}

            if collection_name == "tables":
                data = item.get("data") or {}
                metadata["table_shape"] = {"rows": data.get("num_rows"), "columns": data.get("num_cols")}
                metadata["table_ref"] = self_ref

            if self_ref:
                metadata["region_id"] = f"{self_ref}:page={page_no}:fragment={fragment_index}"

            if fragment.status != "exact" or fragment.warnings:
                metadata["text_fragment_recovery"] = {
                    "status": fragment.status,
                    "confidence": fragment.confidence,
                    "raw_charspan": fragment.raw_charspan,
                    "effective_charspan": fragment.effective_charspan,
                    "warnings": fragment.warnings,
                    "auto_classification_allowed": fragment.status in {
                        "offset_adjusted", "realigned", "single_provenance",
                    },
                }

            if recovery.warnings and not source_text_saved:
                metadata["unmapped_source_text"] = recovery.source_text
                source_text_saved = True

            region_method: ExtractionMethod = (
                extraction_method
                if fragment.text is not None and collection_name != "pictures"
                else "unknown"
            )

            regions.append((page_no, Region(
                type=region_type, text=fragment.text, bbox=bbox, raw_label=label,
                metadata=metadata, extraction_method=region_method,
                provenance=[{
                    "self_ref": self_ref, "collection": collection_name,
                    "docling_label": label, "charspan": provenance.get("charspan"),
                    "raw_bbox": provenance.get("bbox"), "page": page_no,
                    "text_field": "text", "extraction_method": region_method,
                }],
            )))

        valid_pages = {
            int(prov["page_no"]) for prov in provenances
            if prov.get("page_no") is not None and str(prov["page_no"]).isdigit()
        }

        if len(valid_pages) == 1 and recovery.source_text is not None:
            page_no = next(iter(valid_pages))

            for start, end in recovery.unmapped_intervals:
                candidate_text = recovery.source_text[start:end]
                if not candidate_text.strip():
                    continue

                regions.append((page_no, Region(
                    type=RegionType.UNKNOWN, text=candidate_text, bbox=None, raw_label=label,
                    extraction_method=extraction_method,
                    metadata={
                        "region_id": f"{self_ref}:page={page_no}:unmapped={start}-{end}",
                        "recovery_parent_ref": self_ref,
                        "text_fragment_recovery": {
                            "status": "ambiguous",
                            "source_interval": [start, end],
                            "auto_classification_allowed": False,
                            "warnings": recovery.warnings,
                        },
                    },
                    provenance=[{
                        "self_ref": self_ref, "collection": collection_name,
                        "docling_label": label, "charspan": prov.get("charspan"),
                        "raw_bbox": prov.get("bbox"), "page": page_no,
                        "extraction_method": extraction_method, "recovery_candidate": True,
                    } for prov in provenances],
                )))

        return regions    
    
    def _map_region_type(self, label: str | None, collection_name: str) -> RegionType:
        
        if collection_name == "tables":
            return RegionType.TABLE
        
        if collection_name == "pictures":
            return RegionType.IMAGE

        if label == "picture":
            return RegionType.IMAGE

        if label == "caption":
            return RegionType.CAPTION

        if label == "section_header":
            return RegionType.UNKNOWN

        if label == "page_header":
            return RegionType.HEADER_METADATA

        if label == "page_footer":
            return RegionType.FOOTER

        if label == "text":
            return RegionType.UNKNOWN

        logger.info(
            "Unknown Docling label mapping: collection=%s label=%s",
            collection_name,
            label,
        )

        return RegionType.UNKNOWN
    
    def _normalize_bbox(self, raw_bbox: dict[str, Any] | None, page_no: int, doc_dict: dict[str, Any]) -> list[float] | None:
        if not raw_bbox:
            return None
        
        try:
            left = float(raw_bbox.get("l"))
            top = float(raw_bbox.get("t"))
            right = float(raw_bbox.get("r"))
            bottom = float(raw_bbox.get("b"))
        except (TypeError, ValueError) as e:
            logger.warning("Invalid bbox values for page %s: %s", page_no, raw_bbox)
            return None
        
        coord_origin = raw_bbox.get("coord_origin")
        
        if coord_origin == "BOTTOMLEFT":
            page_height = self._get_page_height(doc_dict=doc_dict, page_no=page_no)
            
            if page_height is None:
                logger.warning("Cannot convert BOTTOMLEFT bbox without page height: page=%s, bbox=%s", page_no, raw_bbox)
                return None
            
            x0 = left
            y0 = page_height - top
            x1 = right
            y1 = page_height - bottom
            
            return [x0, y0, x1, y1]
        
        if coord_origin == "TOPLEFT":
            return [left, top, right, bottom]
        
        logger.warning("Unknown coord_origin for bbox: page=%s, bbox=%s", page_no, raw_bbox)
        return None
    
    def _get_page_height(self, doc_dict: dict[str, Any], page_no: int) -> float | None:
        page_data = doc_dict.get("pages", {}).get(str(page_no))
        if not page_data:
            return None
        
        size = page_data.get("size") or {}
        height = size.get("height")
        
        if height is None:
            return None
        
        return float(height)