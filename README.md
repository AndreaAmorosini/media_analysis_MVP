# PressKG

PressKG è una pipeline Python per costruire un knowledge graph da articoli di stampa. Legge gli articoli in formato JSON, estrae entità e claim con un modello LLM servito esternamente e registra risultati e riferimenti al testo in Neo4j.

## Requisiti

- Docker con Compose, per l'esecuzione tramite container; oppure Pixi per eseguire la pipeline in locale.
- Un modello disponibile tramite endpoint compatibile OpenAI, ad esempio Unsloth Studio. Il server del modello **non** è incluso nei container.
- Articoli JSON nella directory `Data/`, anche in sottodirectory. Ogni articolo deve avere un campo `body` non vuoto; `title`, `subtitle`, `publication_date` (o `date`) e `source` sono supportati.

## Configurazione

Copia `.env.example` in `.env` e imposta almeno `NEO4J_PASSWORD`, `UNSLOTH_MODEL` e l'URL del server del modello. La directory `Data/` deve contenere gli articoli da elaborare.

Per l'esecuzione con Docker, imposta inoltre in `.env`:

```dotenv
NEO4J_PUBLIC_HOST=localhost
UNSLOTH_BASE_URL=http://host.docker.internal:8081/v1
```

Adatta porta e indirizzo di `UNSLOTH_BASE_URL` al tuo server. L'indirizzo `127.0.0.1` dell'esempio originale, **dentro il container**, non raggiunge un server avviato sull'host.

## Esecuzione con Docker Compose

Dalla radice del progetto:

```bash
docker compose up -d --build
docker compose exec presskg pixi run preflight
docker compose exec presskg pixi run init-db
docker compose exec presskg pixi run ingest
docker compose exec presskg pixi run stats
```

`preflight` verifica la disponibilità del server del modello e di Neo4j. Per una prima prova su pochi articoli:

```bash
docker compose exec presskg pixi run python -m presskg.cli ingest --limit 10
```

L'interfaccia web di Neo4j è disponibile su `http://localhost:7474`, salvo modifica delle porte nel file `.env`. Il container `presskg` rimane in attesa: l'ingestione si avvia esplicitamente con i comandi sopra.

## Esecuzione locale con Pixi

Avvia Neo4j, rendi raggiungibile il server del modello e configura `.env` con gli indirizzi accessibili dalla macchina locale. Poi esegui:

```bash
pixi install
pixi run preflight
pixi run init-db
pixi run ingest
pixi run stats
```

Pixi utilizza Python 3.11 o 3.12; il progetto Pixi dichiara la piattaforma `linux-64`.

## Comandi utili

```bash
# Rielabora articoli già presenti, se contenuto o configurazione sono cambiati
pixi run python -m presskg.cli ingest --replace

# Elabora soltanto gli articoli assenti in Neo4j
pixi run python -m presskg.cli ingest --missing-only

# Arricchisce il grafo e normalizza i predicati
pixi run enrich
pixi run normalize-predicates
```

Le estrazioni vengono salvate in `output/extractions/`. Le directory `Data/` e `output/` non sono incluse nel repository.