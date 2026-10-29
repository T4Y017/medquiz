"""Hesap bağlama ve otomatik eşitleme (arayüz tarafı)."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from core import gdrive
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
        return bool(self.cfg.get("folder") or self.cfg.get("refresh_token"))

    @property
    def is_google(self) -> bool:
        return bool(self.cfg.get("refresh_token"))

    def describe(self) -> str:
        if not self.linked:
            return ("Giriş yapılmadı. Google hesabınla giriş yaparsan bu kullanıcıyı (API anahtarları, istatistikler, "
                    "kelimeler, özetler, kütüphane) başka bilgisayarlarda da aynı hesapla kullanırsın.")
        c = self.cfg
        who = f"Google hesabı: {c.get('email') or '?'}" if self.is_google else f"Klasör: {S.account_dir(c['folder'])}"
        return (f"✓ {who}\nSon eşitleme: {_when(c.get('last'))}"
                + (f" · {c['last_note']}" if c.get("last_note") else ""))

    def _account(self) -> S.Account:
        if self._acc is None:
            c = self.cfg
            if self.is_google:
                self._acc = S.Account.google(gdrive.DriveStore(gdrive.GoogleAuth(c["refresh_token"])), c.get("email", ""))
                return self._acc
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

    def google_sign_in(self) -> None:
        if not gdrive.available():
            QMessageBox.information(self.parent_w, "Google ile giriş",
                                    "Bu sürümde Google ile giriş henüz etkin değil. Bir sonraki güncellemede gelecek.")
            return
        from PySide6.QtWidgets import QProgressDialog
        state = {"cancel": False}
        dlg = QProgressDialog("Tarayıcıda Google hesabını seç ve MedQuiz'e izin ver…\n\n"
                              "MedQuiz yalnızca kendi verilerini Drive'ında gizli bir uygulama alanına yazar; "
                              "Drive'ındaki diğer dosyaları göremez.", "Vazgeç", 0, 0, self.parent_w)
        dlg.setWindowTitle("Google ile giriş")
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.canceled.connect(lambda: state.__setitem__("cancel", True))
        dlg.show()
        self.busy = True
        engine = self.engine

        def job(progress, task):
            tok = gdrive.sign_in(cancelled=lambda: state["cancel"] or task.cancelled)
            progress("Google hesabına bağlanıldı; veriler eşitleniyor…")
            store = gdrive.DriveStore(gdrive.GoogleAuth(tok["refresh_token"]))
            existed = bool(store.list("devices/"))
            acc = S.Account.google(store, tok["email"])
            st = engine.sync(acc, initial=existed)
            return tok, acc, st, existed

        def prog(msg):
            dlg.setLabelText(msg)

        def done(res):
            tok, acc, st, existed = res
            dlg.close()
            self.busy = False
            self._acc = acc
            self.ctx.settings.extra["sync"] = {"refresh_token": tok["refresh_token"], "email": tok["email"],
                                               "last": st["at"], "last_note": summarize(st)}
            self.ctx.settings.save(touch=False)
            self._after(st, initial=existed)
            self.timer.start()
            msg = (f"{tok['email']} hesabındaki MedQuiz verilerin bu bilgisayara geldi:\n{summarize(st)}" if existed else
                   f"{tok['email']} hesabına bağlandın; bu bilgisayardaki veriler Google Drive'ına yüklendi.\n\n"
                   "Başka bir bilgisayarda: Ayarlar → Hesap → “Google ile giriş yap”, aynı Google hesabı.")
            QMessageBox.information(self.parent_w, "Google ile giriş", msg)

        def fail(e):
            dlg.close()
            self.busy = False
            self.status.emit(self.describe())
            if not state["cancel"]:
                QMessageBox.warning(self.parent_w, "Google ile giriş", str(e))

        self.ctx.runner.start(job, on_progress=prog, on_result=done, on_error=fail)

    def unlink(self) -> None:
        if QMessageBox.question(self.parent_w, "Bağlantıyı kaldır",
                                "Bu bilgisayarda hesaptan çıkılsın ve eşitleme durdurulsun mu?\n\n"
                                "Bu bilgisayardaki veriler ve hesaptaki (Google Drive) veriler silinmez.") != QMessageBox.Yes:
            return
        old = self.ctx.settings.extra.pop("sync", None) or {}
        self.ctx.settings.save(touch=False)
        if old.get("refresh_token"):
            gdrive.revoke(old["refresh_token"])
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
            self._acc = None
            self.status.emit(f"⚠ Eşitlenemedi: {e}")
            if not silent:
                QMessageBox.warning(self.parent_w, "Eşitleme", str(e))

        self.ctx.runner.start(job, on_result=done, on_error=fail)

    def _remote_changed(self) -> bool:
        if self.is_google:
            return True          # Drive'ı sorgulamak bir istek; 5 dakikada bir eşitlemek zaten ucuz
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


def _google_icon():
    from PySide6.QtCore import QByteArray
    from PySide6.QtGui import QIcon, QPixmap
    svg = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48"><circle cx="24" cy="24" r="23" fill="white"/>'
           b'<path fill="#4285F4" d="M43.6 24.5c0-1.6-.1-2.8-.4-4H24v7.4h11.2c-.5 2.6-2 4.8-4.3 6.3v5.2h6.9c4-3.7 5.8-9.1 '
           b'5.8-14.9z"/><path fill="#34A853" d="M24 44c5.8 0 10.6-1.9 14.1-5.2l-6.9-5.2c-1.9 1.3-4.3 2-7.2 2-5.6 0-10.3-3.7-12-8.8H4.9v5.4C8.4 39.2 15.6 44 24 44z"/>'
           b'<path fill="#FBBC05" d="M12 26.8c-.4-1.3-.7-2.6-.7-4s.3-2.7.7-4v-5.4H4.9C3.4 16.3 2.6 19.6 2.6 22.8s.8 6.5 2.3 9.4z"/>'
           b'<path fill="#EA4335" d="M24 10.3c3.1 0 5.9 1.1 8.1 3.2l6.1-6.1C34.6 4 29.8 2 24 2 15.6 2 8.4 6.8 4.9 13.4l7.1 5.4c1.7-5.1 6.4-8.5 12-8.5z"/></svg>')
    pm = QPixmap()
    pm.loadFromData(QByteArray(svg), "SVG")
    return QIcon(pm)


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
        self.link_btn = QPushButton("  Google ile giriş yap")
        self.link_btn.setIcon(_google_icon())
        self.link_btn.setObjectName("primary")
        self.link_btn.setToolTip("Aynı Google hesabıyla giriş yaptığın her bilgisayarda aynı kullanıcı")
        self.link_btn.clicked.connect(ctl.google_sign_in)
        self.sync_btn = QPushButton("Şimdi eşitle")
        self.sync_btn.clicked.connect(lambda: ctl.sync(silent=False))
        self.unlink_btn = QPushButton("Çıkış yap")
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
