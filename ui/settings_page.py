"""Ayarlar: birden fazla yapay zekâ sağlayıcısı (Claude, Gemini, OpenAI), yedekleme, arama ve görünüm."""
from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from core.config import PROVIDERS, data_dir
from core.quiz import DIFFICULTIES

from . import style
from .context import AppContext


class ProviderRow:
    """Bir sağlayıcı için anahtar + model + test kontrolleri."""

    def __init__(self, page: "SettingsPage", grid: QGridLayout, row: int, provider: str):
        self.page = page
        self.provider = provider
        info = PROVIDERS[provider]
        s = page.ctx.settings

        name = QLabel(info["label"])
        name.setStyleSheet("font-weight:600;")
        self.key = QLineEdit(s.key_for(provider))
        self.key.setEchoMode(QLineEdit.Password)
        self.key.setPlaceholderText("API anahtarını yapıştırın (boş = kullanılmaz)")
        self.show_btn = QPushButton("👁")
        self.show_btn.setCheckable(True)
        self.show_btn.setFixedWidth(42)
        self.show_btn.setToolTip("Anahtarı göster/gizle")
        self.show_btn.toggled.connect(lambda on: self.key.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.addItems(info["models"])
        self.model.setCurrentText(s.model_for(provider))
        self.model.setMinimumWidth(190)
        self.model.setToolTip("Listede olmayan bir model adını elle de yazabilirsiniz.")
        get_key = QPushButton("Anahtar al ↗")
        get_key.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(info["key_url"])))
        self.test_btn = QPushButton("Test")
        self.test_btn.clicked.connect(self.test)
        self.status = QLabel("")
        self.status.setObjectName("muted")

        grid.addWidget(name, row, 0)
        grid.addWidget(self.key, row, 1)
        grid.addWidget(self.show_btn, row, 2)
        grid.addWidget(self.model, row, 3)
        grid.addWidget(get_key, row, 4)
        grid.addWidget(self.test_btn, row, 5)
        grid.addWidget(self.status, row + 1, 1, 1, 5)

    def collect(self):
        s = self.page.ctx.settings
        setattr(s, f"{self.provider}_api_key", self.key.text().strip())
        setattr(s, f"{self.provider}_model", self.model.currentText().strip() or PROVIDERS[self.provider]["models"][0])

    def test(self):
        self.collect()
        if not self.key.text().strip():
            self.status.setText("Önce anahtarı girin.")
            return
        self.page.ctx.settings.save()
        self.test_btn.setEnabled(False)
        self.status.setText("Test ediliyor…")
        settings, provider = self.page.ctx.settings, self.provider

        def job(progress, task):
            from core.llm import test_provider
            return test_provider(settings, provider)

        def ok(res):
            note = ""
            if res["notes"]:
                note = " (birincil model şu an meşgul; anahtar geçerli, uygulama gerektiğinde yedek modeli kullanır)"
            self.status.setText(f"✓ Çalışıyor — {res['label']} · {res['secs']:.1f} sn{note} · "
                                f"örnek soru: “{res['question'][:90]}”")
            self.status.setStyleSheet(f"color:{style.OK};")
            self.status.setWordWrap(True)

        def fail(e):
            self.status.setText(f"✗ {e}")
            self.status.setStyleSheet(f"color:{style.BAD};")
            self.status.setWordWrap(True)
            self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.page.ctx.runner.start(job, on_result=ok, on_error=fail,
                                   on_finished=lambda: self.test_btn.setEnabled(True))


