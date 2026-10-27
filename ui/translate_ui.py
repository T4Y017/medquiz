"""Çeviri penceresi, sağ tık menüsü (çevir / kelime havuzuna ekle) ve kısa bildirimler."""
from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QAction, QCursor, QGuiApplication
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QTextBrowser,
    QTextEdit, QToolTip, QVBoxLayout, QWidget,
)

from core.translate import clean_term

from . import style
from .context import AppContext


def toast(parent: QWidget, text: str, ms: int = 3500) -> None:
    """İmlecin yanında kısa, engellemeyen bildirim."""
    QToolTip.showText(QCursor.pos() + QPoint(12, 12), text, parent, parent.rect(), ms)


def selected_text(w: QWidget) -> str:
    if isinstance(w, QLabel):
        t = w.selectedText()
    elif isinstance(w, (QTextBrowser, QTextEdit, QPlainTextEdit)):
        t = w.textCursor().selectedText()
    else:
        t = ""
    return (t or "").replace(" ", "\n").replace(" ", "\n").strip()


def plain_text(w: QWidget) -> str:
    if isinstance(w, QLabel):
        from PySide6.QtGui import QTextDocument
        d = QTextDocument()
        d.setHtml(w.text()) if Qt.mightBeRichText(w.text()) else d.setPlainText(w.text())
        return d.toPlainText()
    if isinstance(w, (QTextBrowser, QTextEdit, QPlainTextEdit)):
        return w.toPlainText()
    return ""


# ------------------------------------------------------------------ kelime ekleme
def add_to_vocab(ctx: AppContext, parent: QWidget, term: str, context: str = "", source: str = "") -> None:
    term = clean_term(term)
    if not term:
        return
    if len(term.split()) > 6:
        if QMessageBox.question(parent, "Uzun ifade",
                                f"“{term[:60]}” oldukça uzun. Yine de kelime havuzuna eklensin mi?") != QMessageBox.Yes:
            return
    if ctx.db.find_vocab(term, ctx.profile):
        toast(parent, f"📘 “{term}” zaten kelime havuzunda.")
        return
    if not ctx.settings.has_any_key:
        ctx.db.add_vocab(term, source=source, profile=ctx.profile, example=context[:300])
        ctx.vocab_changed.emit()
        toast(parent, f"📘 “{term}” eklendi (anlamı Kelimeler sayfasından girilebilir).")
        return
    toast(parent, f"📘 “{term}” ekleniyor… anlamı hazırlanıyor", 2000)
    tr = ctx.translator

    def done(d):
        vid = ctx.db.add_vocab(d["term"], d["meaning"], d["definition"], d["example"], d["pos"], d["ipa"],
                               source=source, profile=ctx.profile)
        if vid is None:   # model sözlük biçimine çevirdi ve o biçim zaten vardı
            toast(parent, f"📘 “{d['term']}” zaten kelime havuzunda.")
            return
        ctx.vocab_changed.emit()
        toast(parent, f"📘 Eklendi: <b>{d['term']}</b> — {d['meaning']}", 4500)

    def fail(e):
        vid = ctx.db.add_vocab(term, source=source, profile=ctx.profile, example=context[:300])
        if vid:
            ctx.vocab_changed.emit()
        QMessageBox.warning(parent, "Anlam alınamadı",
                            f"“{term}” havuza eklendi ama anlamı alınamadı:\n{e}\n\n"
                            "Kelimeler sayfasından anlamı elle girebilir ya da “Anlamı doldur” diyebilirsiniz.")

    ctx.runner.start(lambda p, t: tr.define(term, context), on_result=done, on_error=fail)


