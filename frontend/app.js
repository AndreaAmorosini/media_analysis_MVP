// Dashboard: articoli in cartelle anno/mese, grafici di sentiment del periodo selezionato, apertura articolo.
// Dati: ../dati/articoli.json (elenco leggero); il testo di un articolo si carica dalla sua rassegna.json
// all'apertura (schema in docs/schema_articolo.md).

const CLASSI = ["positivo", "neutro", "negativo"];
// Scala divergente: verde e rosso ai poli, grigio neutro al centro (contrasto basso compensato da legenda e tooltip).
const COLORI = { positivo: "#2e9e5b", neutro: "#9aa1ab", negativo: "#d64545" };
const ACCENTO = "#2f6fd6";
const SUPERFICIE = "#ffffff";
const DATI = "../dati/";
const SVG = "http://www.w3.org/2000/svg";
const MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre",
              "ottobre", "novembre", "dicembre"];
const MESI_BREVI = ["gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"];

let articoli = [];
// Filtri globali: tipo di fonte e ricerca per parola chiave (titolo, testata e, appena caricato, testo).
let filtro = { tipo: "tutti", parole: [], regole: [] };
let testiArticoli = null;       // id -> testo normalizzato (dati/testi.json, caricato alla prima ricerca)
let caricamentoTesti = null;
// Periodo selezionato: tutti gli anni, un anno ("2025") o un mese ("2025" + "03").
let periodo = { anno: null, mese: null };
const $ = (sel) => document.querySelector(sel);

function crea(tag, classe, testo) {
  const el = document.createElement(tag);
  if (classe) el.className = classe;
  if (testo != null) el.textContent = testo;
  return el;
}

const percento = (p) => `${(p * 100).toFixed(0)}%`;
const nomeMese = (m) => MESI[Number(m) - 1];
const conSentiment = (lista) => lista.filter((a) => a.sentiment);

function formattaData(iso) {
  return new Date(iso).toLocaleDateString("it-IT", { day: "2-digit", month: "short", year: "numeric" });
}

// ---- Caricamento ----

async function caricaArticoli() {
  const r = await fetch(DATI + "articoli.json");
  for (const a of await r.json()) {
    if (!a.data) continue;
    a.cartella = DATI + a.file.replace(/[^/]+$/, "");
    articoli.push(a);
  }
  articoli.sort((x, y) => y.data.localeCompare(x.data));
}

// ---- Filtri: tipo di fonte e ricerca ----

// Minuscole e senza accenti: "Città" e "citta" coincidono.
const normalizza = (t) => (t || "").toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");

// Parola intera: prima e dopo non ci devono essere lettere o cifre ("nola" non trova "nolano",
// trova invece "Nola," "(Nola)" "nola-napoli").
const LETTERA = "[\\p{L}\\p{N}]";
const regolaParola = (p) =>
  new RegExp(`(?<!${LETTERA})${p.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?!${LETTERA})`, "u");

function corrisponde(a) {
  if (filtro.tipo !== "tutti" && a.tipo !== filtro.tipo) return false;
  if (!filtro.parole.length) return true;
  a._cerca ??= normalizza(`${a.titolo} ${a.testata}`);
  const testo = testiArticoli?.[a.id] ?? "";
  return filtro.regole.every((r) => r.test(a._cerca) || r.test(testo));
}

let cacheFiltrati = null;
function filtrati() {
  return (cacheFiltrati ??= articoli.filter(corrisponde));
}

async function caricaTesti() {
  caricamentoTesti ??= fetch(DATI + "testi.json").then((r) => (r.ok ? r.json() : {})).then((t) => {
    testiArticoli = Object.fromEntries(Object.entries(t).map(([k, v]) => [k, normalizza(v)]));
  });
  return caricamentoTesti;
}

// Ricalcola tutto dopo un cambio di filtro.
function applicaFiltri() {
  cacheFiltrati = null;
  const n = filtrati().length;
  // Con pochi risultati di ricerca si aprono le cartelle che li contengono.
  if (filtro.parole.length && n <= 150) {
    for (const a of filtrati()) { aperti.add(a.data.slice(0, 4)); aperti.add(a.data.slice(0, 7)); }
  }
  if (!$("#articolo").hidden) chiudiArticolo();
  disegnaAlbero();
  disegnaPercorso();
  aggiornaGrafici();
  disegnaConfronto();
}

let attesaRicerca = null;
function alCambioRicerca() {
  clearTimeout(attesaRicerca);
  attesaRicerca = setTimeout(async () => {
    // Divide su tutto ciò che non è lettera o cifra: "l'acqua" → "acqua", "nola," → "nola".
    filtro.parole = normalizza($("#ricerca").value).split(/[^\p{L}\p{N}]+/u).filter((p) => p.length >= 2);
    filtro.regole = filtro.parole.map(regolaParola);
    if (filtro.parole.length && !testiArticoli) {
      applicaFiltri();  // subito su titolo e testata
      $("#stato-ricerca").textContent = "cerco anche nei testi…";
      await caricaTesti();
    }
    applicaFiltri();
    $("#stato-ricerca").textContent = filtro.parole.length ? `${filtrati().length} risultati` : "";
  }, 250);
}

// ---- Periodo ----

function articoliDelPeriodo() {
  const { anno, mese } = periodo;
  return filtrati().filter((a) => (!anno || a.data.startsWith(anno)) && (!mese || a.data.slice(5, 7) === mese));
}

function nomePeriodo({ anno, mese } = periodo) {
  if (!anno) return "Tutti gli anni";
  return mese ? `${nomeMese(mese)} ${anno}` : anno;
}

