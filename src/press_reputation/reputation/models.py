from datetime import date
from typing import Optional

from pydantic import BaseModel, Field

class ReputationConfig(BaseModel):
    half_life_days: float = 30.0
    bootstrap_samples: int = 10_000
    bootstrap_seed: int = 42
    
    title_prominence: float = 1.0
    subtitle_prominence: float = 0.8
    lead_prominence: float = 0.6
    body_prominence: float = 0.4
    marginal_prominence: float = 0.2
    
    missing_audience_strategy: str = "ignore_dimension"
    default_source_relevance: float = 1.0
    
class ArticleScoreInput(BaseModel):
    id: str
    
    publication_date: Optional[date] = None
    
    positive_sentences: int = 0
    negative_sentences: int = 0
    neutral_sentences: int = 0
    
    prominence: Optional[float] = None
    audience: Optional[float] = None
    source_relevance: Optional[float] = None
    
class ArticleIntermediateScore(BaseModel):
    id: str
    
    sentiment: Optional[float] = None
    prominence: Optional[float] = None
    recency: Optional[float] = None
    audience: Optional[float] = None
    source_relevance: Optional[float] = None
    
    weight: Optional[float] = None
    weighted_sentiment: Optional[float] = None
    
    warnings: list[str] = Field(default_factory=list)
    
class CoverageDistribution(BaseModel):
    positive_share: float
    neutral_share: float
    negative_share: float
    
class MediaReputationScore(BaseModel):
    score: Optional[float]
    raw_score: Optional[float]
    confidence_interval_95: Optional[tuple[float, float]] = None
    
class MediaReputationResult(BaseModel):
    media_reputation: MediaReputationScore
    visibility: float
    volume: int
    coverage_distribution: CoverageDistribution
    articles: list[ArticleIntermediateScore]
    warnings: list[str] = Field(default_factory=list)