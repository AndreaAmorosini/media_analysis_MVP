FROM ghcr.io/prefix-dev/pixi:latest

WORKDIR /app

# Il lockfile rende riproducibile l'installazione dell'ambiente.
COPY pixi.toml pixi.lock ./
RUN pixi install --locked

COPY src/ ./src/

# L'attività predefinita è l'ingestione; può essere sostituita con "graph".
ENTRYPOINT ["pixi", "run", "--locked"]
CMD ["ingest"]