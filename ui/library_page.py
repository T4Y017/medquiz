"""Kütüphane: belge ekleme (dosya, klasör, web), listeleme, silme."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QProgressBar, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.ingest import SUPPORTED_EXT, IngestError, chunk_doc, load_any

from .context import AppContext

KIND_LABEL = {"pdf": "PDF", "docx": "Word", "txt": "Metin", "md": "Markdown", "markdown": "Markdown",
              "web": "Web", "web-pdf": "Web (PDF)"}


class LibraryPage(QWidget):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.setObjectName("page")
        self.ctx = ctx
        self.setAcceptDrops(True)
        self.ingesting = False

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 22, 28, 18)
        root.setSpacing(12)
        head = QHBoxLayout()
        t = QLabel("Kütüphane")
        t.setObjectName("h1")
        head.addWidget(t)
        head.addStretch()
        self.summary = QLabel("")
        self.summary.setObjectName("muted")
        head.addWidget(self.summary)
        root.addLayout(head)

        card = QFrame()
        card.setObjectName("card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 14, 16, 14)
        cl.setSpacing(10)
        r1 = QHBoxLayout()
        lbl = QLabel("Konu etiketi:")
        lbl.setObjectName("muted")
        self.topic_le = QLineEdit()
        self.topic_le.setPlaceholderText("örn. Kardiyoloji, Farmakoloji (isteğe bağlı — eklenen belgelere uygulanır)")
        r1.addWidget(lbl)
        r1.addWidget(self.topic_le, 1)
        cl.addLayout(r1)
        r2 = QHBoxLayout()
        b_files = QPushButton("📄  Dosya ekle")
        b_files.setObjectName("primary")
        b_files.clicked.connect(self.add_files)
        b_dir = QPushButton("📁  Klasör ekle")
        b_dir.clicked.connect(self.add_folder)
        self.url_le = QLineEdit()
        self.url_le.setPlaceholderText("https://… web sayfası veya PDF bağlantısı (birden fazlası için boşlukla ayırın)")
        self.url_le.returnPressed.connect(self.add_urls)
        b_url = QPushButton("🌐  Web ekle")
        b_url.clicked.connect(self.add_urls)
        r2.addWidget(b_files)
        r2.addWidget(b_dir)
        r2.addSpacing(10)
        r2.addWidget(self.url_le, 1)
        r2.addWidget(b_url)
        cl.addLayout(r2)
        hint = QLabel("Desteklenen: PDF, Word (.docx), .txt, .md ve web sayfaları. Dosyaları bu pencereye sürükleyip bırakabilirsiniz.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        cl.addWidget(hint)
        root.addWidget(card)

        st = QHBoxLayout()
        self.prog = QProgressBar()
        self.prog.setTextVisible(False)
        self.prog.setFixedWidth(180)
        self.prog.hide()
        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        st.addWidget(self.prog)
        st.addWidget(self.status, 1)
        root.addLayout(st)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Başlık", "Tür", "Konu", "Parça", "Eklendi"])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(False)
        self.table.setShowGrid(False)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        for i in range(1, 5):
            h.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        root.addWidget(self.table, 1)

        bot = QHBoxLayout()
        self.index_lbl = QLabel("")
        self.index_lbl.setObjectName("muted")
        bot.addWidget(self.index_lbl, 1)
        b_del = QPushButton("Seçileni sil")
        b_del.setObjectName("danger")
        b_del.clicked.connect(self.delete_selected)
        bot.addWidget(b_del)
        root.addLayout(bot)

        ctx.index_status.connect(self.index_lbl.setText)
        ctx.library_changed.connect(self.refresh)
        self.refresh()

    # ----------------------------------------------------------------
    def refresh(self):
        docs = self.ctx.db.list_documents()
        self.table.setRowCount(len(docs))
        total_chunks = 0
        for r, d in enumerate(docs):
            total_chunks += d["n_chunks"]
            items = [
                d["title"], KIND_LABEL.get(d["kind"], d["kind"]), d.get("topic") or "—", str(d["n_chunks"]),
                dt.datetime.fromtimestamp(d["added_at"]).strftime("%d.%m.%Y"),
            ]
            for c, val in enumerate(items):
                it = QTableWidgetItem(val)
                if c == 0:
                    it.setData(Qt.UserRole, d["id"])
                    it.setToolTip(d["source"])
                self.table.setItem(r, c, it)
        self.summary.setText(f"{len(docs)} belge · {total_chunks} parça")

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = []
        for u in e.mimeData().urls():
            if u.isLocalFile():
                p = Path(u.toLocalFile())
                paths.extend(self._expand(p))
            else:
                paths.append(u.toString())
        self.ingest(paths)

    def _expand(self, p: Path) -> list[str]:
        if p.is_dir():
            return [str(f) for f in sorted(p.rglob("*")) if f.suffix.lower() in SUPPORTED_EXT]
        return [str(p)]

    def add_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Belge seç", str(Path.home()),
            "Belgeler (*.pdf *.docx *.txt *.md *.markdown);;Tüm dosyalar (*)")
        if files:
            self.ingest(files)

    def add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Klasör seç", str(Path.home()))
        if d:
            paths = self._expand(Path(d))
            if not paths:
                QMessageBox.information(self, "Dosya yok", "Bu klasörde desteklenen belge bulunamadı.")
                return
            self.ingest(paths)

    def add_urls(self):
        urls = [u for u in self.url_le.text().split() if u.strip()]
        if urls:
            self.url_le.clear()
            self.ingest(urls)

    def ingest(self, sources: list[str]):
        if self.ingesting:
            QMessageBox.information(self, "Lütfen bekleyin", "Önceki ekleme işlemi sürüyor.")
            return
        if not sources:
            return
        topic = self.topic_le.text().strip()
        db = self.ctx.db
        self.ingesting = True
        self.prog.setRange(0, len(sources) * 100)
        self.prog.setValue(0)
        self.prog.show()

        def job(progress, task):
            added, skipped, errors, warnings = 0, 0, [], []
            for i, src in enumerate(sources):
                if task.cancelled:
                    break
                name = Path(src).name if not src.startswith("http") else src
                progress((i, f"Okunuyor: {name}"))
                if db.source_exists(src):
                    skipped += 1
                    continue
                try:
                    def on_page(p, n, i=i, name=name):
                        progress((i + p / max(1, n), f"Okunuyor: {name} — sayfa {p}/{n}"))

                    doc = load_any(src, on_page)
                    progress((i + 1, f"Bölümleniyor: {name}"))
                    chunks = chunk_doc(doc)
                    if not chunks:
                        raise IngestError("Metin bulunamadı.")
                    db.add_document(doc.title, doc.source, doc.kind, chunks, topic=topic)
                    warnings.extend(f"{name}: {w}" for w in doc.warnings)
                    added += 1
                except IngestError as e:
                    errors.append(f"{Path(src).name if not src.startswith('http') else src}: {e}")
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{src}: beklenmeyen hata — {e}")
                progress((i + 1, ""))
            return added, skipped, errors, warnings

        def on_prog(ev):
            i, msg = ev
            self.prog.setValue(int(i * 100))
            if msg:
                self.status.setText(msg)

        def on_res(res):
            added, skipped, errors, warnings = res
            parts = [f"{added} belge eklendi"]
            if skipped:
                parts.append(f"{skipped} zaten vardı")
            self.status.setText(" · ".join(parts) + ".")
            if errors:
                QMessageBox.warning(self, "Bazı belgeler eklenemedi", "\n\n".join(errors[:10]))
            if warnings:
                QMessageBox.information(self, "Eklendi, ancak dikkat", "\n\n".join(warnings[:10]))
            if added:
                self.ctx.notify_library_changed()

        def on_fin():
            self.ingesting = False
            self.prog.hide()

        self.ctx.runner.start(job, on_result=on_res, on_progress=on_prog,
                              on_error=lambda e: QMessageBox.warning(self, "Hata", e), on_finished=on_fin)

    def delete_selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if not rows:
            return
        ids = [self.table.item(r, 0).data(Qt.UserRole) for r in rows]
        names = [self.table.item(r, 0).text() for r in rows]
        if QMessageBox.question(self, "Sil", f"{len(ids)} belge kütüphaneden silinsin mi?\n\n" + "\n".join(names[:8])) \
                != QMessageBox.Yes:
            return
        for i in ids:
            self.ctx.db.delete_document(i)
        self.ctx.notify_library_changed()
