"""Trasformazione dei testi in embedding con modelli multilingue (sentence-transformers).

Ogni modello ha le sue convenzioni: alcuni vogliono un prefisso diverso per la query (la
notizia) e per i documenti (i post), altri un'istruzione che descrive il compito. `MODELLI`
raccoglie queste differenze, così il resto del codice chiama solo `codifica(testi, nome, ruolo)`.

Gli embedding restituiti sono normalizzati (norma 1): il prodotto scalare è la similarità coseno.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache

import numpy as np

# il compito, descritto per i modelli che accettano un'istruzione (vanno scritte in inglese)
ISTRUZIONE = (
    "Given an Italian news article, retrieve Reddit posts from Italian communities "
    "where the article would be shared and discussed"
)


@dataclass(frozen=True)
class Modello:
    repo: str  # nome su Hugging Face
    prefisso_query: str = ""  # "{istruzione}" viene sostituito con ISTRUZIONE
    prefisso_doc: str = ""
    trust_remote_code: bool = False


MODELLI = {
    "bge-m3": Modello("BAAI/bge-m3"),
    "e5-large": Modello(
        "intfloat/multilingual-e5-large", prefisso_query="query: ", prefisso_doc="passage: "
    ),
    "e5-large-instruct": Modello(
        "intfloat/multilingual-e5-large-instruct",
        prefisso_query="Instruct: {istruzione}\nQuery: ",
    ),
    "qwen3-0.6b": Modello(
        "Qwen/Qwen3-Embedding-0.6B", prefisso_query="Instruct: {istruzione}\nQuery:"
    ),
    # provato e tolto: Alibaba-NLP/gte-multilingual-base usa codice proprio scaricato da Hugging
    # Face (trust_remote_code), che con transformers 5 va in errore sulla GPU
}

# token letti per testo: oltre, il testo si taglia. Uguale per tutti i modelli per confrontarli
MAX_TOKEN = 512


@cache
def carica(nome: str, device: str | None = None):
    import torch
    from sentence_transformers import SentenceTransformer

    m = MODELLI[nome]
    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    modello = SentenceTransformer(
        m.repo,
        device=device,
        trust_remote_code=m.trust_remote_code,
        # mezza precisione su GPU: il doppio più veloce, differenze trascurabili sulla similarità
        model_kwargs={"torch_dtype": torch.float16} if device.startswith("cuda") else {},
    )
    modello.max_seq_length = MAX_TOKEN
    return modello


def codifica(
    testi: list[str], nome: str, ruolo: str = "doc", batch_size: int = 32, device: str | None = None
) -> np.ndarray:
    """Embedding normalizzati (float32, una riga per testo). `ruolo`: "query" o "doc"."""
    m = MODELLI[nome]
    prefisso = m.prefisso_query if ruolo == "query" else m.prefisso_doc
    prefisso = prefisso.format(istruzione=ISTRUZIONE)
    emb = carica(nome, device).encode(
        [prefisso + t for t in testi],
        batch_size=batch_size,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=len(testi) > 1000,
    )
    return emb.astype(np.float32)