function impostaPeriodo(anno = null, mese = null) {
  periodo = { anno, mese };
  if (!$("#articolo").hidden) chiudiArticolo();
  aggiornaAlbero();
  disegnaPercorso();
  aggiornaGrafici();
}

// Percorso sopra i grafici: Tutti gli anni › 2025 › marzo, ogni livello cliccabile per risalire.
function disegnaPercorso() {
  const nav = $("#percorso");
  nav.innerHTML = "";
  const livelli = [["Tutti gli anni", null, null]];
  if (periodo.anno) livelli.push([periodo.anno, periodo.anno, null]);
  if (periodo.mese) {
    const nome = nomeMese(periodo.mese);
    livelli.push([nome[0].toUpperCase() + nome.slice(1), periodo.anno, periodo.mese]);
  }
  livelli.forEach(([nome, anno, mese], i) => {
    if (i) nav.append(crea("span", "separatore", "›"));
    const ultimo = i === livelli.length - 1;
    const el = crea(ultimo ? "span" : "button", ultimo ? "corrente" : "livello", nome);
    if (!ultimo) el.addEventListener("click", () => impostaPeriodo(anno, mese));
    nav.append(el);
  });
}

// ---- Albero anno / mese ----

// Barretta con la ripartizione del sentiment di un gruppo di articoli.
function barraSentiment(lista) {
  const conteggi = Object.fromEntries(CLASSI.map((c) => [c, 0]));
  for (const a of conSentiment(lista)) conteggi[a.sentiment.etichetta]++;
  const tot = CLASSI.reduce((s, c) => s + conteggi[c], 0) || 1;
  const barra = crea("span", "mini-barra");
  barra.title = CLASSI.map((c) => `${c} ${conteggi[c]}`).join(" · ");
  for (const c of CLASSI) {
    if (!conteggi[c]) continue;
    const pezzo = crea("span", c);
    pezzo.style.width = `${(conteggi[c] / tot) * 100}%`;
    barra.appendChild(pezzo);
  }
  return barra;
}

function raggruppa() {
  const anni = new Map();
  for (const a of filtrati()) {
    const anno = a.data.slice(0, 4), mese = a.data.slice(5, 7);
    if (!anni.has(anno)) anni.set(anno, new Map());
    const mesi = anni.get(anno);
    if (!mesi.has(mese)) mesi.set(mese, []);
    mesi.get(mese).push(a);
  }
  return anni;  // già in ordine decrescente: articoli è ordinato per data
}

const aperti = new Set();  // cartelle espanse: "2025" e "2025-03"

function rigaCartella(classe, nome, lista, chiave, alClick) {
  const riga = crea("button", `cartella ${classe}`);
  riga.dataset.chiave = chiave;
  riga.setAttribute("aria-expanded", aperti.has(chiave));
  riga.append(crea("span", "freccia", "▸"), crea("span", "nome", nome), barraSentiment(lista),
              crea("span", "conta", lista.length));
  riga.addEventListener("click", alClick);
  return riga;
}

function rigaArticolo(a) {
  const li = crea("li", "articolo");
  li.dataset.id = a.id;
  const etichetta = a.sentiment?.etichetta ?? "assente";
  const pallino = crea("span", `pallino ${etichetta}`);
  pallino.title = etichetta;
  const testi = crea("div");
  const meta = [a.testata, formattaData(a.data)];
  if (a.tipo) meta.push(a.tipo);
  testi.append(titoloEvidenziato(a.titolo), crea("div", "meta", meta.join(" · ")));
  li.append(pallino, testi);
  li.addEventListener("click", () => apriArticolo(a.id));
  return li;
}

// Titolo con le parole cercate evidenziate (confronto senza accenti: posizioni uguali nel testo normalizzato).
function titoloEvidenziato(titolo) {
  const div = crea("div", "titolo");
  const norm = normalizza(titolo);
  const segni = new Array(titolo.length).fill(false);
  for (const r of filtro.regole) {
    for (const m of norm.matchAll(new RegExp(r.source, "gu"))) segni.fill(true, m.index, m.index + m[0].length);
  }
  if (norm.length !== titolo.length || !segni.includes(true)) { div.textContent = titolo; return div; }
  let i = 0;
  while (i < titolo.length) {
    let j = i;
    while (j < titolo.length && segni[j] === segni[i]) j++;
    div.append(segni[i] ? crea("mark", null, titolo.slice(i, j)) : titolo.slice(i, j));
    i = j;
  }
  return div;
}

function disegnaAlbero() {
  const albero = $("#albero");
  albero.innerHTML = "";
  const tutti = rigaCartella("radice", "Tutti gli anni", filtrati(), "tutti", () => impostaPeriodo());
  tutti.querySelector(".freccia").textContent = "";
  albero.appendChild(tutti);
  for (const [anno, mesi] of raggruppa()) {
    const listaAnno = [...mesi.values()].flat();
    const gruppo = crea("div", "gruppo-anno");
    gruppo.appendChild(rigaCartella("anno", anno, listaAnno, anno, () => {
      // Clic su un anno: lo seleziona; se era già selezionato, apre o chiude la cartella.
      if (periodo.anno === anno && !periodo.mese) aperti.has(anno) ? aperti.delete(anno) : aperti.add(anno);
      else aperti.add(anno);
      impostaPeriodo(anno);
    }));
    const contenitoreMesi = crea("div", "mesi");
    for (const [mese, lista] of mesi) {
      const chiave = `${anno}-${mese}`;
      const gruppoMese = crea("div", "gruppo-mese");
      gruppoMese.appendChild(rigaCartella("mese", nomeMese(mese), lista, chiave, () => {
        if (periodo.anno === anno && periodo.mese === mese) aperti.has(chiave) ? aperti.delete(chiave) : aperti.add(chiave);
        else aperti.add(chiave);
        impostaPeriodo(anno, mese);
      }));
      const ul = crea("ul", "articoli");
      ul.dataset.chiave = chiave;
      gruppoMese.appendChild(ul);
      contenitoreMesi.appendChild(gruppoMese);
    }
    gruppo.appendChild(contenitoreMesi);
    albero.appendChild(gruppo);
  }
  aggiornaAlbero();
}

