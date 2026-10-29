"""Hesap bağlama ve otomatik eşitleme (arayüz tarafı)."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from core import sync as S

from .context import AppContext

AUTO_EVERY_MS = 5 * 60 * 1000


def _when(ts: float | None) -> str:
    if not ts:
        return "henüz yok"
    d = dt.datetime.fromtimestamp(ts)
    return d.strftime("bugün %H:%M") if d.date() == dt.date.today() else d.strftime("%d.%m.%Y %H:%M")


def summarize(st: dict) -> str:
    parts = []
    if st.get("settings"):
        parts.append("ayarlar/API anahtarları")
    n = st.get("added", 0) + st.get("updated", 0)
    if n:
        parts.append(f"{n} kayıt")
    if st.get("lib_down"):
        parts.append(f"{st['lib_down']} kaynak")
    if st.get("deleted"):
        parts.append(f"{st['deleted']} silme")
    got = ("alındı: " + ", ".join(parts)) if parts else "yeni değişiklik yok"
    up = f" · {st['lib_up']} kaynak yüklendi" if st.get("lib_up") else ""
    return got + up


class LinkDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Hesabı bağla")
        self.resize(620, 360)
        lay = QVBoxLayout(self)
        info = QLabel(
            "<b>Aynı kullanıcıyı başka bilgisayarlarda kullan.</b><br>MedQuiz verilerini (API anahtarları, ayarlar, "
            "istatistikler, kelimeler, özetler, sohbetler, vakalar ve kütüphane) senin bulut klasöründe "
            "<b>şifreli</b> olarak tutar. Diğer bilgisayarda aynı klasörü ve şifreyi seçmen yeterli.<br><br>"
            "Bu bilgisayardaki veriler silinmez; iki taraf birleştirilir.")
        info.setWordWrap(True)
        lay.addWidget(info)
        f = QFormLayout()
        self.folder = QComboBox()
        self.folder.setMinimumWidth(380)
        for label, path in S.cloud_folders():
            self.folder.addItem(f"{label} — {path}", path)
        self.folder.addItem("Başka klasör seç…", "__pick__")
        self.folder.activated.connect(self._folder_changed)
        f.addRow("Bulut klasörü", self.folder)
        self.state = QLabel("")
        self.state.setWordWrap(True)
        f.addRow("", self.state)
        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.Password)
        self.pw2 = QLineEdit()
        self.pw2.setEchoMode(QLineEdit.Password)
        self.pw2_lbl = QLabel("Şifre (tekrar)")
        f.addRow("Hesap şifresi", self.pw)
        f.addRow(self.pw2_lbl, self.pw2)
        lay.addLayout(f)
        warn = QLabel("Şifre unutulursa bulut klasöründeki veriler açılamaz (bilgisayarındaki veriler durur). "
                      "Şifre yalnızca bu bilgisayarda saklanır, bir daha sorulmaz.")
        warn.setObjectName("muted")
        warn.setWordWrap(True)
        lay.addWidget(warn)
        lay.addStretch()
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok_btn = bb.button(QDialogButtonBox.Ok)
        bb.button(QDialogButtonBox.Cancel).setText("Vazgeç")
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._picked: str | None = None
        if self.folder.count() == 1:
            self.state.setText("⚠ Bu bilgisayarda OneDrive, Google Drive ya da Dropbox klasörü bulunamadı. "
                               "Bir klasör seçebilir ya da Ayarlar'daki “Yedek dosyası” ile taşıyabilirsin.")
        self._folder_changed()

    def path(self) -> str | None:
        d = self.folder.currentData()
        return self._picked if d == "__pick__" else d

    def _folder_changed(self, *_):
        if self.folder.currentData() == "__pick__":
            p = QFileDialog.getExistingDirectory(self, "Eşitleme klasörünü seç (ör. OneDrive ya da Google Drive)")
            self._picked = p or None
        p = self.path()
        if not p:
            self.ok_btn.setEnabled(False)
            return
        self.ok_btn.setEnabled(True)
        self.existing = S.Account.exists(p)
        if self.existing:
            self.state.setText(f"✓ Bu klasörde MedQuiz hesabın bulundu ({S.account_dir(p)}). Şifreni gir.")
            self.ok_btn.setText("Bağlan")
        else:
            self.state.setText(f"Yeni hesap oluşturulacak: {S.account_dir(p)}. Bir şifre belirle; diğer "
                               "bilgisayarda bu şifreyi gireceksin.")
            self.ok_btn.setText("Hesap oluştur")
        self.pw2.setVisible(not self.existing)
        self.pw2_lbl.setVisible(not self.existing)

    def _accept(self):
        if not self.path():
            return
        if len(self.pw.text()) < 4:
            QMessageBox.information(self, "Şifre", "Şifre en az 4 karakter olmalı.")
            return
        if not self.existing and self.pw.text() != self.pw2.text():
            QMessageBox.information(self, "Şifre", "Şifreler aynı değil.")
            return
        self.accept()


class SyncController(QObject):
    status = Signal(str)

    def __init__(self, ctx: AppContext, parent: QWidget):
        super().__init__(parent)
        self.ctx = ctx
        self.parent_w = parent
        self.engine = S.SyncEngine(ctx.db, ctx.settings)
        self.busy = False
        self._acc: S.Account | None = None
        self._last_version = ""
        self.timer = QTimer(self)
        self.timer.setInterval(AUTO_EVERY_MS)
        self.timer.timeout.connect(lambda: self.sync(silent=True, only_if_changed=True))
        if self.linked:
            self.timer.start()

    # ---------------------------------------------------------- durum
    @property
    def cfg(self) -> dict:
        return self.ctx.settings.extra.get("sync") or {}

    @property
    def linked(self) -> bool:
        return bool(self.cfg.get("folder"))

    def describe(self) -> str:
        if not self.linked:
            return "Bağlı değil. Bağlarsan bu kullanıcıyı başka bilgisayarlarda API anahtarı girmeden kullanırsın."
        c = self.cfg
        return (f"✓ Bağlı: {S.account_dir(c['folder'])}\nSon eşitleme: {_when(c.get('last'))}"
                + (f" · {c['last_note']}" if c.get("last_note") else ""))

    def _account(self) -> S.Account:
        if self._acc is None:
            c = self.cfg
            if not Path(c["folder"]).exists():
                raise S.SyncError(f"Eşitleme klasörü bulunamadı: {c['folder']} (bulut uygulaması kapalı olabilir).")
            self._acc = S.Account.open(c["folder"], c.get("password", ""))
        return self._acc

    # ---------------------------------------------------------- bağlama
    def link(self) -> None:
        dlg = LinkDialog(self.parent_w)
        if not dlg.exec():
            return
        path, pw, existing = dlg.path(), dlg.pw.text(), dlg.existing
        self.busy = True
        self.status.emit("Hesap bağlanıyor ve veriler eşitleniyor…")
        engine = self.engine

        def job(progress, task):
            acc = S.Account.open(path, pw) if existing else S.Account.create(path, pw)
            st = engine.sync(acc, initial=existing)
            return acc, st

        def done(res):
            acc, st = res
            self.busy = False
            self._acc = acc
            self.ctx.settings.extra["sync"] = {"folder": path, "password": pw, "last": st["at"],
                                               "last_note": summarize(st)}
            self.ctx.settings.save(touch=False)
            self._after(st, initial=existing)
            self.timer.start()
            msg = ("Hesap bağlandı ve diğer bilgisayardaki verilerin alındı:\n" + summarize(st) if existing else
                   "Hesap oluşturuldu ve bu bilgisayardaki veriler yüklendi.\n\nDiğer bilgisayarda: Ayarlar → "
                   "Hesap → “Hesabı bağla”, aynı bulut klasörü ve aynı şifre.")
            QMessageBox.information(self.parent_w, "Hesap", msg)

        def fail(e):
            self.busy = False
            self.status.emit(self.describe())
            QMessageBox.warning(self.parent_w, "Hesap bağlanamadı", str(e))

        self.ctx.runner.start(job, on_result=done, on_error=fail)

    def unlink(self) -> None:
        if QMessageBox.question(self.parent_w, "Bağlantıyı kaldır",
                                "Bu bilgisayarın hesapla eşitlenmesi durdurulsun mu?\n\n"
                                "Bu bilgisayardaki veriler ve bulut klasöründeki veriler silinmez.") != QMessageBox.Yes:
            return
        self.ctx.settings.extra.pop("sync", None)
        self.ctx.settings.save(touch=False)
        self._acc = None
        self.timer.stop()
        self.status.emit(self.describe())

    # ---------------------------------------------------------- eşitleme
    def sync(self, silent: bool = False, only_if_changed: bool = False) -> None:
        if not self.linked or self.busy:
            return
        if only_if_changed and self.engine.data_version() == self._last_version and not self._remote_changed():
            return
        self.busy = True
        if not silent:
            self.status.emit("Eşitleniyor…")
        engine = self.engine

        def job(progress, task):
            return engine.sync(self._account())

        def done(st):
            self.busy = False
            c = self.ctx.settings.extra.setdefault("sync", {})
            c["last"], c["last_note"] = st["at"], summarize(st)
            self.ctx.settings.save(touch=False)
            self._after(st)

        def fail(e):
            self.busy = False
            self._acc = None if "Şifre" in str(e) else self._acc
            self.status.emit(f"⚠ Eşitlenemedi: {e}")
            if not silent:
                QMessageBox.warning(self.parent_w, "Eşitleme", str(e))

        self.ctx.runner.start(job, on_result=done, on_error=fail)

    def _remote_changed(self) -> bool:
        try:
            acc_dir = S.account_dir(self.cfg["folder"]) / "devices"
            newest = max((f.stat().st_mtime for f in acc_dir.glob("*.mqd")), default=0)
        except OSError:
            return False
        return newest > (self.cfg.get("last") or 0)

    def _after(self, st: dict, initial: bool = False) -> None:
        self._last_version = self.engine.data_version()
        ctx = self.ctx
        if st.get("settings"):
            if initial and ctx.settings.user_name and ctx.settings.user_name != ctx.profile:
                ctx.set_profile(ctx.settings.user_name)
            ctx.settings_changed.emit()
        if st.get("lib_down") or st.get("deleted"):
            ctx.notify_library_changed()
        if st.get("added") or st.get("updated") or st.get("deleted") or st.get("settings"):
            ctx.data_synced.emit()
            ctx.vocab_changed.emit()
        self.status.emit(self.describe())

    def push_on_exit(self) -> None:
        """Kapanırken: yalnızca bu bilgisayarın anlık görüntüsünü yaz (hızlı)."""
        if not self.linked or self.busy:
            return
        try:
            if self.engine.data_version() != self._last_version:
                self.engine.sync(self._account(), push_only=True, library=False)
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------- yedek dosyası
    def export_backup(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self.parent_w, "Yedek dosyası kaydet",
                                              str(Path.home() / f"MedQuiz_yedek_{dt.date.today():%Y%m%d}.mqbackup"),
                                              "MedQuiz yedeği (*.mqbackup)")
        if not path:
            return
        pw, ok = QInputDialog.getText(self.parent_w, "Yedek şifresi",
                                      "Yedek için bir şifre belirle (yüklerken sorulacak, en az 4 karakter):",
                                      QLineEdit.Password)
        if not ok or len(pw) < 4:
            return
        db, settings = self.ctx.db, self.ctx.settings
        self.status.emit("Yedek hazırlanıyor…")
        self.ctx.runner.start(lambda p, t: S.export_backup(db, settings, path, pw),
                              on_result=lambda st: (self.status.emit(self.describe()),
                                                    QMessageBox.information(self.parent_w, "Yedek",
                                                                            f"Yedek kaydedildi:\n{path}")),
                              on_error=lambda e: QMessageBox.warning(self.parent_w, "Yedek alınamadı", e))

    def import_backup(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self.parent_w, "Yedek dosyası seç", str(Path.home()),
                                              "MedQuiz yedeği (*.mqbackup)")
        if not path:
            return
        pw, ok = QInputDialog.getText(self.parent_w, "Yedek şifresi", "Yedeğin şifresi:", QLineEdit.Password)
        if not ok:
            return
        db, settings = self.ctx.db, self.ctx.settings
        initial = not settings.has_any_key

        def done(st):
            self._after(st, initial=initial)
            QMessageBox.information(self.parent_w, "Yedek yüklendi",
                                    "Bu bilgisayardaki verilerle birleştirildi:\n" + summarize(st))

        self.ctx.runner.start(lambda p, t: S.import_backup(db, settings, path, pw), on_result=done,
                              on_error=lambda e: QMessageBox.warning(self.parent_w, "Yedek yüklenemedi", e))


class AccountCard(QWidget):
    """Ayarlar sayfasındaki 'Hesap' kartının içeriği."""

    def __init__(self, ctl: SyncController):
        super().__init__()
        self.ctl = ctl
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.lbl = QLabel(ctl.describe())
        self.lbl.setWordWrap(True)
        lay.addWidget(self.lbl)
        row = QHBoxLayout()
        self.link_btn = QPushButton("🔗 Hesabı bağla…")
        self.link_btn.setObjectName("primary")
        self.link_btn.clicked.connect(ctl.link)
        self.sync_btn = QPushButton("Şimdi eşitle")
        self.sync_btn.clicked.connect(lambda: ctl.sync(silent=False))
        self.unlink_btn = QPushButton("Bağlantıyı kaldır")
        self.unlink_btn.clicked.connect(ctl.unlink)
        exp = QPushButton("Yedek dosyası al…")
        exp.setToolTip("Bulut klasörü yoksa: tüm verini tek, şifreli bir dosyaya kaydet (USB/e-posta ile taşı)")
        exp.clicked.connect(ctl.export_backup)
        imp = QPushButton("Yedekten yükle…")
        imp.clicked.connect(ctl.import_backup)
        for b in (self.link_btn, self.sync_btn, self.unlink_btn):
            row.addWidget(b)
        row.addStretch()
        row.addWidget(exp)
        row.addWidget(imp)
        lay.addLayout(row)
        ctl.status.connect(self._status)
        self._refresh()

    def _status(self, text: str):
        self.lbl.setText(text)
        self._refresh()

    def _refresh(self):
        on = self.ctl.linked
        self.link_btn.setVisible(not on)
        self.sync_btn.setVisible(on)
        self.unlink_btn.setVisible(on)
        self.lbl.setStyleSheet("" if on else "color:#475569;")
        self.lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
