"""Bilgi kartları: desteler, kart listesi ve aralıklı tekrarla çalışma."""
from __future__ import annotations

import base64
import datetime as dt
import html
import random
import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QFormLayout, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QStackedWidget,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import style
from .context import AppContext


def esc(s) -> str:
    return html.escape(s or "").replace("\n", "<br>")


def _due(ts: float) -> str:
    d = ts - time.time()
    if d <= 0:
        return "bugün"
    if d < 86400:
        return "yarın" if dt.datetime.fromtimestamp(ts).date() > dt.date.today() else "bugün"
    return f"{int(d // 86400) + 1} gün sonra"


class CardDialog(QDialog):
    def __init__(self, card: dict | None, decks: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Bilgi kartı")
        self.resize(560, 380)
        f = QFormLayout(self)
        self.deck = QLineEdit((card or {}).get("deck") or (decks[0] if decks else "Kartlarım"))
        self.front = QPlainTextEdit((card or {}).get("front", ""))
        self.back = QPlainTextEdit((card or {}).get("back", ""))
        self.front.setPlaceholderText("Soru (ön yüz)")
        self.back.setPlaceholderText("Cevap (arka yüz)")
        f.addRow("Deste", self.deck)
        f.addRow("Ön yüz", self.front)
        f.addRow("Arka yüz", self.back)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Save).setText("Kaydet")
        bb.button(QDialogButtonBox.Cancel).setText("Vazgeç")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def values(self) -> dict:
        return {"deck": self.deck.text().strip() or "Kartlarım", "front": self.front.toPlainText().strip(),
                "back": self.back.toPlainText().strip()}


