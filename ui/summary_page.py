"""Özet sayfası: seçili kaynaktan (PDF sayfaları / konu) hazır ya da kendi şablonunla ayrıntılı analiz."""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from PySide6.QtCore import QMarginsF, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QPageLayout, QPageSize, QPdfWriter, QTextDocument
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSplitter,
    QTextBrowser, QVBoxLayout, QWidget,
)

from core.summary import TEMPLATES, pdf_page_count, vocab_rows

from . import style
from .context import AppContext
from .translate_ui import attach_menu, toast


class TemplateDialog(QDialog):
    def __init__(self, name: str, prompt: str, parent=None, deletable: bool = False):
        super().__init__(parent)
        self.setWindowTitle("Şablon")
        self.resize(760, 560)
        self.deleted = False
        lay = QVBoxLayout(self)
        info = QLabel("Kendi komutunu yaz ya da yapıştır. <b>[TOPIC]</b> yazdığın yere konu (ya da belge adı) gelir. "
                      "Tablolar otomatik olarak gerçek tablo hâlinde, görseller açıklamalı verilir.")
        info.setWordWrap(True)
        info.setObjectName("muted")
        lay.addWidget(info)
        f = QFormLayout()
        self.name = QLineEdit(name)
        f.addRow("Şablon adı", self.name)
        lay.addLayout(f)
        self.prompt = QPlainTextEdit(prompt)
        lay.addWidget(self.prompt, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Save).setText("Kaydet")
        bb.button(QDialogButtonBox.Cancel).setText("Vazgeç")
        if deletable:
            d = bb.addButton("Şablonu sil", QDialogButtonBox.DestructiveRole)
            d.clicked.connect(self._delete)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _delete(self):
        self.deleted = True
        self.accept()


