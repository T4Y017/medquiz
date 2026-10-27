"""Kelime havuzu: işaretlenen bilinmeyen kelimeler, aralıklı tekrarla bilgi kartı çalışması."""
from __future__ import annotations

import csv
import datetime as dt
import html
import re
import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QStackedWidget, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from . import style
from .context import AppContext
from .translate_ui import add_to_vocab, toast

LEVEL_NAMES = ["Yeni", "1 gün", "3 gün", "1 hafta", "2 hafta", "1 ay", "Öğrenildi"]


def esc(s) -> str:
    return html.escape(str(s or ""))


def _due_text(ts: float) -> str:
    d = ts - time.time()
    if d <= 0:
        return "Şimdi"
    if d < 3600:
        return f"{int(d // 60) + 1} dk sonra"
    if d < 86400:
        return f"{int(d // 3600)} saat sonra"
    return dt.datetime.fromtimestamp(ts).strftime("%d.%m.%Y")


class EditDialog(QDialog):
    def __init__(self, row: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Kelimeyi düzenle")
        self.resize(520, 380)
        f = QFormLayout(self)
        self.fields = {}
        for key, label in (("term", "Kelime"), ("meaning", "Türkçe anlamı"), ("pos", "Tür"), ("ipa", "Okunuş (IPA)")):
            le = QLineEdit(row.get(key) or "")
            f.addRow(label, le)
            self.fields[key] = le
        for key, label in (("definition", "Açıklama"), ("example", "Örnek cümle")):
            te = QPlainTextEdit(row.get(key) or "")
            te.setFixedHeight(70)
            f.addRow(label, te)
            self.fields[key] = te
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Save).setText("Kaydet")
        bb.button(QDialogButtonBox.Cancel).setText("Vazgeç")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def values(self) -> dict:
        return {k: (w.text() if isinstance(w, QLineEdit) else w.toPlainText()).strip() for k, w in self.fields.items()}


