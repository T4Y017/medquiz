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


_DOT_LEADER = re.compile(r"\.{5,}|…{2,}|(\s\.){4,}")
_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_ETAL = re.compile(r"\bet al\b|\bvd\.|\band\s+[A-Z]\b", re.I)
_INDEX_LINE = re.compile(r"^[^\d\n]{2,60}[,\s]\s*\d{1,4}(\s*[-,–]\s*\d{1,4})*\s*$", re.M)


def chunk_quality(text: str) -> float:
    """0..1 arası 'öğretici düz metin' skoru. İçindekiler, dizin, kaynakça ve bozuk çıkarılmış
    metinler düşük puan alır; bunlar konu belirtilmeden yapılan rastgele seçimlerde atlanır."""
    t = text.strip()
    if len(t) < 150:
        return 0.2
    nonspace = [ch for ch in t if not ch.isspace()]
    letters = sum(ch.isalpha() for ch in nonspace)
    ratio = letters / max(1, len(nonspace))
    score = 1.0
    if ratio < 0.55:
        score -= 0.6
    elif ratio < 0.7:
        score -= 0.25
    lines = [ln for ln in t.splitlines() if ln.strip()]
    n = max(1, len(lines))
    if len(_DOT_LEADER.findall(t)) >= 3:
        score -= 0.6                                    # içindekiler
    if len(_INDEX_LINE.findall(t)) / n > 0.4:
        score -= 0.5                                    # dizin
    per_k = 1000 / max(1, len(t))
    if len(_YEAR.findall(t)) * per_k > 6 or len(_ETAL.findall(t)) * per_k > 3:
        score -= 0.5                                    # kaynakça
    avg_line = sum(len(ln) for ln in lines) / n
    if avg_line < 25 and n > 8:
        score -= 0.2                                    # parçalı/tablo artığı
    words = t.split()
    if words and sum(len(w) == 1 for w in words) / len(words) > 0.3:
        score -= 0.5                                    # "h a r f  h a r f" bozuk çıkarım
    return max(0.0, min(1.0, score))


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

    @property
    def ready(self) -> bool:
        """Model belleğe yüklü mü (indirme/yükleme beklemeden)."""
        return self._model is not None

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
        try:
            vecs = np.array(list(m.embed(texts, batch_size=32)), dtype=np.float32)
        except Exception as e:  # noqa: BLE001
            log.warning("Embedding hesaplanamadı: %s", e)
            return None
        vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9
        return vecs


