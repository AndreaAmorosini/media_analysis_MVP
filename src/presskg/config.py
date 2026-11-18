from dataclasses import dataclass
from pathlib import Path

import os
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[2]

load_dotenv(ROOT / ".env", override=False)

PROFILE = os.getenv("PRESSKG_PROFILE", "4070")

if PROFILE not in {"4070", "5090"}:
    raise ValueError("PRESSKG_PROFILE deve essere 4070 o 5090")

load_dotenv(ROOT / "profiles" / f"{PROFILE}.env", override=False)


def root_path(raw: str) -> Path:
    path = Path(raw).expanduser()
    return path if path.is_absolute() else ROOT / path


@dataclass(frozen=True)
class Settings:
    neo4j_uri: str = os.getenv("NEO4J_URI", "bolt://127.0.0.1:7687")
    neo4j_user: str = os.getenv("NEO4J_USER", "neo4j")
    neo4j_password: str = os.getenv("NEO4J_PASSWORD", "")
    neo4j_database: str = os.getenv("NEO4J_DATABASE", "neo4j")
    base_url: str = os.getenv("UNSLOTH_BASE_URL", "http://127.0.0.1:8888/v1")
    api_key: str = os.getenv("UNSLOTH_API_KEY", "local")
    model_id: str = os.getenv("UNSLOTH_MODEL", "")
    eval_model_id: str = os.getenv("UNSLOTH_EVAL_MODEL", "")
    data_dir: Path = root_path(os.getenv("PRESSKG_DATA_DIR", "Data"))
    output_dir: Path = root_path(os.getenv("PRESSKG_OUTPUT_DIR", "output/extractions"))
    window_chars: int = int(os.getenv("PRESSKG_WINDOW_CHARS", "6500"))
    window_overlap: int = int(os.getenv("PRESSKG_WINDOW_OVERLAP", "500"))
    max_output_tokens: int = int(os.getenv("PRESSKG_MAX_OUTPUT_TOKENS", "2600"))
    json_mode: bool = os.getenv("PRESSKG_JSON_MODE", "false").lower() in {"true", "1", "yes"}
    request_timeout: float = float(os.getenv("UNSLOTH_REQUEST_TIMEOUT", "600"))


CFG = Settings()