class VocabPage(QWidget):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.setObjectName("page")
        self.ctx = ctx
        self.queue: list[dict] = []
        self.qi = 0
        self.session = {"knew": 0, "missed": 0}
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        root.addWidget(self.stack)
        self.stack.addWidget(self._build_list())
        self.stack.addWidget(self._build_study())
        ctx.vocab_changed.connect(self.refresh)
        ctx.profile_changed.connect(lambda _: self.refresh())
        self.refresh()

    # ================================================================ liste
    def _build_list(self) -> QWidget:
        w = QWidget()
        w.setObjectName("page")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(28, 22, 28, 18)
        lay.setSpacing(12)
        head = QHBoxLayout()
        t = QLabel("Kelimeler")
        t.setObjectName("h1")
        head.addWidget(t)
        self.summary = QLabel("")
        self.summary.setObjectName("muted")
        head.addWidget(self.summary)
        head.addStretch()
        self.study_btn = QPushButton("▶  Çalış")
        self.study_btn.setObjectName("primary")
        self.study_btn.clicked.connect(lambda: self.start_study(due_only=True))
        head.addWidget(self.study_btn)
        lay.addLayout(head)

        card = QFrame()
        card.setObjectName("card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 12, 16, 12)
        info = QLabel("Quiz, vaka, sohbet veya çeviri penceresinde bilmediğin bir kelimeyi <b>seçip sağ tıkla → "
                      "“Kelime havuzuna ekle”</b>. Anlamı, okunuşu ve örnek cümlesi otomatik hazırlanır; "
                      "burada aralıklı tekrarla (1 gün → 3 gün → 1 hafta → …) çalışırsın.")
        info.setObjectName("muted")
        info.setWordWrap(True)
        cl.addWidget(info)
        row = QHBoxLayout()
        self.new_le = QLineEdit()
        self.new_le.setPlaceholderText("Elle kelime ekle (örn. dyspnea) ve Enter'a bas")
        self.new_le.returnPressed.connect(self._add_manual)
        add = QPushButton("Ekle")
        add.clicked.connect(self._add_manual)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Ara…")
        self.search.setFixedWidth(200)
        self.search.textChanged.connect(lambda _: self.refresh())
        row.addWidget(self.new_le, 1)
        row.addWidget(add)
        row.addSpacing(16)
        row.addWidget(self.search)
        cl.addLayout(row)
        lay.addWidget(card)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Kelime", "Anlam", "Örnek", "Seviye", "Sonraki tekrar"])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.Stretch)
        h.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.table.doubleClicked.connect(lambda _: self.edit_selected())
        lay.addWidget(self.table, 1)

        bot = QHBoxLayout()
        all_btn = QPushButton("Tümünü çalış")
        all_btn.setToolTip("Tekrar zamanı gelmemiş kelimeler dahil hepsini karışık sırayla çalış")
        all_btn.clicked.connect(lambda: self.start_study(due_only=False))
        edit = QPushButton("Düzenle")
        edit.clicked.connect(self.edit_selected)
        fill = QPushButton("Anlamı doldur")
        fill.setToolTip("Anlamı boş kalan seçili kelimeler için yapay zekâdan anlam ister")
        fill.clicked.connect(self.fill_selected)
        exp = QPushButton("CSV / Anki'ye aktar")
        exp.clicked.connect(self.export_csv)
        dele = QPushButton("Sil")
        dele.setObjectName("danger")
        dele.clicked.connect(self.delete_selected)
        bot.addWidget(all_btn)
        bot.addStretch()
        bot.addWidget(edit)
        bot.addWidget(fill)
        bot.addWidget(exp)
        bot.addWidget(dele)
        lay.addLayout(bot)
        return w

    def _rows(self) -> list[dict]:
        return self.ctx.db.list_vocab(self.ctx.profile, self.search.text() if hasattr(self, "search") else "")

    def refresh(self):
        rows = self._rows()
        self.table.setRowCount(len(rows))
        for r, v in enumerate(rows):
            ex = re.sub(r"\s+", " ", v.get("example") or "")
            vals = [v["term"], v["meaning"] or "—", ex, LEVEL_NAMES[min(v["box"], len(LEVEL_NAMES) - 1)],
                    _due_text(v["due_at"])]
            for c, val in enumerate(vals):
                it = QTableWidgetItem(val)
                if c == 0:
                    it.setData(Qt.UserRole, v["id"])
                    f = it.font()
                    f.setBold(True)
                    it.setFont(f)
                    tip = f"{v['term']} {('/' + v['ipa'] + '/') if v.get('ipa') else ''}\n{v.get('definition') or ''}"
                    it.setToolTip(tip.strip())
                if c == 2:
                    it.setToolTip(v.get("example") or "")
                if c == 4 and v["due_at"] <= time.time():
                    it.setForeground(Qt.darkGreen)
                self.table.setItem(r, c, it)
        due = len(self.ctx.db.due_vocab(self.ctx.profile, limit=1000))
        total = len(self.ctx.db.list_vocab(self.ctx.profile))
        self.summary.setText(f"{total} kelime · bugün tekrar: {due}")
        self.study_btn.setText(f"▶  Çalış ({due})")
        self.study_btn.setEnabled(due > 0)

    def _selected_ids(self) -> list[int]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.table.item(r, 0).data(Qt.UserRole) for r in rows]

    def _add_manual(self):
        t = self.new_le.text().strip()
        if t:
            self.new_le.clear()
            add_to_vocab(self.ctx, self, t, "", "elle")

    def edit_selected(self):
        ids = self._selected_ids()
        if not ids:
            return
        row = next((v for v in self.ctx.db.list_vocab(self.ctx.profile) if v["id"] == ids[0]), None)
        if not row:
            return
        dlg = EditDialog(row, self)
        if dlg.exec():
            self.ctx.db.update_vocab(ids[0], **dlg.values())
            self.refresh()

    def fill_selected(self):
        ids = set(self._selected_ids())
        rows = [v for v in self.ctx.db.list_vocab(self.ctx.profile) if v["id"] in ids] if ids else \
            [v for v in self.ctx.db.list_vocab(self.ctx.profile) if not v["meaning"]]
        if not rows:
            toast(self, "Doldurulacak kelime yok (önce kelime seçin).")
            return
        tr = self.ctx.translator
        db = self.ctx.db

        def job(progress, task):
            done = 0
            for v in rows:
                if task.cancelled:
                    break
                d = tr.define(v["term"], v.get("example") or "")
                db.update_vocab(v["id"], meaning=d["meaning"], definition=d["definition"], pos=d["pos"],
                                ipa=d["ipa"], example=v.get("example") or d["example"])
                done += 1
            return done

        self.ctx.runner.start(job, on_result=lambda n: (self.refresh(), toast(self, f"{n} kelimenin anlamı dolduruldu.")),
                              on_error=lambda e: QMessageBox.warning(self, "Hata", e))

    def delete_selected(self):
        ids = self._selected_ids()
        if not ids:
            return
        if QMessageBox.question(self, "Sil", f"{len(ids)} kelime silinsin mi?") != QMessageBox.Yes:
            return
        for i in ids:
            self.ctx.db.delete_vocab(i)
        self.refresh()

    def export_csv(self):
        rows = self.ctx.db.list_vocab(self.ctx.profile)
        if not rows:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Dışa aktar", str(Path.home() / "medquiz_kelimeler.csv"),
                                              "CSV (*.csv)")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["Kelime", "Anlam", "Açıklama", "Örnek", "Okunuş", "Tür"])
            for v in rows:
                w.writerow([v["term"], v["meaning"], v["definition"], v["example"], v["ipa"], v["pos"]])
        toast(self, f"{len(rows)} kelime dışa aktarıldı. (Anki: Dosya → İçe aktar, ayraç ';')", 5000)

    # ============================================================ çalışma
    def _build_study(self) -> QWidget:
        w = QWidget()
        w.setObjectName("page")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(28, 22, 28, 18)
        head = QHBoxLayout()
        back = QPushButton("← Kelime listesi")
        back.clicked.connect(self.end_study)
        head.addWidget(back)
        head.addStretch()
        self.prog_lbl = QLabel("")
        self.prog_lbl.setObjectName("badge")
        head.addWidget(self.prog_lbl)
        lay.addLayout(head)
        lay.addStretch(1)
        self.card = QFrame()
        self.card.setObjectName("card")
        self.card.setMinimumHeight(320)
        self.card.setMaximumWidth(760)
        cl = QVBoxLayout(self.card)
        cl.setContentsMargins(36, 30, 36, 30)
        cl.setSpacing(14)
        self.term_lbl = QLabel("")
        self.term_lbl.setAlignment(Qt.AlignCenter)
        self.term_lbl.setStyleSheet(f"font-size: 28pt; font-weight: 700; color: {style.ACCENT};")
        self.ipa_lbl = QLabel("")
        self.ipa_lbl.setAlignment(Qt.AlignCenter)
        self.ipa_lbl.setObjectName("muted")
        self.ex_lbl = QLabel("")
        self.ex_lbl.setAlignment(Qt.AlignCenter)
        self.ex_lbl.setWordWrap(True)
        self.ex_lbl.setTextFormat(Qt.RichText)
        self.ans_lbl = QLabel("")
        self.ans_lbl.setAlignment(Qt.AlignCenter)
        self.ans_lbl.setWordWrap(True)
        self.ans_lbl.setTextFormat(Qt.RichText)
        for x in (self.term_lbl, self.ipa_lbl, self.ex_lbl, self.ans_lbl):
            cl.addWidget(x)
        cl.addStretch()
        btns = QHBoxLayout()
        self.show_btn = QPushButton("Anlamı göster  (Boşluk)")
        self.show_btn.setObjectName("primary")
        self.show_btn.clicked.connect(self.reveal)
        self.miss_btn = QPushButton("✗  Bilemedim  (1)")
        self.miss_btn.setObjectName("danger")
        self.miss_btn.clicked.connect(lambda: self.answer(False))
        self.knew_btn = QPushButton("✓  Bildim  (2)")
        self.knew_btn.setObjectName("primary")
        self.knew_btn.clicked.connect(lambda: self.answer(True))
        btns.addStretch()
        for b in (self.show_btn, self.miss_btn, self.knew_btn):
            b.setMinimumWidth(170)
            b.setMinimumHeight(44)
            btns.addWidget(b)
        btns.addStretch()
        cl.addLayout(btns)
        hc = QHBoxLayout()
        hc.addStretch()
        hc.addWidget(self.card, 3)
        hc.addStretch()
        lay.addLayout(hc)
        lay.addStretch(2)
        return w

    def keyPressEvent(self, e):
        if self.stack.currentIndex() == 1:
            if e.key() == Qt.Key_Space and self.show_btn.isVisible():
                self.reveal()
                return
            if e.key() == Qt.Key_1 and self.miss_btn.isVisible():
                self.answer(False)
                return
            if e.key() == Qt.Key_2 and self.knew_btn.isVisible():
                self.answer(True)
                return
        super().keyPressEvent(e)

    def start_study(self, due_only: bool = True):
        import random
        rows = self.ctx.db.due_vocab(self.ctx.profile, limit=50) if due_only else \
            self.ctx.db.list_vocab(self.ctx.profile)
        if not rows:
            QMessageBox.information(self, "Kelimeler", "Şu an tekrar edilecek kelime yok. 👍")
            return
        if not due_only:
            random.shuffle(rows)
        self.queue, self.qi = rows, 0
        self.session = {"knew": 0, "missed": 0}
        self.stack.setCurrentIndex(1)
        self.setFocus()
        self._show_card()

    def _show_card(self):
        if self.qi >= len(self.queue):
            k, m = self.session["knew"], self.session["missed"]
            QMessageBox.information(self, "Tebrikler", f"Çalışma bitti: {k} bildin, {m} tekrar edilecek.")
            self.end_study()
            return
        v = self.queue[self.qi]
        self.prog_lbl.setText(f"{self.qi + 1} / {len(self.queue)}")
        self.term_lbl.setText(esc(v["term"]))
        self.ipa_lbl.setText(f"/{esc(v['ipa'])}/" + (f"  ·  {esc(v['pos'])}" if v.get("pos") else "") if v.get("ipa")
                             else esc(v.get("pos") or ""))
        ex = esc(v.get("example") or "")
        if ex and v["term"]:
            ex = re.sub(re.escape(esc(v["term"])), lambda m: f"<b>{m.group(0)}</b>", ex, flags=re.I)
        self.ex_lbl.setText(f"<i>{ex}</i>" if ex else "")
        self.ans_lbl.setText("")
        self.show_btn.show()
        self.miss_btn.hide()
        self.knew_btn.hide()

    def reveal(self):
        v = self.queue[self.qi]
        meaning = esc(v["meaning"] or "(anlam girilmemiş)")
        self.ans_lbl.setText(f"<p style='font-size:18pt; font-weight:600'>{meaning}</p>"
                             f"<p style='color:{style.MUTED}'>{esc(v.get('definition') or '')}</p>")
        self.show_btn.hide()
        self.miss_btn.show()
        self.knew_btn.show()

    def answer(self, knew: bool):
        v = self.queue[self.qi]
        self.ctx.db.review_vocab(v["id"], knew)
        self.session["knew" if knew else "missed"] += 1
        self.qi += 1
        self._show_card()

    def end_study(self):
        self.stack.setCurrentIndex(0)
        self.refresh()
