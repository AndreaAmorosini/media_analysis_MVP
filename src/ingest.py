import asyncio
import json
import time
from pathlib import Path

from openai import AsyncOpenAI
from sentence_transformers import SentenceTransformer
from lightrag import LightRAG
from lightrag.utils import EmbeddingFunc

from config import BASE_URL, API_KEY, LLM_MODEL, EMBEDDING_MODEL, EMBEDDING_DEVICE, EMBEDDING_DIM, EMBEDDING_MAX_TOKENS, DATA_DIR, WORKDIR, OUT_DIR
import logging
from tqdm import tqdm

for lg in ("lightrag", "nano-vectordb", "httpx", "httpx2"):
    logging.getLogger(lg).setLevel(logging.WARNING)

llm_client = AsyncOpenAI(base_url=BASE_URL, api_key=API_KEY)

async def unsloth_llm(prompt: str, system_prompt: str = None, **kwargs) -> str:
    resp = await  llm_client.chat.completions.create(
        model=LLM_MODEL,
        messages =[
            {"role": "system", "content": system_prompt or "You are a precise information extraction assistant"},
            {"role": "user", "content": prompt}
        ],
        extra_body={"enable_thinking": False}
    )
    return resp.choices[0].message.content

embedder = SentenceTransformer(EMBEDDING_MODEL, device=EMBEDDING_DEVICE)

async def embed_func(texts: list[str]) -> list[list[float]]:
    return embedder.encode(texts, batch_size=16, normalize_embeddings=True)

rag = LightRAG(
    working_dir=str(WORKDIR),
    llm_model_func=unsloth_llm,
    llm_model_name=LLM_MODEL,
    llm_model_max_async=1,
    embedding_func=EmbeddingFunc(embedding_dim=EMBEDDING_DIM, max_token_size=EMBEDDING_MAX_TOKENS, func=embed_func),
    entity_extract_max_gleaning=0
)

def article_text(rel_path: Path, art: dict) -> str:
    """
    Given a relative path to the article and the article metadata, return the text of the article.
    """
    header= [f"[FONTE: {rel_path.as_posix()}]"]
    if art.get("title"):
        header.append(f"[TITOLO: {art['title']}]")
    if art.get("subtitle"):
        header.append(f"[SOTTOTITOLO: {art['subtitle']}]")
    if art.get("publication_date"):
        header.append(f"[DATA PUBBLICAZIONE: {art['publication_date']}]")
    body = art.get("body", "").strip()
    if not body:
        return ""
    return "\n".join(header) + "\n\n" + body
    
    
async def main():
    await rag.initialize_storages()
    articles = sorted(DATA_DIR.rglob("*.json"))
    if not articles:
        raise SystemExit(f"No articles found in {DATA_DIR}")
    
    total, n = 0.0, 0
    for f in tqdm(articles, desc="Ingest rassegna", unit="art"):
        art = json.loads(f.read_text(encoding="utf-8"))
        text = article_text(f.relative_to(DATA_DIR), art)
        if not text:
            print(f"- {f.relative_to(DATA_DIR)}: body vuoto, skipping")
            continue
        t0 = time.perf_counter()
        await rag.ainsert(text)
        dt = time.perf_counter() - t0
        total, n = total + dt, n + 1
        print(f"- {f.relative_to(DATA_DIR)}: {dt:.2f}s")
        
    print(f"\n{n} articoli ingestiti in {total:.0f}s ({total/max(n,1):.1f}s per articolo)")
    print(f"Grafo unificato in: {WORKDIR}")
    await rag.finalize_storages()
    
if __name__ == "__main__":
    asyncio.run(main())