# Media Analysis MVP — knowledge graph della rassegna stampa

Questo progetto costruisce un knowledge graph a partire da articoli di rassegna stampa **già estratti in file JSON**. Usa LightRAG per indicizzare i testi e ricavare entità e relazioni, un modello di embedding eseguito localmente su CPU e un modello linguistico accessibile tramite API compatibile con OpenAI.

Sono disponibili tre operazioni:

- **Ingestione:** legge gli articoli da `Data/` e salva lo stato LightRAG in `rag_stampa/`.
- **Interrogazione:** esegue due domande di esempio sul grafo indicizzato.
- **Ispezione:** stampa statistiche del grafo e genera una visualizzazione HTML in `out/kg_rassegna.html`.

> Il README precedente descriveva l'estrazione di articoli dai PDF. Questa versione del progetto non include quella pipeline: per l'ingestione servono JSON contenenti articoli già estratti. Non calcola un Media Reputation Score.

## Requisiti

- Linux x86-64 e [Pixi](https://pixi.sh/) per l'esecuzione locale; **oppure** Docker con Docker Compose.
- Un server LLM raggiungibile tramite API compatibile con OpenAI. La configurazione predefinita punta a `http://127.0.0.1:8888/v1` e al modello `unsloth/Qwen3.5-9B-GGUF`.
- Articoli JSON nella directory `Data/`. Al primo utilizzo, `sentence-transformers` deve poter scaricare il modello di embedding `Qwen/Qwen3-Embedding-0.6B`.

Le dipendenze del progetto sono definite in `pixi.toml`; `pixi.lock` è usato per l'installazione riproducibile nell'immagine Docker.

## Preparazione dei dati

Lo script cerca ricorsivamente tutti i file `*.json` sotto `Data/`. Ogni file deve rappresentare un articolo e contenere almeno un campo `body` non vuoto. I campi `title`, `subtitle` e `publication_date` sono facoltativi e vengono aggiunti al testo indicizzato quando presenti.

Struttura prevista, a titolo di esempio:

```text
Data/
└── 2026/
    └── 10/
        └── documento/
            └── Articles/
                └── Article_1.json
```

Esempio di contenuto di un articolo:

```json
{
  "title": "Titolo dell'articolo",
  "subtitle": "Sottotitolo facoltativo",
  "publication_date": "2026-10-23",
  "body": "Testo dell'articolo..."
}
```

I JSON senza `body` valorizzato vengono saltati. Se non viene trovato alcun JSON, l'ingestione termina con un errore.

## Configurazione del modello linguistico

Crea un file `.env` nella radice del progetto:

```dotenv
UNSLOTH_BASE_URL=http://127.0.0.1:8888/v1
UNSLOTH_API_KEY=la-tua-chiave-o-un-valore-accettato-dal-server
UNSLOTH_MODEL=unsloth/Qwen3.5-9B-GGUF
```

`UNSLOTH_BASE_URL` deve essere un endpoint compatibile con l'API Chat Completions di OpenAI. Il server LLM **non è avviato** da questo progetto e deve essere disponibile prima dell'ingestione o delle query.

Per Docker Compose, se il server LLM gira sulla macchina host, imposta invece:

```dotenv
UNSLOTH_BASE_URL=http://host.docker.internal:8888/v1
```

Il Compose configura `host.docker.internal` per raggiungere l'host dal container. Se il server LLM gira altrove, usa un URL raggiungibile dal container.

**Non pubblicare `.env`:** verifica che sia escluso dal controllo versione prima di aggiungere o pubblicare file nel repository.

## Avvio in locale con Pixi

Esegui i comandi dalla radice del progetto:

```bash
mkdir -p Data rag_stampa out
pixi install --locked
pixi run ingest
```

Dopo l'ingestione:

```bash
pixi run query
pixi run graph
```

`query` esegue due domande predefinite, scritte in `src/query.py`. `graph` legge il grafo generato dall'ingestione e crea `out/kg_rassegna.html`, apribile con un browser. Non occorre avviare un server web per visualizzare il file HTML.

## Avvio con Docker Compose

Dopo aver preparato `Data/` e `.env`, dalla radice del progetto:

```bash
mkdir -p Data rag_stampa out
docker compose build
docker compose run --rm app
```

Il comando predefinito del container è `ingest`. Per le altre operazioni:

```bash
docker compose run --rm app query
docker compose run --rm app graph
```

La configurazione Compose monta:

| Directory host | Utilizzo nel container |
| --- | --- |
| `./Data` | Articoli JSON in sola lettura |
| `./rag_stampa` | Stato persistente di LightRAG |
| `./out` | Visualizzazione HTML generata |

Per questo flusso si usa `docker compose run`: gli script eseguono un'operazione e terminano; non avviano un servizio HTTP permanente.

## Output e note operative

- `rag_stampa/` contiene lo stato dell'indice e il grafo LightRAG, incluso `graph_chunk_entity_relation.graphml`.
- `out/kg_rassegna.html` è la visualizzazione interattiva prodotta da `graph`.
- `src/ingest.py` importa tutti i JSON trovati in `Data/` e li invia a LightRAG uno per volta.
- L'embedding è configurato per usare la CPU. L'ingestione può richiedere tempo in funzione del numero e della lunghezza degli articoli e della velocità del server LLM.
- La task `ui` presente in `pixi.toml` avvia `lightrag-server`, ma non è collegata esplicitamente alla configurazione LightRAG usata dagli script né esposta dal Compose: non è necessaria per il flusso documentato sopra.