// Stato visivo: cartelle aperte, periodo selezionato; gli articoli di un mese si creano solo quando si apre.
function aggiornaAlbero() {
  const gruppi = raggruppa();
  document.querySelectorAll("#albero .cartella").forEach((riga) => {
    const k = riga.dataset.chiave;
    const aperta = k === "tutti" || aperti.has(k);
    riga.setAttribute("aria-expanded", aperta);
    const selezionata = k === "tutti" ? !periodo.anno
      : k === periodo.anno && !periodo.mese || k === `${periodo.anno}-${periodo.mese}`;
    riga.classList.toggle("selezionata", selezionata);
    const figli = riga.nextElementSibling;
    if (figli) figli.hidden = !aperta;
    if (riga.classList.contains("mese") && aperta && !figli.children.length) {
      const [anno, mese] = k.split("-");
      for (const a of gruppi.get(anno)?.get(mese) ?? []) figli.appendChild(rigaArticolo(a));
    }
  });
}

function mostraNellAlbero(a) {
  const anno = a.data.slice(0, 4), mese = a.data.slice(5, 7);
  aperti.add(anno);
  aperti.add(`${anno}-${mese}`);
  aggiornaAlbero();
  document.querySelectorAll("#albero .articolo").forEach((li) => li.classList.toggle("selezionato", li.dataset.id === a.id));
  document.querySelector("#albero .articolo.selezionato")?.scrollIntoView({ block: "nearest" });
}

// ---- Articolo ----

function barraProb(prob) {
  const barra = crea("div", "barra-prob");
  for (const c of CLASSI) {
    const pezzo = crea("div", c);
    pezzo.style.width = `${prob[c] * 100}%`;
    pezzo.title = `${c} ${percento(prob[c])}`;
    barra.appendChild(pezzo);
  }
  return barra;
}

// Testo, frasi e pagine di un articolo dell'elenco leggero, dalla sua rassegna.json (una volta sola per file).
const cacheRassegne = {};
async function completaArticolo(a) {
  if (a.frasi || !a.file) return;
  cacheRassegne[a.file] ??= fetch(DATI + a.file).then((r) => r.json());
  const rassegna = await cacheRassegne[a.file];
  const completo = rassegna.articoli.find((x) => x.id === a.id);
  Object.assign(a, completo);
  a.pagine = rassegna.pagine
    .filter((p) => a.pagine_rassegna.includes(p.n))
    .map((p) => ({ ...p, immagine: a.cartella + p.immagine }));
}

async function apriArticolo(id) {
  const a = articoli.find((x) => x.id === id);
  if (!a) return;
  await completaArticolo(a);
  history.replaceState(null, "", `#articolo=${encodeURIComponent(id)}${mostraRiquadri ? "&riquadri=1" : ""}`);
  mostraNellAlbero(a);

  const meta = $("#articolo-meta");
  meta.innerHTML = "";
  if (a.occhiello) meta.appendChild(crea("div", "occhiello", a.occhiello));
  meta.appendChild(crea("h2", "titolo-articolo", a.titolo));
  const righe = [a.testata, formattaData(a.data)];
  if (a.pagina_originale) righe.push(`pag. ${a.pagina_originale}`);
  if (a.autore) righe.push(a.autore);
  if (a.sezione) righe.push(a.sezione.toLowerCase());
  meta.appendChild(crea("div", null, righe.join(" · ")));
  if (a.url) {
    const link = crea("a", "link-originale", "Articolo originale ↗");
    link.href = a.url;
    link.target = "_blank";
    link.rel = "noopener";
    meta.appendChild(link);
  }

  const s = a.sentiment;
  if (s) {
    meta.appendChild(barraProb(s.prob));
    const n = a.frasi?.length;
    const come = s.metodo === "media_frasi" ? ` — media di ${n} frasi` : "";
    meta.appendChild(crea("div", null,
      `${CLASSI.map((c) => `${c} ${percento(s.prob[c])}`).join(" · ")}${come} — ${s.modello}`));
  } else {
    meta.appendChild(crea("div", null, "Sentiment non calcolato"));
  }

  const testo = $("#articolo-testo");
  const pagina = $("#articolo-pagina");
  testo.innerHTML = "";
  pagina.innerHTML = "";
  if (a.frasi?.length) {
    disegnaFrasi(a, testo);
    disegnaControlliRiquadri(a, pagina);
    disegnaPagine(a, pagina);
    if (mostraRiquadri) disegnaDiagnostica(a);
  } else {
    testo.textContent = a.testo || "Nessun testo estratto.";
  }
  pagina.hidden = !a.pagine?.length;

  $("#grafici").hidden = true;
  $("#articolo").hidden = false;
}

