
Data una **notizia in italiano**, il sistema suggerisce i **subreddit italiani più adatti** in cui
pubblicarla, senza addestrare un classificatore a classi fisse: aggiungere un subreddit significa
aggiungerlo al catalogo, non riaddestrare un modello.

```
Notizia ──► embedding ──► confronto con i testi dei subreddit ──► top 20 candidati ──► Jev ──► subreddit consigliati
```

## Indice

- [Struttura del progetto](#struttura-del-progetto)
- [Installazione](#installazione)
- [Dati](#dati)
- [Fasi del progetto](#fasi-del-progetto)
  - [Fase 1 – Scoperta dei subreddit italiani](#fase-1--scoperta-dei-subreddit-italiani) ✅
  - [Fase 2 – Pulizia dei commenti](#fase-2--pulizia-dei-commenti) ✅
  - [Fase 3 – Pulizia dei post](#fase-3--pulizia-dei-post) ⏳
  - [Fase 4 – Embedding e selezione dei top 20](#fase-4--embedding-e-selezione-dei-top-20) ⏳
  - [Fase 5 – Valutazione](#fase-5--valutazione) ⏳
  - [Fase 6 – Re-ranking con Jev](#fase-6--re-ranking-con-jev) ⏳
  - [Fase 7 – Recupero periodico tramite API Reddit](#fase-7--recupero-periodico-tramite-api-reddit) ⏳
- [Decisioni prese](#decisioni-prese)
- [Problemi noti](#problemi-noti)

## Struttura del progetto

```
subreddit_recommender/
├── README.md                  questo file
├── requirements.txt           dipendenze Python
├── data/
│   └── subreddit_italiani.csv tabella dei subreddit italiani (Fase 1)
├── src/                       codice riutilizzabile, indipendente dalla sorgente dei dati
│   ├── sorgenti.py            lettura dei dati (oggi: dump; in futuro: API Reddit)
│   └── pulizia.py             regole di pulizia dei commenti
├── 01_analisi/                script di analisi sul dump
│   ├── analisi_commenti.py    applica la pulizia e conta gli scarti per regola
│   ├── esempi_commenti.py     esempi reali per ogni regola
│   └── output/                risultati (Parquet, non versionati)
└── reddit/                    dump Reddit (facoltativo, non versionato: vedi sotto)
```

`src/` contiene solo codice che deve funzionare con qualsiasi sorgente. Le cartelle numerate
(`01_analisi/`, …) contengono gli script di ogni fase.

## Installazione

Serve Python ≥ 3.10.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Il dump Reddit (~38 GB) non è nel repository. Gli script lo cercano in:
1. la variabile d'ambiente `REDDIT_DUMP`, se impostata (cartella che contiene `comments/` e `submissions/`);
2. altrimenti `reddit/reddit_parquet/` dentro questa cartella (va bene anche un link simbolico).

```bash
export REDDIT_DUMP=/percorso/del/dump/reddit_parquet
# oppure
ln -s /percorso/del/dump/reddit reddit
```

Gli script si lanciano da questa cartella (`subreddit_recommender/`):

```bash
.venv/bin/python 01_analisi/analisi_commenti.py
.venv/bin/python 01_analisi/esempi_commenti.py
```

## Dati

Dump Reddit mensile di **giugno 2026**, convertito in Parquet sotto `reddit/reddit_parquet/`,
partizionato per `year=2026/month=06/`.

| Tipo | Righe | File | Dimensione | Stato |
|---|---|---|---|---|
| comments | 347.582.376 | 1391 | ~34 GB | completi |
| submissions | 44.257.470 | 178 | ~3,5 GB | **90 file su 178 vuoti** (copia incompleta) |

Campi principali:
- **submissions**: `id, subreddit, author, title, selftext, url, domain, score, num_comments, created_utc, over_18, is_self, stickied, distinguished`
- **comments**: `id, parent_id, link_id, subreddit, author, body, score, created_utc, distinguished, stickied`
- Collegamento commento → post: `comments.link_id = 't3_' || submissions.id`.
  `parent_id` che inizia con `t3_` indica una risposta diretta al post, `t1_` una risposta a un commento.

I campi sono gli stessi dell'API di Reddit: il codice in `src/` si potrà usare anche sui dati
scaricati dall'API.

### Come usiamo i due tipi di dati

| Uso | Submissions (post) | Commenti |
|---|---|---|
| Similarità con la notizia | **sì, è il nucleo** | come aggiunta ai post (risposte dirette, score positivo) |
| Valutazione | **sì**, i post con link a testate italiane | no |
| Scoprire i subreddit italiani | utile | **sì** (Fase 1) |
| Profilo del subreddit (tipo di post, testate linkate) | sì | – |
| Qualità e popolarità | `score`, `num_comments` | numero di autori attivi |
| Subreddit simili tra loro | – | utenti in comune |

## Fasi del progetto

### Fase 1 – Scoperta dei subreddit italiani

✅ **Fatta.** Risultato: `data/subreddit_italiani.csv` (268 subreddit).

**Metodo.** Su tutti i 347M commenti e sui post disponibili:
1. un testo di almeno 5 parole è "italiano" se almeno il 20% delle sue parole sono parole
   funzionali italiane (`che, non, sono, della, anche, perché, …`);
2. per ogni subreddit: `quota_it` = testi italiani / testi di almeno 5 parole;
3. si tengono i sub con `quota_it ≥ 0,3` e almeno 30 commenti italiani nel mese, più quelli con
   un nome che richiama l'Italia (`italy`, `italia`, `italian`, `…ITA`) e almeno 100 commenti;
4. esclusi i profili utente (`u_…`), AutoModerator, `[deleted]` e gli autori il cui nome finisce in `bot`.

**Colonne del CSV.**

| Colonna | Significato |
|---|---|
| `subreddit` | nome |
| `quota_it` | quota di commenti italiani (su quelli di almeno 5 parole) |
| `lingua` | `it` (≥ 0,6), `misto` (0,3–0,6), `altra` (< 0,3: sub sull'Italia scritti in altre lingue) |
| `n_commenti`, `n_commenti_it`, `n_autori` | attività nel mese |
| `n_post`, `quota_it_post` | post disponibili (metà del mese) e quota italiana |
| `quota_nsfw` | quota di post marcati NSFW |
| `quota_testuali` | quota di post testuali (0 = solo link, es. r/oknotizie) |
| `mediana_commenti_post` | commenti tipici per post |
| `top_domini` | 5 domini più linkati |
| `nome_italiano` | il nome richiama l'Italia |
| `da_verificare` | caso al limite, da controllare a mano |

**Da fare.** Lo script che ha generato il CSV non è più nel progetto: va ricreato in `src/` se
serve rigenerare la tabella (per esempio su un nuovo dump).

### Fase 2 – Pulizia dei commenti

✅ **Fatta** (regole in `src/pulizia.py`, analisi in `01_analisi/`).

**Subreddit considerati:** `lingua` = `it` o `misto` e `quota_nsfw ≤ 0,2` → **200 subreddit, ~912.000 commenti**.

`pulisci(df)` non elimina righe: aggiunge `testo_pulito`, `n_parole` e `motivo_scarto`
(il primo motivo che si applica, `None` se il commento si tiene).

**Normalizzazione del testo:** si tolgono le citazioni (righe che iniziano con `>`), gli URL,
le menzioni `u/…` e i simboli markdown; i link markdown `[testo](url)` diventano `testo`;
si decodificano le entità HTML (`&amp;` → `&`).

**Regole, in ordine di applicazione, con i risultati sul dump:**

| # | Motivo | Regola | Commenti | Quota |
|---|---|---|---|---|
| – | **tenuto** | | **692.668** | **76,0%** |
| 1 | `rimosso` | testo `[deleted]`, `[removed]`, `[ Removed by Reddit ]` | 18.297 | 2,0% |
| 2 | `bot_nome` | autore AutoModerator, profanitycounter o nome che finisce in `bot` | 32.749 | 3,6% |
| 3 | `bot_frase` | "I am a bot", "sono un bot", "performed automatically", "beep boop" | 52 | – |
| 4 | `moderazione` | `distinguished = moderator` oppure commento fissato in alto | 5.006 | 0,5% |
| 5 | `corto` | meno di **5 parole** dopo la normalizzazione | 162.028 | 17,8% |
| 6 | `ripetuto` | stesso testo ripetuto almeno 5 volte (spam, messaggi automatici) | 933 | 0,1% |
| 7 | `autore_ripetitivo` | autore con ≥ 20 commenti lunghi, di cui ≥ 50% già scritti | 228 | – |

Le soglie sono nella classe `Regole` di `src/pulizia.py`.

Le regole 6 e 7 guardano tutto il lotto di commenti insieme: funzionano meglio su lotti grandi
come il dump che su piccoli lotti scaricati dall'API.

**Osservazioni.** I commenti tenuti sono in italiano e sensati, ma sono per lo più conversazione:
descrivono bene il tono di un subreddit, meno i suoi argomenti. Useremo soprattutto le risposte
dirette ai post con score positivo.

### Fase 3 – Pulizia dei post

⏳ **Da fare.** Circa 34.000 post utilizzabili nei subreddit italiani (metà del mese), di cui
~14.000 link (i più vicini al nostro caso: notizie condivise).

Regole previste:
- togliere post NSFW, fissati in alto, di AutoModerator e bot, con titolo `[deleted]`/`[removed]`;
- tenere il titolo quando il testo è `[removed]`;
- togliere i post quasi vuoti e i duplicati o crosspost della stessa notizia;
- tenere `score`, `num_comments` e `domain` come informazioni in più.

Da riusare: `src/pulizia.py` (normalizzazione, regole sui bot) con una funzione per i post.

### Fase 4 – Embedding e selezione dei top 20

⏳ **Da fare.**

**Modelli candidati** (multilingue, buoni sull'italiano): `BAAI/bge-m3`, `intfloat/multilingual-e5-large`.
Con ~34.000 post non serve un database vettoriale: basta una matrice numpy.

**Due modi di confrontare la notizia con il dump**, da misurare entrambi:
1. **un vettore per subreddit**: media dei suoi post. È semplice, ma per sub generalisti
   (r/Italia) la media diventa vaga;
2. **confronto con i singoli post**: si cercano i ~200 post più simili alla notizia e si assegna
   un punteggio a ogni subreddit in base ai suoi post più vicini (es. media dei 3 migliori), non
   al numero di post, altrimenti vincono sempre i sub grandi.

I 20 subreddit con il punteggio più alto passano alla Fase 6.

### Fase 5 – Valutazione

⏳ **Da fare.**

**Dataset.** Post del dump con link a testate italiane (`ansa.it`, `ilpost.it`, `open.online`, …):
ognuno è una notizia vera con il subreddit reale in cui è stata pubblicata.
Serve pulizia: escludere bot e subreddit automatici (r/ANSAauto, r/Formula1_world) e spam.
Attenzione: `domain LIKE '%.it'` prende anche `i.redd.it`/`v.redd.it`, da escludere.

**Regola importante:** i post usati per la valutazione vanno **tolti dall'indice** della
Fase 4, altrimenti la notizia trova se stessa e i risultati sembrano perfetti.

**Metriche.**
- `recall@20`: il subreddit reale è tra i 20 candidati? (obiettivo ≥ 90%)
- `hit@1`, `hit@3`, `MRR` del ranking finale.

### Fase 6 – Re-ranking con Jev

⏳ **Da fare** (la configurazione di Jev si affronta alla fine).

**Cos'è Jev.** Modello "System One" di TypeSafe (https://typesafe.ai, documentazione:
https://docs.typesafe.ai/llms.txt). Non genera testo: riceve uno *stato* (la notizia) e domande
tipizzate, e restituisce probabilità calibrate. Si usa solo via API (`typesafe-sdk`,
`POST https://api.typesafe.ai/v1/systemone`, chiave `TYPESAFE_API_KEY`): non esiste una versione locale.

**Tipi di domanda.**
- `Choice`: sceglie un'opzione tra al massimo 255, con una probabilità per ogni opzione;
- `Noul`: domanda sì/no, restituisce la probabilità del sì;
- `Score`: valutazione su livelli ordinati.

**Uso previsto** (come nel cookbook TypeSafe "skill suggestion"), in una sola chiamata per notizia:
- una `Choice` sui 20 candidati più "nessuno" → classifica relativa;
- un `Noul` per candidato: "la notizia è adatta a r/X?" → giudizio indipendente, permette di
  consigliare più subreddit o nessuno.

**Limiti di jev-1.13.**
- L'inglese è la lingua principale: l'italiano funziona ma va testato.
- Contesto: 32k token per notizia + domanda più lunga, 64k in totale.
- Soffre di testo irrilevante: la notizia va tagliata alle parti utili.
- Le regole verificabili (NSFW, solo link) vanno controllate nel codice, non chieste a Jev.

**Costo:** $0,042 per milione di token in ingresso, output gratuito; latenza 70–500 ms.

### Fase 7 – Recupero periodico tramite API Reddit

⏳ **Futuro.** Il dump copre un solo mese e il catalogo va aggiornato nel tempo.

**Cosa offre l'API.**
- `/subreddits/search?q=…`: ricerca per parole in nome e descrizione (massimo ~1000 risultati per ricerca);
- `/r/<sub>/about`: descrizione, iscritti, `over18`, `lang` (impostato dai moderatori, poco affidabile);
- `/r/<sub>/about/rules`: regole;
- `/r/<sub>/new`, `/r/<sub>/comments`: post e commenti recenti.

**Limiti.** Serve un'app registrata con OAuth (~100 richieste al minuto). Dalla fine del 2025
le nuove app potrebbero richiedere l'approvazione di Reddit: da verificare. Nessun endpoint
filtra per lingua.

**Strategia.**
1. Partire dai subreddit noti.
2. Leggere sidebar e wiki, che spesso elencano altri sub italiani.
3. Aggiungere ricerche per parole chiave (città, regioni, temi).
4. Verificare la lingua scaricando alcuni post e passandoli alla stessa pulizia di `src/`.
5. Ripetere sui nuovi subreddit trovati.

Da aggiungere in `src/sorgenti.py`: una funzione che restituisce commenti e post nello stesso
formato del dump.

## Decisioni prese

| Tema | Decisione |
|---|---|
| Subreddit usati | `lingua` `it` o `misto`, `quota_nsfw ≤ 0,2` |
| Commenti corti | scartati sotto le 5 parole |
| Ruolo dei dati | post al centro, commenti come supporto |
| Codice | `src/` indipendente dalla sorgente (dump o API); script per fase in cartelle numerate |
| Re-ranker | Jev, da configurare alla fine |

## Problemi noti

- **90 file submissions su 178 sono vuoti**: abbiamo circa metà dei post del mese. I file vuoti
  vanno saltati prima della lettura (DuckDB fallisce su "too small to be a Parquet file").
- **Subreddit non adatti con poca quota NSFW**: la soglia del 20% esclude solo femboy_italia e
  Scapezzolate_Italiane. Restano sub per adulti o di spam con pochi post marcati NSFW (Seghe_Vip 6%,
  sborratesuvipitaliane 8%, piedi_fetish 9%, Elisa_Bernardoni__ 12%) e sub di referral o amicizie
  (CodiciAmicoITA, ReferralITA, AmicizieConoscenze). Per toglierli serve una lista di esclusione manuale.
- **`top_domini` nel CSV** contiene un dominio vuoto (es. `" (138)"` per r/Italia): da togliere
  nella Fase 3.
- **L'ambiente `reddit/.pixi/`** è rotto (python da 0 byte): non va usato, c'è `.venv`.
