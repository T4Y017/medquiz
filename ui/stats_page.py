"""İstatistikler: genel başarı, türe ve konuya göre performans, son cevaplar."""
from __future__ import annotations

import datetime as dt
import json

from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QTableWidget,
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
        f4, self.v_open = _tile("Klasik")
        for i, f in enumerate((f1, f2, f3, f4)):
            tiles.addWidget(f, 0, i)
        root.addLayout(tiles)

        mid = QHBoxLayout()
        left = QVBoxLayout()
        l1 = QLabel("Konulara göre (en zayıftan güçlüye)")
        l1.setObjectName("h2")
        left.addWidget(l1)
        self.t_topic = _table(["Konu", "Soru", "Başarı"])
        left.addWidget(self.t_topic)
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
        self.v_open.setText(f"%{by['open'][2] * 100:.0f}  ({by['open'][1]})" if "open" in by else "—")

        self.t_topic.setRowCount(len(s["by_topic"]))
        for r, (topic, n, avg) in enumerate(s["by_topic"]):
            self.t_topic.setItem(r, 0, QTableWidgetItem(topic))
            self.t_topic.setItem(r, 1, QTableWidgetItem(str(n)))
            it = QTableWidgetItem(f"%{avg * 100:.0f}")
            it.setForeground(self._color(avg))
            self.t_topic.setItem(r, 2, it)

        self.t_recent.setRowCount(len(s["recent"]))
        for r, row in enumerate(s["recent"]):
            try:
                q = json.loads(row["payload"]).get("question", "")
            except json.JSONDecodeError:
                q = ""
            self.t_recent.setItem(r, 0, QTableWidgetItem(q[:140]))
            self.t_recent.setItem(r, 1, QTableWidgetItem({"mcq": "ÇS", "match": "Eşleştirme"}.get(row["qtype"], "Klasik")))
            it = QTableWidgetItem(f"%{row['score'] * 100:.0f}")
            it.setForeground(self._color(row["score"]))
            self.t_recent.setItem(r, 2, it)
            when = dt.datetime.fromtimestamp(row["answered_at"]).strftime("%d.%m %H:%M") if row["answered_at"] else ""
            self.t_recent.setItem(r, 3, QTableWidgetItem(when))

    @staticmethod
    def _color(v: float):
        from PySide6.QtGui import QColor
        return QColor(style.OK if v >= 0.7 else "#a16207" if v >= 0.4 else style.BAD)
