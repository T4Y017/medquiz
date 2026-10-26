"""Hibrit arama: Türkçe uyumlu BM25 + (varsa) çok dilli embedding, RRF ile birleştirilir."""
from __future__ import annotations

import logging
import math
import random
import re
import threading
from collections import Counter

import numpy as np

from .db import Database

log = logging.getLogger(__name__)

_TR_LOWER = str.maketrans({"İ": "i", "I": "ı"})
_TOKEN = re.compile(r"[0-9a-zçğıöşüâîûéèáàäëïíóúñß]+", re.I)
_STOP = set(
    """ve veya ile de da ki bu şu o bir için gibi daha en çok az ise ama fakat ancak hem ne mi mu mü mı
    olan olarak olur olup oldu olması olduğu ya yani her bazı tüm diğer kadar sonra önce arasında üzerine
    the of and or to in on for with is are be by as at an a from that this which it""".split()
)
STEM_LEN = 6  # Türkçe için basit "ilk n harf" kökleme; ekli kelimeleri eşleştirir


def tokenize(text: str) -> list[str]:
    text = text.translate(_TR_LOWER).lower()
    out = []
    for t in _TOKEN.findall(text):
        if t in _STOP or len(t) < 2:
            continue
        out.append(t[:STEM_LEN] if not t.isdigit() else t)
    return out


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.n = len(docs)
        self.tf = [Counter(d) for d in docs]
        self.len = np.array([len(d) for d in docs], dtype=np.float32)
        self.avgdl = float(self.len.mean()) if self.n else 0.0
        df: Counter = Counter()
        for d in self.tf:
            df.update(d.keys())
        self.idf = {t: math.log(1 + (self.n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: list[str]) -> np.ndarray:
        s = np.zeros(self.n, dtype=np.float32)
        if not self.n:
            return s
        norm = self.k1 * (1 - self.b + self.b * self.len / (self.avgdl or 1))
        for q in set(query):
            idf = self.idf.get(q)
            if idf is None:
                continue
            f = np.array([tf.get(q, 0) for tf in self.tf], dtype=np.float32)
            s += idf * f * (self.k1 + 1) / (f + norm)
        return s


class Embedder:
    """fastembed sarmalayıcı. Yüklenemezse sessizce devre dışı kalır."""

    def __init__(self, model_name: str, cache_dir: str | None = None):
        self.model_name = model_name
        self.cache_dir = cache_dir
        self._model = None
        self._failed: str | None = None
        self._lock = threading.Lock()

    @property
    def error(self) -> str | None:
        return self._failed

    def available(self) -> bool:
        return self._load() is not None

    def _load(self):
        if self._model is not None or self._failed:
            return self._model
        with self._lock:
            if self._model is None and not self._failed:
                try:
                    from fastembed import TextEmbedding

                    self._model = TextEmbedding(self.model_name, cache_dir=self.cache_dir)
                except Exception as e:  # noqa: BLE001
                    self._failed = str(e)
                    log.warning("Embedding modeli yüklenemedi, yalnızca anahtar kelime araması kullanılacak: %s", e)
        return self._model

    def embed(self, texts: list[str]) -> np.ndarray | None:
        m = self._load()
        if m is None or not texts:
            return None
        vecs = np.array(list(m.embed(texts, batch_size=32)), dtype=np.float32)
        vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9
        return vecs


class Retriever:
    def __init__(self, db: Database, embedder: Embedder | None = None):
        self.db = db
        self.embedder = embedder
        self._lock = threading.RLock()
        self._dirty = True
        self._chunks: list[dict] = []
        self._bm25: BM25 | None = None
        self._emb_ids: list[int] = []
        self._emb_mat: np.ndarray | None = None
        self._pos: dict[int, int] = {}

    def invalidate(self) -> None:
        with self._lock:
            self._dirty = True

    def _ensure(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            self._chunks = self.db.all_chunks()
            self._bm25 = BM25([tokenize(c["title"] + " " + c["text"]) for c in self._chunks])
            self._pos = {c["id"]: i for i, c in enumerate(self._chunks)}
            self._load_embeddings()
            self._dirty = False

    def _load_embeddings(self) -> None:
        self._emb_ids, self._emb_mat = [], None
        if not self.embedder or not self.embedder.available():
            return
        model = self.embedder.model_name
        missing = self.db.chunks_missing_embeddings(model)
        for i in range(0, len(missing), 64):
            batch = missing[i:i + 64]
            vecs = self.embedder.embed([t for _, t in batch])
            if vecs is None:
                return
            self.db.set_embeddings(list(zip([cid for cid, _ in batch], vecs)), model)
        self._emb_ids, self._emb_mat = self.db.load_embeddings(model)

    def mark_used(self, ids: list[int]) -> None:
        self.db.mark_used(ids)
        with self._lock:
            s = set(ids)
            for c in self._chunks:
                if c["id"] in s:
                    c["used_count"] += 1

    def warmup(self) -> None:
        """Arka planda indeks + eksik embedding'leri hazırlar."""
        self._ensure()

    @property
    def size(self) -> int:
        self._ensure()
        return len(self._chunks)

    @property
    def semantic_enabled(self) -> bool:
        return self._emb_mat is not None

    # -------------------------------------------------------------- search
    def search(self, query: str, k: int = 6, doc_ids: list[int] | None = None) -> list[dict]:
        self._ensure()
        with self._lock:
            chunks = self._chunks
            if not chunks:
                return []
            allowed = None
            if doc_ids:
                ds = set(doc_ids)
                allowed = {i for i, c in enumerate(chunks) if c["doc_id"] in ds}

            rankings: list[list[int]] = []
            bm = self._bm25.scores(tokenize(query)) if self._bm25 else np.zeros(len(chunks))
            order = [i for i in np.argsort(-bm) if bm[i] > 0 and (allowed is None or i in allowed)]
            rankings.append(order[:50])

            if self._emb_mat is not None and self.embedder:
                qv = self.embedder.embed([query])
                if qv is not None:
                    sims = self._emb_mat @ qv[0]
                    pos = self._pos
                    ranked = [pos[self._emb_ids[j]] for j in np.argsort(-sims) if self._emb_ids[j] in pos]
                    ranked = [i for i in ranked if allowed is None or i in allowed]
                    rankings.append(ranked[:50])

            # Reciprocal Rank Fusion
            fused: dict[int, float] = {}
            for r in rankings:
                for rank, i in enumerate(r):
                    fused[i] = fused.get(i, 0.0) + 1.0 / (60 + rank)
            best = sorted(fused, key=fused.get, reverse=True)[:k]
            return [dict(chunks[i], score=fused[i]) for i in best]

    # --------------------------------------------------- quiz context pick
    def pick_context(self, topic: str = "", n_chunks: int = 4, doc_ids: list[int] | None = None,
                     rng: random.Random | None = None) -> list[dict]:
        """Quiz için bağlam seçer.

        Konu verilmişse: aramada en ilgili parçalardan, daha az kullanılmışlara öncelik vererek seçer.
        Konu yoksa: kütüphaneden az kullanılmış rastgele bir 'çekirdek' parça + komşularını alır,
        böylece zamanla tüm içerik kapsanır.
        """
        rng = rng or random.Random()
        self._ensure()
        if not self._chunks:
            return []
        if topic.strip():
            hits = self.search(topic, k=max(12, n_chunks * 3), doc_ids=doc_ids)
            if not hits:
                return []
            # skor + az kullanılmışlık ile ağırlıklı örnekleme
            weights = [h["score"] / (1 + 0.5 * h.get("used_count", 0)) for h in hits]
            picked: list[dict] = []
            pool = list(zip(hits, weights))
            while pool and len(picked) < n_chunks:
                tot = sum(w for _, w in pool)
                r, acc = rng.random() * tot, 0.0
                for j, (h, w) in enumerate(pool):
                    acc += w
                    if acc >= r:
                        picked.append(h)
                        pool.pop(j)
                        break
            return sorted(picked, key=lambda c: (c["doc_id"], c["idx"]))

        pool = [c for c in self._chunks if not doc_ids or c["doc_id"] in doc_ids]
        if not pool:
            return []
        min_used = min(c["used_count"] for c in pool)
        fresh = [c for c in pool if c["used_count"] <= min_used + 1]
        seed = rng.choice(fresh)
        span = max(1, (n_chunks - 1) // 2)
        ctx = self.db.neighbors(seed, span=span)
        return ctx[:n_chunks] if ctx else [seed]
