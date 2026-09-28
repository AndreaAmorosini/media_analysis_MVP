from press_reputation.models.page import PageRecord, RegionType

class BodyGroupingResolver:
    def enrich(self, page: PageRecord) -> PageRecord:
        body_regions = [region for region in page.regions if region.type == RegionType.ARTICLE_BODY and region.bbox and not region.exclude_from_article_text]
        
        body_regions.sort(key=lambda region: (region.bbox[1], region.bbox[0]))
        
        for index, region in enumerate(body_regions, start=1):
            region.metadata["body_group_id"] = "body_001"
            region.metadata["body_reading_order"] = index
            
        if body_regions:
            page_metadata = getattr(page, "metadata", None)
            
        return page