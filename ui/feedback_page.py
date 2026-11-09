"""Geri bildirim: şikayet / istek yaz, kaydet, GitHub'a gönder; durumunu ve geliştiricinin cevabını takip et."""
from __future__ import annotations

import datetime as dt
import html
import time

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit,
    QPushButton, QSplitter, QTextBrowser, QVBoxLayout, QWidget,
)

from core import feedback as fb

from . import style
from .context import AppContext


def _when(ts: float | None) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M") if ts else ""


def status_of(f: dict) -> tuple[str, str]:
    """(kısa durum, renk)"""
    if not f.get("sent_at"):
        return "Gönderilmedi", "#64748b"
    if not f.get("issue_no"):
        if not f.get("checked_at") or (f.get("checked_at") or 0) < (f.get("sent_at") or 0) + 30:
            return "📨 Gönderildi", "#a16207"
        return "⚠ GitHub'da bulunamadı (gönder düğmesine bastın mı?)", "#b91c1c"
    if f.get("state") == "closed":
        return "✓ Tamamlandı", style.OK
    if f.get("reply"):
        return "💬 Cevap var", "#0369a1"
    return "⏳ Sırada", "#a16207"


class FeedbackPage(QWidget):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.setObjectName("page")
        self.ctx = ctx
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 22, 28, 18)
        root.setSpacing(12)
        head = QHBoxLayout()
        t = QLabel("Geri bildirim")
        t.setObjectName("h1")
        head.addWidget(t)
        sub = QLabel("Şikayetlerin ve isteklerin geliştiriciye gider; yapılınca uygulama kendini günceller")
        sub.setObjectName("muted")
        head.addWidget(sub)
        head.addStretch()
        root.addLayout(head)

        # --- yeni kayıt
        card = QFrame()
        card.setObjectName("card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(18, 14, 18, 14)
        r = QHBoxLayout()
        self.kind = QComboBox()
        for k, v in fb.KINDS.items():
            self.kind.addItem(v, k)
        self.area = QComboBox()
        self.area.addItems(fb.AREAS)
        r.addWidget(QLabel("Tür:"))
        r.addWidget(self.kind)
        r.addSpacing(12)
        r.addWidget(QLabel("Bölüm:"))
        r.addWidget(self.area)
        r.addStretch()
        cl.addLayout(r)
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText(
            "Ne oldu ya da ne istiyorsun? Ne kadar ayrıntılı yazarsan o kadar doğru düzeltilir.\n"
            "Örn: “Özette 40-60. sayfaları seçtim, 3 sayfadan sonra yazı kesildi; altta ‘yedek model’ uyarısı vardı.”\n"
            "Örn: “Kartlarda ‘zor’ diye üçüncü bir seçenek olsun, 2 gün sonra tekrar sorsun.”")
        self.text.setMinimumHeight(110)
        cl.addWidget(self.text)
        r2 = QHBoxLayout()
        self.diag = QCheckBox("Tanılama bilgisini ekle (sürüm, sistem, son hata satırları)")
        self.diag.setChecked(True)
        self.diag.setToolTip("API anahtarları, e-posta adresleri, dosya yolları ve belge adları otomatik çıkarılır.")
        r2.addWidget(self.diag)
        r2.addStretch()
        save = QPushButton("Kaydet")
        save.setToolTip("Sonra göndermek üzere sakla")
        save.clicked.connect(lambda: self.save(send=False))
        send = QPushButton("Kaydet ve gönder")
        send.setObjectName("primary")
        send.clicked.connect(lambda: self.save(send=True))
        r2.addWidget(save)
        r2.addWidget(send)
        cl.addLayout(r2)
        note = QLabel("Gönder'e basınca tarayıcıda GitHub açılır; yazdıkların hazır gelir, yalnızca yeşil "
                      "<b>Create / Submit</b> düğmesine basarsın. Geliştirici deposu herkese açıktır; kişisel bilgi "
                      "yazma.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        cl.addWidget(note)
        root.addWidget(card)

        # --- liste + ayrıntı
        split = QSplitter(Qt.Horizontal)
        left = QFrame()
        left.setObjectName("card")
        ll = QVBoxLayout(left)
        ll.setContentsMargins(10, 10, 10, 10)
        lh = QHBoxLayout()
        h = QLabel("Kayıtlarım")
        h.setObjectName("h2")
        lh.addWidget(h)
        lh.addStretch()
        self.check_btn = QPushButton("Durumları yenile")
        self.check_btn.clicked.connect(self.check)
        lh.addWidget(self.check_btn)
        ll.addLayout(lh)
        self.list = QListWidget()
        self.list.setStyleSheet("QListWidget{border:none; background:transparent;}"
                                "QListWidget::item{padding:7px 6px; border-radius:7px;}"
                                "QListWidget::item:selected{background:#ccfbf1; color:#0f172a;}")
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.currentRowChanged.connect(lambda _: self._show())
        ll.addWidget(self.list, 1)
        split.addWidget(left)
        right = QFrame()
        right.setObjectName("card")
        rl = QVBoxLayout(right)
        rl.setContentsMargins(14, 12, 14, 12)
        self.detail = QTextBrowser()
        self.detail.setOpenExternalLinks(True)
        rl.addWidget(self.detail, 1)
        br = QHBoxLayout()
        self.send_btn = QPushButton("GitHub'a gönder")
        self.send_btn.setObjectName("primary")
        self.send_btn.clicked.connect(self.send_selected)
        self.mark_btn = QPushButton("Gönderdim")
        self.mark_btn.setToolTip("Tarayıcıda gönderdiysen işaretle; durum GitHub'dan otomatik bulunur")
        self.mark_btn.clicked.connect(self.mark_sent)
        self.copy_btn = QPushButton("Metni kopyala")
        self.copy_btn.setToolTip("GitHub hesabın yoksa: kopyala ve geliştiriciye (sohbete) yapıştır")
        self.copy_btn.clicked.connect(self.copy_selected)
        self.open_btn = QPushButton("GitHub'da aç")
        self.open_btn.clicked.connect(self.open_selected)
        self.del_btn = QPushButton("Sil")
        self.del_btn.setObjectName("danger")
        self.del_btn.clicked.connect(self.delete_selected)
        for b in (self.send_btn, self.mark_btn, self.copy_btn, self.open_btn):
            br.addWidget(b)
        br.addStretch()
        br.addWidget(self.del_btn)
        rl.addLayout(br)
        split.addWidget(right)
        split.setSizes([330, 700])
        root.addWidget(split, 1)
        ctx.data_synced.connect(self.refresh)
        self.refresh()

    # ------------------------------------------------------------------
    def refresh(self, select: int | None = None):
        cur = select if select is not None else self._sel_id()
        self.items = self.ctx.db.list_feedback()
        self.list.blockSignals(True)
        self.list.clear()
        for f in self.items:
            st, _c = status_of(f)
            first = " ".join(f["text"].split())
            it = QListWidgetItem(f"{st} · {f['area']}\n{first[:46]}{'…' if len(first) > 46 else ''}")
            it.setData(Qt.UserRole, f["id"])
            it.setToolTip(first[:300])
            self.list.addItem(it)
        idx = next((i for i, f in enumerate(self.items) if f["id"] == cur), 0)
        self.list.setCurrentRow(idx if self.items else -1)
        self.list.blockSignals(False)
        self._show()

    def _sel_id(self) -> int | None:
        it = self.list.currentItem() if hasattr(self, "list") else None
        return it.data(Qt.UserRole) if it else None

    def _sel(self) -> dict | None:
        sid = self._sel_id()
        return next((f for f in getattr(self, "items", []) if f["id"] == sid), None)

    def _show(self):
        f = self._sel()
        for b in (self.send_btn, self.mark_btn, self.copy_btn, self.open_btn, self.del_btn):
            b.setEnabled(bool(f))
        if not f:
            self.detail.setHtml(
                f"<div style='color:{style.MUTED}; padding:16px'><h3>Nasıl çalışır?</h3><ol>"
                "<li>Yukarıya şikayetini ya da isteğini yaz, <b>Kaydet ve gönder</b>'e bas.</li>"
                "<li>Tarayıcıda GitHub açılır, metin hazır gelir; yeşil düğmeye basıp gönderirsin "
                "(ilk seferde GitHub'a giriş yapman gerekebilir).</li>"
                "<li>Geliştirici (Claude) gönderilenleri düzenli olarak okur, düzeltir ve yeni sürüm yayınlar; "
                "uygulama kendini günceller.</li>"
                "<li>Durumu ve cevabı burada görürsün: ⏳ Sırada → 💬 Cevap var → ✓ Tamamlandı.</li></ol></div>")
            return
        st, color = status_of(f)
        kind = fb.KINDS.get(f["kind"], f["kind"])
        parts = [f"<p style='color:{color}; font-weight:600'>{st}"
                 + (f" · <a href='https://github.com/{fb.UPDATE_REPO}/issues/{f['issue_no']}'>#{f['issue_no']}</a>"
                    if f.get("issue_no") else "") + "</p>",
                 f"<p><b>{html.escape(kind)}</b> · {html.escape(f['area'])} · {_when(f['created_at'])}</p>",
                 f"<p>{html.escape(f['text']).replace(chr(10), '<br>')}</p>"]
        if f.get("reply"):
            parts.append(f"<hr><p><b>Geliştiricinin cevabı:</b></p><p>{html.escape(f['reply']).replace(chr(10), '<br>')}"
                         "</p>")
        if f.get("diag"):
            parts.append(f"<hr><p style='color:{style.MUTED}'>Tanılama:<br><span style='font-family:Consolas,monospace;"
                         f"font-size:small'>{html.escape(f['diag']).replace(chr(10), '<br>')}</span></p>")
        self.detail.setHtml("".join(parts))
        sent = bool(f.get("sent_at"))
        self.send_btn.setText("Yeniden gönder" if sent else "GitHub'a gönder")
        self.mark_btn.setVisible(not sent)
        self.open_btn.setVisible(bool(f.get("issue_no")))

    # ------------------------------------------------------------------
    def save(self, send: bool):
        text = self.text.toPlainText().strip()
        if len(text) < 5:
            QMessageBox.information(self, "Geri bildirim", "Lütfen ne olduğunu ya da ne istediğini birkaç kelimeyle yaz.")
            return
        diag = fb.diagnostics(self.ctx) if self.diag.isChecked() else ""
        fid = self.ctx.db.add_feedback(self.kind.currentData(), self.area.currentText(), fb.clean(text), diag,
                                       fb.new_marker(), self.ctx.profile)
        self.text.clear()
        self.refresh(select=fid)
        if send:
            self.send_selected()

    def send_selected(self):
        f = self._sel()
        if not f:
            return
        url = fb.new_issue_url(f["kind"], f["area"], f["text"], f["diag"], f["marker"])
        QDesktopServices.openUrl(QUrl(url))
        self.ctx.db.update_feedback(f["id"], sent_at=time.time())
        self.refresh(select=f["id"])
        QMessageBox.information(
            self, "Geri bildirim",
            "Tarayıcıda GitHub açıldı. Metin hazır; yeşil <b>Create</b> (ya da <b>Submit new issue</b>) düğmesine bas.\n\n"
            "GitHub hesabın yoksa: “Metni kopyala” ile kopyalayıp geliştiriciye gönderebilirsin.".replace("<b>", "")
            .replace("</b>", ""))

    def mark_sent(self):
        f = self._sel()
        if f:
            self.ctx.db.update_feedback(f["id"], sent_at=time.time())
            self.refresh(select=f["id"])
            self.check()

    def copy_selected(self):
        f = self._sel()
        if f:
            QGuiApplication.clipboard().setText(fb.issue_title(f["kind"], f["area"], f["text"]) + "\n\n" +
                                                fb.issue_body(f["kind"], f["area"], f["text"], f["diag"], f["marker"]))
            QMessageBox.information(self, "Kopyalandı", "Metin panoya kopyalandı.")

    def open_selected(self):
        f = self._sel()
        if f and f.get("issue_no"):
            QDesktopServices.openUrl(QUrl(f"https://github.com/{fb.UPDATE_REPO}/issues/{f['issue_no']}"))

    def delete_selected(self):
        f = self._sel()
        if f and QMessageBox.question(self, "Sil", "Bu kayıt silinsin mi? (GitHub'daki konu silinmez)") == QMessageBox.Yes:
            self.ctx.db.delete_feedback(f["id"])
            self.refresh()

    def check(self, silent: bool = False):
        sent = [f for f in self.ctx.db.list_feedback() if f.get("sent_at")]
        if not sent:
            return
        self.check_btn.setEnabled(False)
        self.check_btn.setText("Bakılıyor…")
        markers = [f["marker"] for f in sent]

        def done(res):
            self.check_btn.setEnabled(True)
            self.check_btn.setText("Durumları yenile")
            changed = 0
            for f in sent:
                info = res.get(f["marker"])
                if not info:
                    self.ctx.db.update_feedback(f["id"], checked_at=time.time())
                    continue
                new = {"issue_no": info["number"], "state": info["state"], "reply": info["reply"],
                       "checked_at": time.time()}
                if any(f.get(k) != v for k, v in new.items() if k != "checked_at"):
                    changed += 1
                self.ctx.db.update_feedback(f["id"], **new)
            self.refresh()
            if changed and silent:
                self.ctx.feedback_news.emit(changed)

        def fail(e):
            self.check_btn.setEnabled(True)
            self.check_btn.setText("Durumları yenile")
            if not silent:
                QMessageBox.warning(self, "Geri bildirim", str(e))

        self.ctx.runner.start(lambda p, t: fb.fetch_statuses(markers), on_result=done, on_error=fail)

    def showEvent(self, e):
        super().showEvent(e)
        self.check(silent=True)