// Testo dell'articolo con ogni frase colorata per sentiment; i paragrafi sono separati da "\n\n".
function disegnaFrasi(a, contenitore) {
  let p = crea("p");
  contenitore.appendChild(p);
  let fine = 0;
  for (const f of a.frasi) {
    const intermezzo = a.testo.slice(fine, f.inizio);
    if (intermezzo.includes("\n\n")) {
      p = crea("p");
      contenitore.appendChild(p);
    } else if (intermezzo) {
      p.append(intermezzo);
    }
    const etichetta = f.sentiment?.etichetta ?? "assente";
    const span = crea("span", `frase frase-${etichetta}`, f.testo);
    span.dataset.n = f.n;
    if (f.sentiment) {
      span.title = CLASSI.map((c) => `${c} ${percento(f.sentiment.prob[c])}`).join(" · ");
    }
    span.addEventListener("mouseenter", () => evidenzia(f));
    span.addEventListener("mouseleave", () => evidenzia(null));
    p.appendChild(span);
    fine = f.fine;
  }
}

// Immagini delle pagine con un livello SVG sopra, nelle coordinate della pagina PDF.
function disegnaPagine(a, contenitore) {
  for (const pg of a.pagine ?? []) {
    const box = crea("div", "pagina");
    const img = crea("img");
    img.src = pg.immagine;
    img.alt = `Pagina ${pg.n}`;
    const svg = document.createElementNS(SVG, "svg");
    svg.setAttribute("viewBox", `0 0 ${pg.larghezza} ${pg.altezza}`);
    svg.dataset.pagina = pg.n;
    // Due livelli: riquadri della diagnostica sotto, frase evidenziata sopra.
    const diag = document.createElementNS(SVG, "g");
    diag.classList.add("livello-diagnostica");
    const evid = document.createElementNS(SVG, "g");
    evid.classList.add("livello-evidenza");
    svg.append(diag, evid);
    box.append(img, svg);
    contenitore.appendChild(box);
  }
}

function rettangolo(bbox, attributi) {
  const [x0, y0, x1, y1] = bbox;
  const r = document.createElementNS(SVG, "rect");
  Object.entries({ x: x0, y: y0, width: x1 - x0, height: y1 - y0, ...attributi })
    .forEach(([k, v]) => r.setAttribute(k, v));
  return r;
}

function evidenzia(frase) {
  document.querySelectorAll(".livello-evidenza").forEach((g) => (g.innerHTML = ""));
  if (!frase) return;
  const colore = "#2f6fd6";  // accento: il sentiment è già indicato dal colore della frase nel testo
  for (const pos of frase.posizioni) {
    const g = document.querySelector(`.pagina svg[data-pagina="${pos.pagina}"] .livello-evidenza`);
    if (g) g.appendChild(rettangolo(pos.bbox, { fill: colore, "fill-opacity": 0.25, stroke: colore, "stroke-width": 1.5 }));
  }
}

// ---- Diagnostica dell'estrazione: riquadri di ciò che è stato tenuto o scartato ----

const STILE_DIAGNOSTICA = {
  tenuto:   { stroke: "#2e9e5b", fill: "#2e9e5b", "fill-opacity": 0.12, "stroke-width": 0.8 },
  scartato: { stroke: "#d64545", fill: "#d64545", "fill-opacity": 0.08, "stroke-width": 0.8 },
  info:     { stroke: "#2f6fd6", fill: "none", "stroke-width": 1, "stroke-dasharray": "4 3" },
};
const ORDINE_STATI = ["info", "scartato", "tenuto"];  // le righe tenute sopra a tutto
let mostraRiquadri = false;
const cacheDiagnostica = {};

async function diagnosticaDi(a) {
  cacheDiagnostica[a.cartella] ??= fetch(a.cartella + "diagnostica.json").then((r) => (r.ok ? r.json() : {}));
  return (await cacheDiagnostica[a.cartella])[a.id];
}

function disegnaControlliRiquadri(a, contenitore) {
  const barra = crea("div", "controlli-riquadri");
  const bottone = crea("button", null, mostraRiquadri ? "Nascondi riquadri" : "Mostra riquadri");
  bottone.addEventListener("click", () => {
    mostraRiquadri = !mostraRiquadri;
    bottone.textContent = mostraRiquadri ? "Nascondi riquadri" : "Mostra riquadri";
    legenda.hidden = !mostraRiquadri;
    if (mostraRiquadri) disegnaDiagnostica(a);
    else document.querySelectorAll(".livello-diagnostica").forEach((g) => (g.innerHTML = ""));
  });
  const legenda = crea("div", "legenda-riquadri");
  legenda.hidden = !mostraRiquadri;
  for (const [stato, nome] of [["tenuto", "tenuto"], ["scartato", "scartato"], ["info", "colonna OCR"]]) {
    const voce = crea("span", `voce-legenda voce-${stato}`, nome);
    legenda.appendChild(voce);
  }
  legenda.appendChild(crea("span", "metodo-estrazione", `metodo: ${a.estrazione?.metodo ?? "?"}`));
  // Immagini delle pagine con i riquadri già disegnati (estrazione/disegna_riquadri.py).
  const immagini = crea("span", "immagini-riquadri", "immagini: ");
  for (const pg of a.pagine ?? []) {
    const link = crea("a", null, `p${pg.n}`);
    link.href = `${a.cartella}riquadri/page_${String(pg.n).padStart(3, "0")}.jpg`;
    link.target = "_blank";
    immagini.append(link, " ");
  }
  barra.append(bottone, legenda, immagini);
  contenitore.appendChild(barra);
}

