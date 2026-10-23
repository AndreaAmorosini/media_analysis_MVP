import asyncio

from lightrag import LightRAG, QueryParam
from lightrag.utils import EmbeddingFunc
from openai import AsyncOpenAI
from sentence_transformers import SentenceTransformer

from config import (
    API_KEY,
    BASE_URL,
    EMBEDDING_DEVICE,
    EMBEDDING_DIM,
    EMBEDDING_MAX_TOKENS,
    EMBEDDING_MODEL,
    LLM_MODEL,
    WORKDIR,
)


llm_client = AsyncOpenAI(base_url=BASE_URL, api_key=API_KEY)
embedder = SentenceTransformer(EMBEDDING_MODEL, device=EMBEDDING_DEVICE)


async def unsloth_llm(
    prompt: str, system_prompt: str | None = None, **kwargs
) -> str:
    response = await llm_client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {
                "role": "system",
                "content": system_prompt
                or "You are a precise information extraction assistant",
            },
            {"role": "user", "content": prompt},
        ],
        extra_body={"enable_thinking": False},
    )
    return response.choices[0].message.content or ""


async def embed_func(texts: list[str]):
    return embedder.encode(
        texts, batch_size=16, normalize_embeddings=True
    )


async def main():
    rag = LightRAG(
        working_dir=str(WORKDIR),
        llm_model_func=unsloth_llm,
        llm_model_name=LLM_MODEL,
        llm_model_max_async=1,
        embedding_func=EmbeddingFunc(
            embedding_dim=EMBEDDING_DIM,
            max_token_size=EMBEDDING_MAX_TOKENS,
            func=embed_func,
        ),
        entity_extract_max_gleaning=0,
    )

    await rag.initialize_storages()
    try:
        for question in [
            "Quali aziende sono citate e in relazione a quali temi?",
            "Che relazioni legano l'azienda target agli altri attori della rassegna?",
        ]:
            print(f"\n{'=' * 80}\nQ: {question}\n{'=' * 80}")
            result = await rag.aquery(
                question, param=QueryParam(mode="hybrid")
            )
            print(result)
    finally:
        await rag.finalize_storages()


if __name__ == "__main__":
    asyncio.run(main())