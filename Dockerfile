
FROM ghcr.io/prefix-dev/pixi:0.81.0

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    PIXI_NO_PROGRESS=true

# Manifest e package locale.
COPY pixi.toml pixi.lock pyproject.toml ./
COPY src/ ./src/

# Installa l'ambiente riproducibile.
RUN pixi install --locked

# Script e configurazioni iniziali.
COPY scripts/ ./scripts/
COPY config/ ./config/
COPY profiles/ ./profiles/
COPY tests/ ./tests/

# Non installiamo CUDA né server LLM:
# l'inferenza è gestita da Unsloth separatamente.

CMD ["sleep", "infinity"]