async function disegnaDiagnostica(a) {
  const d = await diagnosticaDi(a);
  document.querySelectorAll(".livello-diagnostica").forEach((g) => (g.innerHTML = ""));
  if (!d || !mostraRiquadri) return;
  const elementi = [...d.elementi].sort((x, y) => ORDINE_STATI.indexOf(x.stato) - ORDINE_STATI.indexOf(y.stato));
  for (const e of elementi) {
    const g = document.querySelector(`.pagina svg[data-pagina="${e.pagina}"] .livello-diagnostica`);
    if (!g) continue;
    const r = rettangolo(e.bbox, STILE_DIAGNOSTICA[e.stato] ?? STILE_DIAGNOSTICA.info);
    const titolo = document.createElementNS(SVG, "title");
    titolo.textContent = `${e.stato} (${e.livello}): ${e.motivo}${e.testo ? "\n«" + e.testo + "»" : ""}`;
    r.appendChild(titolo);
    g.appendChild(r);
  }
}

function chiudiArticolo() {
  if (location.hash) history.replaceState(null, "", location.pathname);
  document.querySelectorAll("#albero .articolo.selezionato").forEach((li) => li.classList.remove("selezionato"));
  $("#articolo").hidden = true;
  $("#grafici").hidden = false;
  // I grafici erano nascosti: vanno ridimensionati quando tornano visibili.
  torta.resize();
  linea.resize();
  confronto.resize();
  distacco.resize();
}

// ---- Grafici del periodo selezionato ----

const torta = echarts.init($("#grafico-torta"));
const linea = echarts.init($("#grafico-linea"));
const modo = () => $(".selettore-modo button.attivo").dataset.modo;

function aggiornaGrafici() {
  const lista = articoliDelPeriodo();
  const parti = [`${nomePeriodo()} · ${lista.length} articoli`];
  if (filtro.tipo !== "tutti") parti.push(filtro.tipo === "web" ? "solo web" : "solo giornali");
  if (filtro.parole.length) parti.push(`ricerca «${$("#ricerca").value.trim()}»`);
  $("#riepilogo").textContent = parti.join(" · ");
  disegnaTorta(lista);
  disegnaLinea(lista);
}

function disegnaTorta(lista) {
  const conteggi = Object.fromEntries(CLASSI.map((c) => [c, 0]));
  for (const a of conSentiment(lista)) conteggi[a.sentiment.etichetta]++;
  $("#titolo-torta").textContent = `Distribuzione · ${nomePeriodo()}`;
  torta.setOption({
    tooltip: { trigger: "item", formatter: "{b}: {c} articoli ({d}%)" },
    legend: { bottom: 0, data: CLASSI },
    series: [{
      type: "pie",
      radius: ["42%", "70%"],
      itemStyle: { borderColor: SUPERFICIE, borderWidth: 2 },
      label: { formatter: "{b}\n{d}%", color: "#4b5563" },
      data: CLASSI.filter((c) => conteggi[c] > 0)
        .map((c) => ({ name: c, value: conteggi[c], itemStyle: { color: COLORI[c] } })),
    }],
  }, { replaceMerge: ["series"] });
}

// Intervalli dell'asse del tempo secondo il periodo: tutti gli anni -> mesi di tutto l'archivio,
// un anno -> i suoi 12 mesi, un mese -> i suoi giorni. Ogni intervallo ha chiave, etichetta e periodo di arrivo al clic.
function intervalli(lista) {
  const { anno, mese } = periodo;
  if (mese) {
    const giorni = new Date(Number(anno), Number(mese), 0).getDate();
    return Array.from({ length: giorni }, (_, i) => {
      const g = String(i + 1).padStart(2, "0");
      return { chiave: `${anno}-${mese}-${g}`, etichetta: String(i + 1), lunga: `${i + 1} ${nomeMese(mese)} ${anno}` };
    });
  }
  if (anno) {
    return MESI_BREVI.map((m, i) => {
      const mm = String(i + 1).padStart(2, "0");
      return { chiave: `${anno}-${mm}`, etichetta: m, lunga: `${MESI[i]} ${anno}`, va: [anno, mm] };
    });
  }
  const mesi = lista.map((a) => a.data.slice(0, 7)).sort();
  if (!mesi.length) return [];
  const uscita = [];
  let [a, m] = mesi[0].split("-").map(Number);
  const [af, mf] = mesi[mesi.length - 1].split("-").map(Number);
  while (a < af || (a === af && m <= mf)) {
    const mm = String(m).padStart(2, "0");
    uscita.push({ chiave: `${a}-${mm}`, etichetta: `${MESI_BREVI[m - 1]} ${String(a).slice(2)}`,
                  lunga: `${MESI[m - 1]} ${a}`, va: [String(a), mm] });
    if (++m > 12) { m = 1; a++; }
  }
  return uscita;
}

let intervalliCorrenti = [];

