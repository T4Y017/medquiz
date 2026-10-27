"""Destek için tanılama raporu: sürüm, ayarlar (anahtarsız), kütüphane durumu, son log satırları."""
from __future__ import annotations

import platform
import re
import sys

from .config import PROVIDERS, data_dir

_SECRET = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|AIza[0-9A-Za-z_\-]{20,}|sk-ant-[A-Za-z0-9_\-]{8,}|"
                     r"(?i:api[_-]?key[\"'=:\s]+)[A-Za-z0-9_\-]{12,})")


def redact(text: str) -> str:
    return _SECRET.sub("[GİZLENDİ]", text)


def build_report(ctx) -> str:
    s = ctx.settings
    lines = ["MedQuiz tanılama raporu", "=" * 30]
    try:
        from app import VERSION  # type: ignore
    except Exception:  # noqa: BLE001
        VERSION = "?"
    lines.append(f"Sürüm: {VERSION} · Python {sys.version.split()[0]} · {platform.platform()}")
    lines.append(f"Birincil: {s.provider} · yedekleme: {'açık' if s.fallback_enabled else 'kapalı'}")
    for p in PROVIDERS:
        lines.append(f"  {p}: anahtar {'VAR' if s.key_for(p) else 'yok'} · model {s.model_for(p)}")
    try:
        docs = ctx.db.list_documents()
        lines.append(f"Kütüphane: {len(docs)} belge · {sum(d['n_chunks'] for d in docs)} parça")
        for d in docs[:15]:
            lines.append(f"  - {d['title'][:60]} ({d['kind']}, {d['n_chunks']} parça)")
        done, total = ctx.retriever.embed_progress
        emb = ctx.embedder
        lines.append(f"Anlamsal arama: {'kapalı' if not emb else ('hazır' if emb.ready else 'yüklenmedi')}"
                     f" · {done}/{total} parça vektörlü" + (f" · hata: {emb.error[:150]}" if emb and emb.error else ""))
    except Exception as e:  # noqa: BLE001
        lines.append(f"Kütüphane okunamadı: {e}")
    log = data_dir() / "medquiz.log"
    if log.exists():
        tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]
        lines.append("")
        lines.append("Son log satırları:")
        lines.extend(tail)
    return redact("\n".join(lines))
