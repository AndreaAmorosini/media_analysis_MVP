import re
import unicodedata
from difflib import SequenceMatcher

from press_reputation.config import ReviewIndexConfig
from press_reputation.lookup import normalize_key
from press_reputation.models.page import PageRecord, Region, RegionType
from press_reputation.review_index.models import ReviewIndexEntry, ReviewIndexMatch

def normalized_title(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join(re.findall(r"\w+", value))

def squeezed_title(value: str) -> str:
    """Forma compattata del titolo: senza spazi/punteggiatura.

    Robusta alle fusioni e alla perdita di spazi tipiche dell'OCR
    ("Scafatiy il Sindaco", "FOSSOIMPERATORE667RESIDENTI")."""
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", (value or "").casefold()))

def title_similarity(left: str, right: str) -> float:
    a = normalized_title(left)
    b = normalized_title(right)
    if not a or not b:
        return 0.0
    
    character_score = SequenceMatcher(None, a, b).ratio()
    a_tokens = set(a.split())
    b_tokens = set(b.split())
    token_score = len(a_tokens & b_tokens) / max(len(a_tokens | b_tokens), 1)
    similarity = 0.65 * character_score + 0.35 * token_score
    
    # Ripiego robusto all'OCR: il confronto compattato recupera i casi in cui
    # l'OCR ha fuso o perso spazi/apostrofi nella regione del titolo.
    sa = squeezed_title(left)
    sb = squeezed_title(right)
    if len(sa) >= 10 and len(sb) >= 10:
        similarity = max(similarity, SequenceMatcher(None, sa, sb).ratio())
    
    return similarity

class ReviewIndexMatcher:
    def __init__(self, config: ReviewIndexConfig | None = None) -> None:
        self.config = config or ReviewIndexConfig()
        
    def match(self, pages: list[PageRecord], entries: list[ReviewIndexEntry]) -> list[ReviewIndexMatch]:
        matches: list[ReviewIndexMatch] = []
        index_pages = {entry.index_pdf_page for entry in entries}
        
        for page in pages:
            if page.pdf_page in index_pages:
                continue
            
            titles = [region for region in page.regions if region.type == RegionType.ARTICLE_TITLE and region.text and not region.exclude_from_article_text]
            
            if not titles:
                continue
            
            for title_region in titles:
                scored = sorted(
                    (self._score(entry, page, title_region) for entry in entries if entry.document_id == page.document_id),
                    key=lambda m: m.score,
                    reverse=True
                )
                
                if not scored:
                    continue
                
                best = scored[0]
                second_score = (scored[1].score if len(scored) > 1 else 0.0)
                
                # Override con evidenza forte: la voce e la regione condividono
                # testata, data e pagina originale del quotidiano. Il titolo
                # puo' restare sotto la soglia di similarita' per rumore OCR,
                # ma la tripla di evidenze esclude che si tratti di un altro
                # articolo.
                strong_evidence = (
                    best.title_similarity >= self.config.strong_evidence_min_similarity
                    and {"same_publication_date", "same_source", "same_original_page"}.issubset(best.evidence)
                )
                
                if (best.score < self.config.min_match_score or (best.score - second_score < self.config.min_score_margin) or 
                    (best.contradictions and not strong_evidence)):
                    continue
                
                best.status = "matched"
                title_region.metadata["review_index_prior"] = {
                    "entry_id": best.entry_id,
                    "score": best.score,
                    "title_similarity": best.title_similarity,
                    "evidence": best.evidence,
                }
                matches.append(best)
                
        return matches
    
    def _score(self, entry: ReviewIndexEntry, page: PageRecord, title_region: Region) -> ReviewIndexMatch:
        similarity = title_similarity(entry.title, title_region.text or "")
        evidence: list[str] = []
        contradictions: list[str] = []
        score = 0.60 * similarity
        
        if similarity >= self.config.min_title_similarity:
            evidence.append("fuzzy_title")
        if similarity >= self.config.strong_title_similarity:
            evidence.append("strong_fuzzy_title")
            
        if (entry.publication_date and page.source.publication_date):
            if entry.publication_date == page.source.publication_date:
                score += 0.15
                evidence.append("same_publication_date")
            else:
                contradictions.append("different_publication_date")
                
        if entry.source and page.source.name:
            if normalize_key(entry.source) == normalize_key(page.source.name):
                score += 0.15
                evidence.append("same_source")
            else:
                evidence.append("source_not_exactly_comparable")
                
        if (entry.original_page is not None and page.source.original_page is not None):
            if entry.original_page == page.source.original_page:
                score += 0.10
                evidence.append("same_original_page")
            else:
                contradictions.append("different_original_page")
                
        if similarity < self.config.min_title_similarity:
            contradictions.append("title_similarity_too_low")
            
        return ReviewIndexMatch(
            entry_id=entry.id,
            pdf_page=page.pdf_page,
            title_region_id=title_region.metadata.get("region_id"),
            score=round(min(score, 1.0), 4),
            title_similarity=round(similarity, 4),
            evidence=evidence,
            contradictions=contradictions
        )