class SettingsPage(QWidget):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.setObjectName("page")
        self.ctx = ctx
        s = ctx.settings
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        outer.addWidget(scroll)
        body = QWidget()
        body.setObjectName("page")
        scroll.setWidget(body)
        root = QVBoxLayout(body)
        root.setContentsMargins(28, 22, 28, 18)
        root.setSpacing(14)
        t = QLabel("Ayarlar")
        t.setObjectName("h1")
        root.addWidget(t)

        # --- yapay zeka
        card = QFrame()
        card.setObjectName("card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(20, 16, 20, 16)
        cl.setSpacing(10)
        h = QLabel("Yapay zekâ hesapları")
        h.setObjectName("h2")
        cl.addWidget(h)
        info = QLabel("Birden fazla sağlayıcının anahtarını aynı anda girebilirsiniz. Hangisinin kullanılacağını "
                      "aşağıdan veya sol menüdeki “Yapay zekâ” seçicisinden değiştirebilirsiniz.")
        info.setObjectName("muted")
        info.setWordWrap(True)
        cl.addWidget(info)
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(4)
        grid.setColumnStretch(1, 1)
        self.rows: list[ProviderRow] = []
        for i, p in enumerate(PROVIDERS):
            self.rows.append(ProviderRow(self, grid, i * 2, p))
        cl.addLayout(grid)

        f = QFormLayout()
        f.setVerticalSpacing(8)
        self.primary = QComboBox()
        for k, v in PROVIDERS.items():
            self.primary.addItem(v["label"], k)
        self.primary.setCurrentIndex(max(0, self.primary.findData(s.provider)))
        f.addRow("Birincil yapay zekâ", self.primary)
        self.fallback = QCheckBox("Yanıt alınamazsa otomatik olarak yedeğe geç")
        self.fallback.setChecked(s.fallback_enabled)
        f.addRow("", self.fallback)
        fb_note = QLabel("Yoğunluk, hız sınırı veya bağlantı sorununda önce aynı sağlayıcının hafif modeli "
                         "(Gemini Flash-Lite / Claude Haiku), sonra diğer sağlayıcı denenir.")
        fb_note.setObjectName("muted")
        fb_note.setWordWrap(True)
        f.addRow("", fb_note)
        cl.addLayout(f)
        note = QLabel("ℹ️ Claude Pro veya Google AI Pro/Gemini Advanced gibi sohbet abonelikleri burada kullanılamaz; "
                      "API anahtarları ayrı faturalandırılır. Anahtarlar yalnızca bu bilgisayarda saklanır.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        cl.addWidget(note)
        root.addWidget(card)

        # --- quiz & arama
        card2 = QFrame()
        card2.setObjectName("card")
        f2 = QFormLayout(card2)
        f2.setContentsMargins(20, 16, 20, 16)
        f2.setVerticalSpacing(10)
        h2 = QLabel("Quiz ve arama")
        h2.setObjectName("h2")
        f2.addRow(h2)
        self.diff = QComboBox()
        self.diff.addItems(list(DIFFICULTIES))
        self.diff.setCurrentText(s.default_difficulty)
        f2.addRow("Varsayılan zorluk", self.diff)
        self.count = QSpinBox()
        self.count.setRange(1, 50)
        self.count.setValue(s.default_count)
        f2.addRow("Varsayılan soru sayısı", self.count)
        self.emb = QCheckBox("Anlamsal arama (çok dilli embedding modeli, ilk kullanımda ~220 MB indirilir)")
        self.emb.setChecked(s.use_embeddings)
        f2.addRow("", self.emb)
        self.font = QSpinBox()
        self.font.setRange(9, 18)
        self.font.setValue(s.font_size)
        self.font.setSuffix(" pt")
        f2.addRow("Yazı boyutu", self.font)
        root.addWidget(card2)

        # --- güncellemeler
        from core.version import __version__
        card3 = QFrame()
        card3.setObjectName("card")
        f3 = QFormLayout(card3)
        f3.setContentsMargins(20, 16, 20, 16)
        f3.setVerticalSpacing(10)
        h3 = QLabel("Güncellemeler")
        h3.setObjectName("h2")
        f3.addRow(h3)
        f3.addRow("Kullanılan sürüm", QLabel(__version__))
        self.auto_upd = QCheckBox("Açılışta yeni sürüm olup olmadığını kontrol et")
        self.auto_upd.setChecked(s.auto_update)
        f3.addRow("", self.auto_upd)
        urow = QHBoxLayout()
        self.upd_btn = QPushButton("Güncellemeleri şimdi kontrol et")
        self.upd_btn.setEnabled(False)
        self.upd_lbl = QLabel("")
        self.upd_lbl.setObjectName("muted")
        self.upd_lbl.setWordWrap(True)
        urow.addWidget(self.upd_btn)
        urow.addWidget(self.upd_lbl, 1)
        f3.addRow("", urow)
        root.addWidget(card3)

        row = QHBoxLayout()
        diag = QPushButton("Tanılama raporunu kopyala")
        diag.setToolTip("Sorun yaşarsanız bu raporu (API anahtarları çıkarılmış hâlde) destek için paylaşabilirsiniz.")
        diag.clicked.connect(self.copy_diagnostics)
        row.addWidget(diag)
        open_dir = QPushButton("Veri klasörünü aç")
        open_dir.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(data_dir()))))
        row.addWidget(open_dir)
        row.addStretch()
        self.saved_lbl = QLabel("")
        self.saved_lbl.setObjectName("muted")
        row.addWidget(self.saved_lbl)
        save = QPushButton("Kaydet")
        save.setObjectName("primary")
        save.clicked.connect(self.save)
        row.addWidget(save)
        root.addLayout(row)

        disc = QLabel("⚕️ MedQuiz bir eğitim aracıdır. Yapay zekâ hata yapabilir; üretilen içerik tıbbi tavsiye değildir "
                      "ve klinik kararlarda güncel kılavuzlar ile hekim değerlendirmesinin yerini tutmaz.")
        disc.setObjectName("disclaimer")
        disc.setWordWrap(True)
        root.addWidget(disc)
        root.addStretch()
        ctx.settings_changed.connect(self._sync_primary)

    def attach_updater(self, upd):
        self.upd_btn.setEnabled(True)
        self.upd_btn.clicked.connect(lambda: upd.check(silent=False))
        upd.status.connect(self.upd_lbl.setText)

    def copy_diagnostics(self):
        from PySide6.QtWidgets import QApplication

        from core.diagnostics import build_report

        report = build_report(self.ctx)
        QApplication.clipboard().setText(report)
        QMessageBox.information(self, "Kopyalandı",
                                "Tanılama raporu panoya kopyalandı (API anahtarları çıkarıldı).\n\n"
                                "Sohbete yapıştırarak paylaşabilirsiniz.")

    def _sync_primary(self):
        i = self.primary.findData(self.ctx.settings.provider)
        if i >= 0 and i != self.primary.currentIndex():
            self.primary.blockSignals(True)
            self.primary.setCurrentIndex(i)
            self.primary.blockSignals(False)

    def save(self):
        s = self.ctx.settings
        for r in self.rows:
            r.collect()
        s.provider = self.primary.currentData()
        s.fallback_enabled = self.fallback.isChecked()
        s.default_difficulty = self.diff.currentText()
        s.default_count = self.count.value()
        s.use_embeddings = self.emb.isChecked()
        s.font_size = self.font.value()
        s.auto_update = self.auto_upd.isChecked()
        if not s.key_for(s.provider) and s.has_any_key:
            first = s.configured_providers()[0]
            QMessageBox.information(
                self, "Birincil sağlayıcının anahtarı yok",
                f"{PROVIDERS[s.provider]['short']} için anahtar girilmedi. "
                f"Birincil olarak anahtarı olan {PROVIDERS[first]['short']} seçildi.")
            s.provider = first
            self._sync_primary()
        self.ctx.apply_settings()
        self.saved_lbl.setText("✓ Kaydedildi")
