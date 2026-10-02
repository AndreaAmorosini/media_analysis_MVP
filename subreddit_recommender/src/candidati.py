"""Dai post ai subreddit: punteggio di ogni subreddit per una notizia, e top N candidati.

Due metodi, da confrontare:
- `centroide`: un vettore per subreddit, la media dei suoi post. Semplice, ma per i sub
  generalisti (r/Italia) la media diventa vaga;
- `vicini`: si confronta la notizia con i singoli post; il punteggio di un sub è la media delle
  sue `k` similarità migliori. Non conta quanti post ha il sub, altrimenti vincerebbero i grandi.

In entrambi `escludi` toglie dei post dal confronto (per la valutazione: i post della notizia
stessa, altrimenti la notizia trova se stessa).
"""

from __future__ import annotations

import numpy as np


class IndicePost:
    """Embedding dei post (normalizzati) con il subreddit di ognuno."""

    def __init__(self, emb: np.ndarray, subreddit: list[str]):
        self.emb = emb
        self.sub_nomi, self.sub_idx = np.unique(np.asarray(subreddit), return_inverse=True)
        self.colonne = [np.flatnonzero(self.sub_idx == s) for s in range(len(self.sub_nomi))]
        somme = np.zeros((len(self.sub_nomi), emb.shape[1]), dtype=np.float64)
        np.add.at(somme, self.sub_idx, emb)
        self._somme = somme
        self._conteggi = np.bincount(self.sub_idx, minlength=len(self.sub_nomi)).astype(float)

    def centroide(self, q: np.ndarray, escludi: list[np.ndarray] | None = None) -> np.ndarray:
        """Punteggi (n_query × n_sub): similarità coseno fra query e media dei post del sub."""
        out = np.empty((len(q), len(self.sub_nomi)), dtype=np.float32)
        for i in range(len(q)):
            somme, conteggi = self._somme, self._conteggi
            if escludi is not None and len(escludi[i]):
                somme, conteggi = somme.copy(), conteggi.copy()
                np.subtract.at(somme, self.sub_idx[escludi[i]], self.emb[escludi[i]])
                np.subtract.at(conteggi, self.sub_idx[escludi[i]], 1)
            c = somme / np.maximum(conteggi, 1)[:, None]
            c /= np.maximum(np.linalg.norm(c, axis=1, keepdims=True), 1e-12)
            out[i] = c @ q[i]
        return out

    def vicini(
        self, q: np.ndarray, k: int = 3, escludi: list[np.ndarray] | None = None
    ) -> np.ndarray:
        """Punteggi (n_query × n_sub): media delle `k` similarità migliori fra i post del sub."""
        sim = q @ self.emb.T
        if escludi is not None:
            for i, e in enumerate(escludi):
                sim[i, e] = -np.inf
        out = np.empty((len(q), len(self.sub_nomi)), dtype=np.float32)
        for s, cols in enumerate(self.colonne):
            kk = min(k, len(cols))
            migliori = -np.partition(-sim[:, cols], kk - 1, axis=1)[:, :kk]
            # un post escluso vale -inf: si ignora nella media
            migliori = np.where(np.isfinite(migliori), migliori, np.nan)
            out[:, s] = np.nan_to_num(np.nanmean(migliori, axis=1), nan=-1.0)
        return out

    def classifica(self, punteggi: np.ndarray, n: int = 20) -> list[list[str]]:
        """I `n` subreddit con il punteggio più alto, per ogni query."""
        ordine = np.argsort(-punteggi, axis=1)[:, :n]
        return [[str(s) for s in self.sub_nomi[r]] for r in ordine]


# ------------------------------------------------------------------ attività dei subreddit

# peso di ogni colonna di `subreddit_puliti.csv` nel punteggio di attività (negativo = penalità).
# I conteggi si prendono in scala logaritmica: passare da 10 a 100 autori conta quanto da 100 a 1000
PESI_ATTIVITA = {
    "n_autori": 0.4,  # quante persone diverse pubblicano: dimensione reale della community
    "mediana_commenti": 0.3,  # quanta discussione genera un post tipico
    "mediana_score": 0.3,  # quanti voti prende un post tipico
    "quota_rimossi": -0.3,  # rischio che il post venga tolto dai moderatori
}
_LOG = {"n_autori", "mediana_commenti", "mediana_score"}


def punteggio_attivita(catalogo, sub_nomi, pesi: dict[str, float] = PESI_ATTIVITA) -> np.ndarray:
    """Attività di ogni subreddit (nell'ordine di `sub_nomi`), standardizzata: media 0,
    deviazione standard 1. `catalogo`: DataFrame di `subreddit_puliti.csv`."""
    cat = catalogo.set_index("subreddit").reindex(list(sub_nomi))
    totale = np.zeros(len(cat))
    for col, peso in pesi.items():
        x = cat[col].to_numpy(dtype=float)
        x = np.log1p(np.maximum(x, 0)) if col in _LOG else x
        totale += peso * _standardizza(x)
    return _standardizza(totale)


def combina(punteggi: np.ndarray, attivita: np.ndarray, alfa: float) -> np.ndarray:
    """Punteggio finale = similarità standardizzata per notizia + `alfa` × attività.

    Le similarità sono compresse (0,80–0,90 con e5): standardizzate dicono quanto un sub si
    stacca dagli altri per quella notizia. Con `alfa` piccolo la similarità resta la priorità:
    alfa = 0,3 vuol dire che un sub molto più attivo della media (+1) guadagna quanto
    0,3 deviazioni standard di similarità.
    """
    z = (punteggi - punteggi.mean(axis=1, keepdims=True)) / punteggi.std(axis=1, keepdims=True)
    return z + alfa * attivita[None, :]


def _standardizza(x: np.ndarray) -> np.ndarray:
    x = np.nan_to_num(x, nan=np.nanmean(x))
    return (x - x.mean()) / (x.std() or 1.0)