class Retriever:
    """Anahtar kelime (BM25) dizini anında hazırdır. Anlamsal vektörler arka planda parça parça hesaplanır;
    hesaplananlar hemen aramaya katılır, böylece büyük bir kitap eklendiğinde hiçbir şey beklemez."""

    def __init__(self, db: Database, embedder: Embedder | None = None):
        self.db = db
        self.embedder = embedder
        self._lock = threading.RLock()
        self._dirty = True
        self._gen = 0                      # her invalidate'te artar; eski arka plan işleri durur
        self._chunks: list[dict] = []
        self._bm25: BM25 | None = None
        self._emb: tuple[list[int], np.ndarray | None] = ([], None)
        self._pos: dict[int, int] = {}
        self._embed_lock = threading.Lock()
        self.embed_progress: tuple[int, int] = (0, 0)   # (hazır, toplam)

    def invalidate(self) -> None:
        with self._lock:
            self._dirty = True
            self._gen += 1

    def _ensure(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            chunks = self.db.all_chunks()
            for c in chunks:
                c["quality"] = chunk_quality(c["text"])
            self._chunks = chunks
            self._bm25 = BM25([tokenize(c["title"] + " " + c["text"]) for c in chunks])
            self._pos = {c["id"]: i for i, c in enumerate(chunks)}
            if self.embedder:
                ids, mat = self.db.load_embeddings(self.embedder.model_name)
                self._emb = (ids, mat)
                self.embed_progress = (len(ids), len(chunks))
            else:
                self._emb = ([], None)
                self.embed_progress = (0, 0)
            self._dirty = False

    def embed_missing(self, progress=None, should_stop=None, batch: int = 64) -> str:
        """Eksik embedding'leri hesaplar (arka plan işi). Yalnızca bir örnek aynı anda çalışır."""
        if not self.embedder:
            return "off"
        if not self._embed_lock.acquire(blocking=False):
            return "busy"
        try:
            self._ensure()
            gen = self._gen
            model = self.embedder.model_name
            missing = self.db.chunks_missing_embeddings(model)
            total = len(self._chunks)
            done = total - len(missing)
            if not missing:
                return "ready" if self.embedder.available() or done else "failed"
            if progress:
                progress((done, total))
            if not self.embedder.available():
                return "failed"
            since_swap = 0
            for i in range(0, len(missing), batch):
                if (should_stop and should_stop()) or gen != self._gen:
                    return "stopped"
                part = missing[i:i + batch]
                vecs = self.embedder.embed([t for _, t in part])
                if vecs is None:
                    return "failed"
                self.db.set_embeddings(list(zip([cid for cid, _ in part], vecs)), model)
                done += len(part)
                since_swap += len(part)
                self.embed_progress = (done, total)
                if since_swap >= 512 or i + batch >= len(missing):
                    ids, mat = self.db.load_embeddings(model)
                    with self._lock:
                        if gen == self._gen:
                            self._emb = (ids, mat)
                    since_swap = 0
                if progress:
                    progress((done, total))
            return "ready"
        finally:
            self._embed_lock.release()

    def mark_used(self, ids: list[int]) -> None:
        self.db.mark_used(ids)
        with self._lock:
            s = set(ids)
            for c in self._chunks:
                if c["id"] in s:
                    c["used_count"] += 1

    def warmup(self) -> None:
        self._ensure()

    @property
    def size(self) -> int:
        self._ensure()
        return len(self._chunks)

    @property
    def semantic_enabled(self) -> bool:
        return self._emb[1] is not None

    # -------------------------------------------------------------- search
    def search(self, query: str, k: int = 6, doc_ids: list[int] | None = None) -> list[dict]:
        self._ensure()
        with self._lock:
            chunks = self._chunks
            emb_ids, emb_mat = self._emb
            pos = self._pos
            bm25 = self._bm25
        if not chunks:
            return []
        allowed = None
        if doc_ids:
            ds = set(doc_ids)
            allowed = {i for i, c in enumerate(chunks) if c["doc_id"] in ds}

        rankings: list[list[int]] = []
        bm = bm25.scores(tokenize(query)) if bm25 else np.zeros(len(chunks))
        order = [i for i in np.argsort(-bm)[:400] if bm[i] > 0 and (allowed is None or i in allowed)]
        rankings.append(order[:50])

        # Anlamsal arama yalnızca model hazırsa (indirme sürerken beklemez)
        if emb_mat is not None and self.embedder and self.embedder.ready:
            qv = self.embedder.embed([query])
            if qv is not None:
                sims = emb_mat @ qv[0]
                top = np.argsort(-sims)[:400]
                ranked = [pos[emb_ids[j]] for j in top if emb_ids[j] in pos]
                ranked = [i for i in ranked if allowed is None or i in allowed]
                rankings.append(ranked[:50])

        fused: dict[int, float] = {}
        for r in rankings:
            for rank, i in enumerate(r):
                fused[i] = fused.get(i, 0.0) + 1.0 / (60 + rank)
        # düşük kaliteli parçaları (dizin, kaynakça) hafifçe geriye it
        for i in fused:
            fused[i] *= 0.6 + 0.4 * chunks[i].get("quality", 1.0)
        best = sorted(fused, key=fused.get, reverse=True)[:k]
        return [dict(chunks[i], score=fused[i]) for i in best]

    # --------------------------------------------------- quiz context pick
    def pick_context(self, topic: str = "", n_chunks: int = 4, doc_ids: list[int] | None = None,
                     rng: random.Random | None = None) -> list[dict]:
        """Quiz/vaka için bağlam seçer.

        Konu verilmişse: aramada en ilgili parçalardan, daha az kullanılmışlara öncelik vererek seçer.
        Konu yoksa: kütüphaneden az kullanılmış, kaliteli (içindekiler/dizin/kaynakça olmayan) rastgele bir
        'çekirdek' parça + komşularını alır; böylece zamanla tüm içerik kapsanır.
        """
        rng = rng or random.Random()
        self._ensure()
        if not self._chunks:
            return []
        if topic.strip():
            hits = self.search(topic, k=max(12, n_chunks * 3), doc_ids=doc_ids)
            if not hits:
                return []
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

        ds = set(doc_ids) if doc_ids else None
        pool = [c for c in self._chunks if ds is None or c["doc_id"] in ds]
        if not pool:
            return []
        good = [c for c in pool if c.get("quality", 1.0) >= 0.6] or pool
        min_used = min(c["used_count"] for c in good)
        fresh = [c for c in good if c["used_count"] <= min_used + 1]
        seed = rng.choice(fresh)
        span = max(1, (n_chunks - 1) // 2)
        ctx = [c for c in self.db.neighbors(seed, span=span) if chunk_quality(c["text"]) >= 0.4] or [seed]
        return ctx[:n_chunks]
