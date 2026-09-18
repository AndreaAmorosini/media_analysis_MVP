from pydantic import BaseModel
from press_reputation.models.page import PageRecord

class PageFeatures(BaseModel):
    pdf_page: int
    
    text_char_count: int = 0
    region_count: int = 0
    text_region_count: int = 0
    image_region_count: int = 0
    header_metadata_count: int = 0
    source_name_count: int = 0
    article_title_count: int = 0
    article_body_count: int = 0
    footer_count: int = 0
    unknown_count: int = 0
    
    index_entry_count: int = 0

    has_url: bool = False
    has_foglio: bool = False
    has_superficie: bool = False
    has_tiratura: bool = False
    has_diffusione: bool = False
    has_lettori: bool = False
    has_dir_resp: bool = False
    has_quotidiano: bool = False

    has_newsletter: bool = False
    has_related_content_marker: bool = False
    has_cookie_marker: bool = False

    clipping_marker_count: int = 0
    web_marker_count: int = 0

    text_region_ratio: float = 0.0
    image_region_ratio: float = 0.0
    unknown_region_ratio: float = 0.0
    
class PageFeatureExtractor:
    def extract(self, page: PageRecord) -> PageFeatures:
        text = self.page_text(page)
        lower = text.lower()

        region_count = len(page.regions)
        
        def count_type(value: str) -> int:
            return sum(1 for region in page.regions if region.type.value == value)
        
        index_entry_count = sum(1 for region in page.regions if region.text and ("pag." in region.text.lower() or "pagina" in region.text.lower()))

        # text_region_count = sum(1 for region in page.regions if region.text and region.text.strip())
        # image_region_count = sum(1 for region in page.regions if region.type.value == "image")
        # header_metadata_count = sum(1 for region in page.regions if region.type.value == "header_metadata")
        # article_title_count = sum(1 for region in page.regions if region.type.value == "article_title")
        # article_body_count = sum(1 for region in page.regions if region.type.value == "article_body")
        # footer_count = sum(1 for region in page.regions if region.type.value == "footer")
        # unknown_count = sum(1 for region in page.regions if region.type.value == "unknown")

        has_url = "http://" in lower or "https://" in lower
        has_foglio = "foglio" in lower
        has_superficie = "superficie" in lower
        has_tiratura = "tiratura" in lower
        has_diffusione = "diffusione" in lower
        has_lettori = "lettori" in lower
        has_dir_resp = "dir. resp" in lower or "dir.resp" in lower
        has_quotidiano = "quotidiano" in lower

        has_newsletter = "newsletter" in lower
        has_related_content_marker = (
            "articoli correlati" in lower or "ultimi articoli" in lower or "leggi anche" in lower
        )
        has_cookie_marker = "cookie" in lower or "privacy policy" in lower

        clipping_marker_count = sum(
            [
                has_foglio,
                has_superficie,
                has_tiratura,
                has_diffusione,
                has_lettori,
                has_dir_resp,
                has_quotidiano,
            ]
        )

        web_marker_count = sum(
            [
                has_url,
                has_newsletter,
                has_related_content_marker,
                has_cookie_marker,
            ]
        )
        
        image_count = count_type("image") + count_type("article_position_thumbnail")
        unknown_count = count_type("unknown")

        return PageFeatures(
            pdf_page=page.pdf_page,
            text_char_count=len(text),
            region_count=region_count,
            image_region_count=image_count,
            header_metadata_count=count_type("header_metadata"),
            article_title_count=count_type("article_title"),
            article_body_count=count_type("article_body"),
            footer_count=count_type("footer"),
            index_entry_count=index_entry_count,
            unknown_count=unknown_count,
            has_url=has_url,
            has_foglio=has_foglio,
            has_superficie=has_superficie,
            has_tiratura=has_tiratura,
            has_diffusione=has_diffusione,
            has_lettori=has_lettori,
            has_dir_resp=has_dir_resp,
            has_quotidiano=has_quotidiano,
            has_newsletter=has_newsletter,
            has_related_content_marker=has_related_content_marker,
            has_cookie_marker=has_cookie_marker,
            clipping_marker_count=clipping_marker_count,
            web_marker_count=web_marker_count,
            image_region_ratio=self.safe_ratio(image_count, region_count),
            unknown_region_ratio=self.safe_ratio(unknown_count, region_count),
        )
        
    @staticmethod
    def page_text(page: PageRecord) -> str:
        return "\n".join(region.text.strip() for region in page.regions if region.text and region.text.strip())
    
    @staticmethod
    def safe_ratio(value: int, total: int) -> float:
        if total == 0:
            return 0.0
        return value / total