class SummaryPage(QWidget):
    quiz_requested = Signal(int, str)       # doc_id, konu

    def __init__(self, ctx: AppContext):
        super().__init__()
        self.setObjectName("page")
        self.ctx = ctx
        self.task = None
        self.buf = ""
        self.current: dict | None = None    # {"doc_id","title","scope","template","markdown","model","notes"}
        self._flush = QTimer(self)
        self._flush.setInterval(350)
        self._flush.timeout.connect(self._render_buf)

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 22, 28, 18)
        root.setSpacing(12)
        head = QHBoxLayout()
        t = QLabel("Özet ve analiz")
        t.setObjectName("h1")
        head.addWidget(t)
        sub = QLabel("Kaynaktan ayrıntılı özet, klinik protokol, tablolar, soru setleri")
        sub.setObjectName("muted")
        head.addWidget(sub)
        head.addStretch()
        root.addLayout(head)

        card = QFrame()
        card.setObjectName("card")
        g = QGridLayout(card)
        g.setContentsMargins(16, 14, 16, 14)
        g.setHorizontalSpacing(10)
        g.setVerticalSpacing(8)

        def lab(s):
            l = QLabel(s)
            l.setObjectName("muted")
            return l

        self.doc_cb = QComboBox()
        self.doc_cb.setMinimumWidth(260)
        self.doc_cb.currentIndexChanged.connect(self._doc_changed)
        self.pages_le = QLineEdit()
        self.pages_le.setPlaceholderText("boş = tümü · örn. 120-145")
        self.topic_le = QLineEdit()
        self.topic_le.setPlaceholderText("ya da konu: örn. allerjik rinit tedavisi")
        self.tpl_cb = QComboBox()
        self.tpl_cb.currentIndexChanged.connect(self._tpl_changed)
        self.tpl_desc = QLabel("")
        self.tpl_desc.setObjectName("muted")
        self.tpl_desc.setWordWrap(True)
        self.doc_info = QLabel("")
        self.doc_info.setObjectName("muted")
        self.doc_info.setWordWrap(True)
        g.addWidget(lab("Kaynak"), 0, 0)
        g.addWidget(lab("Sayfalar (PDF)"), 0, 1)
        g.addWidget(lab("Konu (isteğe bağlı)"), 0, 2)
        g.addWidget(self.doc_cb, 1, 0)
        g.addWidget(self.pages_le, 1, 1)
        g.addWidget(self.topic_le, 1, 2)
        g.setColumnStretch(0, 2)
        g.setColumnStretch(1, 1)
        g.setColumnStretch(2, 2)
        g.addWidget(self.doc_info, 2, 0, 1, 3)
        g.addWidget(lab("Şablon"), 3, 0)
        trow = QHBoxLayout()
        trow.addWidget(self.tpl_cb, 1)
        self.edit_btn = QPushButton("Düzenle / kendi şablonum")
        self.edit_btn.clicked.connect(self.edit_template)
        trow.addWidget(self.edit_btn)
        self.run_btn = QPushButton("Özeti oluştur")
        self.run_btn.setObjectName("primary")
        self.run_btn.clicked.connect(self.run)
        self.stop_btn = QPushButton("Durdur")
        self.stop_btn.clicked.connect(self.stop)
        self.stop_btn.hide()
        trow.addWidget(self.stop_btn)
        trow.addWidget(self.run_btn)
        g.addLayout(trow, 4, 0, 1, 3)
        g.addWidget(self.tpl_desc, 5, 0, 1, 3)
        root.addWidget(card)

        st = QHBoxLayout()
        self.busy = QProgressBar()
        self.busy.setRange(0, 0)
        self.busy.setFixedWidth(140)
        self.busy.setTextVisible(False)
        self.busy.hide()
        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        st.addWidget(self.busy)
        st.addWidget(self.status, 1)
        root.addLayout(st)

        split = QSplitter(Qt.Horizontal)
        left = QFrame()
        left.setObjectName("card")
        ll = QVBoxLayout(left)
        ll.setContentsMargins(10, 10, 10, 10)
        hl = QLabel("Kayıtlı özetler")
        hl.setObjectName("h2")
        ll.addWidget(hl)
        self.list = QListWidget()
        self.list.setStyleSheet("QListWidget{border:none; background:transparent;}"
                                "QListWidget::item{padding:6px; border-radius:6px;}"
                                "QListWidget::item:selected{background:#ccfbf1; color:#0f172a;}")
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.itemClicked.connect(self._open_item)
        ll.addWidget(self.list, 1)
        dl = QPushButton("Sil")
        dl.setObjectName("danger")
        dl.clicked.connect(self.delete_selected)
        ll.addWidget(dl)
        split.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(True)
        attach_menu(self.view, ctx, "özet", "Özet — çeviri")
        rl.addWidget(self.view, 1)
        br = QHBoxLayout()
        self.meta = QLabel("")
        self.meta.setObjectName("muted")
        br.addWidget(self.meta, 1)
        self.vocab_btn = QPushButton("📘 Kelimeler → havuz")
        self.vocab_btn.setToolTip("Özetteki kelime tablosunu Kelimeler sekmesine ekler")
        self.vocab_btn.clicked.connect(self.add_vocab)
        self.vocab_btn.hide()
        self.quiz_btn = QPushButton("📝 Quiz")
        self.quiz_btn.setToolTip("Bu kaynaktan quiz başlat")
        self.quiz_btn.clicked.connect(self._quiz)
        cp = QPushButton("Kopyala")
        cp.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.current["markdown"] if self.current else ""))
        md = QPushButton(".md")
        md.setToolTip("Markdown dosyası olarak kaydet (Word/Notion/Obsidian'a yapıştırılabilir)")
        md.clicked.connect(self.save_md)
        pdf = QPushButton("PDF")
        pdf.setToolTip("Tablolarıyla birlikte PDF olarak kaydet")
        pdf.setObjectName("primary")
        pdf.clicked.connect(self.save_pdf)
        for b in (self.vocab_btn, self.quiz_btn, cp, md, pdf):
            br.addWidget(b)
        rl.addLayout(br)
        split.addWidget(right)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 4)
        split.setSizes([230, 900])
        root.addWidget(split, 1)

        ctx.library_changed.connect(self.refresh_docs)
        ctx.profile_changed.connect(lambda _: self.refresh_list())
        self.refresh_docs()
        self.refresh_templates()
        self.refresh_list()
        self._show(None)

    # ------------------------------------------------------------------ seçimler
    def refresh_docs(self):
        cur = self.doc_cb.currentData()
        self.doc_cb.blockSignals(True)
        self.doc_cb.clear()
        for d in self.ctx.db.list_documents():
            kind = {"pdf": "PDF", "docx": "Word", "web": "Web", "web-pdf": "Web PDF"}.get(d["kind"], d["kind"])
            self.doc_cb.addItem(f"{d['title'][:60]}  ·  {kind}", d["id"])
        if cur is not None and self.doc_cb.findData(cur) >= 0:
            self.doc_cb.setCurrentIndex(self.doc_cb.findData(cur))
        self.doc_cb.blockSignals(False)
        self._doc_changed()

    def _doc_changed(self, *_):
        did = self.doc_cb.currentData()
        if did is None:
            self.doc_info.setText("Kütüphane boş — önce Kütüphane sekmesinden kaynak ekle.")
            return
        d = self.ctx.db.get_document(did)
        if d and d["kind"] == "pdf" and Path(d["source"]).exists():
            n = pdf_page_count(d["source"])
            self.pages_le.setEnabled(True)
            self.doc_info.setText(f"PDF · {n} sayfa · sayfalar yapay zekâya PDF olarak gönderilir (tablolar ve şekiller "
                                  "okunur). Uzun aralıklar 25 sayfalık bölümlere ayrılır; büyük kitapta aralık ya da "
                                  "konu seçmen önerilir.")
        else:
            self.pages_le.setEnabled(bool(d and d["kind"] in ("pdf", "web-pdf")))
            self.doc_info.setText("Metin kaynağı · kayıtlı metin gönderilir (Word tabloları dahil).")

    def _custom(self) -> list[dict]:
        return self.ctx.settings.extra.setdefault("summary_templates", [])

    def refresh_templates(self, select: str | None = None):
        self.tpl_cb.blockSignals(True)
        self.tpl_cb.clear()
        for k, t in TEMPLATES.items():
            self.tpl_cb.addItem(t["name"], k)
        for c in self._custom():
            self.tpl_cb.addItem(f"★ {c['name']}", c["id"])
        if select and self.tpl_cb.findData(select) >= 0:
            self.tpl_cb.setCurrentIndex(self.tpl_cb.findData(select))
        self.tpl_cb.blockSignals(False)
        self._tpl_changed()

    def _template(self) -> tuple[str, str, str]:
        key = self.tpl_cb.currentData()
        if key in TEMPLATES:
            t = TEMPLATES[key]
            return key, t["name"], t["prompt"]
        c = next((c for c in self._custom() if c["id"] == key), None)
        return (key, c["name"], c["prompt"]) if c else ("detayli", TEMPLATES["detayli"]["name"],
                                                        TEMPLATES["detayli"]["prompt"])

    def _tpl_changed(self, *_):
        key = self.tpl_cb.currentData()
        self.tpl_desc.setText(TEMPLATES[key]["desc"] if key in TEMPLATES else "Kendi şablonun.")

    def edit_template(self):
        key, name, prompt = self._template()
        custom = key not in TEMPLATES
        dlg = TemplateDialog(name if custom else f"{name} (kopyam)", prompt, self, deletable=custom)
        if not dlg.exec():
            return
        lst = self._custom()
        if dlg.deleted:
            self.ctx.settings.extra["summary_templates"] = [c for c in lst if c["id"] != key]
            self.ctx.settings.save()
            self.refresh_templates()
            return
        new_name = dlg.name.text().strip() or "Şablonum"
        text = dlg.prompt.toPlainText().strip()
        if not text:
            return
        if custom:
            for c in lst:
                if c["id"] == key:
                    c["name"], c["prompt"] = new_name, text
        else:
            key = f"c{len(lst) + 1}_{abs(hash(new_name)) % 10000}"
            lst.append({"id": key, "name": new_name, "prompt": text})
        self.ctx.settings.save()
        self.refresh_templates(select=key)

    # ------------------------------------------------------------------ çalıştır
    def run(self):
        did = self.doc_cb.currentData()
        if did is None:
            QMessageBox.information(self, "Kaynak yok", "Önce Kütüphane sekmesinden bir kaynak ekle.")
            return
        if not self.ctx.settings.has_any_key:
            QMessageBox.warning(self, "API anahtarı gerekli", "Lütfen önce Ayarlar sekmesinden API anahtarınızı girin.")
            return
        key, name, prompt = self._template()
        pages, topic = self.pages_le.text().strip(), self.topic_le.text().strip()
        engine = self.ctx.summary_engine
        try:
            src = engine.build_source(did, pages if self.pages_le.isEnabled() else "", topic)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Kaynak hazırlanamadı", str(e))
            return
        n = len(src.packets)
        self.current = {"doc_id": did, "title": src.title, "scope": src.scope, "template": name, "markdown": "",
                        "model": "", "notes": src.notes}
        self.buf = ""
        self._show(self.current, streaming=True)
        self.run_btn.setEnabled(False)
        self.stop_btn.show()
        self.busy.show()
        self.status.setText(f"{src.title} · {src.scope} · {n} bölüm hâlinde işleniyor"
                            f"{' (PDF sayfaları olarak)' if src.packets[0].pdf else ''}… Uzun kaynaklarda birkaç "
                            "dakika sürebilir; yazılanlar aşağıda canlı görünür.")
        self._flush.start()
        state = {"stop": False}
        self._state = state

        def job(progress, task):
            for piece in engine.run(src, prompt, topic, should_stop=lambda: state["stop"] or task.cancelled):
                progress(piece)
            return getattr(engine, "last_label", "")

        def prog(p):
            self.buf += p

        def done(label):
            self._finish(label, None)

        def fail(e):
            self._finish("", e)

        self.task = self.ctx.runner.start(job, on_progress=prog, on_result=done, on_error=fail)

    def stop(self):
        if getattr(self, "_state", None):
            self._state["stop"] = True
        self.status.setText("Durduruluyor…")

    def _finish(self, label: str, err: str | None):
        self._flush.stop()
        self.run_btn.setEnabled(True)
        self.stop_btn.hide()
        self.busy.hide()
        self.task = None
        if self.current is None:
            return
        self.current["markdown"] = self.buf.strip()
        self.current["model"] = label
        if err and not self.current["markdown"]:
            self.status.setText("")
            self._show(None)
            QMessageBox.warning(self, "Özet oluşturulamadı", err)
            return
        if err:
            self.current["markdown"] += f"\n\n> ⚠️ Yarıda kaldı: {err}"
        sid = self.ctx.db.save_summary(self.current["doc_id"], self.current["title"], self.current["scope"],
                                       self.current["template"], self.current["markdown"], label, self.ctx.profile)
        self.current["id"] = sid
        self.status.setText("✓ Özet hazır ve kaydedildi." + (f" Not: {' '.join(self.current['notes'])}"
                                                            if self.current["notes"] else ""))
        self._show(self.current)
        self.refresh_list(select=sid)

    def _render_buf(self):
        if self.current is not None:
            sb = self.view.verticalScrollBar()
            at_end = sb.value() >= sb.maximum() - 60
            self.view.setMarkdown(self._header(self.current) + (self.buf or "_Kaynak okunuyor…_"))
            if at_end:
                sb.setValue(sb.maximum())

    @staticmethod
    def _header(c: dict) -> str:
        notes = "".join(f"\n> ⓘ {n}" for n in c.get("notes") or [])
        return f"# {c['title']}\n\n*{c['scope']} · {c['template']}*{notes}\n\n---\n\n"

    def _show(self, c: dict | None, streaming: bool = False):
        if c is None:
            self.view.setHtml(
                f"<div style='color:{style.MUTED}; padding:30px'>"
                "<h2>Nasıl kullanılır?</h2>"
                "<ol><li>Yukarıdan <b>kaynağı</b> seç (Kütüphane'deki PDF, Word, metin, web).</li>"
                "<li>Büyük bir kitapsa <b>sayfa aralığı</b> (örn. 120-145) ya da <b>konu</b> yaz; küçük kaynakta boş bırak.</li>"
                "<li><b>Şablon</b> seç: Detaylı özet, + 10 tekrar sorusu, pediatrik vaka seti, tam analiz, İngilizce "
                "cloze testi, akademik kelimeler… ya da “Düzenle / kendi şablonum” ile kendi komutunu ekle.</li>"
                "<li><b>Özeti oluştur</b>. Tablolar gerçek tablo olarak gelir; sonuç otomatik kaydedilir, PDF olarak "
                "indirilebilir.</li></ol>"
                "<p>İpucu: Özette bilmediğin bir kelimeyi seçip sağ tıkla → kelime havuzuna ekle.</p></div>")
            self.meta.setText("")
            self.vocab_btn.hide()
            return
        if not streaming:
            self.view.setMarkdown(self._header(c) + c["markdown"])
            self.view.verticalScrollBar().setValue(0)
        else:
            self._render_buf()
        self.meta.setText(f"🤖 {c['model']}" if c.get("model") else "")
        self.vocab_btn.setVisible(bool(vocab_rows(c.get("markdown", ""))))

    # ------------------------------------------------------------------ kayıtlı özetler
    def refresh_list(self, select: int | None = None):
        self.list.clear()
        for s in self.ctx.db.list_summaries(self.ctx.profile):
            when = dt.datetime.fromtimestamp(s["created_at"]).strftime("%d.%m %H:%M")
            title = s["title"] if len(s["title"]) <= 28 else s["title"][:27] + "…"
            it = QListWidgetItem(f"{title}\n{s['scope'][:30]} · {when}")
            it.setToolTip(f"{s['title']}\n{s['scope']}\n{s['template']}")
            it.setData(Qt.UserRole, s["id"])
            self.list.addItem(it)
            if s["id"] == select:
                it.setSelected(True)

    def _open_item(self, it: QListWidgetItem):
        if self.task:
            return
        s = self.ctx.db.get_summary(it.data(Qt.UserRole))
        if s:
            self.current = {"id": s["id"], "doc_id": s["doc_id"], "title": s["title"], "scope": s["scope"],
                            "template": s["template"], "markdown": s["markdown"], "model": s["model"], "notes": []}
            self._show(self.current)

    def delete_selected(self):
        items = self.list.selectedItems()
        if not items or QMessageBox.question(self, "Sil", "Bu özet silinsin mi?") != QMessageBox.Yes:
            return
        sid = items[0].data(Qt.UserRole)
        self.ctx.db.delete_summary(sid)
        if self.current and self.current.get("id") == sid:
            self.current = None
            self._show(None)
        self.refresh_list()

    # ------------------------------------------------------------------ dışa aktarma / diğer
    def _safe_name(self) -> str:
        c = self.current or {}
        return (re.sub(r'[\\/:*?"<>|]+', "", f"{c.get('title', 'ozet')} {c.get('scope', '')}")[:70].strip()
                or "ozet")

    def save_md(self):
        if not self.current or not self.current.get("markdown"):
            return
        path, _ = QFileDialog.getSaveFileName(self, "Markdown kaydet", str(Path.home() / f"{self._safe_name()}.md"),
                                              "Markdown (*.md)")
        if path:
            Path(path).write_text(self._header(self.current) + self.current["markdown"], encoding="utf-8")
            toast(self, "Kaydedildi.")

    def save_pdf(self):
        if not self.current or not self.current.get("markdown"):
            return
        path, _ = QFileDialog.getSaveFileName(self, "PDF kaydet", str(Path.home() / f"{self._safe_name()}.pdf"),
                                              "PDF (*.pdf)")
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        ok = write_markdown_pdf(self._header(self.current) + self.current["markdown"], path, self.current["title"])
        if ok:
            toast(self, "PDF kaydedildi.")
        else:
            QMessageBox.warning(self, "Hata", "PDF yazılamadı. Dosya başka bir programda açık olabilir.")

    def _quiz(self):
        if self.current and self.current.get("doc_id"):
            self.quiz_requested.emit(self.current["doc_id"], "")

    def add_vocab(self):
        rows = vocab_rows(self.current.get("markdown", "") if self.current else "")
        added = 0
        for r in rows:
            if self.ctx.db.add_vocab(r["term"], r["meaning"], r["definition"], r["example"], source="özet",
                                     profile=self.ctx.profile):
                added += 1
        self.ctx.vocab_changed.emit()
        toast(self, f"📘 {added} kelime havuza eklendi ({len(rows) - added} zaten vardı).", 4500)


def write_markdown_pdf(markdown: str, path: str, title: str = "") -> bool:
    doc = QTextDocument()
    doc.setDefaultStyleSheet("body { font-family: 'Segoe UI', 'Noto Sans', Arial; font-size: 10pt; } "
                             "table { border-collapse: collapse; } td, th { border: 1px solid #cbd5e1; padding: 4px; }")
    doc.setMarkdown(markdown)
    writer = QPdfWriter(path)
    writer.setPageSize(QPageSize(QPageSize.A4))
    writer.setPageMargins(QMarginsF(16, 16, 16, 16), QPageLayout.Millimeter)
    writer.setResolution(96)
    writer.setTitle(title or "MedQuiz özet")
    doc.setPageSize(writer.pageLayout().paintRectPixels(writer.resolution()).size().toSizeF())
    doc.print_(writer)
    return Path(path).exists() and Path(path).stat().st_size > 0
