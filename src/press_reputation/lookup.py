import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

import orjson

RESOURCE_DIR = Path(__file__).resolve().parent / "resources"

def normalize_key(value: str) -> str:
    value = value.strip().lower()
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = re.sub(r"\s+", " ", value)
    return value

def load_json(path: Path) -> Any:
    return orjson.loads(path.read_bytes())

@lru_cache
def newspaper_index() -> dict[str, dict[str, Any]]:
    path = RESOURCE_DIR / "newspaper.json"
    
    if not path.exists():
        return {}
    
    data = load_json(path)
    result: dict[str, dict[str, Any]] = {}
    
    for item in data.get("testate", []):
        name = item.get("nome")
        if not name:
            continue
        
        result[normalize_key(name)] = item
        
    return result

@lru_cache
def press_review_provider_index() -> dict[str, dict[str, Any]]:
    path = RESOURCE_DIR / "agenzie_rassegna_stampa_italia.json"
    
    if not path.exists():
        return {}
    
    data = load_json(path)
    result: dict[str, dict[str, Any]] = {}
    
    collections = [data.get("immrs_promopress", []), data.get("operatori_aggiuntivi", [])]
    
    for collection in collections:
        for item in collection:
            names = [item.get("nome"), item.get("ragione_sociale")]
            names.extend(item.get("aliases" or []))
            
            for name in names:
                if name:
                    result[normalize_key(name)] = item
                    
    return result


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
def municipality_first_token_index() -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    
    for municipality in municipalities_index():
        first = municipality.split()[0]
        result.setdefault(first, set()).add(municipality)
        
    return result


def get_newspaper(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    
    key = normalize_key(text)
    return newspaper_index().get(key)

def is_known_newspaper(text: str | None) -> bool:
    return get_newspaper(text) is not None

def get_press_review_provider(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    
    key = normalize_key(text)
    return press_review_provider_index().get(key)

def is_press_review_provider(text: str | None) -> bool:
    return get_press_review_provider(text) is not None

def find_municipalities(text: str) -> list[dict[str, Any]]:
    normalized = normalize_key(text)
    tokens = re.findall(r"\w+", normalized)
    
    if not tokens:
        return []
    
    first_token_index = municipality_first_token_index()
    municipalities = municipalities_index()
    
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    
    candidates: set[str] = set()
    
    for token in tokens:
        candidates.update(first_token_index.get(token, set()))
        
    ordered_candidates = sorted(
        candidates, key=lambda value: len(value.split()), reverse=True
    )
    
    for candidate in ordered_candidates:
        pattern = rf"\b{re.escape(candidate)}\b"
        
        if not re.search(pattern, normalized):
            continue
        
        for item in municipalities.get(candidate, []):
            identity = (item.get("comune", ""), item.get("provincia", ""), item.get("regione", ""))
            
            if identity in seen:
                continue
            
            seen.add(identity)
            enriched_item = {**item, "matched_name": candidate}
            found.append(enriched_item)
                
    return found
    