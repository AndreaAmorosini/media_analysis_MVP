from datetime import date

from press_reputation.reputation.models import (ArticleIntermediateScore, ArticleScoreInput, CoverageDistribution, MediaReputationScore, MediaReputationResult, ReputationConfig)
from press_reputation.reputation.weights import (compute_sentiment, compute_recency, normalize_audience_values, compute_weight)

class MediaReputationScore:
    def __init__(self, config: ReputationConfig | None = None) -> None:
        self.config = config or ReputationConfig()
        
    def score(self, articles: list[ArticleScoreInput], reference_date: date, confidence_interval_95: tuple[float, float] | None = None) -> MediaReputationResult:
        audience_values = normalize_audience_values(articles)
        intermediates: list[ArticleIntermediateScore] = []
        
        weighted_sum = 0.0
        weight_sum = 0.0
        visibility = 0.0
        
        positive_articles = 0
        negative_articles = 0
        neutral_articles = 0
        
        warnings: list[str] = []
        
        for article in articles:
            sentiment = compute_sentiment(article)

            if sentiment is None:
                warnings.append(f"Article {article.id}: missing sentiment counts")
                
            if sentiment is not None:
                if sentiment > 0:
                    positive_article += 1
                elif sentiment < 0:
                    negative_articles += 1
                else:
                    neutral_articles += 1
                    
            recency = compute_recency(publication_date=article.publication_date, reference_date=reference_date, half_life_days=self.config.half_life_days)
            
            prominence = article.prominence
            audience = audience_values.get(article.id)
            source_relevance = article.source_relevance
            
            weight = compute_weight(prominence=prominence, recency=recency, audience=audience, source_relevance=source_relevance, config=self.config)
            
            weighted_sentiment = None
            
            if sentiment is not None and weight is not None:
                weighted_sentiment = sentiment * weight
                weighted_sum += weighted_sentiment
                weight_sum += weight
                
            if prominence is not None and recency is not None:
                visibility += prominence * recency * (audience or 1.0)
                
            intermediates.append(
                ArticleIntermediateScore(
                    id=article.id,
                    sentiment=sentiment,
                    prominence=prominence,
                    recency=recency,
                    audience=audience,
                    source_relevance=source_relevance,
                    weight=weight,
                    weighted_sentiment=weighted_sentiment
                )
            )
            
        raw_score = None
        score = None
        
        if weight_sum > 0:
            raw_score = weighted_sum / weight_sum
            score = 50 + 50 * raw_score
        else:
            warnings.append("Undefined MRS: sum of weights is zero")
            
        volume = len(articles)
        
        if volume > 0:
            distribution = CoverageDistribution(
                positive_share=positive_articles / volume,
                neutral_share=neutral_articles / volume,
                negative_share=negative_articles / volume
            )
        else:
            distribution = CoverageDistribution(
                positive_share=0.0,
                neutral_share=0.0,
                negative_share=0.0
            )
            
        return MediaReputationResult(
            media_reputation=MediaReputationScore(
                score=score,
                raw_score=raw_score,
                confidence_interval_95=confidence_interval_95
            ),
            visibility=visibility,
            volume=volume,
            coverage_distribution=distribution,
            articles=intermediates,
            warnings=warnings
        )