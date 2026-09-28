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
