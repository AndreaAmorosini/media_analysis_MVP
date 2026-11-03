# Schema dati: rassegna e articolo

È il formato su cui lavora il progetto: frontend, classificazione e (in futuro) la chat leggono solo questo.
Ciò che produce la pipeline di estrazione viene convertito in questo schema da un adattatore;
se la pipeline cambia, si riscrive solo l'adattatore.

Un file JSON per rassegna: `dati/<id_rassegna>/rassegna.json`.

## Rassegna

```jsonc
{
  "schema": 1,                                   // versione di questo schema
  "id": "2023_Febbraio_18_02_Evidenze_Rassegna_18022023",   // nome della cartella in dati/
  "pdf": "2023/Febbraio/18.02/Evidenze Rassegna 18022023.pdf", // percorso nell'archivio Data
  "data": "2023-02-18",                          // data della rassegna (la più recente tra gli articoli)
  "pagine": [                                    // pagine del PDF occupate da articoli
    { "n": 2, "larghezza": 595, "altezza": 842,  // n da 1; dimensioni in punti PDF
      "immagine": "pagine/page_002.jpg" }        // relativa alla cartella della rassegna
  ],
  "articoli": [ /* Articolo */ ]
}
```

## Articolo

```jsonc
{
  "id": "Evidenze_rassegna_23_apr_25/1",         // <rassegna>/<numero d'ordine nel sommario>
  "titolo": "Ripartono i lavori a via Roma «Nessun disagio alla viabilità»",
  "occhiello": "Gragnano",                       // facoltativo, null se assente
  "testata": "Metropolis",
  "data": "2025-04-23",                          // data di pubblicazione
  "pagina_originale": 8,                         // pagina sul giornale, null se ignota
  "autore": "Antonio Cesarano",                  // null se assente
  "sezione": "STAMPA LOCALE",                    // sezione del sommario (STAMPA LOCALE, WEB, ...)
  "tipo": "stampa",                              // "stampa" | "web"
  "url": null,                                   // web: articolo originale, se scritto nella pagina
  "pagine_rassegna": [2],                        // pagine del PDF (da 1) occupate dall'articolo
  "testo": "Gragnano. Partiti i lavori alla rete fognaria: …",   // corpo, paragrafi separati da "\n\n"
  "frasi": [ /* Frase */ ],
  "sentiment": { /* Sentiment, aggregato dalle frasi */ },
  "estrazione": { "metodo": "rapidocr-it-ppocrv6/colonne-ricucite" }  // o "euristica", "llm/qwen2.5:7b"
}
```

## Frase

```jsonc
{
  "n": 0,                                        // indice nell'articolo
  "testo": "Gragnano.",
  "inizio": 0, "fine": 9,                        // posizione nel campo "testo" dell'articolo
  "posizioni": [                                 // blocchi della pagina che contengono la frase
    { "pagina": 2, "bbox": [114.9, 153.2, 234.1, 452.8] }   // [x0, y0, x1, y1], punti PDF, origine in alto a sinistra
  ],
  "sentiment": { /* Sentiment */ }
}
```

Le posizioni sono a livello di riga (da `estrazione/estrai_rassegna.py`): una frase su più righe o
più colonne ha una posizione per riga. Nel vecchio adattatore (`backend/adattatore_evidenze.py`)
erano a livello di blocco.

## Sentiment

```jsonc
{
  "etichetta": "positivo",                       // classe con probabilità più alta
  "prob": { "positivo": 0.71, "neutro": 0.25, "negativo": 0.04 },
  "modello": "laya-multilingual@7b928d8/v2-choice-it",
  "troncato": false,                             // il testo superava il contesto del modello
  "metodo": "frase"                              // "frase" | "media_frasi" (per l'articolo)
}
```

Per l'articolo, `metodo: "media_frasi"`: media delle probabilità delle frasi, etichetta = classe con
media più alta. Regola provvisoria: va verificata sui dati reali (con molte frasi neutre la media tende
a "neutro").
