"""Uygulama ayarları ve veri klasörü yönetimi."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_NAME = "MedQuiz"


def data_dir() -> Path:
    """Kullanıcıya özel veri klasörü (Windows: %APPDATA%\\MedQuiz)."""
    override = os.environ.get("MEDQUIZ_DATA_DIR")
    if override:
        base = Path(override)
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


PROVIDERS = {
    "anthropic": {
        "label": "Claude (Anthropic)",
        "short": "Claude",
        "models": ["claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5-5"],
        "key_url": "https://console.anthropic.com/settings/keys",
        "backup_model": "claude-haiku-5-5",
    },
    "gemini": {
        "label": "Gemini (Google)",
        "short": "Gemini",
        "models": ["gemini-3.8-flash", "gemini-4-argon", "gemini-3.5-flash-lite"],
        "key_url": "https://aistudio.google.com/apikey",
        "backup_model": "gemini-3.5-flash-lite",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
    },
    "openai": {
        "label": "OpenAI",
        "short": "OpenAI",
        "models": ["gpt-4.1-mini", "gpt-4.1", "gpt-4o-mini"],
        "key_url": "https://platform.openai.com/api-keys",
        "backup_model": "gpt-4o-mini",
    },
}


@dataclass
class Settings:
    provider: str = "anthropic"
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5-5"
    openai_model: str = "gpt-4.1-mini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"
    fallback_enabled: bool = True
    auto_update: bool = True
    gemini_save_quota: bool = True   # sohbet/çeviri/puanlamada hafif model: güçlü modelin günlük kotası korunur
    use_embeddings: bool = True
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    user_name: str = ""
    default_difficulty: str = "Orta"
    default_count: int = 5
    font_size: int = 11
    extra: dict = field(default_factory=dict)

    # --- sağlayıcı yardımcıları ---
    def key_for(self, provider: str) -> str:
        return (getattr(self, f"{provider}_api_key", "") or "").strip()

    def model_for(self, provider: str) -> str:
        return (getattr(self, f"{provider}_model", "") or PROVIDERS[provider]["models"][0]).strip()

    def configured_providers(self) -> list[str]:
        """Anahtarı girilmiş sağlayıcılar; birincil sağlayıcı en başta."""
        order = [self.provider] + [p for p in PROVIDERS if p != self.provider]
        return [p for p in order if p in PROVIDERS and self.key_for(p)]

    @property
    def has_any_key(self) -> bool:
        return bool(self.configured_providers())

    @property
    def api_key(self) -> str:
        return self.key_for(self.provider)

    @property
    def model(self) -> str:
        return self.model_for(self.provider)

    def label_for(self, provider: str) -> str:
        return f"{PROVIDERS[provider]['short']} · {self.model_for(provider)}"

    # --- kalıcılık ---
    @staticmethod
    def path() -> Path:
        return data_dir() / "settings.json"

    @classmethod
    def load(cls) -> "Settings":
        p = cls.path()
        if not p.exists():
            return cls()
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self, touch: bool = True) -> None:
        """touch=False: eşitlemeden gelen ayar uygulanırken değişiklik zamanı değiştirilmez."""
        if touch:
            import time
            self.extra["_saved_at"] = time.time()
        p = self.path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
