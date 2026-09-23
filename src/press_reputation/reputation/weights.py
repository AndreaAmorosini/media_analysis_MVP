import math
from datetime import date

from press_reputation.reputation.models import (ArticleScoreInput, ReputationConfig)

def compute_sentiment(article: ArticleScoreInput) -> float | None:
    total = (article.positive_sentences + article.negative_sentences + article.neutral_sentences)
    
    if total == 0:
        return None
    
    return (article.positive_sentences - article.negative_sentences) / total

def compute_recency(publication_date: date | None, reference_date: date, half_life_days: float) -> float | None:
    if publication_date is None:
        return None
    
    age_days = max((reference_date - publication_date).days, 0)
    return 2 ** (-(age_days / half_life_days))

def normalize_audience_values(articles: list[ArticleScoreInput]) -> dict[str, float]:
    available = [article.audience for article in articles if article.audience is not None and article.audience > 0]
    
    if not available:
        return {}
    
    max_log = max(math.log1p(value) for value in available)
    
    if max_log <= 0:
        return {}
    
    result = {}
    
    for article in articles:
        if article.audience is None or article.audience <= 0:
            continue
        
        result[article.id] = math.log1p(article.audience) / max_log
        
    return result

def compute_weight(prominence: float | None, recency: float | None, audience: float | None, source_relevance: float | None, config: ReputationConfig) -> float | None:
    factors = []
    
    if prominence is not None:
        factors.append(prominence)
        
    if recency is not None:
        factors.append(recency)
        
    if audience is not None:
        factors.append(audience)
    elif config.missing_audience_strategy != "ignore_dimension":
        return None
    
    if source_relevance is not None:
        factors.append(source_relevance)
    else:
        factors.append(config.default_source_relevance)
        
    if not factors:
        return None
    
    weight = 1.0
    
    for factor in factors:
        weight *= factor
        
    return weight
    
