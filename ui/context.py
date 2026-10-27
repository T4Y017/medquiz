"""Sayfaların paylaştığı uygulama durumu."""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from core.case import CaseEngine
from core.config import Settings, data_dir
from core.db import Database
from core.llm import RoutedLLM
from core.quiz import QuizEngine
from core.retrieval import Embedder, Retriever
from core.translate import Translator

from .workers import Runner


class AppContext(QObject):
    library_changed = Signal()
    settings_changed = Signal()
    profile_changed = Signal(str)
    index_status = Signal(str)
    vocab_changed = Signal()
    ai_status = Signal(str)          # "model meşgul, 20 sn sonra tekrar" gibi anlık durumlar

    def __init__(self):
        super().__init__()
        self.settings = Settings.load()
        self.db = Database(data_dir() / "medquiz.db")
        self.runner = Runner()
        self.embedder = self._make_embedder()
        self.retriever = Retriever(self.db, self.embedder)
        factory = lambda task="heavy": RoutedLLM(self.settings, task=task)  # noqa: E731
        self.engine = QuizEngine(self.db, self.retriever, factory)
        self.case_engine = CaseEngine(self.db, self.retriever, self.engine, factory)
        self.translator = Translator(self.db, lambda: RoutedLLM(self.settings, task="light"))
        from core import usage
        usage.attach(self.db)
        RoutedLLM.status_hook = staticmethod(self.ai_status.emit)    # arka plan iş parçacığından güvenli
        self.profile = self.settings.user_name or ""

    def _make_embedder(self) -> Embedder | None:
        if not self.settings.use_embeddings:
            return None
        cache = data_dir() / "models"
        cache.mkdir(exist_ok=True)
        return Embedder(self.settings.embedding_model, cache_dir=str(cache))

    # ------------------------------------------------------------------
    def apply_settings(self) -> None:
        old_emb = self.embedder.model_name if self.embedder else None
        self.settings.save()
        new_emb = self.settings.embedding_model if self.settings.use_embeddings else None
        if old_emb != new_emb:
            self.embedder = self._make_embedder()
            self.retriever.embedder = self.embedder
            self.retriever.invalidate()
            self.warmup_index()
        self.settings_changed.emit()

    def notify_library_changed(self) -> None:
        self.retriever.invalidate()
        self.library_changed.emit()
        self.warmup_index()

    def warmup_index(self) -> None:
        """Anahtar kelime dizinini hemen kurar; anlamsal vektörleri arka planda parça parça hesaplar.
        Bu sırada quiz/vaka/sohbet beklemeden çalışır."""
        retriever = self.retriever

        def job(progress, task):
            retriever.warmup()
            n = retriever.size
            progress(f"Anahtar kelime araması hazır · {n} parça")
            if not self.embedder:
                return f"Anahtar kelime araması aktif · {n} parça"
            if not self.embedder.ready:
                progress(f"Anahtar kelime araması hazır · {n} parça · anlamsal arama modeli yükleniyor "
                         "(ilk seferde ~220 MB indirilir)…")

            def on_prog(p):
                done, total = p
                if total and done < total:
                    progress(f"Arama hazır · anlamsal dizin arka planda hazırlanıyor: {done}/{total} parça "
                             f"(%{done * 100 // total})")

            state = retriever.embed_missing(progress=on_prog, should_stop=lambda: task.cancelled)
            if state == "busy":
                return None
            if state == "failed" or (self.embedder and self.embedder.error):
                return f"Anahtar kelime araması aktif · {n} parça (anlamsal model yüklenemedi)"
            if state == "stopped":
                return None
            return f"Hibrit arama aktif · {n} parça"

        def done(msg):
            if msg:
                self.index_status.emit(msg)

        if getattr(self, "_index_task", None):
            self._index_task.cancel()
        self._index_task = self.runner.start(job, on_result=done, on_progress=self.index_status.emit,
                                             on_error=lambda e: self.index_status.emit(f"Dizin hatası: {e}"))

    def set_profile(self, name: str) -> None:
        self.profile = name.strip()
        profiles = self.settings.extra.setdefault("profiles", [])
        if self.profile and self.profile not in profiles:
            profiles.append(self.profile)
        self.settings.user_name = self.profile
        self.settings.save()
        self.profile_changed.emit(self.profile)
