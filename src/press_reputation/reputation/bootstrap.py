import random
from datetime import date

from press_reputation.reputation.models import ArticleScoreInput, ReputationConfig
from press_reputation.reputation.scoring import MediaReputationScore

def bootstrap_mrs_ci(articles: list[ArticleScoreInput], reference_date: date, config: ReputationConfig) -> tuple[float, float] | None:
    if not articles:
        return None
    
    rng = random.Random(config.bootstrap_seed)
    values: list[float] = []
    
    scorer = MediaReputationScore(config=config)
    
    for _ in range(config.bootstrap_samples):
        sample = [rng.choice(articles) for _ in articles]
        result = scorer.score(sample, reference_date)
        
        score = result.media_reputation.score
        
        if score is not None:
            values.append(score)
            
    if not values:
        return None
    
    values.sort()
    
    low_index = int(0.025 * len(values))
    high_index = int(0.975 * len(values)) - 1
    
    return values[low_index], values[high_index]