import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("UNSLOTH_BASE_URL", "http://127.0.0.1:8888/v1")
API_KEY = os.getenv("UNSLOTH_API_KEY")
LLM_MODEL = os.getenv("UNSLOTH_MODEL", "unsloth/Qwen3.5-9B-GGUF")

EMBEDDING_MODEL = "Qwen/Qwen3-Embedding-0.6B"
EMBEDDING_DIM = 1024
EMBEDDING_MAX_TOKENS = 8192
EMBEDDING_DEVICE = "cpu"

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "Data"          # dentro: {ANNO}/{MESE}/{NOME_DOC_ORIGINALE}/Articles/Article_x.json
WORKDIR = ROOT / "rag_stampa"
OUT_DIR = ROOT / "out"