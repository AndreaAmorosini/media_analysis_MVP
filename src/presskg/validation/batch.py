"""Derivazione del batch id di validazione dai modelli effettivamente usati.

Il batch id identifica il set di ValidatedFact prodotti da una catena di
validazione: deve quindi variare quando cambiano i modelli, per non
mischiare claim convalidati da giudici diversi sotto la stessa etichetta
(l'implementazione precedente codificava l'id storico a mano, congelando
i modelli di un run ormai superato).

Convenzione (3 slot, come nell'id storico):

    <tag modello ingest>-<tag giudice primario>-<tag giudice secondario>-vN

I tag sono slug leggibili e deterministici:

    unsloth/Qwen3.8-27B-GGUF                  -> qwen38-27b
    unsloth/Qwen3.5-4B-MTP-GGUF:UD-Q4_K_XL    -> qwen35-4b-mtp
    unsloth/gemma-4-31B-it-GGUF                -> gemma-4-31b

Default: ingest = ``UNSLOTH_MODEL``, giudice primario = ``UNSLOTH_EVAL_MODEL``,
giudice secondario = ``DEFAULT_SECONDARY_JUDGE`` (overridabile).
"""

from __future__ import annotations

BATCH_VERSION = "v1"

DEFAULT_SECONDARY_JUDGE = "unsloth/gemma-4-12b-it-GGUF"

# token di coda privi di significato per il confronto fra modelli
_STRIP_TOKENS = ("gguf", "it", "instruct")


def model_tag(model_id: str | None) -> str:
    """Slug leggibile e deterministico di un id di modello.

    ``unsloth/gemma-4-31B-it-GGUF:UD-Q4_K_XL`` -> ``gemma-4-31b``
    ``unsloth/Qwen3.5-4B-MTP-GGUF``            -> ``qwen35-4b-mtp``
    """
    if not model_id:
        return "unknown"
    base = model_id.rsplit("/", 1)[-1]
    base = base.split(":", 1)[0].lower().replace(".", "")
    if base.endswith(".gguf"):
        base = base[:-5]
    tokens = [
        t for t in base.split("-")
        if t and t not in _STRIP_TOKENS
    ]
    slug = "-".join(tokens)
    return slug or "unknown"


def derive_batch_id(
    extract_model: str | None = None,
    judge_model: str | None = None,
    secondary_model: str | None = None,
    version: str | None = None,
) -> str:
    """Batch id deterministico per la tripletta di modelli della catena.

    ``extract_model`` None -> modello di ingest configurato
    (``UNSLOTH_MODEL``).
    """
    if extract_model is None:
        from presskg.config import CFG
        extract_model = CFG.model_id
    if secondary_model is None:
        secondary_model = DEFAULT_SECONDARY_JUDGE
    version = version or BATCH_VERSION
    parts = [
        model_tag(extract_model),
        model_tag(judge_model),
        model_tag(secondary_model) if secondary_model else None,
    ]
    return "-".join(p for p in parts if p) + f"-{version}"


def default_batch_id() -> str:
    """Batch id dai modelli configurati (``UNSLOTH_MODEL`` + ``UNSLOTH_EVAL_MODEL``)."""
    from presskg.config import CFG
    return derive_batch_id(judge_model=CFG.eval_model_id)