function disegnaLinea(lista) {
  const iv = intervalli(lista);
  intervalliCorrenti = iv;
  const lunghezza = iv[0]?.chiave.length ?? 7;
  const dati = new Map(iv.map((x) => [x.chiave, { positivo: 0, neutro: 0, negativo: 0, somma: 0, n: 0 }]));
  for (const a of conSentiment(lista)) {
    const d = dati.get(a.data.slice(0, lunghezza));
    if (!d) continue;
    d[a.sentiment.etichetta]++;
    d.somma += a.sentiment.prob.positivo - a.sentiment.prob.negativo;
    d.n++;
  }
  $("#titolo-linea").textContent = `Andamento · ${nomePeriodo()}`;
  $("#nota-linea").textContent = periodo.mese
    ? "Un valore per giorno."
    : "Un valore per mese. Clic su un mese per vederne i giorni.";

  const asseX = {
    type: "category", data: iv.map((x) => x.etichetta),
    axisLine: { lineStyle: { color: "#d1d5db" } }, axisTick: { show: false },
    axisLabel: { color: "#6b7280", hideOverlap: true },
  };
  const asseY = { type: "value", splitLine: { lineStyle: { color: "#eef0f3" } }, axisLabel: { color: "#6b7280" } };
  const titoloTooltip = (i) => iv[i]?.lunga ?? "";
  const base = {
    grid: { left: 44, right: 16, top: 16, bottom: 56 },
    legend: { bottom: 0, itemWidth: 12, itemHeight: 12 },
    xAxis: asseX,
  };

  if (modo() === "conteggi") {
    linea.setOption({
      ...base,
      tooltip: {
        trigger: "axis", axisPointer: { type: "shadow" },
        formatter: (punti) => {
          const i = punti[0].dataIndex, d = dati.get(iv[i].chiave);
          const tot = d.positivo + d.neutro + d.negativo;
          return `<b>${titoloTooltip(i)}</b> · ${tot} articoli<br>` + CLASSI.map((c) =>
            `${punti.find((p) => p.seriesName === c)?.marker ?? ""}${c}: ${d[c]}` +
            (tot ? ` (${percento(d[c] / tot)})` : "")).join("<br>");
        },
      },
      yAxis: { ...asseY, minInterval: 1, min: 0, max: null, name: "articoli", nameTextStyle: { color: "#6b7280" } },
      series: CLASSI.map((c, k) => ({
        name: c, type: "bar", stack: "sentiment", barMaxWidth: 28, cursor: periodo.mese ? "default" : "pointer",
        data: iv.map((x) => dati.get(x.chiave)[c]),
        // Bordo del colore dello sfondo: separa i segmenti impilati; estremità superiore arrotondata.
        itemStyle: { color: COLORI[c], borderColor: SUPERFICIE, borderWidth: 1,
                     borderRadius: k === CLASSI.length - 1 ? [3, 3, 0, 0] : 0 },
        emphasis: { focus: "series" },
      })),
    }, { replaceMerge: ["series", "yAxis"] });
  } else {
    linea.setOption({
      ...base,
      tooltip: {
        trigger: "axis",
        formatter: (punti) => {
          const i = punti[0].dataIndex, d = dati.get(iv[i].chiave);
          return `<b>${titoloTooltip(i)}</b><br>` +
            (d.n ? `media ${(d.somma / d.n).toFixed(2)} su ${d.n} articoli` : "nessun articolo");
        },
      },
      yAxis: { ...asseY, min: -1, max: 1, minInterval: null, name: "p(positivo) − p(negativo)",
               nameTextStyle: { color: "#6b7280", align: "left" } },
      series: [{
        name: "media del sentiment", type: "line", connectNulls: false,
        symbol: "circle", symbolSize: 7, showSymbol: iv.length <= 60,
        lineStyle: { width: 2, color: ACCENTO }, itemStyle: { color: ACCENTO, borderColor: SUPERFICIE, borderWidth: 2 },
        data: iv.map((x) => { const d = dati.get(x.chiave); return d.n ? +(d.somma / d.n).toFixed(3) : null; }),
        markLine: { silent: true, symbol: "none", data: [{ yAxis: 0 }],
                    lineStyle: { color: "#9aa1ab", type: "dashed", width: 1 }, label: { show: false } },
      }],
    }, { replaceMerge: ["series", "yAxis"] });
  }
}

// Clic su un intervallo: si scende di livello (mese -> giorni).
linea.on("click", (e) => {
  const x = intervalliCorrenti[e.dataIndex];
  if (x?.va) {
    aperti.add(x.va[0]);
    aperti.add(`${x.va[0]}-${x.va[1]}`);
    impostaPeriodo(...x.va);
  }
});
linea.getZr().on("click", (e) => {
  // Clic sullo sfondo di una colonna (non solo sulla barra).
  if (e.target) return;
  const punto = [e.offsetX, e.offsetY];
  if (!linea.containPixel("grid", punto)) return;
  const i = linea.convertFromPixel({ seriesIndex: 0 }, punto)[0];
  const x = intervalliCorrenti[i];
  if (x?.va) {
    aperti.add(x.va[0]);
    aperti.add(`${x.va[0]}-${x.va[1]}`);
    impostaPeriodo(...x.va);
  }
});

// ---- Confronto tra anni ----

// Colori degli anni: palette per categorie validata (dataviz), assegnata in ordine fisso per anno crescente,
// così un anno ha sempre lo stesso colore qualunque selezione si faccia.
const COLORI_ANNI = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"];
const confronto = echarts.init($("#grafico-confronto"));
const distacco = echarts.init($("#grafico-distacco"));
let anniTutti = [];
const anniScelti = new Set();
let riferimento = null;

const METRICHE = {
  articoli: { nome: "articoli", asse: { min: 0 }, valore: (d) => d.n,
              testo: (v) => `${Math.round(v)}`, delta: (v) => `${v > 0 ? "+" : ""}${Math.round(v)}` },
  positivi: { nome: "% positivi", asse: { min: 0 }, valore: (d) => (d.ns ? (d.pos / d.ns) * 100 : null),
              testo: (v) => `${v.toFixed(0)}%`, delta: (v) => `${v > 0 ? "+" : ""}${v.toFixed(0)} punti` },
  negativi: { nome: "% negativi", asse: { min: 0 }, valore: (d) => (d.ns ? (d.neg / d.ns) * 100 : null),
              testo: (v) => `${v.toFixed(0)}%`, delta: (v) => `${v > 0 ? "+" : ""}${v.toFixed(0)} punti` },
  media: { nome: "sentiment medio", asse: { min: -1, max: 1 }, valore: (d) => (d.ns ? d.somma / d.ns : null),
           testo: (v) => v.toFixed(2), delta: (v) => `${v > 0 ? "+" : ""}${v.toFixed(2)}` },
};
const metrica = () => METRICHE[$(".selettore-metrica button.attivo").dataset.metrica];
const coloreAnno = (anno) => COLORI_ANNI[anniTutti.indexOf(anno)] ?? "#6b7280";

