"""Özet sayfası: seçili kaynaktan (PDF sayfaları / konu) hazır ya da kendi şablonunla ayrıntılı analiz."""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from PySide6.QtCore import QMarginsF, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QPageLayout, QPageSize, QPdfWriter, QTextDocument
from PySide6.QtWidgets import (
    QAbstractItemView, QHeaderView, QTableWidget, QTableWidgetItem, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSplitter,
    QTextBrowser, QVBoxLayout, QWidget,
)

from core.mdfix import normalize_markdown as fix_md
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
    quiz_requested = Signal(list, str)      # belge kimlikleri, konu

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

        # kaynak listesi: birden çok kaynak işaretlenebilir; PDF'lerde her birine ayrı sayfa aralığı
        self.src_tbl = QTableWidget(0, 3)
        self.src_tbl.setHorizontalHeaderLabels(["Kaynak (birden çok seçebilirsin)", "Tür", "Sayfalar (boş = tümü)"])
        self.src_tbl.verticalHeader().hide()
        self.src_tbl.setShowGrid(False)
        self.src_tbl.setSelectionMode(QAbstractItemView.NoSelection)
        self.src_tbl.setEditTriggers(QAbstractItemView.AllEditTriggers)
        hh = self.src_tbl.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.Interactive)
        self.src_tbl.setColumnWidth(2, 190)
        self.src_tbl.setFixedHeight(132)
        self.src_tbl.itemChanged.connect(self._doc_changed)
        self.filter_le = QLineEdit()
        self.filter_le.setPlaceholderText("Kaynaklarda ara…")
        self.filter_le.setClearButtonEnabled(True)
        self.filter_le.textChanged.connect(self._filter)
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
        top = QHBoxLayout()
        top.addWidget(lab("Kaynaklar"))
        top.addStretch()
        top.addWidget(self.filter_le)
        g.addLayout(top, 0, 0, 1, 3)
        g.addWidget(self.src_tbl, 1, 0, 1, 3)
        trow0 = QHBoxLayout()
        trow0.addWidget(lab("Konu (isteğe bağlı)"))
        trow0.addWidget(self.topic_le, 1)
        g.addLayout(trow0, 2, 0, 1, 3)
        g.setColumnStretch(0, 1)
        g.addWidget(self.doc_info, 3, 0, 1, 3)
        g.addWidget(lab("Şablon"), 4, 0)
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
        g.addLayout(trow, 5, 0, 1, 3)
        g.addWidget(self.tpl_desc, 6, 0, 1, 3)
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
        ctx.data_synced.connect(lambda: self.refresh_list() if not self.task else None)
        ctx.data_synced.connect(lambda: self.refresh_templates(self.tpl_cb.currentData()))
        self.refresh_docs()
        self.refresh_templates()
        self.refresh_list()
        self._show(None)

    # ------------------------------------------------------------------ seçimler
    def refresh_docs(self):
        prev = {did: pages for did, pages in self.selected()} if self.src_tbl.rowCount() else {}
        self.src_tbl.blockSignals(True)
        self.src_tbl.setRowCount(0)
        docs = self.ctx.db.list_documents()
        for r, d in enumerate(docs):
            self.src_tbl.insertRow(r)
            kind = {"pdf": "PDF", "docx": "Word", "web": "Web", "web-pdf": "Web PDF"}.get(d["kind"], d["kind"])
            it = QTableWidgetItem(d["title"][:90])
            it.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            it.setCheckState(Qt.Checked if d["id"] in prev or (not prev and r == 0) else Qt.Unchecked)
            it.setData(Qt.UserRole, d["id"])
            it.setToolTip(d["title"])
            self.src_tbl.setItem(r, 0, it)
            k = QTableWidgetItem(kind)
            k.setFlags(Qt.ItemIsEnabled)
            self.src_tbl.setItem(r, 1, k)
            paged = d["kind"] in ("pdf", "web-pdf")
            pg = QTableWidgetItem(prev.get(d["id"], "") if paged else "—")
            pg.setFlags(Qt.ItemIsEnabled | Qt.ItemIsEditable if paged else Qt.ItemIsEnabled)
            pg.setToolTip("örn. 120-145 ya da 12-30, 45 · boş = tümü" if paged else "Metin kaynağında sayfa yok")
            self.src_tbl.setItem(r, 2, pg)
        self.src_tbl.blockSignals(False)
        self._filter(self.filter_le.text())
        self._doc_changed()

    def _filter(self, text: str):
        t = text.strip().lower()
        for r in range(self.src_tbl.rowCount()):
            it = self.src_tbl.item(r, 0)
            self.src_tbl.setRowHidden(r, bool(t) and t not in it.text().lower() and it.checkState() != Qt.Checked)

    def selected(self) -> list[tuple[int, str]]:
        out = []
        for r in range(self.src_tbl.rowCount()):
            it = self.src_tbl.item(r, 0)
            if it and it.checkState() == Qt.Checked:
                pg = self.src_tbl.item(r, 2)
                pages = pg.text().strip() if pg and pg.text() != "—" else ""
                out.append((it.data(Qt.UserRole), pages))
        return out

    def select_docs(self, ids: list[int]):
        self.src_tbl.blockSignals(True)
        for r in range(self.src_tbl.rowCount()):
            it = self.src_tbl.item(r, 0)
            it.setCheckState(Qt.Checked if it.data(Qt.UserRole) in ids else Qt.Unchecked)
        self.src_tbl.blockSignals(False)
        self._doc_changed()

    def _doc_changed(self, *_):
        if not self.src_tbl.rowCount():
            self.doc_info.setText("Kütüphane boş — önce Kütüphane sekmesinden kaynak ekle.")
            return
        sel = self.selected()
        if not sel:
            self.doc_info.setText("Özetlenecek en az bir kaynağı işaretle.")
            return
        n_pdf = 0
        for did, _p in sel:
            d = self.ctx.db.get_document(did)
            if d and d["kind"] == "pdf" and Path(d["source"]).exists():
                n_pdf += 1
        if len(sel) == 1:
            did = sel[0][0]
            d = self.ctx.db.get_document(did)
            if n_pdf:
                self.doc_info.setText(f"PDF · {pdf_page_count(d['source'])} sayfa · sayfalar yapay zekâya PDF olarak "
                                      "gönderilir (tablolar ve şekiller okunur). Büyük kitapta sayfa aralığı ya da "
                                      "konu yazman önerilir.")
            else:
                self.doc_info.setText("Metin kaynağı · kayıtlı metin gönderilir (Word tabloları dahil).")
        else:
            self.doc_info.setText(f"{len(sel)} kaynak seçili ({n_pdf} PDF). Sığarsa hepsi birlikte okunur ve TEK "
                                  "bütünleşik özet çıkar (ortak noktalar birleştirilir, farklar belirtilir); "
                                  "sığmazsa her kaynak kendi bölümünde özetlenir.")

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
        items = self.selected()
        if not items:
            QMessageBox.information(self, "Kaynak yok", "Özetlenecek en az bir kaynağı işaretle (Kütüphane boşsa "
                                                        "önce kaynak ekle).")
            return
        did = items[0][0]
        if not self.ctx.settings.has_any_key:
            QMessageBox.warning(self, "API anahtarı gerekli", "Lütfen önce Ayarlar sekmesinden API anahtarınızı girin.")
            return
        key, name, prompt = self._template()
        want_tables = TEMPLATES[key].get("tables", False) if key in TEMPLATES else None
        topic = self.topic_le.text().strip()
        engine = self.ctx.summary_engine
        try:
            src = engine.build_multi(items, topic)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Kaynak hazırlanamadı", str(e))
            return
        n = len(src.packets)
        self.current = {"doc_id": did, "doc_ids": src.doc_ids, "title": src.title, "scope": src.scope, "template": name, "markdown": "",
                        "model": "", "notes": src.notes}
        self.buf = ""
        self._show(self.current, streaming=True)
        self.run_btn.setEnabled(False)
        self.stop_btn.show()
        self.busy.show()
        self.status.setText(f"{src.title} · {src.scope} · {n} bölüm hâlinde işleniyor"
                            f"{' (PDF sayfaları olarak)' if src.packets[0].pdf or any(m.pdf for m in src.packets[0].members) else ''}… Uzun kaynaklarda birkaç "
                            "dakika sürebilir; yazılanlar aşağıda canlı görünür.")
        self._flush.start()
        state = {"stop": False}
        self._state = state

        def job(progress, task):
            for piece in engine.run(src, prompt, topic, should_stop=lambda: state["stop"] or task.cancelled,
                                    want_tables=want_tables):
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
        self.current["markdown"] = fix_md(self.buf.strip())
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
            self.view.setMarkdown(self._header(self.current) + (fix_md(self.buf) or "_Kaynak okunuyor…_"))
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
                "<ol><li>Yukarıdaki listeden <b>kaynakları işaretle</b> — bir ya da birden çok (PDF, Word, metin, web). "
                "Birden çok kaynak birlikte okunur ve tek bütünleşik özet çıkar.</li>"
                "<li>Büyük bir kitapsa satırındaki <b>Sayfalar</b> hücresine aralık (örn. 120-145) ya da aşağıya <b>konu</b> "
                "yaz; küçük kaynakta boş bırak.</li>"
                "<li><b>Şablon</b> seç: Detaylı özet, + 10 tekrar sorusu, pediatrik vaka seti, tam analiz, İngilizce "
                "cloze testi, akademik kelimeler… ya da “Düzenle / kendi şablonum” ile kendi komutunu ekle.</li>"
                "<li><b>Özeti oluştur</b>. Tablolar gerçek tablo olarak gelir; sonuç otomatik kaydedilir, PDF olarak "
                "indirilebilir.</li></ol>"
                "<p>İpucu: Özette bilmediğin bir kelimeyi seçip sağ tıkla → kelime havuzuna ekle.</p></div>")
            self.meta.setText("")
            self.vocab_btn.hide()
            return
        if not streaming:
            self.view.setMarkdown(self._header(c) + fix_md(c["markdown"]))
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
            self.quiz_requested.emit(list(self.current.get("doc_ids") or [self.current["doc_id"]]), "")

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
    doc.setMarkdown(fix_md(markdown))
    writer = QPdfWriter(path)
    writer.setPageSize(QPageSize(QPageSize.A4))
    writer.setPageMargins(QMarginsF(16, 16, 16, 16), QPageLayout.Millimeter)
    writer.setResolution(96)
    writer.setTitle(title or "MedQuiz özet")
    doc.setPageSize(writer.pageLayout().paintRectPixels(writer.resolution()).size().toSizeF())
    doc.print_(writer)
    return Path(path).exists() and Path(path).stat().st_size > 0
