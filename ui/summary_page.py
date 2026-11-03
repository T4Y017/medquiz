"""Özet sayfası: seçili kaynaktan (PDF sayfaları / konu) hazır ya da kendi şablonunla ayrıntılı analiz."""
from __future__ import annotations

import datetime as dt
import html
import re
from pathlib import Path

from PySide6.QtCore import QMarginsF, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QPageLayout, QPageSize, QPdfWriter, QTextDocument
from PySide6.QtWidgets import (
    QToolButton, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSplitter,
    QTextBrowser, QVBoxLayout, QWidget,
)

from core.mdfix import normalize_markdown as fix_md
from core.summary import TEMPLATES, pdf_page_count, vocab_rows

from . import style
from .context import AppContext
from .flow import FlowLayout
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


class SourceChip(QFrame):
    """Seçili bir kaynak: başlık, tür/sayfa sayısı, (PDF ise) sayfa aralığı kutusu, kaldır düğmesi."""
    removed = Signal()
    changed = Signal()

    def __init__(self, doc: dict, n_pages: int, pages: str = ""):
        super().__init__()
        self.doc = doc
        self.n_pages = n_pages
        self.setObjectName("chipCard")
        self.setStyleSheet("QFrame#chipCard{background:#f0fdfa; border:1px solid #99f6e4; border-radius:10px;}"
                           "QFrame#chipCard QLabel{background:transparent; border:none;}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 4, 4, 4)
        lay.setSpacing(6)
        kind = {"pdf": "PDF", "docx": "Word", "web": "Web", "web-pdf": "Web PDF"}.get(doc["kind"], doc["kind"])
        t = doc["title"]
        title = QLabel(f"<b>{html.escape(t if len(t) <= 38 else t[:37] + '…')}</b>")
        title.setToolTip(t)
        lay.addWidget(title)
        info = QLabel(f"{kind}" + (f" · {n_pages} s." if n_pages else ""))
        info.setObjectName("muted")
        lay.addWidget(info)
        self.page_le = QLineEdit(pages)
        self.page_le.setPlaceholderText("tüm sayfalar")
        self.page_le.setToolTip("Hangi sayfalar? Örn. 120-145 ya da 12-30, 45 · boş = tümü")
        self.page_le.setFixedWidth(120)
        self.page_le.textChanged.connect(self._validate)
        self.page_lbl = QLabel("")
        self.page_lbl.setObjectName("muted")
        if n_pages:
            lay.addWidget(self.page_le)
            lay.addWidget(self.page_lbl)
        else:
            self.page_le.hide()
            self.page_lbl.hide()
        x = QToolButton()
        x.setText("✕")
        x.setToolTip("Bu kaynağı çıkar")
        x.setAutoRaise(True)
        x.setCursor(Qt.PointingHandCursor)
        x.clicked.connect(self.removed.emit)
        lay.addWidget(x)
        self._validate()

    def pages(self) -> str:
        return self.page_le.text().strip() if self.n_pages else ""

    def _validate(self, *_):
        from core.summary import parse_pages
        spec = self.page_le.text().strip()
        ok = True
        if spec and self.n_pages:
            got = parse_pages(spec, self.n_pages)
            ok = bool(got)
            self.page_lbl.setText(f"→ {len(got)} s." if got else "⚠ anlaşılmadı")
        else:
            self.page_lbl.setText("")
        self.page_le.setStyleSheet("" if ok else "border:1px solid #b91c1c;")
        self.changed.emit()


class SourcePicker(QDialog):
    """Kütüphaneden kaynak seçme: arama + satırın her yerine tıklayınca seç/bırak."""

    def __init__(self, docs: list[dict], selected: set[int], page_count, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Kaynak seç")
        self.resize(640, 560)
        lay = QVBoxLayout(self)
        info = QLabel("Özetlenecek kaynakları seç. Birden çok seçersen birlikte okunur ve tek özet çıkar.")
        info.setWordWrap(True)
        lay.addWidget(info)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Ara… (kitap ya da makale adı)")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        lay.addWidget(self.search)
        self.list = QListWidget()
        self.list.setStyleSheet("QListWidget::item{padding:8px 6px; border-bottom:1px solid #e2e8f0;}"
                                "QListWidget::item:hover{background:#f0fdfa;}")
        for d in docs:
            kind = {"pdf": "PDF", "docx": "Word", "web": "Web", "web-pdf": "Web PDF"}.get(d["kind"], d["kind"])
            n = page_count(d)
            when = dt.datetime.fromtimestamp(d["added_at"]).strftime("%d.%m.%Y") if d.get("added_at") else ""
            it = QListWidgetItem(f"{d['title']}\n{kind}" + (f" · {n} sayfa" if n else "")
                                 + (f" · eklendi {when}" if when else ""))
            it.setData(Qt.UserRole, d["id"])
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if d["id"] in selected else Qt.Unchecked)
            self.list.addItem(it)
        self.list.itemClicked.connect(self._toggle)
        self.list.itemChanged.connect(lambda _: self._count())
        lay.addWidget(self.list, 1)
        row = QHBoxLayout()
        self.count_lbl = QLabel("")
        self.count_lbl.setObjectName("muted")
        row.addWidget(self.count_lbl, 1)
        none = QPushButton("Hiçbiri")
        none.clicked.connect(lambda: self._set_all(Qt.Unchecked))
        row.addWidget(none)
        lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Seçilenleri kullan")
        bb.button(QDialogButtonBox.Ok).setObjectName("primary")
        bb.button(QDialogButtonBox.Cancel).setText("Vazgeç")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._count()
        self.search.setFocus()

    def _toggle(self, it: QListWidgetItem):
        # onay kutusuna değil satıra tıklanınca da seç/bırak (kutuya tıklamada Qt zaten değiştirir)
        pos = self.list.viewport().mapFromGlobal(self.cursor().pos())
        if pos.x() > 28:
            it.setCheckState(Qt.Unchecked if it.checkState() == Qt.Checked else Qt.Checked)

    def _filter(self, text: str):
        t = text.strip().lower()
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setHidden(bool(t) and t not in it.text().lower())

    def _set_all(self, state):
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(state)

    def _count(self):
        n = len(self.selected_ids())
        self.count_lbl.setText(f"{n} kaynak seçili" if n else "Henüz seçim yok")

    def selected_ids(self) -> list[int]:
        return [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.Checked]


class SummaryPage(QWidget):
    quiz_requested = Signal(list, str)      # belge kimlikleri, konu
    cards_made = Signal()                   # kartlar kaydedildi → Kartlar sekmesine geç

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
        self.cfg_card = card
        g = QGridLayout(card)
        g.setContentsMargins(16, 14, 16, 14)
        g.setHorizontalSpacing(10)
        g.setVerticalSpacing(8)

        def lab(s):
            l = QLabel(s)
            l.setObjectName("muted")
            return l

        # seçili kaynaklar: her biri bir "etiket" (başlık · tür · sayfa kutusu · ✕); ekleme seçim penceresinden
        self.chips_w = QWidget()
        self.chips = FlowLayout(self.chips_w, spacing=8)
        self.add_src_btn = QPushButton("＋ Kaynak ekle")
        self.add_src_btn.setObjectName("primary")
        self.add_src_btn.setToolTip("Kütüphaneden bir ya da birden çok kaynak seç")
        self.add_src_btn.clicked.connect(self.pick_sources)
        self.clear_src_btn = QPushButton("Temizle")
        self.clear_src_btn.clicked.connect(lambda: self.select_docs([]))
        self._chips: dict[int, SourceChip] = {}
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
        top.addWidget(self.clear_src_btn)
        top.addWidget(self.add_src_btn)
        g.addLayout(top, 0, 0, 1, 3)
        g.addWidget(self.chips_w, 1, 0, 1, 3)
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
        self.left_panel = left
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
        vt = QHBoxLayout()
        vt.addStretch()
        self.zoom_out_btn = QPushButton("A−")
        self.zoom_out_btn.setToolTip("Yazıyı küçült (Ctrl + fare tekerleği de olur)")
        self.zoom_out_btn.clicked.connect(lambda: self._zoom(-1))
        self.zoom_in_btn = QPushButton("A+")
        self.zoom_in_btn.setToolTip("Yazıyı büyüt (Ctrl + fare tekerleği de olur)")
        self.zoom_in_btn.clicked.connect(lambda: self._zoom(+1))
        self.focus_btn = QPushButton("⛶ Geniş görünüm")
        self.focus_btn.setCheckable(True)
        self.focus_btn.setToolTip("Özeti tüm sayfaya yay: ayarları ve kayıtlı listeyi gizle (Esc ile geri dön)")
        self.focus_btn.toggled.connect(self.set_focus_mode)
        for b in (self.zoom_out_btn, self.zoom_in_btn, self.focus_btn):
            b.setFixedHeight(30)
            vt.addWidget(b)
        rl.addLayout(vt)
        rl.addWidget(self.view, 1)
        br = QHBoxLayout()
        self.meta = QLabel("")
        self.meta.setObjectName("muted")
        br.addWidget(self.meta, 1)
        self.vocab_btn = QPushButton("📘 Kelimeler → havuz")
        self.vocab_btn.setToolTip("Özetteki kelime tablosunu Kelimeler sekmesine ekler")
        self.vocab_btn.clicked.connect(self.add_vocab)
        self.vocab_btn.hide()
        self.cards_btn = QPushButton("🃏 Bilgi kartı yap")
        self.cards_btn.setToolTip("Bu özetten soru–cevap bilgi kartları çıkar (kaydetmeden önce görürsün)")
        self.cards_btn.clicked.connect(self.make_cards)
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
        for b in (self.vocab_btn, self.cards_btn, self.quiz_btn, cp, md, pdf):
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
        """Kütüphane değişti: silinen kaynakların etiketlerini kaldır; hiç seçim yoksa son seçimi geri yükle."""
        docs = {d["id"]: d for d in self.ctx.db.list_documents()}
        self._docs = docs
        for did in [d for d in self._chips if d not in docs]:
            self._remove_chip(did, save=False)
        if not self._chips:
            last = self.ctx.settings.extra.get("summary_sources") or []
            for did, pages in last:
                if did in docs:
                    self._add_chip(did, pages)
            if not self._chips and docs:
                self._add_chip(next(iter(docs)))
        self._doc_changed()

    def _page_count(self, d: dict) -> int:
        cache = self.__dict__.setdefault("_pc", {})
        if d["id"] not in cache:
            cache[d["id"]] = pdf_page_count(d["source"]) if d["kind"] == "pdf" and Path(d["source"]).exists() else 0
        return cache[d["id"]]

    def _add_chip(self, did: int, pages: str = "") -> None:
        d = self._docs.get(did) if hasattr(self, "_docs") else self.ctx.db.get_document(did)
        if not d or did in self._chips:
            return
        chip = SourceChip(d, self._page_count(d), pages)
        chip.removed.connect(lambda x=did: self._remove_chip(x))
        chip.changed.connect(self._doc_changed)
        self._chips[did] = chip
        self.chips.addWidget(chip)
        self.chips_w.updateGeometry()

    def _remove_chip(self, did: int, save: bool = True) -> None:
        chip = self._chips.pop(did, None)
        if chip:
            self.chips.removeWidget(chip)
            chip.deleteLater()
            self.chips_w.updateGeometry()
        if save:
            self._doc_changed()

    def pick_sources(self):
        docs = list(self.ctx.db.list_documents())
        if not docs:
            QMessageBox.information(self, "Kütüphane boş", "Önce Kütüphane sekmesinden kaynak ekle.")
            return
        dlg = SourcePicker(docs, set(self._chips), self._page_count, self)
        if dlg.exec():
            chosen = dlg.selected_ids()
            pages = {did: c.pages() for did, c in self._chips.items()}
            for did in list(self._chips):
                if did not in chosen:
                    self._remove_chip(did, save=False)
            for did in chosen:
                self._add_chip(did, pages.get(did, ""))
            self._doc_changed()

    def selected(self) -> list[tuple[int, str]]:
        return [(did, c.pages()) for did, c in self._chips.items()]

    def select_docs(self, ids: list[int], pages: dict | None = None):
        for did in list(self._chips):
            self._remove_chip(did, save=False)
        for did in ids:
            self._add_chip(did, (pages or {}).get(did, ""))
        self._doc_changed()

    def set_pages(self, did: int, spec: str):
        if did in self._chips:
            self._chips[did].page_le.setText(spec)

    def _doc_changed(self, *_):
        sel = self.selected()
        self.ctx.settings.extra["summary_sources"] = [[d, p] for d, p in sel]
        self.clear_src_btn.setVisible(bool(sel))
        if not getattr(self, "_docs", None):
            self.doc_info.setText("Kütüphane boş — önce Kütüphane sekmesinden kaynak ekle.")
            return
        if not sel:
            self.doc_info.setText("Özetlemek için “＋ Kaynak ekle” ile bir ya da birden çok kaynak seç.")
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
                self.doc_info.setText("Sayfalar yapay zekâya PDF olarak gönderilir (tablolar ve şekiller okunur). "
                                      "Büyük kitapta etiketteki sayfa kutusuna aralık (örn. 120-145) ya da aşağıya "
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
        fb = (getattr(self.ctx.summary_engine, "table_report", {}) or {}).get("fell_back")
        if fb:
            QMessageBox.information(
                self, "Özet yedek modelle yapıldı",
                f"Bu özet birincil model yerine yedek modelle ({fb}) hazırlandı. Bunun en sık nedeni, "
                "Gemini'nin ücretsiz katmanındaki günlük istek sınırının dolmasıdır. Yedek model daha hafif "
                "olduğu için özet daha kısa ya da eksik olabilir.\n\nAyarlar → “Bugünkü kullanım” satırında "
                "kotayı görebilirsin; kota her gün yaklaşık 10:00'da yenilenir.")
        self.status.setText(("⚠ Yedek modelle yapıldı. " if fb else "") + "✓ Özet hazır ve kaydedildi." + (f" Not: {' '.join(self.current['notes'])}"
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
        self._apply_zoom()
        if c is None:
            self.view.setHtml(
                f"<div style='color:{style.MUTED}; padding:30px'>"
                "<h2>Nasıl kullanılır?</h2>"
                "<ol><li><b>＋ Kaynak ekle</b> ile kaynakları seç — bir ya da birden çok (PDF, Word, metin, web). "
                "Birden çok kaynak birlikte okunur ve tek bütünleşik özet çıkar. Etiketteki ✕ ile çıkarırsın.</li>"
                "<li>Büyük bir kitapsa etiketindeki <b>sayfa kutusuna</b> aralık yaz (örn. 120-145; yanında kaç sayfa "
                "olduğu görünür) ya da aşağıya <b>konu</b> yaz; küçük kaynakta boş bırak.</li>"
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

    def _zoom(self, step: int):
        z = max(-4, min(10, int(self.ctx.settings.extra.get("summary_zoom", 0)) + step))
        self.ctx.settings.extra["summary_zoom"] = z
        self.ctx.settings.save()
        self._apply_zoom()

    def _apply_zoom(self):
        f = self.view.font()
        f.setPointSizeF(max(7.0, self.ctx.settings.font_size + int(self.ctx.settings.extra.get("summary_zoom", 0))))
        self.view.document().setDefaultFont(f)

    def set_focus_mode(self, on: bool):
        self.cfg_card.setVisible(not on)
        self.left_panel.setVisible(not on)
        self.focus_btn.setText("✕ Geniş görünümden çık" if on else "⛶ Geniş görünüm")
        if self.focus_btn.isChecked() != on:
            self.focus_btn.setChecked(on)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape and self.focus_btn.isChecked():
            self.set_focus_mode(False)
            return
        super().keyPressEvent(e)

    def make_cards(self):
        c = self.current
        if not c or not c.get("markdown") or self.task:
            QMessageBox.information(self, "Bilgi kartı", "Önce bir özet oluştur ya da soldan kayıtlı bir özet aç.")
            return
        from core.cards import cards_from_summary
        from core.quiz import make_llm
        factory = self.ctx.summary_engine.llm_factory
        md, title = c["markdown"], c["title"]
        self.cards_btn.setEnabled(False)
        self.cards_btn.setText("Kartlar hazırlanıyor…")
        self.busy.show()

        def done(cards):
            self._cards_reset()
            if not cards:
                QMessageBox.warning(self, "Bilgi kartı", "Bu özetten kart çıkarılamadı. Tekrar deneyebilirsin.")
                return
            from .cards_page import CardPreviewDialog
            dlg = CardPreviewDialog(cards, title[:80], self)
            if not dlg.exec():
                return
            chosen = dlg.cards()
            n = self.ctx.db.add_cards(chosen, dlg.deck.text().strip() or title[:80], f"özet: {title[:60]}",
                                      self.ctx.profile)
            self.ctx.cards_changed.emit()
            box = QMessageBox(self)
            box.setWindowTitle("Bilgi kartları")
            box.setText(f"🃏 {n} kart “{dlg.deck.text().strip() or title[:80]}” destesine eklendi."
                        + (f" ({len(chosen) - n} tanesi zaten vardı.)" if len(chosen) > n else ""))
            go = box.addButton("Kartlara git ve çalış", QMessageBox.AcceptRole)
            box.addButton("Kapat", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is go:
                self.cards_made.emit()

        def fail(e):
            self._cards_reset()
            QMessageBox.warning(self, "Bilgi kartı", str(e))

        self.ctx.runner.start(lambda p, t: cards_from_summary(make_llm(factory, "heavy"), md, title),
                              on_result=done, on_error=fail)

    def _cards_reset(self):
        self.cards_btn.setEnabled(True)
        self.cards_btn.setText("🃏 Bilgi kartı yap")
        if not self.task:
            self.busy.hide()

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
