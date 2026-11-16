from press_reputation.core.config import DocumentProfilingConfig
from press_reputation.features.geometry import horizontal_overlap
from press_reputation.core.models.page import PageRecord, Region, RegionType
from press_reputation.ingestion.profiling.models import DocumentProfile


def _duplicate(
    candidate: Region, existing: Region, config: DocumentProfilingConfig
) -> bool:
    if (
        not candidate.text
        or not existing.text
        or not candidate.bbox
        or not existing.bbox
    ):
        return False

    candidate_text = " ".join(candidate.text.casefold().split())
    existing_text = " ".join(existing.text.casefold().split())
    return (
        candidate_text == existing_text
        and horizontal_overlap(candidate.bbox, existing.bbox)
        >= config.duplicate_bbox_overlap
    )


def merge_extraction(
    native_pages: list[PageRecord],
    ocr_pages: dict[int, PageRecord],
    profile: DocumentProfile,
    config: DocumentProfilingConfig,
) -> list[PageRecord]:
    for page in native_pages:
        page_profile = profile.pages[page.pdf_page]
        ocr_page = ocr_pages.get(page.pdf_page)

        if ocr_page is not None:
            for region in ocr_page.regions:
                recovery = region.metadata.get("text_fragment_recovery", {})

                if region.extraction_method != "ocr":
                    if recovery and region.type != RegionType.IMAGE:
                        page.regions.append(region)
                    continue

                if not region.text:
                    if recovery:
                        page.regions.append(region)
                    continue

                duplicate = next(
                    (
                        existing
                        for existing in page.regions
                        if _duplicate(region, existing, config)
                    ),
                    None,
                )
                if duplicate is not None:
                    duplicate.metadata.setdefault(
                        "equivalent_extraction_regions", []
                    ).append(
                        {
                            "region_id": region.metadata.get("region_id"),
                            "extraction_method": "ocr",
                            "recovery_status": recovery.get("status"),
                            "provenance": region.provenance,
                        }
                    )
                    continue

                page.regions.append(region)

        methods = {
            region.extraction_method
            for region in page.regions
            if (
                region.text
                and region.text.strip()
                and region.metadata.get("text_fragment_recovery", {}).get(
                    "auto_classification_allowed", True
                )
            )
        }

        if any(
            region.metadata.get("text_fragment_recovery", {}).get("status")
            == "ambiguous"
            for region in page.regions
        ):
            warning = "ocr_text_recovery_candidates_present"
            if warning not in page_profile.warnings:
                page_profile.warnings.append(warning)

        if {"pdf_text", "ocr"} <= methods:
            page_profile.kind = "mixed"
        elif "pdf_text" in methods:
            page_profile.kind = "native_pdf"
        elif "ocr" in methods:
            page_profile.kind = "ocr"
        elif page_profile.image_coverage > 0:
            page_profile.kind = "image_only"
        else:
            page_profile.kind = None

        page.extraction_profile = page_profile

    return native_pages