# ------------------------------------------------------------------ çeviri penceresi
class TranslateDialog(QDialog):
    def __init__(self, ctx: AppContext, text: str, source: str = "", parent: QWidget | None = None,
                 title: str = "Çeviri"):
        super().__init__(parent)
        self.ctx = ctx
        self.source = source
        self.text = text.strip()
        self.setWindowTitle(title)
        self.resize(1000, 620)
        lay = QVBoxLayout(self)
        tip = QLabel("İpucu: Bilmediğin bir kelimeyi soldaki metinde seçip <b>Kelime havuzuna ekle</b>'ye bas "
                     "(veya sağ tıkla). Kelimeler sayfasında bilgi kartlarıyla çalışabilirsin.")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        lay.addWidget(tip)
        split = QSplitter(Qt.Horizontal)
        self.orig = QTextBrowser()
        self.orig.setPlainText(self.text)
        self.trans = QTextBrowser()
        self.trans.setPlainText("Çevriliyor…")
        for w, h in ((self.orig, "Orijinal"), (self.trans, "Türkçe")):
            box = QWidget()
            bl = QVBoxLayout(box)
            bl.setContentsMargins(0, 0, 0, 0)
            lbl = QLabel(h)
            lbl.setObjectName("h2")
            bl.addWidget(lbl)
            bl.addWidget(w)
            split.addWidget(box)
        lay.addWidget(split, 1)
        attach_menu(self.orig, ctx, source, title="")
        attach_menu(self.trans, ctx, source, title="", allow_translate=False)
        row = QHBoxLayout()
        self.model_lbl = QLabel("")
        self.model_lbl.setObjectName("muted")
        row.addWidget(self.model_lbl, 1)
        add = QPushButton("📘 Seçili kelimeyi havuza ekle")
        add.clicked.connect(self._add_selected)
        cp = QPushButton("Çeviriyi kopyala")
        cp.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.trans.toPlainText()))
        close = QPushButton("Kapat")
        close.setObjectName("primary")
        close.clicked.connect(self.accept)
        row.addWidget(add)
        row.addWidget(cp)
        row.addWidget(close)
        lay.addLayout(row)
        self._buf = ""
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(lambda: self.trans.setPlainText(self._buf or "Çevriliyor…"))
        self._start()

    def _add_selected(self):
        sel = selected_text(self.orig) or selected_text(self.trans)
        if not sel:
            toast(self, "Önce soldaki metinde bir kelime seçin.")
            return
        ctx_text = self.orig.toPlainText() if selected_text(self.orig) else self.trans.toPlainText()
        add_to_vocab(self.ctx, self, sel, ctx_text, self.source)

    def _start(self):
        if not self.text:
            self.trans.setPlainText("")
            return
        if not self.ctx.settings.has_any_key:
            self.trans.setPlainText("Çeviri için Ayarlar'dan bir API anahtarı girin.")
            return
        tr = self.ctx.translator

        def job(progress, task):
            for piece in tr.translate_stream(self.text):
                if task.cancelled:
                    break
                progress(piece)
            return tr.last_label

        def prog(piece):
            self._buf += piece

        def done(label):
            self._timer.stop()
            self.trans.setPlainText(self._buf)
            self.model_lbl.setText(f"🤖 {label}" if label else "📦 önbellekten")

        def fail(e):
            self._timer.stop()
            self.trans.setPlainText(f"⚠️ Çeviri yapılamadı: {e}")
            self.trans.setStyleSheet(f"color:{style.BAD};")

        self._timer.start()
        self.task = self.ctx.runner.start(job, on_progress=prog, on_result=done, on_error=fail)

    def done(self, r):
        t = getattr(self, "task", None)
        if t:
            t.cancel()
        super().done(r)


def open_translation(ctx: AppContext, parent: QWidget, text: str, source: str = "", title: str = "Çeviri") -> None:
    dlg = TranslateDialog(ctx, text, source, parent, title)
    dlg.setAttribute(Qt.WA_DeleteOnClose)
    dlg.show()


# ------------------------------------------------------------------ sağ tık menüsü
def attach_menu(w: QWidget, ctx: AppContext, source: str = "", title: str = "Çeviri",
                allow_translate: bool = True, context_getter=None) -> None:
    """Metin içeren bir widget'a 'çevir' ve 'kelime havuzuna ekle' sağ tık seçeneklerini ekler."""
    w.setContextMenuPolicy(Qt.CustomContextMenu)

    def show(pos):
        sel = selected_text(w)
        if isinstance(w, (QTextBrowser, QTextEdit, QPlainTextEdit)):
            menu = w.createStandardContextMenu()
            menu.addSeparator()
        else:
            menu = QMenu(w)
            if sel:
                cp = QAction("Kopyala", menu)
                cp.triggered.connect(lambda: QGuiApplication.clipboard().setText(sel))
                menu.addAction(cp)
                menu.addSeparator()
        full = (context_getter() if context_getter else plain_text(w)) or ""
        if sel:
            short = sel if len(sel) <= 30 else sel[:28] + "…"
            if len(sel) <= 80:
                a = QAction(f"📘 “{short}” kelime havuzuna ekle", menu)
                a.triggered.connect(lambda: add_to_vocab(ctx, w.window(), sel, full, source))
                menu.addAction(a)
            if allow_translate:
                t = QAction("🌐 Seçili metni çevir", menu)
                t.triggered.connect(lambda: open_translation(ctx, w.window(), sel, source, title or "Çeviri"))
                menu.addAction(t)
        if allow_translate and full.strip():
            a2 = QAction("🌐 Tümünü çevir", menu)
            a2.triggered.connect(lambda: open_translation(ctx, w.window(), full, source, title or "Çeviri"))
            menu.addAction(a2)
        if not sel:
            hint = QAction("(Kelime eklemek için önce metni seçin)", menu)
            hint.setEnabled(False)
            menu.addAction(hint)
        menu.exec(w.mapToGlobal(pos))

    w.customContextMenuRequested.connect(show)
