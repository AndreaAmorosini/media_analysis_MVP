"""Fixture condivise per la suite di regressione PressKG.

Fornisce dati di esempio coerenti tra i test, senza richiedere
Neo4j o un server LLM attivo.
"""

import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]

# --- Stub di dipendenze esterne se non installate ---

try:
    import neo4j  # noqa: F401
except ImportError:
    fake_neo4j = types.ModuleType("neo4j")
    fake_neo4j.GraphDatabase = object()
    sys.modules["neo4j"] = fake_neo4j

try:
    import openai  # noqa: F401
except ImportError:
    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = object
    sys.modules["openai"] = fake_openai

try:
    import pydantic  # noqa: F401
except ImportError:
    pass  # pydantic è richiesto dal progetto, non stubbabile

try:
    import json_repair  # noqa: F401
except ImportError:
    fake_jr = types.ModuleType("json_repair")
    fake_jr.loads = json.loads
    sys.modules["json_repair"] = fake_jr

try:
    import tqdm  # noqa: F401
except ImportError:
    fake_tqdm = types.ModuleType("tqdm")
    fake_tqdm.tqdm = lambda x, **kw: x
    fake_tqdm.tqdm.write = print
    sys.modules["tqdm"] = fake_tqdm


# --- Fixture dati ---

@pytest.fixture
def sample_body() -> str:
    return (
        "GORI ha ripristinato il servizio idrico a Scafati. "
        "La rete idrica è tornata funzionante dopo il guasto. "
        "Il sindaco ha ringraziato l'ente gestore."
    )


@pytest.fixture
def sample_article_dict() -> dict:
    return {
        "title": "Servizio idrico ripristinato",
        "subtitle": "A Scafati torna l'acqua",
        "publication_date": "2026-01-15",
        "source": "Il Giornale di Scafati",
        "body": (
            "GORI ha ripristinato il servizio idrico a Scafati. "
            "La rete idrica è tornata funzionante dopo il guasto."
        ),
    }


@pytest.fixture
def sample_article_file(tmp_path, sample_article_dict) -> Path:
    data_dir = tmp_path / "Data"
    data_dir.mkdir(exist_ok=True)
    f = data_dir / "2026" / "article_0001.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(sample_article_dict, ensure_ascii=False), encoding="utf-8")
    return f


@pytest.fixture
def sample_data_dir(tmp_path, sample_article_dict) -> Path:
    data_dir = tmp_path / "Data"
    data_dir.mkdir(exist_ok=True)
    f = data_dir / "2026" / "article_0001.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(sample_article_dict, ensure_ascii=False), encoding="utf-8")
    return data_dir


@pytest.fixture
def sample_extracted_graph() -> dict:
    """Un grafo estratto valido, come output del modello LLM."""
    return {
        "entities": [
            {"local_id": "e1", "name": "GORI", "type": "Company"},
            {"local_id": "e2", "name": "guasto rete idrica", "type": "Event",
             "event_type": "ServiceDisruption", "location": "Scafati"},
        ],
        "claims": [
            {
                "subject_id": "e1",
                "predicate": "RESTORES",
                "object_id": "e2",
                "quote": "GORI ha ripristinato il servizio idrico a Scafati.",
                "modality": "reported",
            }
        ],
    }


@pytest.fixture
def sample_fact() -> dict:
    """Una FACT valida come letta dal grafo Neo4j."""
    return {
        "id": "vf-001",
        "claim_id": "claim-001",
        "article_id": "article-001",
        "validation_id": "val-001",
        "batch_id": "batch-001",
        "article_found": True,
        "claim_found": True,
        "validation_found": True,
        "article_link": True,
        "claim_link": True,
        "validation_link": True,
        "claim_article_id": "article-001",
        "validation_claim_id": "claim-001",
        "validation_batch_id": "batch-001",
        "original_quote_in_body": True,
        "supporting_span_in_body": True,
        "subject_name": "GORI",
        "subject_kind": "Company",
        "object_name": "Scafati",
        "object_kind": "GPE",
        "subjects": [
            {"id": "vf-001:subject", "fact_id": "vf-001", "role": "subject",
             "name": "GORI", "kind": "Company"}
        ],
        "objects": [
            {"id": "vf-001:object", "fact_id": "vf-001", "role": "object",
             "name": "Scafati", "kind": "GPE"}
        ],
    }


@pytest.fixture
def entity_groups() -> dict:
    """Gruppi di entità per testare candidate_generation."""
    from presskg.resolution.identity.candidate_generation import normalize

    def group(name, kinds=None):
        return {
            "group_id": f"test:{name}",
            "normalized_surface": normalize(name),
            "preferred_surface": name,
            "occurrence_count": 1,
            "kind_counts": {k: 1 for k in (kinds or ["Company"])},
        }

    return {
        normalize("Gori"): group("Gori"),
        normalize("GORI S.p.A."): group("GORI S.p.A."),
        normalize("Ente Idrico Campano"): group("Ente Idrico Campano"),
        normalize("EIC"): group("EIC"),
    }
