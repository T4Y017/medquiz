"""Sayfaların paylaştığı uygulama durumu."""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from core.case import CaseEngine
from core.config import Settings, data_dir
from core.db import Database
from core.llm import RoutedLLM
from core.quiz import QuizEngine
from core.retrieval import Embedder, Retriever

from .workers import Runner


class AppContext(QObject):
    library_changed = Signal()
    settings_changed = Signal()
    profile_changed = Signal(str)
    index_status = Signal(str)

    def __init__(self):
        super().__init__()
        self.settings = Settings.load()
        self.db = Database(data_dir() / "medquiz.db")
        self.runner = Runner()
        self.embedder = self._make_embedder()
        self.retriever = Retriever(self.db, self.embedder)
        self.engine = QuizEngine(self.db, self.retriever, lambda: RoutedLLM(self.settings))
        self.case_engine = CaseEngine(self.db, self.retriever, self.engine, lambda: RoutedLLM(self.settings))
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
        """İndeksi ve eksik embedding'leri arka planda hazırlar."""
        def job(progress, task):
            progress("Arama dizini hazırlanıyor… (ilk seferde anlamsal arama modeli indirilebilir)")
            self.retriever.warmup()
            if self.embedder and self.embedder.error:
                return f"Anahtar kelime araması aktif (anlamsal model yüklenemedi)"
            if self.retriever.semantic_enabled:
                return f"Hibrit arama aktif · {self.retriever.size} parça"
            return f"Anahtar kelime araması aktif · {self.retriever.size} parça"

        self.runner.start(job, on_result=self.index_status.emit, on_progress=self.index_status.emit,
                          on_error=lambda e: self.index_status.emit(f"Dizin hatası: {e}"))

    def set_profile(self, name: str) -> None:
        self.profile = name.strip()
        profiles = self.settings.extra.setdefault("profiles", [])
        if self.profile and self.profile not in profiles:
            profiles.append(self.profile)
        self.settings.user_name = self.profile
        self.settings.save()
        self.profile_changed.emit(self.profile)
