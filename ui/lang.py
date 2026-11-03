"""İçerik dili seçicisi (soru, özet, kart, sohbet yanıtı). Arayüz dili Türkçe kalır."""
from __future__ import annotations

from PySide6.QtWidgets import QComboBox

LANGS = [("tr", "Türkçe"), ("en", "English")]


def default_lang(ctx) -> str:
    return ctx.settings.extra.get("content_lang", "tr")


def lang_combo(ctx, auto: bool = False, tooltip: str = "") -> QComboBox:
    cb = QComboBox()
    if auto:
        cb.addItem("Otomatik (sorduğun dilde)", "auto")
    for code, name in LANGS:
        cb.addItem(name, code)
    if not auto:
        i = cb.findData(default_lang(ctx))
        cb.setCurrentIndex(max(0, i))
    if tooltip:
        cb.setToolTip(tooltip)
    return cb