// Mesi coperti dall'archivio: fuori da questo intervallo un mese non ha dati (null), non zero articoli.
function copertura() {
  const mesi = articoli.map((a) => a.data.slice(0, 7)).sort();
  return [mesi[0], mesi[mesi.length - 1]];
}

function statisticheAnno(anno) {
  const vuoto = () => ({ n: 0, ns: 0, pos: 0, neg: 0, somma: 0 });
  const mesi = Array.from({ length: 12 }, vuoto), tot = vuoto();
  for (const a of filtrati()) {
    if (!a.data.startsWith(anno)) continue;
    for (const d of [mesi[Number(a.data.slice(5, 7)) - 1], tot]) {
      d.n++;
      if (!a.sentiment) continue;
      d.ns++;
      if (a.sentiment.etichetta === "positivo") d.pos++;
      if (a.sentiment.etichetta === "negativo") d.neg++;
      d.somma += a.sentiment.prob.positivo - a.sentiment.prob.negativo;
    }
  }
  const [primo, ultimo] = copertura();
  const coperti = mesi.map((_, i) => { const k = `${anno}-${String(i + 1).padStart(2, "0")}`; return k >= primo && k <= ultimo; });
  return { mesi, tot, coperti, nCoperti: coperti.filter(Boolean).length };
}

function disegnaSceltaAnni() {
  const box = $("#scelta-anni");
  box.innerHTML = "";
  for (const anno of anniTutti) {
    const b = crea("button", "chip");
    b.setAttribute("aria-pressed", anniScelti.has(anno));
    const pallino = crea("span", "campione");
    pallino.style.background = coloreAnno(anno);
    b.append(pallino, anno);
    b.addEventListener("click", () => {
      anniScelti.has(anno) ? anniScelti.delete(anno) : anniScelti.add(anno);
      disegnaConfronto();
    });
    box.appendChild(b);
  }
  const scelti = [...anniScelti].sort();
  if (!scelti.includes(riferimento)) riferimento = scelti[0] ?? null;
  if (scelti.length >= 2) {
    const etichetta = crea("label", "riferimento", "rispetto a ");
    const sel = crea("select");
    for (const anno of scelti) {
      const o = crea("option", null, anno);
      o.value = anno;
      o.selected = anno === riferimento;
      sel.appendChild(o);
    }
    sel.addEventListener("change", () => { riferimento = sel.value; disegnaConfronto(); });
    etichetta.appendChild(sel);
    box.appendChild(etichetta);
  }
}

function disegnaConfronto() {
  disegnaSceltaAnni();
  const m = metrica();
  const scelti = [...anniScelti].sort();
  const stat = Object.fromEntries(scelti.map((a) => [a, statisticheAnno(a)]));
  const valoriMese = (anno) => stat[anno].mesi.map((d, i) => (stat[anno].coperti[i] ? m.valore(d) : null));
  const ref = riferimento;

  $("#nota-confronto").textContent = scelti.length
    ? `Mese per mese, ${m.nome}. Mesi senza dati nell'archivio lasciati vuoti.`
    : "Scegli uno o più anni da confrontare.";
  confronto.setOption({
    animationDuration: 300,
    grid: { left: 48, right: 56, top: 16, bottom: 64 },
    legend: { bottom: 0, itemWidth: 14, itemHeight: 8 },
    tooltip: {
      trigger: "axis",
      formatter: (punti) => {
        const i = punti[0].dataIndex;
        const vr = ref ? valoriMese(ref)[i] : null;
        return `<b>${MESI[i]}</b><br>` + punti.map((p) => {
          if (p.value == null) return `${p.marker}${p.seriesName}: nessun dato`;
          const d = p.seriesName !== ref && vr != null ? ` (${m.delta(p.value - vr)} sul ${ref})` : "";
          return `${p.marker}${p.seriesName}: ${m.testo(p.value)}${d}`;
        }).join("<br>");
      },
    },
    xAxis: { type: "category", data: MESI_BREVI, axisTick: { show: false },
             axisLine: { lineStyle: { color: "#d1d5db" } }, axisLabel: { color: "#6b7280" } },
    yAxis: { type: "value", ...m.asse, max: m.asse.max ?? null, splitLine: { lineStyle: { color: "#eef0f3" } },
             axisLabel: { color: "#6b7280" }, name: m.nome, nameTextStyle: { color: "#6b7280", align: "left" } },
    series: scelti.map((anno) => ({
      name: anno, type: "line", data: valoriMese(anno), connectNulls: false,
      symbol: "circle", symbolSize: 8, lineStyle: { width: anno === ref ? 3 : 2, color: coloreAnno(anno) },
      itemStyle: { color: coloreAnno(anno), borderColor: SUPERFICIE, borderWidth: 2 },
      // Etichetta diretta a fine linea, in colore del testo (non della serie).
      endLabel: { show: true, formatter: anno, color: "#374151", fontWeight: anno === ref ? 600 : 400 },
      emphasis: { focus: "series" },
    })),
  }, { replaceMerge: ["series"] });

  // Distacchi dal riferimento, mese per mese.
  const altri = scelti.filter((a) => a !== ref);
  $("#blocco-distacco").hidden = !altri.length;
  let massimo = null;
  if (altri.length) {
    const vr = valoriMese(ref);
    const serie = altri.map((anno) => {
      const dati = valoriMese(anno).map((v, i) => (v == null || vr[i] == null ? null : +(v - vr[i]).toFixed(3)));
      dati.forEach((d, i) => {
        if (d != null && (!massimo || Math.abs(d) > Math.abs(massimo.d))) massimo = { d, i, anno };
      });
      return { name: anno, type: "bar", data: dati, barMaxWidth: 18, barGap: "15%",
               itemStyle: { color: coloreAnno(anno), borderRadius: 3 } };
    });
    $("#titolo-distacco").textContent = `Distacco dal ${ref} (${m.nome})`;
    distacco.setOption({
      animationDuration: 300,
      grid: { left: 48, right: 56, top: 12, bottom: 64 },
      legend: { bottom: 0, itemWidth: 14, itemHeight: 8 },
      tooltip: {
        trigger: "axis", axisPointer: { type: "shadow" },
        formatter: (punti) => `<b>${MESI[punti[0].dataIndex]}</b><br>` + punti.map((p) =>
          `${p.marker}${p.seriesName}: ${p.value == null ? "nessun dato" : m.delta(p.value)} rispetto al ${ref}`).join("<br>"),
      },
      xAxis: { type: "category", data: MESI_BREVI, axisTick: { show: false },
               axisLine: { lineStyle: { color: "#9aa1ab" } }, axisLabel: { color: "#6b7280" } },
      yAxis: { type: "value", splitLine: { lineStyle: { color: "#eef0f3" } }, axisLabel: { color: "#6b7280" } },
      series: serie.map((x, k) => k ? x : { ...x, markLine: { silent: true, symbol: "none", data: [{ yAxis: 0 }],
        lineStyle: { color: "#6b7280", width: 1, type: "solid" }, label: { show: false } } }),
    }, { replaceMerge: ["series"] });
  }
  $("#distacco-massimo").textContent = massimo
    ? `Distacco più ampio: ${MESI[massimo.i]}, ${massimo.anno} ${m.delta(massimo.d)} rispetto al ${ref}.` : "";

  disegnaTabellaConfronto(scelti, stat, ref);
}

