"""Güncelleme bildirimi, indirme ilerlemesi ve kurulum başlatma (arayüz tarafı)."""
from __future__ import annotations

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog, QWidget

from core import updater
from core.version import __version__

from .context import AppContext


class UpdateController(QObject):
    status = Signal(str)      # Ayarlar sayfasındaki durum etiketi için

    def __init__(self, ctx: AppContext, parent: QWidget):
        super().__init__(parent)
        self.ctx = ctx
        self.parent_w = parent
        self.busy = False
        self._dl_task = None

    # ------------------------------------------------------------- kontrol
    def check(self, silent: bool = True) -> None:
        """silent=True: açılışta; yalnızca yeni sürüm varsa konuşur. False: kullanıcı elle istedi."""
        if self.busy:
            return
        self.busy = True
        self.status.emit("Güncellemeler kontrol ediliyor…")

        def done(info):
            self.busy = False
            if info is None:
                self.status.emit(f"MedQuiz güncel (sürüm {__version__}).")
                if not silent:
                    QMessageBox.information(self.parent_w, "Güncel", f"En güncel sürümü kullanıyorsunuz ({__version__}).")
                return
            self.status.emit(f"Yeni sürüm mevcut: {info.version}")
            skip = self.ctx.settings.extra.get("skip_version")
            if silent and skip == info.version:
                return
            self._offer(info)

        def fail(e):
            self.busy = False
            self.status.emit(f"Güncelleme kontrol edilemedi: {e}")
            if not silent:
                QMessageBox.warning(self.parent_w, "Güncelleme", str(e))

        self.ctx.runner.start(lambda p, t: updater.check(), on_result=done, on_error=fail)

    def _offer(self, info: updater.UpdateInfo) -> None:
        box = QMessageBox(self.parent_w)
        box.setWindowTitle("Yeni sürüm hazır")
        box.setIcon(QMessageBox.Information)
        box.setText(f"<b>MedQuiz {info.version}</b> yayınlandı (şu an kullanılan: {__version__}).")
        notes = info.notes.strip()
        if notes:
            first = "\n".join(notes.splitlines()[:12])
            box.setInformativeText(first[:900] + ("…" if len(notes) > 900 else ""))
        box.setDetailedText(notes or "")
        can = updater.can_self_install()
        if can:
            now = box.addButton("Şimdi güncelle", QMessageBox.AcceptRole)
        else:
            now = box.addButton("Sürüm sayfasını aç", QMessageBox.AcceptRole)
        box.addButton("Sonra", QMessageBox.RejectRole)
        skip = box.addButton("Bu sürümü atla", QMessageBox.DestructiveRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is now:
            if can:
                self._download_and_install(info)
            else:
                QDesktopServices.openUrl(QUrl(info.page))
        elif clicked is skip:
            self.ctx.settings.extra["skip_version"] = info.version
            self.ctx.settings.save()

    # ------------------------------------------------------- indir + kur
    def _download_and_install(self, info: updater.UpdateInfo) -> None:
        dlg = QProgressDialog(f"MedQuiz {info.version} indiriliyor…", "İptal", 0, 100, self.parent_w)
        dlg.setWindowTitle("Güncelleme")
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setValue(0)
        state = {"cancel": False}
        dlg.canceled.connect(lambda: state.__setitem__("cancel", True))

        def job(progress, task):
            return updater.download(info, progress=lambda d, t: progress((d, t)),
                                    should_stop=lambda: state["cancel"] or task.cancelled)

        def on_prog(ev):
            d, t = ev
            if t:
                dlg.setValue(int(d * 100 / t))
                dlg.setLabelText(f"MedQuiz {info.version} indiriliyor… {d / 1e6:.0f} / {t / 1e6:.0f} MB")

        def done(path):
            dlg.setLabelText("Kurulum başlatılıyor; MedQuiz kapanıp birkaç saniye içinde yeniden açılacak…")
            dlg.setCancelButton(None)
            dlg.setValue(100)
            QApplication.processEvents()
            try:
                updater.install(path)
            except updater.UpdateError as e:
                dlg.close()
                QMessageBox.warning(self.parent_w, "Güncelleme", str(e))
                return
            # kurulum programı dosyaları değiştirebilsin diye uygulamayı kapat
            QTimer.singleShot(800, QApplication.instance().quit)

        def fail(e):
            dlg.close()
            if not state["cancel"]:
                QMessageBox.warning(self.parent_w, "Güncelleme", f"{e}\n\nMevcut sürüm çalışmaya devam ediyor.")

        self._dl_task = self.ctx.runner.start(job, on_result=done, on_error=fail, on_progress=on_prog)
        dlg.show()
