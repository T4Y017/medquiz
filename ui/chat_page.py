"""Kaynaklı soru-cevap sohbeti (RAG)."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QPushButton, QTextBrowser, QVBoxLayout, QWidget,
)

from core.quiz import source_label

from .context import AppContext


class _Input(QPlainTextEdit):
    """Enter gönderir, Shift+Enter yeni satır."""

    def __init__(self, on_send):
        super().__init__()
        self.on_send = on_send

    def keyPressEvent(self, e: QKeyEvent):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and not (e.modifiers() & Qt.ShiftModifier):
            self.on_send()
            return
        super().keyPressEvent(e)


class ChatPage(QWidget):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.setObjectName("page")
        self.ctx = ctx
        self.history: list[dict] = []     # {"role", "content", "sources"?}
        self.busy = False
        self.task = None
        self._pending = ""
        self._flush = QTimer(self)
        self._flush.setInterval(120)
        self._flush.timeout.connect(self._render)

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 22, 28, 18)
        root.setSpacing(12)
        head = QHBoxLayout()
        t = QLabel("Sohbet")
        t.setObjectName("h1")
        head.addWidget(t)
        sub = QLabel("Kütüphanendeki kaynaklara dayanarak sorularını yanıtlar.")
        sub.setObjectName("muted")
        head.addWidget(sub)
        head.addStretch()
        self.quiz_btn = QPushButton("Bu konudan quiz yap")
        self.quiz_btn.setToolTip("Son sorduğun konuyu Quiz sekmesine aktarır.")
        head.addWidget(self.quiz_btn)
        clear = QPushButton("Temizle")
        clear.clicked.connect(self.clear)
        head.addWidget(clear)
        root.addLayout(head)

        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(True)
        root.addWidget(self.view, 1)

        row = QHBoxLayout()
        self.input = _Input(self.send)
        self.input.setPlaceholderText("Bir soru yaz… (Enter: gönder, Shift+Enter: yeni satır)")
        self.input.setFixedHeight(80)
        row.addWidget(self.input, 1)
        self.send_btn = QPushButton("Gönder")
        self.send_btn.setObjectName("primary")
        self.send_btn.setFixedHeight(80)
        self.send_btn.clicked.connect(self.send)
        row.addWidget(self.send_btn)
        root.addLayout(row)
        disc = QLabel("Eğitim amaçlıdır; kişisel sağlık sorunlarında mutlaka bir hekime başvurun.")
        disc.setObjectName("disclaimer")
        root.addWidget(disc)
        self._render()

    def last_user_question(self) -> str:
        for m in reversed(self.history):
            if m["role"] == "user":
                return m["content"]
        return ""

    def clear(self):
        if self.busy and self.task:
            self.task.cancel()
        self.history = []
        self.busy = False
        self._set_busy(False)
        self._render()

    def _set_busy(self, on: bool):
        self.busy = on
        self.send_btn.setEnabled(not on)
        self.send_btn.setText("…" if on else "Gönder")

    def send(self):
        text = self.input.toPlainText().strip()
        if not text or self.busy:
            return
        if not self.ctx.settings.has_any_key:
            QMessageBox.warning(self, "API anahtarı gerekli", "Lütfen önce Ayarlar sekmesinden API anahtarınızı girin.")
            return
        self.input.clear()
        prior = [{"role": m["role"], "content": m["content"]} for m in self.history if m["content"]]
        self.history.append({"role": "user", "content": text})
        self.history.append({"role": "assistant", "content": "", "sources": []})
        self._set_busy(True)
        self._render()
        self._flush.start()
        engine = self.ctx.engine
        target = self.history[-1]

        def job(progress, task):
            stream, hits, llm = engine.chat_stream(text, prior)
            progress(("sources", hits))
            try:
                for piece in stream:
                    if task.cancelled:
                        break
                    progress(("text", piece))
            finally:
                progress(("model", getattr(llm, "label", "") + (" (yedek)" if getattr(llm, "fell_back", False) else "")))
            return True

        def on_prog(ev):
            kind, val = ev
            if kind == "sources":
                target["sources"] = val
            elif kind == "model":
                target["model"] = val
            else:
                target["content"] += val

        def on_err(e):
            target["content"] += f"\n\n⚠️ {e}"

        def on_fin():
            self._flush.stop()
            self._set_busy(False)
            self._render()

        self.task = self.ctx.runner.start(job, on_progress=on_prog, on_error=on_err, on_finished=on_fin)

    def _render(self):
        if not self.history:
            self.view.setHtml(
                "<div style='color:#64748b; padding:40px; text-align:center'>"
                "<h2>Merhaba 👋</h2><p>Kütüphanene eklediğin kaynaklar hakkında soru sorabilirsin.</p>"
                "<p><i>Örnek: “Akut pankreatitte Ranson kriterleri nelerdir?”</i></p></div>")
            return
        md = []
        for m in self.history:
            if m["role"] == "user":
                md.append(f"#### 🧑 Sen\n\n{m['content']}\n")
            else:
                body = m["content"] or ("_Kaynaklar aranıyor ve yanıt yazılıyor…_" if self.busy else "")
                model = f" · _{m['model']}_" if m.get("model") else ""
                md.append(f"#### 🩺 Asistan{model}\n\n{body}\n")
                if m.get("sources") and m["content"]:
                    groups: dict[str, list[str]] = {}
                    for i, c in enumerate(m["sources"], 1):
                        groups.setdefault(source_label(c), []).append(f"K{i}")
                    lines = [f"- {', '.join(keys)}: {lbl}" for lbl, keys in groups.items()]
                    md.append("📖 **Kaynaklar**\n\n" + "\n".join(lines) + "\n")
            md.append("\n---\n")
        sb = self.view.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 40
        self.view.setMarkdown("\n".join(md))
        if at_bottom or self.busy:
            sb.setValue(sb.maximum())