// Tabella: totali di ogni anno e differenza dal riferimento (anche vista testuale del grafico).
function disegnaTabellaConfronto(scelti, stat, ref) {
  const t = $("#tabella-confronto");
  t.innerHTML = "";
  if (!scelti.length) return;
  const colonne = [
    ["Articoli", (s) => s.tot.n, METRICHE.articoli],
    ["Al mese", (s) => (s.nCoperti ? s.tot.n / s.nCoperti : null),
     { testo: (v) => v.toFixed(1), delta: (v) => `${v > 0 ? "+" : ""}${v.toFixed(1)}` }],
    ["% positivi", (s) => METRICHE.positivi.valore(s.tot), METRICHE.positivi],
    ["% negativi", (s) => METRICHE.negativi.valore(s.tot), METRICHE.negativi],
    ["Sentiment medio", (s) => METRICHE.media.valore(s.tot), METRICHE.media],
  ];
  const testa = crea("tr");
  testa.append(crea("th", null, "Anno"), crea("th", "n", "Mesi coperti"), ...colonne.map(([n]) => crea("th", "n", n)));
  t.appendChild(testa);
  for (const anno of scelti) {
    const s = stat[anno], tr = crea("tr");
    const cella = crea("td");
    const campione = crea("span", "campione");
    campione.style.background = coloreAnno(anno);
    cella.append(campione, anno + (anno === ref && scelti.length > 1 ? " (riferimento)" : ""));
    tr.append(cella, crea("td", "n", s.nCoperti));
    for (const [, f, fmt] of colonne) {
      const v = f(s), td = crea("td", "n");
      td.textContent = v == null ? "–" : fmt.testo(v);
      const vr = ref && anno !== ref ? f(stat[ref]) : null;
      if (v != null && vr != null) td.appendChild(crea("span", "delta", ` ${fmt.delta(v - vr)}`));
      tr.appendChild(td);
    }
    t.appendChild(tr);
  }
}

// ---- Avvio ----

$("#chiudi").addEventListener("click", chiudiArticolo);
document.querySelectorAll(".selettore-modo button").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll(".selettore-modo button").forEach((x) => x.classList.toggle("attivo", x === b));
    disegnaLinea(articoliDelPeriodo());
  })
);
window.addEventListener("resize", () => { torta.resize(); linea.resize(); confronto.resize(); distacco.resize(); });
document.querySelectorAll(".selettore-metrica button").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll(".selettore-metrica button").forEach((x) => x.classList.toggle("attivo", x === b));
    disegnaConfronto();
  })
);
document.querySelectorAll(".selettore-tipo button").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll(".selettore-tipo button").forEach((x) => x.classList.toggle("attivo", x === b));
    filtro.tipo = b.dataset.tipo;
    applicaFiltri();
  })
);
$("#ricerca").addEventListener("input", alCambioRicerca);

caricaArticoli().then(() => {
  // Collegamento diretto (es. dal resoconto): #articolo=<id>&riquadri=1
  const parametri = new URLSearchParams(location.hash.slice(1));
  // Confronto: di partenza gli ultimi due anni dell'archivio.
  anniTutti = [...new Set(articoli.map((a) => a.data.slice(0, 4)))].sort();
  anniTutti.slice(-2).forEach((a) => anniScelti.add(a));
  disegnaAlbero();
  disegnaPercorso();
  aggiornaGrafici();
  disegnaConfronto();
  if (parametri.get("articolo")) {
    mostraRiquadri = parametri.get("riquadri") === "1";
    apriArticolo(parametri.get("articolo"));
  }
});
