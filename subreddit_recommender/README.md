
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
  - [Fase 3 – Pulizia dei post](#fase-3--pulizia-dei-post) ✅
  - [Fase 4 – Embedding e selezione dei top 20](#fase-4--embedding-e-selezione-dei-top-20) ✅ (confronto modelli)
  - [Fase 5 – Valutazione](#fase-5--valutazione) ✅ (prima versione)
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
│   ├── subreddit_italiani.csv tabella dei subreddit italiani (Fase 1)
│   ├── subreddit_esclusi.csv  subreddit tolti a mano (adulti, referral, spam…)
│   └── subreddit_puliti.csv   catalogo dei subreddit rimasti dopo la pulizia dei post (Fase 3)
├── src/                       codice riutilizzabile, indipendente dalla sorgente dei dati
│   ├── sorgenti.py            lettura dei dati (oggi: dump; in futuro: API Reddit)
│   ├── pulizia.py             regole di pulizia di commenti e post
│   ├── embedding.py           modelli di embedding e codifica dei testi (Fase 4)
│   ├── candidati.py           punteggio dei subreddit per una notizia, top N (Fase 4)
│   └── valutazione.py         dataset di valutazione e metriche (Fase 5)
├── 01_analisi/                script di analisi sul dump
│   ├── analisi_commenti.py    applica la pulizia e conta gli scarti per regola
│   ├── esempi_commenti.py     esempi reali per ogni regola
│   ├── analisi_post.py        lo stesso per i post (Fase 3)
│   ├── esempi_post.py
│   └── output/                risultati (Parquet, non versionati)
├── 02_embedding/
│   ├── confronta_modelli.py   embedding dei post con più modelli e confronto sulla valutazione
│   ├── suggerisci.py          top 20 subreddit per una notizia (titolo + descrizione)
│   └── output/                embedding in cache e risultati.csv (non versionati)
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
.venv/bin/python 01_analisi/analisi_post.py
.venv/bin/python 01_analisi/esempi_post.py
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

✅ **Fatta** (regole in `src/pulizia.py`, analisi in `01_analisi/`).

**Subreddit considerati:** gli stessi della Fase 2, meno i **18** di `data/subreddit_esclusi.csv`
(per adulti con poca quota NSFW, referral, spam di casinò, offerte automatiche, annunci di
amicizia) → **181 subreddit, 21.744 post** (metà del mese). Da soli, gli esclusi valevano
14.000 post.

`pulisci_post(df)` non elimina righe. Aggiunge:

| Colonna | Significato |
|---|---|
| `titolo_pulito`, `corpo_pulito` | normalizzati come i commenti; il corpo è vuoto se `[removed]`/`[deleted]` |
| `testo_pulito` | titolo + corpo: è il testo da usare per gli embedding |
| `n_parole` | parole di `testo_pulito` |
| `tipo` | `testo`, `link`, `immagine`, `video`, `galleria`, `crosspost`, `altro` |
| `dominio` | dominio del link esterno (senza `www.`), vuoto per i contenuti ospitati su Reddit. Si ricava dall'`url`: il campo `domain` del dump per alcuni video `v.redd.it` contiene il permalink del post |
| `conf_italiano` | probabilità (0–1) che il testo sia italiano, dal rilevatore [`lingua`](https://github.com/pemistahl/lingua-py) |
| `motivo_scarto` | primo motivo che si applica, `None` se il post si tiene |

**Regole, in ordine di applicazione, con i risultati sul dump:**

| # | Motivo | Regola | Post | Quota |
|---|---|---|---|---|
| – | **tenuto** | | **16.130** | **74,2%** |
| 1 | `rimosso` | titolo o testo `[ Removed by Reddit/moderator ]` | 104 | 0,5% |
| 2 | `rimosso_mod` | testo `[removed]`: tolto dai moderatori | 2.187 | 10,1% |
| 3 | `nsfw` | `over_18` | 234 | 1,1% |
| 4 | `bot_nome` | come nei commenti (AutoModerator, nome che finisce in `bot`) | 74 | 0,3% |
| 5 | `bot_frase` | come nei commenti | 2 | – |
| 6 | `moderazione` | `distinguished = moderator` o fissato in alto | 33 | 0,2% |
| 7 | `corto` | titolo + testo sotto le **4 parole** ("Opinioni?", "Per non dimenticare") | 1.263 | 5,8% |
| 8 | `non_italiano` | `conf_italiano < 0,2` | 859 | 4,0% |
| 9 | `ripetuto` | stesso autore, stesso post almeno 5 volte (autopromozione in molti sub) | 79 | 0,4% |
| 10 | `duplicato` | stesso post ripubblicato nello stesso sub: si tiene il primo | 169 | 0,8% |
| 11 | `sub_piccolo` | subreddit con meno di **20 post** rimasti dopo le regole precedenti | 610 | 2,8% |

Le soglie sono nella classe `RegolePost` di `src/pulizia.py`.

**Scelte.**
- **I post `[removed]` si scartano** (`tieni_rimossi_dai_mod = False`): se i moderatori li hanno
  tolti, il sub li ha giudicati non adatti, quindi non lo descrivono e non vanno nella valutazione.
  Il costo è alto su r/italy, che toglie molti post e li rimanda al thread giornaliero
  (440 rimossi, 252 tenuti).
- I post di autori `[deleted]` si tengono: il titolo resta valido.
- `ripetuto` conta per autore: la stessa notizia pubblicata da utenti diversi in sub diversi
  si tiene (esempio: un articolo di ilpost.it su oknotizie, TuttoItalia, Italia e italy).
  Nello stesso sub resta una sola copia (`duplicato`).
- **Lingua**: si usa `lingua` (confronto fra italiano, inglese, spagnolo, portoghese, francese,
  tedesco, rumeno; ~2 s su tutti i post) invece delle parole funzionali della Fase 1, che sui
  titoli brevi sbagliavano spesso. La soglia 0,2 tiene i titoli misti ("Che ne pensate di Steam
  Machine?", "Ebola in Congo 2026: l'epidemia…"); sotto è quasi tutto inglese (turisti e
  studenti stranieri su r/italy, r/napoli, r/milano, r/Universitaly). Vicino alla soglia gli
  errori ci sono in entrambe le direzioni, ma sono pochi.
- **Subreddit piccoli**: la soglia si applica ai post rimasti dopo tutte le altre regole.
  Sul dump attuale (metà del mese) 20 post corrispondono a circa 40 al mese. Escono 62 sub
  (Veneto, brescia, Bergamo, memesITA, adhd_italia, …). Con il dump completo la soglia
  andrà ricontrollata.
- Il testo automatico "This post contains content not supported on old Reddit…" si toglie
  nella normalizzazione.

**Risultato.** 16.130 post in **118 subreddit** (mediana 58 post per sub, 71 sub con almeno 50):
9.904 testuali, poi immagini, link, gallerie, video e crosspost. I link esterni sono 1.323, di cui
765 verso domini `.it` (ansa.it 81, ilpost.it 46, rainews.it 40, ilfattoquotidiano.it 36…):
sono la base della Fase 5.

**Catalogo.** `analisi_post.py` salva anche `data/subreddit_puliti.csv`, una riga per ognuno dei
118 subreddit: è l'elenco da usare nella Fase 4.

| Colonna | Significato |
|---|---|
| `subreddit` | nome |
| `n_post`, `n_autori` | post tenuti e autori distinti |
| `quota_testuali`, `quota_link`, `quota_media` | quota di post testuali, con link esterno, immagini/gallerie/video |
| `mediana_commenti`, `mediana_score` | coinvolgimento tipico |
| `quota_rimossi` | post rimossi dai moderatori / (tenuti + rimossi): rischio di rimozione |
| `top_domini` | 5 domini esterni più linkati |

**Osservazioni.** Per immagini, gallerie e video il testo è solo il titolo: descrivono il sub
meno dei post testuali e dei link.

### Fase 4 – Embedding e selezione dei top 20

✅ **Confronto dei modelli fatto** (`02_embedding/confronta_modelli.py`).
**Scelta: `intfloat/multilingual-e5-large-instruct` con il metodo del centroide.**

I 16.130 post puliti (`testo_pulito`) diventano embedding; la notizia pure, con lo stesso modello.
Con questi numeri non serve un database vettoriale: basta una matrice numpy.

**Due metodi per il punteggio dei subreddit** (`src/candidati.py`):
1. **centroide**: un vettore per subreddit, la media dei suoi post;
2. **vicini k**: confronto con i singoli post, il punteggio di un sub è la media delle sue k
   similarità migliori (non conta il numero di post, altrimenti vincono i sub grandi).

**Modelli** (`src/embedding.py`), tutti con testi tagliati a 512 token, float16 su GPU:

| Nome | Modello | Prefissi | Tempo (GPU) |
|---|---|---|---|
| `bge-m3` | BAAI/bge-m3 | – | 312 s |
| `e5-large` | intfloat/multilingual-e5-large | `query:` / `passage:` | 291 s |
| `e5-large-instruct` | intfloat/multilingual-e5-large-instruct | istruzione sulla query | 172 s |
| `qwen3-0.6b` | Qwen/Qwen3-Embedding-0.6B | istruzione sulla query | 405 s |

`Alibaba-NLP/gte-multilingual-base` è stato provato e tolto: richiede `trust_remote_code` e con
`transformers` 5 va in errore sulla GPU.

**Risultati** (824 articoli, metodo migliore per modello; tutto in `02_embedding/output/risultati.csv`):

| Modello | Metodo | r@1 | r@5 | r@10 | r@20 | mrr | macro_r@20 |
|---|---|---|---|---|---|---|---|
| baseline | sempre i sub più attivi | 0,415 | 0,786 | 0,862 | 0,934 | 0,574 | 0,303 |
| bge-m3 | vicini k=3 | 0,391 | 0,705 | 0,825 | 0,909 | 0,527 | 0,809 |
| e5-large | vicini k=3 | 0,376 | 0,721 | 0,835 | 0,936 | 0,525 | 0,741 |
| **e5-large-instruct** | **centroide** | 0,322 | **0,779** | **0,921** | **0,967** | 0,500 | **0,847** |
| qwen3-0.6b | centroide | 0,273 | 0,624 | 0,811 | 0,920 | 0,425 | 0,841 |

**Peso dell'attività.** Dopo la similarità si aggiunge un punteggio di attività del subreddit,
così a parità di pertinenza si preferiscono le community più vive (`src/candidati.py`):

- **attività** = combinazione pesata (`PESI_ATTIVITA`) di `n_autori` (0,4), `mediana_commenti`
  (0,3), `mediana_score` (0,3) in scala logaritmica, meno `quota_rimossi` (−0,3); standardizzata;
- **punteggio finale** = similarità standardizzata per notizia + α × attività, calcolato su
  **tutti** i 118 sub prima di prendere i primi 20 (così un sub grande appena fuori può rientrare).

| α | r@1 | r@3 | mrr | r@20 | macro_r@20 | attività media top 5 |
|---|---|---|---|---|---|---|
| 0 (solo similarità) | 0,322 | 0,572 | 0,500 | 0,967 | 0,847 | 0,32 |
| 0,1 | 0,335 | 0,630 | 0,517 | 0,960 | 0,841 | 0,51 |
| **0,2** (default) | 0,351 | 0,653 | 0,531 | 0,956 | 0,825 | 0,69 |
| 0,3 | 0,373 | 0,669 | 0,545 | 0,949 | 0,823 | 0,82 |
| 0,5 | 0,367 | 0,672 | 0,539 | 0,934 | 0,781 | 1,02 |
| 1,0 | 0,235 | 0,536 | 0,426 | 0,903 | 0,685 | 1,44 |

Un peso piccolo migliora le prime posizioni perdendo poco sui 20 candidati; oltre 0,5 l'attività
prevale e peggiora tutto. Default **α = 0,2** (`--alfa` in `suggerisci.py` per cambiarlo).
Attenzione: la valutazione dice dove le persone hanno pubblicato, non dove il post ha avuto più
successo; il guadagno in r@1 dice anche che le persone pubblicano di più nei sub attivi.

**Uso.** Dopo `confronta_modelli.py` (che calcola gli embedding dei post):

```bash
.venv/bin/python 02_embedding/suggerisci.py "titolo della notizia" "descrizione o testo" [--n 20] [--alfa 0.2]
```

**Conclusioni.**
- Con `e5-large-instruct` il sub giusto è tra i 20 candidati nel **96,7%** dei casi (obiettivo
  ≥ 90%), e il risultato tiene anche sui sub piccoli (`macro_r@20` 0,85).
- La baseline ha un buon r@20 solo perché 4 sub raccolgono il 75% degli articoli; sui sub
  piccoli fallisce (`macro_r@20` 0,30).
- Nessun modello batte la baseline su r@1: gli embedding trovano i candidati, la scelta finale
  spetta a Jev (Fase 6).
- k=1 è sempre il metodo peggiore: un solo post simile è troppo rumoroso.
- Incertezza: circa ±1,5 punti su r@20; le differenze di `macro_r@20` sotto ~0,05 non sono
  significative (66 sub, molti con pochi articoli).

### Fase 5 – Valutazione

✅ **Prima versione** (`src/valutazione.py`).

**Dataset.** Post puliti con un link a un articolo esterno, escluse le piattaforme (YouTube,
Instagram, TikTok, Spotify, Steam, GitHub, …): **824 articoli in 66 subreddit**. Ogni articolo è
una notizia vera; i sub in cui è stato pubblicato sono la risposta giusta (206 articoli sono in
più sub: vale uno qualsiasi). La query è **solo il titolo**: in produzione ci sarà anche il testo,
quindi i numeri sono prudenti.

**Esclusione:** per ogni articolo, i post che lo contengono (stesso url o stesso titolo) si
tolgono dal confronto, altrimenti la notizia trova se stessa.

**Metriche.**
- `r@k`: un sub giusto è tra i primi k? `r@20` è la più importante (obiettivo ≥ 90%);
- `mrr`: media di 1/posizione del primo sub giusto;
- `macro_r@20`: `r@20` medio per subreddit, perché il 75% degli articoli è in 4 sub
  (oknotizie, Italia, TuttoItalia, italy);
- una **baseline** senza testo (sempre i sub con più articoli) per capire quanto aggiungono
  gli embedding.

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
| Post corti | scartati sotto le 4 parole (titolo + testo) |
| Post non in italiano | scartati (`lingua`, confidenza dell'italiano < 0,2) |
| Subreddit piccoli | esclusi sotto i 20 post puliti |
| Post rimossi dai moderatori | scartati (configurabile in `RegolePost`) |
| Subreddit non adatti | lista manuale in `data/subreddit_esclusi.csv` |
| Ruolo dei dati | post al centro, commenti come supporto |
| Codice | `src/` indipendente dalla sorgente (dump o API); script per fase in cartelle numerate |
| Modello di embedding | `multilingual-e5-large-instruct`, metodo del centroide (miglior r@20 sui nostri dati) |
| Attività dei subreddit | aggiunta alla similarità con peso α = 0,2, prima di prendere i top 20 |
| Re-ranker | Jev, da configurare alla fine |
