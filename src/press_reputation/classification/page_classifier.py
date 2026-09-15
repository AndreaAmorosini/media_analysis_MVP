from press_reputation.models.page import PageRecord, PageType

CLIPPING_PATTERNS = [
    "superficie",
    "tiratura",
    "diffusione",
    "lettori",
    "foglio",
    "dir. resp.",
    "dir. resp",
    "quotidiano",
]

WEB_PATTERNS = [
    "http://",
    "https://",
    "newsletter",
    "condividi",
    "twitta",
    "ultimi articoli",
    "articoli correlati",
    "cookie",
    "privacy policy",
]

class PageClassifier:
    #Classificazione deterministico per PageType. Attualmente usa marker preimpostati per riconoscere le tipologie
    #TODO: migliorare classificazione con ML
    
    def classify(self, page: PageRecord) -> PageType:
        text = self.page_text(page)
        lower = text.lower()
        
        clipping_matches = sum(pattern in lower for pattern in CLIPPING_PATTERNS)
        if clipping_matches >= 2:
            return PageType.CLIPPING
        
        web_matches = sum(pattern in lower for pattern in WEB_PATTERNS)
        if web_matches >= 1:
            return PageType.WEB
        
        if self.looks_like_pure_text(page):
            return PageType.PURE_TEXT
        
        return PageType.UNKNOWN
    
    @staticmethod
    def page_text(page: PageRecord) -> str:
        return "\n".join(region.text.strip() for region in page.regions if region.text and region.text.strip())
    
    @staticmethod
    def looks_like_pure_text(page: PageRecord) -> bool:
        text_regions = [
            region
            for region in page.regions
            if region.text and region.text.strip()
        ]

        image_regions = [
            region
            for region in page.regions
            if region.type.value == "image"
        ]

        total_chars = sum(len(region.text or "") for region in text_regions)

        if total_chars < 500:
            return False

        if image_regions and len(image_regions) >= len(text_regions):
            return False

        return True
