"""İstatistikler: genel başarı, türe ve konuya göre performans, son cevaplar."""
from __future__ import annotations

import datetime as dt
import json

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QPushButton, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import style
from .context import AppContext


def _tile(title: str) -> tuple[QFrame, QLabel]:
    f = QFrame()
    f.setObjectName("card")
    l = QVBoxLayout(f)
    l.setContentsMargins(18, 14, 18, 14)
    t = QLabel(title)
    t.setObjectName("muted")
    v = QLabel("—")
    v.setObjectName("h1")
    l.addWidget(t)
    l.addWidget(v)
    return f, v


def _table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().hide()
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setSelectionMode(QAbstractItemView.NoSelection)
    t.setShowGrid(False)
    h = t.horizontalHeader()
    h.setSectionResizeMode(0, QHeaderView.Stretch)
    for i in range(1, len(headers)):
        h.setSectionResizeMode(i, QHeaderView.ResizeToContents)
    return t


class StatsPage(QWidget):
    review_requested = Signal(str, str)     # kapsam ("all" | "topic"), konu
    quiz_requested = Signal(str)            # konu: yanlış yoksa o konudan yeni quiz

    def __init__(self, ctx: AppContext):
        super().__init__()
        self.setObjectName("page")
        self.ctx = ctx
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 22, 28, 18)
        root.setSpacing(14)
        head = QHBoxLayout()
        t = QLabel("İstatistikler")
        t.setObjectName("h1")
        head.addWidget(t)
        head.addStretch()
        self.who = QLabel("")
        self.who.setObjectName("muted")
        head.addWidget(self.who)
        root.addLayout(head)

        tiles = QGridLayout()
        tiles.setHorizontalSpacing(12)
        f1, self.v_total = _tile("Cevaplanan soru")
        f2, self.v_avg = _tile("Genel başarı")
        f3, self.v_mcq = _tile("Çoktan seçmeli")
        f4, self.v_fill = _tile("Boşluk doldurma")
        f5, self.v_match = _tile("Eşleştirme")
        for i, f in enumerate((f1, f2, f3, f4, f5)):
            tiles.addWidget(f, 0, i)
        root.addLayout(tiles)

        # --- tekrar bekleyen yanlışlar
        rv = QFrame()
        rv.setObjectName("card")
        rl = QHBoxLayout(rv)
        rl.setContentsMargins(18, 12, 18, 12)
        self.rv_lbl = QLabel("")
        self.rv_lbl.setWordWrap(True)
        rl.addWidget(self.rv_lbl, 1)
        self.rv_topic_btn = QPushButton("Seçili konuyu tekrar et")
        self.rv_topic_btn.setToolTip("Soldaki tablodan bir konu seç: yalnızca o konudaki yanlışlarından yeni sorular")
        self.rv_topic_btn.clicked.connect(self._review_selected)
        self.rv_all_btn = QPushButton("🔁 Tüm yanlışları tekrar et")
        self.rv_all_btn.setObjectName("primary")
        self.rv_all_btn.clicked.connect(lambda: self.review_requested.emit("all", ""))
        rl.addWidget(self.rv_topic_btn)
        rl.addWidget(self.rv_all_btn)
        root.addWidget(rv)

        mid = QHBoxLayout()
        left = QVBoxLayout()
        l1 = QLabel("Konulara göre (en zayıftan güçlüye)")
        l1.setObjectName("h2")
        left.addWidget(l1)
        self.t_topic = _table(["Konu", "Soru", "Başarı", "Bekleyen yanlış"])
        self.t_topic.setSelectionMode(QAbstractItemView.SingleSelection)
        self.t_topic.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.t_topic.cellDoubleClicked.connect(lambda r, _c: self._review_row(r))
        self.t_topic.itemSelectionChanged.connect(self._sel_changed)
        left.addWidget(self.t_topic)
        tip = QLabel("İpucu: bir konuya çift tıkla — yanlışın varsa onları, yoksa o konudan yeni bir quiz başlatır.")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        left.addWidget(tip)
        right = QVBoxLayout()
        l2 = QLabel("Son cevaplar")
        l2.setObjectName("h2")
        right.addWidget(l2)
        self.t_recent = _table(["Soru", "Tür", "Puan", "Tarih"])
        right.addWidget(self.t_recent)
        mid.addLayout(left, 2)
        mid.addSpacing(10)
        mid.addLayout(right, 3)
        root.addLayout(mid, 1)
        ctx.profile_changed.connect(lambda _: self.refresh())

    def _sel_topic(self) -> str | None:
        rows = self.t_topic.selectionModel().selectedRows() if self.t_topic.selectionModel() else []
        if not rows:
            return None
        it = self.t_topic.item(rows[0].row(), 0)
        return it.data(Qt.UserRole) if it else None

    def _sel_changed(self):
        t = self._sel_topic()
        w = getattr(self, "_wrong", {}).get(t, 0) if t else 0
        self.rv_topic_btn.setEnabled(bool(t))
        self.rv_topic_btn.setText(f"“{t[:24]}” yanlışlarını tekrar et ({w})" if t and w else
                                  f"“{t[:24]}” konusundan quiz" if t else "Seçili konuyu tekrar et")

    def _review_row(self, r: int):
        it = self.t_topic.item(r, 0)
        if it:
            self._go_topic(it.data(Qt.UserRole))

    def _review_selected(self):
        t = self._sel_topic()
        if t:
            self._go_topic(t)

    def _go_topic(self, t: str):
        if getattr(self, "_wrong", {}).get(t):
            self.review_requested.emit("topic", t)
        else:
            self.quiz_requested.emit("" if t == "Genel" else t)

    def showEvent(self, e):
        self.refresh()
        super().showEvent(e)

    def refresh(self):
        prof = self.ctx.profile or None
        self.who.setText(f"Kullanıcı: {prof}" if prof else "Tüm kullanıcılar")
        s = self.ctx.db.stats(prof)
        self.v_total.setText(str(s["total"]))
        self.v_avg.setText(f"%{s['avg'] * 100:.0f}" if s["total"] else "—")
        by = {r[0]: r for r in s["by_type"]}
        self.v_mcq.setText(f"%{by['mcq'][2] * 100:.0f}  ({by['mcq'][1]})" if "mcq" in by else "—")
        for k, lbl in (("fill", self.v_fill), ("match", self.v_match)):
            lbl.setText(f"%{by[k][2] * 100:.0f}  ({by[k][1]})" if k in by else "—")

        wrong = dict(self.ctx.db.wrong_topics(prof))
        n_wrong = sum(wrong.values())
        self.rv_lbl.setText(
            f"<b>Tekrar bekleyen yanlış: {n_wrong}</b>" + (f" · {len(wrong)} konuda" if wrong else "") +
            ("<br><span style='color:#64748b'>Yanlışlarından, aynı soruları değil aynı kavramları farklı açılardan "
             "soran yeni sorular (5 test + 5 eşleştirme) hazırlanır.</span>" if n_wrong else
             "<br><span style='color:#64748b'>Bekleyen yanlış yok. 👏</span>"))
        self.rv_all_btn.setEnabled(n_wrong > 0)
        self.rv_all_btn.setText(f"🔁 Tüm yanlışları tekrar et ({n_wrong})" if n_wrong else "🔁 Tüm yanlışları tekrar et")
        topics = list(s["by_topic"])
        seen = {t[0] or "Genel" for t in topics}
        topics += [(t, 0, 0.0) for t in wrong if t not in seen]
        self.t_topic.setRowCount(len(topics))
        for r, (topic, n, avg) in enumerate(topics):
            name = topic or "Genel"
            it0 = QTableWidgetItem(name)
            it0.setData(Qt.UserRole, name)
            self.t_topic.setItem(r, 0, it0)
            self.t_topic.setItem(r, 1, QTableWidgetItem(str(n)))
            it = QTableWidgetItem(f"%{avg * 100:.0f}")
            it.setForeground(self._color(avg))
            self.t_topic.setItem(r, 2, it)
            w = wrong.get(name, 0)
            itw = QTableWidgetItem(str(w) if w else "—")
            if w:
                itw.setForeground(self._color(0.0))
            self.t_topic.setItem(r, 3, itw)
        self._wrong = wrong
        self._sel_changed()

        self.t_recent.setRowCount(len(s["recent"]))
        for r, row in enumerate(s["recent"]):
            try:
                q = json.loads(row["payload"]).get("question", "")
            except json.JSONDecodeError:
                q = ""
            self.t_recent.setItem(r, 0, QTableWidgetItem(q[:140]))
            self.t_recent.setItem(r, 1, QTableWidgetItem({"mcq": "ÇS", "match": "Eşleştirme", "fill": "Boşluk"}.get(row["qtype"], "Klasik")))
            it = QTableWidgetItem(f"%{row['score'] * 100:.0f}")
            it.setForeground(self._color(row["score"]))
            self.t_recent.setItem(r, 2, it)
            when = dt.datetime.fromtimestamp(row["answered_at"]).strftime("%d.%m %H:%M") if row["answered_at"] else ""
            self.t_recent.setItem(r, 3, QTableWidgetItem(when))

    @staticmethod
    def _color(v: float):
        from PySide6.QtGui import QColor
        return QColor(style.OK if v >= 0.7 else "#a16207" if v >= 0.4 else style.BAD)
