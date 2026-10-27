"""Kaynaklı soru-cevap sohbeti (RAG) — sohbet geçmişi kalıcı olarak saklanır."""
from __future__ import annotations

import datetime as dt

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit,
    QPushButton, QTextBrowser, QVBoxLayout, QWidget,
)

from core.config import PROVIDERS
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


def _when(ts: float) -> str:
    d = dt.datetime.fromtimestamp(ts)
    today = dt.date.today()
    if d.date() == today:
        return d.strftime("Bugün %H:%M")
    if d.date() == today - dt.timedelta(days=1):
        return d.strftime("Dün %H:%M")
    return d.strftime("%d.%m.%Y")


class ChatPage(QWidget):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.setObjectName("page")
        self.ctx = ctx
        self.chat_id: int | None = None
        self.history: list[dict] = []     # {"role", "content", "sources"?, "model"?}
        self.busy = False
        self.task = None
        self._flush = QTimer(self)
        self._flush.setInterval(120)
        self._flush.timeout.connect(self._render)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(20, 22, 28, 18)
        outer.setSpacing(14)

        # --- sol: sohbet geçmişi
        side = QFrame()
        side.setObjectName("card")
        side.setFixedWidth(240)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(10, 12, 10, 10)
        sl.setSpacing(8)
        new_btn = QPushButton("＋  Yeni sohbet")
        new_btn.setObjectName("primary")
        new_btn.clicked.connect(self.new_chat)
        sl.addWidget(new_btn)
        hl = QLabel("Geçmiş sohbetler")
        hl.setObjectName("muted")
        sl.addWidget(hl)
        self.list = QListWidget()
        self.list.setStyleSheet("QListWidget{border:none; background:transparent;}"
                                "QListWidget::item{padding:7px 6px; border-radius:7px;}"
                                "QListWidget::item:selected{background:#ccfbf1; color:#0f172a;}")
        self.list.itemClicked.connect(self._open_item)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setTextElideMode(Qt.ElideRight)
        sl.addWidget(self.list, 1)
        del_btn = QPushButton("Seçili sohbeti sil")
        del_btn.setObjectName("danger")
        del_btn.clicked.connect(self.delete_selected)
        sl.addWidget(del_btn)
        outer.addWidget(side)

        # --- sağ: sohbet
        main = QWidget()
        root = QVBoxLayout(main)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)
        head = QHBoxLayout()
        t = QLabel("Sohbet")
        t.setObjectName("h1")
        head.addWidget(t)
        head.addStretch()
        al = QLabel("Yapay zekâ:")
        al.setObjectName("muted")
        head.addWidget(al)
        self.ai_cb = QComboBox()
        self.ai_cb.setMinimumWidth(130)
        self.ai_cb.setToolTip("Bu sohbette hangi yapay zekânın yanıt vereceği (tüm uygulamada geçerlidir)")
        self.ai_cb.activated.connect(self._ai_changed)
        head.addWidget(self.ai_cb)
        self.quiz_btn = QPushButton("Bu konudan quiz")
        self.quiz_btn.setToolTip("Son sorduğun konuyu Quiz sekmesine aktarır.")
        head.addWidget(self.quiz_btn)
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
        disc = QLabel("Eğitim amaçlıdır; kişisel sağlık sorunlarında mutlaka bir hekime başvurun. "
                      "Sohbetler yalnızca bu bilgisayarda saklanır.")
        disc.setObjectName("disclaimer")
        root.addWidget(disc)
        outer.addWidget(main, 1)

        ctx.settings_changed.connect(self._fill_ai)
        ctx.profile_changed.connect(lambda _: (self.new_chat(), self.refresh_list()))
        self._fill_ai()
        self.refresh_list()
        self._render()

    # ------------------------------------------------------------ AI seçici
    def _fill_ai(self):
        s = self.ctx.settings
        self.ai_cb.blockSignals(True)
        self.ai_cb.clear()
        provs = sorted(s.configured_providers(), key=list(PROVIDERS).index)
        if not provs:
            self.ai_cb.addItem("Anahtar yok — Ayarlar", None)
        for p in provs:
            self.ai_cb.addItem(PROVIDERS[p]["short"], p)
            self.ai_cb.setItemData(self.ai_cb.count() - 1, s.label_for(p), Qt.ToolTipRole)
        i = self.ai_cb.findData(s.provider)
        self.ai_cb.setCurrentIndex(max(0, i))
        self.ai_cb.blockSignals(False)

    def _ai_changed(self, _i: int):
        p = self.ai_cb.currentData()
        if p and p != self.ctx.settings.provider:
            self.ctx.settings.provider = p
            self.ctx.apply_settings()

    # ---------------------------------------------------------------- geçmiş
    def refresh_list(self):
        self.list.blockSignals(True)
        self.list.clear()
        for c in self.ctx.db.list_chats(self.ctx.profile or None):
            title = c["title"] or "Yeni sohbet"
            if len(title) > 26:
                title = title[:25].rstrip() + "…"
            it = QListWidgetItem(f"{title}\n{_when(c['updated_at'])}")
            it.setData(Qt.UserRole, c["id"])
            it.setToolTip(c["title"])
            self.list.addItem(it)
            if c["id"] == self.chat_id:
                it.setSelected(True)
        self.list.blockSignals(False)

    def _open_item(self, it: QListWidgetItem):
        cid = it.data(Qt.UserRole)
        if self.busy:
            QMessageBox.information(self, "Lütfen bekleyin", "Yanıt yazılırken sohbet değiştirilemez.")
            return
        row = self.ctx.db.get_chat(cid)
        if not row:
            return
        self.chat_id = cid
        self.history = row["messages"]
        self.list.clearSelection()
        it.setSelected(True)
        self._render()
        sb = self.view.verticalScrollBar()
        QTimer.singleShot(0, lambda: sb.setValue(sb.maximum()))

    def new_chat(self):
        if self.busy and self.task:
            self.task.cancel()
        self.chat_id = None
        self.history = []
        self._set_busy(False)
        self.list.clearSelection()
        self._render()
        self.input.setFocus()

    def delete_selected(self):
        items = self.list.selectedItems()
        if not items:
            return
        cid = items[0].data(Qt.UserRole)
        if QMessageBox.question(self, "Sil", "Bu sohbet silinsin mi?") != QMessageBox.Yes:
            return
        self.ctx.db.delete_chat(cid)
        if cid == self.chat_id:
            self.new_chat()
        self.refresh_list()

    def _persist(self):
        if not self.history:
            return
        first = next((m["content"] for m in self.history if m["role"] == "user"), "Sohbet")
        title = " ".join(first.split())[:70]
        msgs = []
        for m in self.history:
            d = {"role": m["role"], "content": m["content"]}
            if m.get("model"):
                d["model"] = m["model"]
            if m.get("sources"):
                d["sources"] = [{"title": c.get("title", ""), "location": c.get("location", "")}
                                for c in m["sources"]]
            msgs.append(d)
        self.chat_id = self.ctx.db.save_chat(self.chat_id, title, msgs, self.ctx.profile)
        self.refresh_list()

    # ------------------------------------------------------------------ gönder
    def last_user_question(self) -> str:
        for m in reversed(self.history):
            if m["role"] == "user":
                return m["content"]
        return ""

    def clear(self):
        self.new_chat()

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
        prior = [{"role": m["role"], "content": m["content"]} for m in self.history
                 if m["content"] and not m["content"].lstrip().startswith("⚠️")]
        self.history.append({"role": "user", "content": text})
        self.history.append({"role": "assistant", "content": "", "sources": []})
        self._persist()
        self._set_busy(True)
        self._render()
        self._flush.start()
        engine = self.ctx.engine
        target = self.history[-1]
        chat_at_start = self.chat_id

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
            if self.chat_id == chat_at_start:
                self._persist()
                self._render()
            else:   # kullanıcı bu arada başka sohbete geçtiyse yine de kaydet
                cur_id, cur_hist = self.chat_id, self.history
                self.chat_id, self.history = chat_at_start, hist_ref
                self._persist()
                self.chat_id, self.history = cur_id, cur_hist

        hist_ref = self.history
        self.task = self.ctx.runner.start(job, on_progress=on_prog, on_error=on_err, on_finished=on_fin)

    # ------------------------------------------------------------------ çiz
    def _render(self):
        if not self.history:
            self.view.setHtml(
                "<div style='color:#64748b; padding:40px; text-align:center'>"
                "<h2>Merhaba 👋</h2><p>Kütüphanene eklediğin kaynaklar hakkında soru sorabilirsin.</p>"
                "<p><i>Örnek: “Akut pankreatitte Ranson kriterleri nelerdir?”</i></p>"
                "<p style='font-size:small'>Sohbetler otomatik kaydedilir; soldan eskilerine dönebilirsin.</p></div>")
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
