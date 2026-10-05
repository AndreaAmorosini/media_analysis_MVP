import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any
from dataclasses import dataclass
from difflib import SequenceMatcher

from press_reputation.config import EntityLookupConfig

import orjson

RESOURCE_DIR = Path(__file__).resolve().parent / "resources"

@dataclass(frozen=True)
class EntityMatch:
    kind: str #source, provider, location
    method: str #exact, alias, fuzzy
    canonical_name: str
    matched_name: str
    similarity: float
    match_coverage: float
    record: dict[str, Any] | None = None
    ambiguous: bool = False
    
    @property
    def priority(self) -> int:
        return {
            ("source", "exact"): 1,
            ("source", "alias"): 2,
            ("source", "fuzzy"): 3,
            ("provider", "exact"): 4,
            ("provider", "alias"): 5,
            ("provider", "fuzzy"): 6,
            ("location", "exact"): 7,
            ("location", "fuzzy"): 8,
        }[(self.kind, self.method)]
        
def normalize_entity_key(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join(re.findall(r"\w+", value))

def match_coverage(whole_text: str, matched_text: str) -> float:
    whole = "".join(normalize_entity_key(whole_text).split())
    matched = "".join(normalize_entity_key(matched_text).split())
    return (min(len(matched) / len(whole), 1.0) if whole else 0.0)

def can_reach_similarity(left: str, right: str, threshold: float) -> bool:
    total = len(left) + len(right)
    return (total > 0 and 2 * min(len(left), len(right)) / total >= threshold)

@lru_cache
def source_names() -> tuple[tuple[str, str, dict], ...]:
    data = load_json(RESOURCE_DIR / "newspaper.json")
    names = []
    
    for item in data.get("testate", []):
        canonical = item.get("nome")
        if not canonical:
            continue
        
        names.append(("exact", canonical, item))
        
        for alias in item.get("aliases") or []:
            if alias:
                names.append(("alias", alias, item))
                
    return tuple(names)

@lru_cache
def provider_names() -> tuple[tuple[str, str, dict], ...]:
    data = load_json(RESOURCE_DIR / "agenzie_rassegna_stampa_italia.json")
    names = []
    
    for collection_name in ("immrs_promopress", "altri_operatori_verificati"):
        for item in data.get(collection_name, []):
            canonical = item.get("nome")
            if not canonical:
                continue
            
            names.append(("exact", canonical, item))
            
            alternatives = [item.get("ragione_sociale"), *(item.get("aliases") or [])]
            
            for alias in alternatives:
                if alias:
                    names.append(("alias", alias, item))

    return tuple(names)

def _name_match(text: str, names: tuple[tuple[str, str, dict], ...], *, kind: str, method: str, threshold: float, config: EntityLookupConfig) -> EntityMatch | None:
    key = normalize_entity_key(text)
    candidates: dict[str, EntityMatch] = {}

    for name_method, name, record in names:
        named_key = normalize_entity_key(name)
        if not named_key:
            continue
        
        if method in {"exact", "alias"}:
            if name_method != method or key != named_key:
                continue
            similarity = 1.0
        else:
            if (len(key.replace(" ", "")) < config.min_fuzzy_name_chars):
                continue
            if not can_reach_similarity(key, named_key, threshold):
                continue
            similarity = SequenceMatcher(None, key, named_key).ratio()
            if similarity < threshold:
                continue
            
        canonical = record["nome"]
        candidate = EntityMatch(
            kind=kind, method=method, canonical_name=canonical, matched_name=name, similarity=similarity, match_coverage=1.0, record=record
        )
        
        previous = candidates.get(canonical)
        if (previous is None or candidate.similarity > previous.similarity):
            candidates[canonical] = candidate
            
    if not candidates:
        return None
    
    ranked = sorted(candidates.values(), key=lambda candidate: (-candidate.similarity, candidate.canonical_name.casefold()))
    best = ranked[0]
    
    if (method == "fuzzy" and len(ranked) > 1 and best.similarity - ranked[1].similarity < config.min_fuzzy_winner_margin):
        return EntityMatch(
            **{
                **best.__dict__,
                "ambiguous": True
            }
        )
        
    return best

def _location_threshold(name: str, config: EntityLookupConfig) -> float:
    length = len(normalize_entity_key(name).replace(" ", ""))
    if length <= config.location_short_max_chars:
        return config.location_fuzzy_short
    if length <= config.location_medium_max_chars:
        return config.location_fuzzy_medium
    return config.location_fuzzy_long

def _exact_location(text: str, config: EntityLookupConfig) -> EntityMatch | None:
    key = normalize_entity_key(text)
    words = key.split()
    index = entity_municipalities_index()
    candidates: list[EntityMatch] = []
    
    for start in range(len(words)):
        for end in range(start + 1, len(words) + 1):
            matched = " ".join(words[start:end])
            records = index.get(matched)
            if not records:
                continue
            
            coverage = match_coverage(key, matched)
            if coverage < config.location_min_match_coverage:
                continue
            
            candidates.append(EntityMatch(
                kind="location", method="exact", canonical_name=records[0]["comune"],
                matched_name=matched, similarity=1.0, match_coverage=coverage, record={"municipalities": records}
            ))
            
    if not candidates:
        return None
    
    return max(candidates, key=lambda item: (item.match_coverage, len(item.matched_name)))

def _fuzzy_location(text: str, config: EntityLookupConfig) -> EntityMatch | None:
    key = normalize_entity_key(text)
    if not key:
        return None
    
    municipalities = entity_municipalities_index()
    candidates: list[EntityMatch] = []
    
    for location_key, records in municipalities.items():
        if not location_key:
            continue
        threshold = _location_threshold(location_key, config)
        
        if not can_reach_similarity(key, location_key, threshold):
            continue
        
        coverage = match_coverage(key, location_key)
        if coverage < config.location_min_match_coverage:
            continue
        
        similarity = SequenceMatcher(None, key, location_key).ratio()
        if similarity < threshold:
            continue
        
        candidates.append(EntityMatch(
            kind="location", method="fuzzy", canonical_name=records[0]["comune"],
            matched_name=records[0]["comune"], similarity=similarity, match_coverage=coverage, record={"municipalities": records}
        ))
        
    if not candidates:
        return None
    
    ranked = sorted(candidates, key=lambda candidate: (-candidate.match_coverage, -candidate.similarity, candidate.canonical_name.casefold()))
    
    best = ranked[0]
    
    if (len(ranked) > 1 and best.similarity - ranked[1].similarity < config.min_fuzzy_winner_margin):
        return EntityMatch(
            **{
                **best.__dict__,
                "ambiguous": True
            }
        )
        
    return best

@lru_cache(maxsize=8129)
def _resolve_entity_cached(normalized_text: str, config_values: tuple[tuple[str, object], ...]) -> EntityMatch | None:
    config = EntityLookupConfig(**dict(config_values))
    return _resolve_entity_uncached(normalized_text, config)

def resolve_entity(text: str | None, config: EntityLookupConfig | None = None) -> EntityMatch | None:
    if not text or not text.strip():
        return None
    
    config = config or EntityLookupConfig()
    if (len(text) > config.max_entity_text_chars or len(text.split()) > config.max_entity_words):
        return None
    
    key = normalize_entity_key(text)
    if not key:
        return None
    
    config_values = tuple(sorted(config.model_dump().items()))
    
    return _resolve_entity_cached(key, config_values)


def _resolve_entity_uncached(text: str | None, config: EntityLookupConfig | None = None) -> EntityMatch | None:
    # if not text or not text.strip():
    #     return None
    
    config = config or EntityLookupConfig()
    # if (len(text) > config.max_entity_text_chars or len(text.split()) > config.max_entity_words):
    #     return None
    
    stages = (
        (source_names(), "source", "exact", 1.0),
        (source_names(), "source", "alias", 1.0),
        (source_names(), "source", "fuzzy", config.fuzzy_source_threshold),
        (provider_names(), "provider", "exact", 1.0),
        (provider_names(), "provider", "alias", 1.0),
        (provider_names(), "provider", "fuzzy", config.fuzzy_provider_threshold),
    )
    
    for names, kind, method , threshold in stages:
        result = _name_match(text, names, kind=kind, method=method, threshold=threshold, config=config)
        if result is not None:
            return result
        
    return (_exact_location(text, config=config) or _fuzzy_location(text, config=config))


def normalize_key(value: str) -> str:
    value = value.strip().lower()
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = re.sub(r"\s+", " ", value)
    return value

def load_json(path: Path) -> Any:
    return orjson.loads(path.read_bytes())



@lru_cache
def municipalities_index() -> dict[str, dict[str, Any]]:
    path = RESOURCE_DIR / "comuni_italiani.json"
    
    if not path.exists():
        return {}
    
    data = load_json(path)
    result: dict[str, list[dict[str, Any]]] = {}
    
    for item in data:
        name = item.get("comune")
        if not name:
            continue
        
        key = normalize_key(name)
        
        result.setdefault(key, []).append(item)
        
    return result

@lru_cache
def entity_municipalities_index() -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    
    for records in municipalities_index().values():
        for record in records:
            key = normalize_entity_key(record["comune"])
            result.setdefault(key, []).append(record)
            
    return result