class CardPreviewDialog(QDialog):
    """Üretilen kartları kaydetmeden önce göster: istenmeyenin işaretini kaldır, metni düzelt."""

    def __init__(self, cards: list[dict], deck: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Bilgi kartları — önizleme")
        self.resize(900, 620)
        lay = QVBoxLayout(self)
        info = QLabel(f"<b>{len(cards)} kart hazırlandı.</b> İstemediklerinin işaretini kaldır; metne çift tıklayıp "
                      "düzeltebilirsin. Kartlar “Kartlar” sekmesinde aşağıdaki destede toplanır.")
        info.setWordWrap(True)
        lay.addWidget(info)
        row = QHBoxLayout()
        row.addWidget(QLabel("Deste adı:"))
        self.deck = QLineEdit(deck)
        row.addWidget(self.deck, 1)
        lay.addLayout(row)
        self.tbl = QTableWidget(len(cards), 2)
        self.tbl.setHorizontalHeaderLabels(["Ön yüz (soru)", "Arka yüz (cevap)"])
        self.tbl.verticalHeader().hide()
        self.tbl.setWordWrap(True)
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        for r, c in enumerate(cards):
            a = QTableWidgetItem(c["front"])
            a.setFlags(a.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEditable)
            a.setCheckState(Qt.Checked)
            a.setData(Qt.UserRole, c)
            b = QTableWidgetItem(c["back"])
            self.tbl.setItem(r, 0, a)
            self.tbl.setItem(r, 1, b)
        self.tbl.resizeRowsToContents()
        lay.addWidget(self.tbl, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Save).setText("Kartları kaydet")
        bb.button(QDialogButtonBox.Cancel).setText("Vazgeç")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def cards(self) -> list[dict]:
        out = []
        for r in range(self.tbl.rowCount()):
            a, b = self.tbl.item(r, 0), self.tbl.item(r, 1)
            if a.checkState() == Qt.Checked and a.text().strip():
                c = dict(a.data(Qt.UserRole) or {})
                c.update(front=a.text().strip(), back=b.text().strip())
                out.append(c)
        return out


class SourceCardsDialog(QDialog):
    """Kaynaktan kart: kaynak seç (sayfa aralığıyla), isteğe bağlı konu, kart sayısı."""

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        from .flow import FlowLayout
        from .summary_page import SourceChip, SourcePicker
        from core.summary import pdf_page_count
        self.ctx = ctx
        self._SourceChip, self._SourcePicker, self._ppc = SourceChip, SourcePicker, pdf_page_count
        self.setWindowTitle("Kaynaktan bilgi kartı")
        self.resize(720, 420)
        lay = QVBoxLayout(self)
        info = QLabel("Kütüphanendeki kitap ya da makaleden doğrudan soru–cevap kartları çıkar. Büyük kitapta sayfa "
                      "aralığı ya da konu yaz; kaydetmeden önce kartları görüp düzeltebilirsin.")
        info.setWordWrap(True)
        lay.addWidget(info)
        top = QHBoxLayout()
        top.addWidget(QLabel("<b>Kaynaklar</b>"))
        top.addStretch()
        add = QPushButton("＋ Kaynak ekle")
        add.setObjectName("primary")
        add.clicked.connect(self.pick)
        top.addWidget(add)
        lay.addLayout(top)
        self.chips_w = QWidget()
        self.flow = FlowLayout(self.chips_w, spacing=8)
        lay.addWidget(self.chips_w)
        self.chips: dict[int, QWidget] = {}
        f = QFormLayout()
        self.topic = QLineEdit()
        self.topic.setPlaceholderText("isteğe bağlı — örn. akut otitis media tedavisi")
        f.addRow("Konu", self.topic)
        from PySide6.QtWidgets import QSpinBox
        self.count = QSpinBox()
        self.count.setRange(5, 80)
        self.count.setValue(25)
        self.count.setSuffix(" kart (yaklaşık)")
        f.addRow("Kart sayısı", self.count)
        from .lang import lang_combo
        self.lang = lang_combo(ctx)
        f.addRow("Kartların dili", self.lang)
        lay.addLayout(f)
        lay.addStretch()
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok = bb.button(QDialogButtonBox.Ok)
        self.ok.setText("Kartları oluştur")
        self.ok.setObjectName("primary")
        bb.button(QDialogButtonBox.Cancel).setText("Vazgeç")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._docs = {d["id"]: d for d in ctx.db.list_documents()}
        last = [x for x in (ctx.settings.extra.get("summary_sources") or []) if x[0] in self._docs]
        for did, pages in last or [[next(iter(self._docs)), ""]]:
            self._add(did, pages)
        self._update()

    def _pc(self, d):
        return self._ppc(d["source"]) if d["kind"] == "pdf" and __import__("pathlib").Path(d["source"]).exists() else 0

    def _add(self, did, pages=""):
        d = self._docs.get(did)
        if not d or did in self.chips:
            return
        c = self._SourceChip(d, self._pc(d), pages)
        c.removed.connect(lambda x=did: self._remove(x))
        self.chips[did] = c
        self.flow.addWidget(c)
        self._update()

    def _remove(self, did):
        c = self.chips.pop(did, None)
        if c:
            self.flow.removeWidget(c)
            c.deleteLater()
        self._update()

    def pick(self):
        dlg = self._SourcePicker(list(self._docs.values()), set(self.chips), self._pc, self)
        if dlg.exec():
            chosen = dlg.selected_ids()
            for did in list(self.chips):
                if did not in chosen:
                    self._remove(did)
            for did in chosen:
                self._add(did)

    def _update(self):
        self.ok.setEnabled(bool(self.chips))

    def items(self) -> list[tuple[int, str]]:
        return [(did, c.pages()) for did, c in self.chips.items()]


class CardsPage(QWidget):
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
        ctx.cards_changed.connect(self.refresh)
        ctx.profile_changed.connect(lambda _: self.refresh())
        ctx.data_synced.connect(self.refresh)
        self.refresh()

    # ============================================================ liste
    def _build_list(self) -> QWidget:
        w = QWidget()
        w.setObjectName("page")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(28, 22, 28, 18)
        lay.setSpacing(12)
        head = QHBoxLayout()
        t = QLabel("Bilgi kartları")
        t.setObjectName("h1")
        head.addWidget(t)
        sub = QLabel("Ön yüzde soru, arkada cevap · bildikçe daha seyrek, bilemedikçe daha sık sorulur")
        sub.setObjectName("muted")
        head.addWidget(sub)
        head.addStretch()
        lay.addLayout(head)

        top = QFrame()
        top.setObjectName("card")
        tl = QHBoxLayout(top)
        tl.setContentsMargins(18, 12, 18, 12)
        self.sum_lbl = QLabel("")
        self.sum_lbl.setWordWrap(True)
        tl.addWidget(self.sum_lbl, 1)
        self.all_btn = QPushButton("Tümünü karışık çalış")
        self.all_btn.clicked.connect(lambda: self.start_study(due_only=False))
        self.study_btn = QPushButton("▶ Bugünkü tekrar")
        self.study_btn.setObjectName("primary")
        self.study_btn.setMinimumHeight(40)
        self.study_btn.clicked.connect(lambda: self.start_study(due_only=True))
        self.src_btn = QPushButton("📚 Kaynaktan kart yap")
        self.src_btn.setToolTip("Kütüphanedeki kitap/makaleden (istersen belli sayfalar ya da bir konu) doğrudan kart üret")
        self.src_btn.clicked.connect(self.cards_from_sources)
        tl.addWidget(self.src_btn)
        tl.addWidget(self.all_btn)
        tl.addWidget(self.study_btn)
        lay.addWidget(top)

        split = QSplitter(Qt.Horizontal)
        left = QFrame()
        left.setObjectName("card")
        ll = QVBoxLayout(left)
        ll.setContentsMargins(10, 10, 10, 10)
        h = QLabel("Desteler")
        h.setObjectName("h2")
        ll.addWidget(h)
        self.decks = QListWidget()
        self.decks.setStyleSheet("QListWidget{border:none; background:transparent;}"
                                 "QListWidget::item{padding:7px 6px; border-radius:7px;}"
                                 "QListWidget::item:selected{background:#ccfbf1; color:#0f172a;}")
        self.decks.currentRowChanged.connect(lambda _: self._fill_table())
        self.decks.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.decks.setTextElideMode(Qt.ElideRight)
        ll.addWidget(self.decks, 1)
        del_deck = QPushButton("Desteyi sil")
        del_deck.setObjectName("danger")
        del_deck.clicked.connect(self.delete_deck)
        ll.addWidget(del_deck)
        split.addWidget(left)

        right = QFrame()
        right.setObjectName("card")
        rl = QVBoxLayout(right)
        rl.setContentsMargins(12, 10, 12, 10)
        r1 = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Kartlarda ara…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _: self._fill_table())
        r1.addWidget(self.search, 1)
        new = QPushButton("+ Yeni kart")
        new.clicked.connect(self.add_card)
        ed = QPushButton("Düzenle")
        ed.clicked.connect(self.edit_card)
        de = QPushButton("Sil")
        de.setObjectName("danger")
        de.clicked.connect(self.delete_selected)
        for b in (new, ed, de):
            r1.addWidget(b)
        rl.addLayout(r1)
        self.tbl = QTableWidget(0, 3)
        self.tbl.setHorizontalHeaderLabels(["Ön yüz", "Arka yüz", "Sonraki tekrar"])
        self.tbl.verticalHeader().hide()
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.setWordWrap(True)
        hh = self.tbl.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.tbl.doubleClicked.connect(lambda _: self.edit_card())
        rl.addWidget(self.tbl, 1)
        self.empty = QLabel(
            "<h3>Henüz bilgi kartın yok</h3>"
            "<p>Kart yapmanın üç yolu var:</p><ul>"
            "<li>Yukarıdaki <b>📚 Kaynaktan kart yap</b>: kütüphanendeki kitap ya da makaleyi seç (istersen sayfa "
            "aralığı ya da konu yaz), yapay zekâ doğrudan kaynaktan kart çıkarsın.</li>"
            "<li><b>Özet</b> sekmesinde bir özet oluştur ya da aç → altındaki <b>🃏 Bilgi kartı yap</b>. "
            "Yapay zekâ özetten tek bilgilik soru–cevap kartları çıkarır; kaydetmeden önce görürsün.</li>"
            "<li><b>Quiz</b>'de bir soruyu cevapladıktan sonra <b>🃏 Karta ekle</b> — özellikle yanlış "
            "yaptıklarını.</li></ul><p>Ya da sağ üstteki <b>+ Yeni kart</b> ile kendin yaz.</p>")
        self.empty.setWordWrap(True)
        self.empty.setObjectName("muted")
        self.empty.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        rl.addWidget(self.empty, 1)
        split.addWidget(right)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 3)
        split.setSizes([260, 800])
        lay.addWidget(split, 1)
        return w

    def _deck(self) -> str | None:
        it = self.decks.currentItem()
        return it.data(Qt.UserRole) if it else None

    def refresh(self):
        prof = self.ctx.profile
        decks = self.ctx.db.card_decks(prof)
        total = sum(d["total"] for d in decks)
        due = sum(d["due"] or 0 for d in decks)
        cur = self._deck()
        self.decks.blockSignals(True)
        self.decks.clear()
        it = QListWidgetItem(f"Tüm kartlar\n{total} kart · bugün {due}")
        it.setData(Qt.UserRole, None)
        self.decks.addItem(it)
        for d in decks:
            it = QListWidgetItem(f"{d['deck'][:34]}\n{d['total']} kart · bugün {d['due'] or 0} · öğrenilen "
                                 f"{d['learned'] or 0}")
            it.setData(Qt.UserRole, d["deck"])
            it.setToolTip(d["deck"])
            self.decks.addItem(it)
        idx = next((i for i in range(self.decks.count()) if self.decks.item(i).data(Qt.UserRole) == cur), 0)
        self.decks.setCurrentRow(idx)
        self.decks.blockSignals(False)
        self.sum_lbl.setText(f"<b>Bugün tekrar edilecek: {due} kart</b> · toplam {total} kart, {len(decks)} deste"
                             if total else "<b>Henüz kart yok.</b> Özet ya da Quiz sekmesinden kart oluşturabilirsin.")
        self.study_btn.setEnabled(due > 0)
        self.study_btn.setText(f"▶ Bugünkü tekrar ({due})")
        self.all_btn.setEnabled(total > 0)
        self._fill_table()

    def _fill_table(self):
        rows = self.ctx.db.list_cards(self.ctx.profile, self._deck(), self.search.text().strip(), limit=2000)
        self._rows = rows
        self.tbl.setRowCount(len(rows))
        for r, c in enumerate(rows):
            front = ("🖼 " if c.get("image") else "") + c["front"]
            a = QTableWidgetItem(front[:300])
            a.setData(Qt.UserRole, c["id"])
            a.setToolTip(c["front"])
            b = QTableWidgetItem(c["back"][:300])
            b.setToolTip(c["back"])
            n = QTableWidgetItem(_due(c["due_at"]))
            self.tbl.setItem(r, 0, a)
            self.tbl.setItem(r, 1, b)
            self.tbl.setItem(r, 2, n)
        self.tbl.resizeRowsToContents()
        has = bool(rows) or bool(self.search.text().strip())
        self.tbl.setVisible(has)
        self.empty.setVisible(not has)

    def _selected_ids(self) -> list[int]:
        return [self.tbl.item(i.row(), 0).data(Qt.UserRole) for i in self.tbl.selectionModel().selectedRows()]

    def add_card(self):
        decks = [d["deck"] for d in self.ctx.db.card_decks(self.ctx.profile)]
        dlg = CardDialog({"deck": self._deck() or (decks[0] if decks else "Kartlarım")}, decks, self)
        if dlg.exec():
            v = dlg.values()
            if v["front"]:
                self.ctx.db.add_cards([v], v["deck"], "elle", self.ctx.profile)
                self.ctx.cards_changed.emit()

    def edit_card(self):
        ids = self._selected_ids()
        if not ids:
            return
        c = next((x for x in self._rows if x["id"] == ids[0]), None)
        if not c:
            return
        dlg = CardDialog(c, [], self)
        if dlg.exec():
            self.ctx.db.update_card(c["id"], **dlg.values())
            self.ctx.cards_changed.emit()

    def delete_selected(self):
        ids = self._selected_ids()
        if ids and QMessageBox.question(self, "Sil", f"{len(ids)} kart silinsin mi?") == QMessageBox.Yes:
            self.ctx.db.delete_cards(ids)
            self.ctx.cards_changed.emit()

    def delete_deck(self):
        deck = self._deck()
        if not deck:
            return
        n = len(self.ctx.db.list_cards(self.ctx.profile, deck))
        if QMessageBox.question(self, "Desteyi sil", f"“{deck}” destesindeki {n} kart silinsin mi?") == QMessageBox.Yes:
            self.ctx.db.delete_cards([c["id"] for c in self.ctx.db.list_cards(self.ctx.profile, deck)])
            self.ctx.cards_changed.emit()

    def cards_from_sources(self):
        if not self.ctx.db.list_documents():
            QMessageBox.information(self, "Kütüphane boş", "Önce Kütüphane sekmesinden kaynak ekle.")
            return
        if not self.ctx.settings.has_any_key:
            QMessageBox.warning(self, "API anahtarı gerekli", "Lütfen önce Ayarlar sekmesinden API anahtarınızı girin.")
            return
        dlg = SourceCardsDialog(self.ctx, self)
        if not dlg.exec():
            return
        items, topic, n = dlg.items(), dlg.topic.text().strip(), dlg.count.value()
        lang = dlg.lang.currentData()
        engine = self.ctx.summary_engine
        try:
            src = engine.build_multi(items, topic)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Kaynak hazırlanamadı", str(e))
            return
        from core.cards import cards_from_source
        factory = engine.llm_factory
        deck = (topic or src.title)[:80]
        self.src_btn.setEnabled(False)
        self.src_btn.setText("Kartlar hazırlanıyor…")
        self.sum_lbl.setText(f"<b>{src.title}</b> · {src.scope} — kaynaktan kartlar hazırlanıyor "
                             f"({len(src.packets)} bölüm), birkaç dakika sürebilir…")

        def reset():
            self.src_btn.setEnabled(True)
            self.src_btn.setText("📚 Kaynaktan kart yap")

        def done(cards):
            reset()
            self.refresh()
            if not cards:
                QMessageBox.warning(self, "Bilgi kartı", "Kaynaktan kart çıkarılamadı. Tekrar deneyebilirsin.")
                return
            prev = CardPreviewDialog(cards, deck, self)
            if not prev.exec():
                return
            chosen = prev.cards()
            name = prev.deck.text().strip() or deck
            added = self.ctx.db.add_cards(chosen, name, f"kaynak: {src.title[:60]}", self.ctx.profile)
            self.ctx.cards_changed.emit()
            self._select_deck(name)
            QMessageBox.information(self, "Bilgi kartları", f"🃏 {added} kart “{name}” destesine eklendi. "
                                                            "“▶ Bugünkü tekrar” ile hemen çalışabilirsin.")

        def fail(e):
            reset()
            self.refresh()
            QMessageBox.warning(self, "Bilgi kartı", str(e))

        self.ctx.runner.start(lambda p, t: cards_from_source(engine, src, factory, n, topic,
                                                             should_stop=lambda: t.cancelled, language=lang),
                              on_result=done, on_error=fail)

    def _select_deck(self, name: str):
        for i in range(self.decks.count()):
            if self.decks.item(i).data(Qt.UserRole) == name:
                self.decks.setCurrentRow(i)
                return

    # ============================================================ çalışma
    def _build_study(self) -> QWidget:
        w = QWidget()
        w.setObjectName("page")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(28, 22, 28, 18)
        head = QHBoxLayout()
        back = QPushButton("← Kartlar")
        back.clicked.connect(self.end_study)
        head.addWidget(back)
        self.deck_lbl = QLabel("")
        self.deck_lbl.setObjectName("muted")
        head.addWidget(self.deck_lbl)
        head.addStretch()
        self.prog_lbl = QLabel("")
        self.prog_lbl.setObjectName("badge")
        head.addWidget(self.prog_lbl)
        lay.addLayout(head)
        lay.addStretch(1)
        self.card = QFrame()
        self.card.setObjectName("card")
        self.card.setMinimumHeight(340)
        self.card.setMaximumWidth(820)
        cl = QVBoxLayout(self.card)
        cl.setContentsMargins(36, 28, 36, 28)
        cl.setSpacing(14)
        self.front_lbl = QLabel("")
        self.front_lbl.setAlignment(Qt.AlignCenter)
        self.front_lbl.setWordWrap(True)
        self.front_lbl.setTextFormat(Qt.RichText)
        self.front_lbl.setStyleSheet("font-size: 15pt; font-weight: 600;")
        self.img_lbl = QLabel()
        self.img_lbl.setAlignment(Qt.AlignCenter)
        self.back_lbl = QLabel("")
        self.back_lbl.setAlignment(Qt.AlignCenter)
        self.back_lbl.setWordWrap(True)
        self.back_lbl.setTextFormat(Qt.RichText)
        self.back_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        for x in (self.front_lbl, self.img_lbl, self.back_lbl):
            cl.addWidget(x)
        cl.addStretch()
        btns = QHBoxLayout()
        self.show_btn = QPushButton("Cevabı göster  (Boşluk)")
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
        hint = QLabel("Bildiğin kart 1, 3, 7, 16… gün sonra; bilemediğin kart bugün tekrar gelir.")
        hint.setObjectName("muted")
        hint.setAlignment(Qt.AlignCenter)
        cl.addWidget(hint)
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

    def start_study(self, due_only: bool = True, deck: str | None = "__current__"):
        deck = self._deck() if deck == "__current__" else deck
        rows = self.ctx.db.list_cards(self.ctx.profile, deck, due_only=due_only, limit=100 if due_only else 400)
        if not rows and deck and due_only:
            rows = self.ctx.db.list_cards(self.ctx.profile, None, due_only=True, limit=100)
            deck = None
        if not rows:
            QMessageBox.information(self, "Bilgi kartları", "Şu an tekrar edilecek kart yok. 👍")
            return
        if not due_only:
            random.shuffle(rows)
        self.queue, self.qi = rows, 0
        self.session = {"knew": 0, "missed": 0}
        self.deck_lbl.setText(deck or "Tüm desteler")
        self.stack.setCurrentIndex(1)
        self.setFocus()
        self._show_card()

    def _show_card(self):
        if self.qi >= len(self.queue):
            k, m = self.session["knew"], self.session["missed"]
            QMessageBox.information(self, "Tebrikler", f"Çalışma bitti: {k} bildin, {m} kart yakında tekrar gelecek.")
            self.end_study()
            return
        c = self.queue[self.qi]
        self.prog_lbl.setText(f"{self.qi + 1} / {len(self.queue)}")
        self.front_lbl.setText(esc(c["front"]))
        if c.get("image"):
            pm = QPixmap()
            pm.loadFromData(base64.b64decode(c["image"]))
            self.img_lbl.setPixmap(pm.scaled(720, 360, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self.img_lbl.show()
        else:
            self.img_lbl.clear()
            self.img_lbl.hide()
        self.back_lbl.setText("")
        self.show_btn.show()
        self.miss_btn.hide()
        self.knew_btn.hide()

    def reveal(self):
        c = self.queue[self.qi]
        self.back_lbl.setText(f"<div style='font-size:13pt; color:{style.ACCENT}; font-weight:600'>"
                              f"{esc(c['back'])}</div>")
        self.show_btn.hide()
        self.miss_btn.show()
        self.knew_btn.show()

    def answer(self, knew: bool):
        c = self.queue[self.qi]
        self.ctx.db.review_card(c["id"], knew)
        self.session["knew" if knew else "missed"] += 1
        self.qi += 1
        self._show_card()

    def end_study(self):
        self.stack.setCurrentIndex(0)
        self.ctx.cards_changed.emit()
