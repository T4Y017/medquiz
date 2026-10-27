"""MedQuiz — tıbbi kaynaklardan quiz üreten RAG masaüstü uygulaması."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# Konsolsuz (pencereli) exe'de stdout/stderr yoktur; ilerleme çubuğu yazan kütüphaneler çökmesin
for _name in ("stdout", "stderr"):
    if getattr(sys, _name) is None:
        setattr(sys, _name, open(os.devnull, "w", encoding="utf-8"))

# PyInstaller ile paketlendiğinde de modüller bulunabilsin
BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QComboBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow,
    QStackedWidget, QVBoxLayout, QWidget,
)

from core.config import PROVIDERS, data_dir  # noqa: E402
from ui import style  # noqa: E402
from ui.case_page import CasePage  # noqa: E402
from ui.chat_page import ChatPage  # noqa: E402
from ui.context import AppContext  # noqa: E402
from ui.library_page import LibraryPage  # noqa: E402
from ui.quiz_page import QuizPage  # noqa: E402
from ui.settings_page import SettingsPage  # noqa: E402
from ui.stats_page import StatsPage  # noqa: E402
from ui.vocab_page import VocabPage  # noqa: E402

from core.version import __version__ as VERSION  # noqa: E402


class MainWindow(QMainWindow):
    SETTINGS_ROW = 6
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.setWindowTitle("MedQuiz — Tıbbi Quiz Asistanı")
        self.resize(1180, 800)
        self.setMinimumSize(900, 620)

        central = QWidget()
        lay = QHBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        side = QWidget()
        side.setObjectName("sidebar")
        side.setFixedWidth(220)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 0, 0, 0)
        brand = QLabel("⚕ MedQuiz")
        brand.setObjectName("brand")
        sub = QLabel("Kaynaklı tıbbi quiz asistanı")
        sub.setObjectName("brandSub")
        sl.addWidget(brand)
        sl.addWidget(sub)

        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setFocusPolicy(Qt.NoFocus)
        sl.addWidget(self.nav, 1)

        al = QLabel("Yapay zekâ")
        al.setObjectName("sideLabel")
        sl.addWidget(al)
        self.ai_cb = QComboBox()
        self.ai_cb.setToolTip("Quiz, vaka ve sohbette hangi yapay zekânın kullanılacağı")
        self.ai_cb.activated.connect(self._ai_changed)
        sl.addWidget(self.ai_cb)

        pl = QLabel("Kullanıcı")
        pl.setObjectName("sideLabel")
        sl.addWidget(pl)
        self.profile_cb = QComboBox()
        self.profile_cb.setEditable(True)
        self.profile_cb.lineEdit().setPlaceholderText("İsim yazın…")
        self.profile_cb.setToolTip("Aile üyeleri kendi ismini seçerse istatistikler ayrı tutulur.")
        names = list(dict.fromkeys(ctx.settings.extra.get("profiles", []) + ctx.db.profiles()))
        self.profile_cb.addItems(names)
        self.profile_cb.setCurrentText(ctx.profile)
        self.profile_cb.lineEdit().editingFinished.connect(self._profile_changed)
        self.profile_cb.activated.connect(lambda _: self._profile_changed())
        sl.addWidget(self.profile_cb)
        ver = QLabel(f"v{VERSION}")
        ver.setObjectName("sideLabel")
        sl.addWidget(ver)
        sl.addSpacing(10)
        lay.addWidget(side)

        self.stack = QStackedWidget()
        lay.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self.quiz = QuizPage(ctx)
        self.case = CasePage(ctx)
        self.chat = ChatPage(ctx)
        self.library = LibraryPage(ctx)
        self.stats = StatsPage(ctx)
        self.vocab = VocabPage(ctx)
        self.settings = SettingsPage(ctx)
        for icon, name, page in (("📝", "Quiz", self.quiz), ("🩺", "Vaka", self.case), ("💬", "Sohbet", self.chat),
                                 ("📚", "Kütüphane", self.library), ("📘", "Kelimeler", self.vocab),
                                 ("📊", "İstatistik", self.stats),
                                 ("⚙", "Ayarlar", self.settings)):
            self.nav.addItem(QListWidgetItem(f"{icon}   {name}"))
            self.stack.addWidget(page)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)

        self.chat.quiz_btn.clicked.connect(self._quiz_from_chat)
        ctx.settings_changed.connect(self._apply_style)

        from ui.update_ui import UpdateController
        self.updater = UpdateController(ctx, self)
        self.settings.attach_updater(self.updater)
        ctx.settings_changed.connect(self._fill_ai)
        self._fill_ai()

        # İlk açılış: anahtar yoksa ayarlar, kütüphane boşsa kütüphane
        if not ctx.settings.has_any_key:
            self.nav.setCurrentRow(self.SETTINGS_ROW)
        elif not ctx.db.list_documents():
            self.nav.setCurrentRow(3)
        else:
            self.nav.setCurrentRow(0)

    def _profile_changed(self):
        name = self.profile_cb.currentText().strip()
        if name != self.ctx.profile:
            self.ctx.set_profile(name)
            if name and self.profile_cb.findText(name) < 0:
                self.profile_cb.addItem(name)

    def _fill_ai(self):
        s = self.ctx.settings
        self.ai_cb.blockSignals(True)
        self.ai_cb.clear()
        provs = [p for p in s.configured_providers()]
        if not provs:
            self.ai_cb.addItem("⚠ Anahtar yok — Ayarlar", None)
        else:
            for p in sorted(provs, key=list(PROVIDERS).index):
                self.ai_cb.addItem(PROVIDERS[p]["short"], p)
                self.ai_cb.setItemData(self.ai_cb.count() - 1, s.label_for(p), Qt.ToolTipRole)
            self.ai_cb.setCurrentIndex(max(0, self.ai_cb.findData(s.provider)))
        self.ai_cb.blockSignals(False)

    def _ai_changed(self, _i: int):
        p = self.ai_cb.currentData()
        if p is None:
            self.nav.setCurrentRow(self.SETTINGS_ROW)
            return
        if p != self.ctx.settings.provider:
            self.ctx.settings.provider = p
            self.ctx.apply_settings()

    def _quiz_from_chat(self):
        q = self.chat.last_user_question()
        if q:
            self.quiz.topic_le.setText(q[:120])
        self.nav.setCurrentRow(0)

    def _apply_style(self):
        QApplication.instance().setStyleSheet(style.stylesheet(self.ctx.settings.font_size))

    def closeEvent(self, e):
        self.ctx.runner.wait_all(1500)
        super().closeEvent(e)


def _selftest(argv: list[str]) -> int:
    import argparse

    from core.selftest import run

    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--pdf", default=None)
    ap.add_argument("--out", default="selftest.json")
    a, _ = ap.parse_known_args(argv)
    return run(a.pdf, a.out)


def main() -> int:
    if "--selftest" in sys.argv:
        return _selftest(sys.argv[1:])
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(data_dir() / "medquiz.log", encoding="utf-8"), logging.StreamHandler()],
    )
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    app = QApplication(sys.argv)
    app.setApplicationName("MedQuiz")
    icon = BASE / "assets" / "icon.ico"
    if not icon.exists():
        icon = Path(__file__).resolve().parent / "assets" / "icon.ico"
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    ctx = AppContext()
    app.setStyleSheet(style.stylesheet(ctx.settings.font_size))
    win = MainWindow(ctx)
    win.show()
    ctx.warmup_index()
    from core import updater as _upd
    _upd.cleanup_old_downloads()
    if ctx.settings.auto_update:
        QTimer.singleShot(4000, lambda: win.updater.check(silent=True))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
