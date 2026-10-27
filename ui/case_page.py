"""Vaka çalışması sayfası: adım adım interaktif klinik vaka, vaka içi soru-cevap, PDF dışa aktarma."""
from __future__ import annotations

import datetime as dt
import html
import re
from pathlib import Path

from PySide6.QtCore import QMarginsF, Qt, QTimer
from PySide6.QtGui import QPageLayout, QPageSize, QPdfWriter, QTextDocument
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QRadioButton, QSplitter,
    QSpinBox, QStackedWidget, QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
)

from core.case import LEVELS, CaseEngine, CaseRequest

from . import style
from .context import AppContext
from .quiz_page import DocPicker

LETTERS = "ABCDEFGHIJ"


def esc(s) -> str:
    return html.escape(str(s or "")).replace("\n", "<br>")


def _box(inner: str, bg: str, border: str) -> str:
    return (f"<table width='100%' cellspacing='0' cellpadding='10' style='margin:6px 0;'>"
            f"<tr><td style='background:{bg}; border:1px solid {border};'>{inner}</td></tr></table>")


def _ul(items) -> str:
    return "<ul>" + "".join(f"<li>{esc(x)}</li>" for x in items) + "</ul>" if items else ""


def _src_line(sources: list[dict]) -> str:
    if not sources:
        return ""
    labels = list(dict.fromkeys(s["label"] for s in sources))
    return f"<p style='color:{style.MUTED}; font-size:small'>📖 Kaynak: {esc('; '.join(labels))}</p>"


_NUM = re.compile(r"^[<>≤≥]?\s*(-?\d+(?:[.,]\d+)?)")


def _auto_flag(f: dict) -> str:
    """Bayrak verilmemişse sonuç ile referans aralığını karşılaştırarak H/L çıkarır."""
    if f.get("flag"):
        return f["flag"]
    m = _NUM.match(str(f.get("result", "")).strip())
    ref = str(f.get("ref", "")).replace(",", ".")
    if not m or not ref:
        return ""
    v = float(m.group(1).replace(",", "."))
    r = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*[-–]\s*(-?\d+(?:\.\d+)?)", ref)
    if r:
        lo, hi = float(r.group(1)), float(r.group(2))
        return "H" if v > hi else "L" if v < lo else "N"
    r = re.match(r"^\s*[<≤]\s*(-?\d+(?:\.\d+)?)", ref)
    if r:
        return "H" if v > float(r.group(1)) else "N"
    r = re.match(r"^\s*[>≥]\s*(-?\d+(?:\.\d+)?)", ref)
    if r:
        return "L" if v < float(r.group(1)) else "N"
    return ""


def findings_html(findings: list[dict]) -> str:
    if not findings:
        return ""
    groups: dict[str, list[dict]] = {}
    for f in findings:
        groups.setdefault(f.get("category") or "Sonuçlar", []).append(f)
    out = []
    for cat, rows in groups.items():
        numeric = [f for f in rows if f.get("unit") or f.get("ref") or len(str(f.get("result", ""))) <= 25]
        textual = [f for f in rows if f not in numeric]
        out.append(f"<p style='margin:8px 0 2px 0'><b>🔬 {esc(cat)}</b></p>")
        if numeric:
            trs = []
            for f in numeric:
                flag = _auto_flag(f)
                arrow = {"H": " ↑", "L": " ↓", "A": " ⚠"}.get(flag, "")
                color = style.BAD if flag in ("H", "L", "A") else style.INK
                weight = "bold" if flag in ("H", "L", "A") else "normal"
                trs.append(f"<tr><td>{esc(f.get('test'))}</td>"
                           f"<td style='color:{color}; font-weight:{weight}'>{esc(f.get('result'))}{arrow}</td>"
                           f"<td style='color:{style.MUTED}'>{esc(f.get('unit'))}</td>"
                           f"<td style='color:{style.MUTED}'>{esc(f.get('ref'))}</td></tr>")
            out.append("<table width='98%' border='1' cellspacing='0' cellpadding='5' "
                       f"style='border-color:{style.LINE}; border-collapse:collapse'>"
                       "<tr style='background:#f1f5f9'><th align='left'>Tetkik</th><th align='left'>Sonuç</th>"
                       "<th align='left'>Birim</th><th align='left'>Referans</th></tr>" + "".join(trs) + "</table>")
        for f in textual:
            flag = _auto_flag(f)
            head = esc(f.get("test")) + (" ⚠" if flag == "A" else "")
            out.append(_box(f"<b>{head}</b><br>{esc(f.get('result'))}", "#f8fafc", style.LINE))
    return "".join(out)


def _phase(state: dict) -> str:
    return state.get("phase") or ("done" if state.get("finished") else "stages")


def test_html(case: dict, state: dict, full: bool) -> str:
    qs = case.get("final_test") or []
    if not qs:
        return ""
    answers = state.get("test_answers", {})
    upto = len(qs) if full else min(len(qs), state.get("test_idx", 0) + 1)
    out = [f"<hr><a name='test'></a><h2 style='color:{style.ACCENT}'>📝 Vaka sonu testi</h2>"]
    for k in range(upto):
        q = qs[k]
        a = answers.get(str(k))
        if a is None and not full:
            continue
        out.append(f"<a name='test{k}'></a><p><b>{k + 1}.</b> {esc(q['question'])}</p>")
        rows = []
        chosen = a.get("answer") if a else None
        for j, opt in enumerate(q["options"]):
            mark, color, weight = "", style.INK, "normal"
            if j == q["answer_index"]:
                mark, color = " ✓", style.OK
            if chosen == j:
                weight = "bold"
                if j != q["answer_index"]:
                    mark, color = " ✗", style.BAD
            rows.append(f"<li style='color:{color}; font-weight:{weight}'>{LETTERS[j]}) {esc(opt)}{mark}</li>")
        out.append("<ul style='list-style:none'>" + "".join(rows) + "</ul>")
        fb = []
        if a is not None:
            ok = a.get("score") == 1.0
            fb.append(f"<p style='color:{style.OK if ok else style.BAD}'><b>"
                      f"{'✓ Doğru' if ok else ('Cevap gösterildi' if a.get('revealed') else '✗ Yanlış')}</b></p>")
        if q.get("explanation"):
            fb.append(f"<p>{esc(q['explanation'])}</p>")
        if fb:
            good = a is None or a.get("score") == 1.0
            out.append(_box("".join(fb), "#f0fdfa" if good else style.BAD_SOFT, "#99f6e4" if good else "#fca5a5"))
    return "".join(out)


def render_case_html(case: dict, state: dict, full: bool = False, for_pdf: bool = False) -> str:
    """Vakayı HTML olarak çizer. full=True ise tüm aşamalar ve cevaplar açılır (PDF/okuma için)."""
    stages = case["stages"]
    phase = _phase(state)
    cur = len(stages) - 1 if (full or phase != "stages") else state.get("stage", 0)
    answers = state.get("answers", {})
    finished = full or phase == "done"
    out = [f"<h2 style='color:{style.ACCENT}'>{esc(case['public_title'])}</h2>"]
    if case.get("patient"):
        model = f" · 🤖 {esc(case['model'])}" if case.get("model") and not for_pdf else ""
        out.append(f"<p style='color:{style.MUTED}'>{esc(case['patient'])} · Seviye: {esc(case.get('level', ''))}{model}</p>")

    for i in range(cur + 1):
        st = stages[i]
        t = st["task"]
        a = answers.get(str(i))
        out.append(f"<a name='stage{i}'></a><h3>{i + 1}. {esc(st['title'])}</h3>")
        out.append(f"<p>{esc(st['narrative'])}</p>")
        out.append(findings_html(st.get("findings") or []))
        out.append(f"<p><b>🩺 Görev:</b> {esc(t['prompt'])}</p>")
        show_answer = a is not None or full
        if t["type"] in ("mcq", "multi"):
            correct = {t["answer_index"]} if t["type"] == "mcq" else set(t["correct_indices"])
            chosen = set()
            if a and a.get("answer") is not None:
                chosen = {a["answer"]} if t["type"] == "mcq" else set(a["answer"])
            rows = []
            for j, opt in enumerate(t["options"]):
                mark, color = "", style.INK
                if show_answer:
                    if j in correct:
                        mark, color = " ✓", style.OK
                    if j in chosen and j not in correct:
                        mark, color = " ✗", style.BAD
                weight = "bold" if j in chosen else "normal"
                rows.append(f"<li style='color:{color}; font-weight:{weight}'>{LETTERS[j]}) {esc(opt)}{mark}</li>")
            if show_answer or for_pdf:
                out.append("<ul style='list-style:none'>" + "".join(rows) + "</ul>")
        elif a and a.get("answer"):
            out.append(_box(f"<b>Cevabınız:</b><br>{esc(a['answer'])}", "#f8fafc", style.LINE))

        if show_answer:
            fb = []
            if a and a.get("score") is not None:
                sc = a["score"]
                color = style.OK if sc >= 0.7 else "#a16207" if sc >= 0.4 else style.BAD
                fb.append(f"<p style='color:{color}'><b>Puan: %{sc * 100:.0f}</b></p>")
            elif a and a.get("revealed"):
                fb.append(f"<p style='color:{style.MUTED}'><b>Cevap gösterildi (puanlanmadı)</b></p>")
            g = (a or {}).get("grade")
            if g:
                if g.get("feedback"):
                    fb.append(f"<p>{esc(g['feedback'])}</p>")
                if g.get("covered"):
                    fb.append("<p><b>✓ Doğru değindikleriniz</b></p>" + _ul(g["covered"]))
                if g.get("missing"):
                    fb.append("<p><b>○ Eksik kalanlar</b></p>" + _ul(g["missing"]))
                if g.get("errors"):
                    fb.append(f"<p style='color:{style.BAD}'><b>⚠ Hatalı ifadeler</b></p>" + _ul(g["errors"]))
            if t["type"] == "open":
                if t.get("expected_points"):
                    fb.append("<p><b>Beklenen noktalar</b></p>" + _ul(t["expected_points"]))
                if t.get("model_answer"):
                    fb.append(f"<p><b>İdeal cevap:</b> {esc(t['model_answer'])}</p>")
            if st.get("teaching"):
                fb.append(f"<p><b>💡 Öğretim notu:</b> {esc(st['teaching'])}</p>")
            if st.get("general_knowledge"):
                fb.append(f"<p style='color:#a16207; font-size:small'>ⓘ Bu aşama kaynak dışı genel tıp bilgisi de içeriyor.</p>")
            fb.append(_src_line(st.get("sources", [])))
            out.append(_box("".join(fb), "#f0fdfa", "#99f6e4"))

    if full or phase in ("test", "done"):
        out.append(test_html(case, state, full=full or phase == "done"))
    if finished:
        out.append("<hr>")
        out.append(f"<a name='summary'></a><h2 style='color:{style.ACCENT}'>Vaka özeti</h2>")
        if state.get("score") is not None and not for_pdf:
            parts = [f"Genel başarı: %{state['score'] * 100:.0f}"]
            if state.get("stage_score") is not None:
                parts.append(f"Vaka aşamaları: %{state['stage_score'] * 100:.0f}")
            if state.get("test_score") is not None:
                parts.append(f"Vaka testi: %{state['test_score'] * 100:.0f}")
            out.append(f"<p><b>{' · '.join(parts)}</b></p>")
        out.append(_box(f"<b>Kesin tanı:</b> {esc(case.get('final_diagnosis', ''))}", "#dcfce7", "#86efac"))
        if case.get("differential"):
            rows = "".join(
                f"<tr><td><b>{esc(d['diagnosis'])}</b></td><td>{esc(d.get('for', ''))}</td>"
                f"<td>{esc(d.get('against', ''))}</td></tr>" for d in case["differential"])
            out.append("<h3>Ayırıcı tanı</h3>"
                       "<table width='100%' border='1' cellspacing='0' cellpadding='6' "
                       f"style='border-color:{style.LINE}; border-collapse:collapse'>"
                       "<tr style='background:#f1f5f9'><th align='left'>Tanı</th><th align='left'>Lehine</th>"
                       f"<th align='left'>Aleyhine</th></tr>{rows}</table>")
        if case.get("discussion"):
            out.append(f"<h3>Tartışma</h3><p>{esc(case['discussion'])}</p>")
        if case.get("teaching_points"):
            out.append("<h3>Akılda kalması gerekenler</h3>" + _ul(case["teaching_points"]))
        if case.get("pitfalls"):
            out.append("<h3>Sık yapılan hatalar</h3>" + _ul(case["pitfalls"]))
        if case.get("sources"):
            labels = list(dict.fromkeys(s["label"] for s in case["sources"]))
            out.append("<h3>Kaynaklar</h3>" + _ul(labels))
        out.append(f"<p style='color:{style.MUTED}; font-size:small'>Kurgusal eğitim vakasıdır; yapay zekâ ile "
                   "üretilmiştir ve klinik karar için kullanılmamalıdır.</p>")
    return "".join(out)


class CasePage(QWidget):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.setObjectName("page")
        self.ctx = ctx
        self.case_id: int | None = None
        self.case: dict | None = None
        self.state: dict = {}
        self.selected_docs: set[int] | None = None
        self.gen_task = None
        self.chat_history: list[dict] = []
        self.chat_busy = False
        self._chat_flush = QTimer(self)
        self._chat_flush.setInterval(120)
        self._chat_flush.timeout.connect(self._render_chat)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        root.addWidget(self.stack)
        self.stack.addWidget(self._build_setup())
        self.stack.addWidget(self._build_runner())
        ctx.profile_changed.connect(lambda _: self.refresh_history())
        self.refresh_history()

    # ================================================================ setup
    def _build_setup(self) -> QWidget:
        w = QWidget()
        w.setObjectName("page")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(28, 22, 28, 18)
        lay.setSpacing(12)
        head = QHBoxLayout()
        t = QLabel("Vaka çalışması")
        t.setObjectName("h1")
        head.addWidget(t)
        sub = QLabel("Bir vakayı başvurudan takibe kadar adım adım çöz; her adımda karar ver, geri bildirim al.")
        sub.setObjectName("muted")
        head.addWidget(sub)
        head.addStretch()
        lay.addLayout(head)

        card = QFrame()
        card.setObjectName("card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(18, 16, 18, 16)
        cl.setSpacing(10)

        self.mode_topic = QRadioButton("Konudan vaka üret")
        self.mode_own = QRadioButton("Kendi vakamı çalış (gördüğüm / okuduğum bir vakayı yaz)")
        self.mode_topic.setChecked(True)
        grp = QButtonGroup(self)
        grp.addButton(self.mode_topic)
        grp.addButton(self.mode_own)
        cl.addWidget(self.mode_topic)
        self.topic_le = QLineEdit()
        self.topic_le.setPlaceholderText("örn. diyabetik ketoasidoz, inferior MI, pulmoner emboli (boş = kütüphaneden rastgele)")
        cl.addWidget(self.topic_le)
        cl.addWidget(self.mode_own)
        self.own_te = QPlainTextEdit()
        self.own_te.setPlaceholderText("Vaka özetini yazın: yaş, cinsiyet, şikâyet, öykü, bulgular, tetkikler…\n"
                                       "Ne kadar ayrıntı verirseniz vaka o kadar gerçeğe yakın olur.")
        self.own_te.setFixedHeight(110)
        self.own_te.setEnabled(False)
        cl.addWidget(self.own_te)
        self.privacy = QLabel("🔒 Gerçek bir hastayı yazıyorsanız ad, TC kimlik no, tarih, hastane gibi kimlik bilgilerini "
                              "YAZMAYIN — metin yapay zekâ sağlayıcısına gönderilir.")
        self.privacy.setWordWrap(True)
        self.privacy.setStyleSheet(f"color:#a16207; background:{style.WARN_SOFT}; border-radius:8px; padding:6px 10px;")
        self.privacy.hide()
        cl.addWidget(self.privacy)
        self.mode_own.toggled.connect(self._mode_changed)

        row = QHBoxLayout()
        lab = QLabel("Seviye")
        lab.setObjectName("muted")
        self.level_cb = QComboBox()
        for k, v in LEVELS.items():
            self.level_cb.addItem(k)
            self.level_cb.setItemData(self.level_cb.count() - 1, v, Qt.ToolTipRole)
        self.docs_btn = QPushButton("Kaynaklar: Tümü")
        self.docs_btn.clicked.connect(self._pick_docs)
        self.para_cb = QCheckBox("Paraklinik ağırlıklı")
        self.para_cb.setChecked(True)
        self.para_cb.setToolTip("Laboratuvar, kan gazı, EKG, görüntüleme ve patoloji sonuçlarını yorumlatan aşamalar")
        tl = QLabel("Vaka sonu test")
        tl.setObjectName("muted")
        self.test_sp = QSpinBox()
        self.test_sp.setRange(0, 10)
        self.test_sp.setValue(5)
        self.test_sp.setSuffix(" soru")
        self.test_sp.setToolTip("Vaka bittikten sonra sorulacak TUS tarzı test sorusu sayısı (0 = test yok)")
        self.create_btn = QPushButton("Vakayı oluştur")
        self.create_btn.setObjectName("primary")
        self.create_btn.clicked.connect(self.create_case)
        row.addWidget(lab)
        row.addWidget(self.level_cb)
        row.addWidget(self.docs_btn)
        row.addWidget(self.para_cb)
        row.addWidget(tl)
        row.addWidget(self.test_sp)
        row.addStretch()
        row.addWidget(self.create_btn)
        cl.addLayout(row)
        st = QHBoxLayout()
        self.busy = QProgressBar()
        self.busy.setRange(0, 0)
        self.busy.setFixedWidth(140)
        self.busy.setTextVisible(False)
        self.busy.hide()
        self.status = QLabel("")
        self.status.setObjectName("muted")
        st.addWidget(self.busy)
        st.addWidget(self.status, 1)
        cl.addLayout(st)
        lay.addWidget(card)

        hl = QHBoxLayout()
        h2 = QLabel("Vakalarım")
        h2.setObjectName("h2")
        hl.addWidget(h2)
        hl.addStretch()
        b_open = QPushButton("Aç")
        b_open.clicked.connect(self.open_selected)
        b_del = QPushButton("Sil")
        b_del.setObjectName("danger")
        b_del.clicked.connect(self.delete_selected)
        hl.addWidget(b_open)
        hl.addWidget(b_del)
        lay.addLayout(hl)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Vaka", "Seviye", "Durum", "Tarih"])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        for i in range(1, 4):
            h.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        self.table.doubleClicked.connect(lambda _: self.open_selected())
        lay.addWidget(self.table, 1)
        return w

    def _mode_changed(self, own: bool):
        self.own_te.setEnabled(own)
        self.topic_le.setEnabled(not own)
        self.privacy.setVisible(own)

    def _pick_docs(self):
        docs = self.ctx.db.list_documents()
        if not docs:
            QMessageBox.information(self, "Kütüphane boş", "Önce Kütüphane sekmesinden belge ekleyin.")
            return
        dlg = DocPicker(docs, self.selected_docs, self)
        if dlg.exec():
            ids = dlg.selected_ids()
            self.selected_docs = None if len(ids) == len(docs) else set(ids)
            self.docs_btn.setText("Kaynaklar: Tümü" if self.selected_docs is None
                                  else f"Kaynaklar: {len(self.selected_docs)} belge")

    def refresh_history(self):
        rows = self.ctx.db.list_cases(self.ctx.profile or None)
        self.table.setRowCount(len(rows))
        import json
        for r, c in enumerate(rows):
            try:
                st = json.loads(c["state"] or "{}")
            except ValueError:
                st = {}
            if c["finished"]:
                status = f"Tamamlandı" + (f" · %{c['score'] * 100:.0f}" if c["score"] is not None else "")
            elif st.get("phase") == "test":
                status = f"Vaka testinde · {st.get('test_idx', 0) + 1}. soru"
            else:
                status = f"Devam ediyor · {st.get('stage', 0) + 1}. aşama"
            vals = [c["title"], c["level"], status, dt.datetime.fromtimestamp(c["updated_at"]).strftime("%d.%m.%Y %H:%M")]
            for col, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if col == 0:
                    it.setData(Qt.UserRole, c["id"])
                    if c["topic"]:
                        it.setToolTip(f"Konu: {c['topic']}")
                if col == 2:
                    it.setForeground(Qt.darkGreen if c["finished"] else Qt.darkYellow)
                self.table.setItem(r, col, it)

    def _selected_case_id(self) -> int | None:
        rows = self.table.selectionModel().selectedRows()
        return self.table.item(rows[0].row(), 0).data(Qt.UserRole) if rows else None

    def open_selected(self):
        cid = self._selected_case_id()
        if cid is not None:
            self.load_case(cid)

    def delete_selected(self):
        cid = self._selected_case_id()
        if cid is None:
            return
        if QMessageBox.question(self, "Sil", "Bu vaka silinsin mi?") == QMessageBox.Yes:
            self.ctx.db.delete_case(cid)
            self.refresh_history()

    def create_case(self):
        if not self.ctx.settings.has_any_key:
            QMessageBox.warning(self, "API anahtarı gerekli", "Lütfen önce Ayarlar sekmesinden API anahtarınızı girin.")
            return
        own = self.own_te.toPlainText().strip() if self.mode_own.isChecked() else ""
        if self.mode_own.isChecked() and len(own) < 30:
            QMessageBox.information(self, "Vaka metni kısa", "Lütfen vakayı biraz daha ayrıntılı yazın (en az birkaç cümle).")
            return
        req = CaseRequest(
            topic="" if own else self.topic_le.text().strip(), own_case=own, level=self.level_cb.currentText(),
            doc_ids=sorted(self.selected_docs) if self.selected_docs is not None else None, profile=self.ctx.profile,
            paraclinic=self.para_cb.isChecked(), test_count=self.test_sp.value(),
        )
        engine: CaseEngine = self.ctx.case_engine
        self.create_btn.setEnabled(False)
        self.busy.show()
        self.status.setText("Vaka hazırlanıyor… Kaynaklar seçiliyor ve aşamalar yazılıyor (20-60 sn sürebilir).")

        def done(res):
            self.status.setText("")
            self.refresh_history()
            self.load_case(res["id"])

        def fail(e):
            self.status.setText("")
            QMessageBox.warning(self, "Vaka oluşturulamadı", e)

        def fin():
            self.create_btn.setEnabled(True)
            self.busy.hide()

        self.gen_task = self.ctx.runner.start(lambda p, t: engine.generate(req), on_result=done, on_error=fail,
                                              on_finished=fin)

    # =============================================================== runner
    def _build_runner(self) -> QWidget:
        w = QWidget()
        w.setObjectName("page")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(28, 18, 28, 14)
        lay.setSpacing(10)
        head = QHBoxLayout()
        back = QPushButton("← Vakalarım")
        back.clicked.connect(self.back_to_setup)
        head.addWidget(back)
        self.title_lbl = QLabel("")
        self.title_lbl.setObjectName("h2")
        self.title_lbl.setWordWrap(True)
        head.addWidget(self.title_lbl, 1)
        self.stage_badge = QLabel("")
        self.stage_badge.setObjectName("badge")
        head.addWidget(self.stage_badge)
        self.pdf_btn = QPushButton("PDF olarak kaydet")
        self.pdf_btn.clicked.connect(self.export_pdf)
        head.addWidget(self.pdf_btn)
        lay.addLayout(head)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        lay.addWidget(self.progress)

        split = QSplitter(Qt.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 6, 0)
        ll.setSpacing(8)
        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(False)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        ll.addWidget(self.view, 1)

        self.task_card = QFrame()
        self.task_card.setObjectName("card")
        tl = QVBoxLayout(self.task_card)
        tl.setContentsMargins(14, 12, 14, 12)
        tl.setSpacing(6)
        self.task_lbl = QLabel("")
        self.task_lbl.setWordWrap(True)
        self.task_lbl.setStyleSheet("font-weight:600;")
        tl.addWidget(self.task_lbl)
        self.choices_box = QWidget()
        self.choices_lay = QVBoxLayout(self.choices_box)
        self.choices_lay.setContentsMargins(0, 0, 0, 0)
        self.choices_lay.setSpacing(4)
        tl.addWidget(self.choices_box)
        self.answer_te = QPlainTextEdit()
        self.answer_te.setPlaceholderText("Cevabınızı yazın…")
        self.answer_te.setFixedHeight(110)
        tl.addWidget(self.answer_te)
        br = QHBoxLayout()
        self.reveal_btn = QPushButton("Bilmiyorum, cevabı göster")
        self.reveal_btn.clicked.connect(self.reveal)
        self.answer_btn = QPushButton("Cevapla")
        self.answer_btn.setObjectName("primary")
        self.answer_btn.clicked.connect(self.submit)
        self.next_btn = QPushButton("Sonraki aşama →")
        self.next_btn.setObjectName("primary")
        self.next_btn.clicked.connect(self.next_stage)
        self.new_btn = QPushButton("Yeni vaka")
        self.new_btn.clicked.connect(self.back_to_setup)
        br.addWidget(self.reveal_btn)
        br.addStretch()
        br.addWidget(self.new_btn)
        br.addWidget(self.answer_btn)
        br.addWidget(self.next_btn)
        tl.addLayout(br)
        ll.addWidget(self.task_card)
        split.addWidget(left)

        right = QFrame()
        right.setObjectName("card")
        rl = QVBoxLayout(right)
        rl.setContentsMargins(12, 12, 12, 12)
        rh = QLabel("💬 Vakaya soru sor")
        rh.setObjectName("h2")
        rl.addWidget(rh)
        hint = QLabel("“Bu bulgu neden önemli?”, “Patofizyolojisi ne?” gibi sorular sorabilirsin. "
                      "Vaka bitmeden cevabı söylemez, ipucu verir.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        rl.addWidget(hint)
        self.chat_view = QTextBrowser()
        rl.addWidget(self.chat_view, 1)
        self.chat_in = QLineEdit()
        self.chat_in.setPlaceholderText("Sorunu yaz, Enter'a bas…")
        self.chat_in.returnPressed.connect(self.ask)
        rl.addWidget(self.chat_in)
        split.addWidget(right)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([700, 380])
        lay.addWidget(split, 1)
        disc = QLabel("Kurgusal eğitim vakasıdır; klinik karar için kullanılmaz.")
        disc.setObjectName("disclaimer")
        lay.addWidget(disc)
        return w

    def back_to_setup(self):
        self.refresh_history()
        self.stack.setCurrentIndex(0)

    def load_case(self, cid: int):
        row = self.ctx.db.get_case(cid)
        if not row:
            return
        self.case_id = cid
        self.case = row["payload"]
        self.state = row["state"] or {}
        self.state.setdefault("stage", 0)
        self.state.setdefault("answers", {})
        self.state.setdefault("finished", False)
        self.state.setdefault("phase", "done" if self.state["finished"] else "stages")
        self.state.setdefault("test_answers", {})
        self.state.setdefault("test_idx", 0)
        self.chat_history = []
        self._render_chat()
        self.stack.setCurrentIndex(1)
        self.render()

    def _save(self):
        st = self.state
        stage_sc = [a["score"] for a in st["answers"].values() if a.get("score") is not None]
        test_sc = [a["score"] for a in st.get("test_answers", {}).values() if a.get("score") is not None]
        st["stage_score"] = sum(stage_sc) / len(stage_sc) if stage_sc else None
        st["test_score"] = sum(test_sc) / len(test_sc) if test_sc else None
        allsc = stage_sc + test_sc
        st["score"] = sum(allsc) / len(allsc) if allsc else None
        st["finished"] = st.get("phase") == "done"
        self.ctx.db.update_case_state(self.case_id, self.state, self.state["finished"], self.state["score"])

    # -------------------------------------------------------------- render
    def render(self, scroll_to_end: bool = True):
        case, st = self.case, self.state
        n = len(case["stages"])
        tests = case.get("final_test") or []
        i = st["stage"]
        phase = _phase(st)
        self.title_lbl.setText(case["public_title"])
        if phase == "done":
            self.stage_badge.setText("Tamamlandı")
        elif phase == "test":
            self.stage_badge.setText(f"Test {st['test_idx'] + 1} / {len(tests)}")
        else:
            self.stage_badge.setText(f"Aşama {i + 1} / {n}")
        self.progress.setRange(0, n + len(tests))
        self.progress.setValue(n + len(tests) if phase == "done" else
                               n + st["test_idx"] if phase == "test" else i)
        self.view.setHtml(render_case_html(case, st))
        if scroll_to_end:
            # görev paneli yeniden boyutlanınca görünüm de değişir; yerleşim oturduktan sonra tekrar kaydır
            QTimer.singleShot(0, self._scroll_to_current)
            QTimer.singleShot(150, self._scroll_to_current)

        answered = str(i) in st["answers"]
        task = case["stages"][i]["task"]
        # seçenek widget'larını yeniden kur
        while self.choices_lay.count():
            it = self.choices_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self.choice_widgets = []
        self.choice_group = None

        if phase == "test":
            self._render_test_task()
            return
        if phase == "done":
            self.task_lbl.setText("Vaka tamamlandı. Özeti yukarıda okuyabilir, sağdan soru sorabilir veya PDF olarak kaydedebilirsin.")
            for wdg in (self.choices_box, self.answer_te, self.reveal_btn, self.answer_btn, self.next_btn):
                wdg.hide()
            self.new_btn.show()
            return
        self.new_btn.hide()
        if answered:
            last = i == n - 1
            if last and tests:
                msg, btn = f"Son aşamayı tamamladın. Şimdi {len(tests)} soruluk vaka sonu testi var.", "Teste başla →"
            elif last:
                msg, btn = "Son aşamayı tamamladın. Vaka özetini görmek için bitir.", "Vakayı bitir ve özeti gör"
            else:
                msg, btn = "Geri bildirimi oku, hazır olduğunda devam et.", "Sonraki aşama →"
            self.task_lbl.setText(msg)
            for wdg in (self.choices_box, self.answer_te, self.reveal_btn, self.answer_btn):
                wdg.hide()
            self.next_btn.setText(btn)
            self.next_btn.show()
            self.next_btn.setEnabled(True)
            return

        self.task_lbl.setText(f"🩺 {task['prompt']}")
        self.next_btn.hide()
        self.answer_btn.show()
        self.answer_btn.setEnabled(True)
        self.answer_btn.setText("Cevapla")
        self.reveal_btn.show()
        self.reveal_btn.setEnabled(True)
        if task["type"] == "open":
            self.choices_box.hide()
            self.answer_te.show()
            self.answer_te.setReadOnly(False)
            self.answer_te.setPlainText(st.get("draft", {}).get(str(i), ""))
            self.answer_te.setFocus()
        else:
            self.answer_te.hide()
            self.choices_box.show()
            if task["type"] == "mcq":
                self.choice_group = QButtonGroup(self)
            for j, opt in enumerate(task["options"]):
                wdg = QRadioButton(f"{LETTERS[j]})  {opt}") if task["type"] == "mcq" else QCheckBox(f"{LETTERS[j]})  {opt}")
                wdg.setStyleSheet("padding:3px;")
                if self.choice_group:
                    self.choice_group.addButton(wdg, j)
                self.choices_lay.addWidget(wdg)
                self.choice_widgets.append(wdg)
            if task["type"] == "multi":
                tip = QLabel("Birden fazla seçenek işaretleyebilirsin. Yanlış seçimler puan düşürür.")
                tip.setObjectName("muted")
                self.choices_lay.addWidget(tip)

    def _render_test_task(self):
        st = self.state
        tests = self.case["final_test"]
        k = st["test_idx"]
        q = tests[k]
        answered = str(k) in st["test_answers"]
        if answered:
            last = k == len(tests) - 1
            self.task_lbl.setText("Açıklamayı oku, hazır olduğunda devam et.")
            for wdg in (self.choices_box, self.answer_te, self.reveal_btn, self.answer_btn):
                wdg.hide()
            self.next_btn.setText("Testi bitir ve vaka özetini gör" if last else "Sonraki soru →")
            self.next_btn.show()
            self.next_btn.setEnabled(True)
            return
        self.task_lbl.setText(f"📝 Soru {k + 1}/{len(tests)}: {q['question']}")
        self.next_btn.hide()
        self.answer_te.hide()
        self.answer_btn.show()
        self.answer_btn.setEnabled(True)
        self.answer_btn.setText("Cevapla")
        self.reveal_btn.show()
        self.reveal_btn.setEnabled(True)
        self.choices_box.show()
        self.choice_group = QButtonGroup(self)
        for j, opt in enumerate(q["options"]):
            wdg = QRadioButton(f"{LETTERS[j]})  {opt}")
            wdg.setStyleSheet("padding:3px;")
            self.choice_group.addButton(wdg, j)
            self.choices_lay.addWidget(wdg)
            self.choice_widgets.append(wdg)

    def _scroll_to_current(self):
        """Yeni açılan aşamanın (veya özetin) başlığına kaydırır; cevaplanınca geri bildirimi gösterir."""
        st = self.state
        phase = _phase(st)
        if phase == "test":
            if str(st["test_idx"]) in st["test_answers"]:
                sb = self.view.verticalScrollBar()
                sb.setValue(sb.maximum())
            elif st["test_idx"] == 0:
                self.view.scrollToAnchor("test")
            else:
                sb = self.view.verticalScrollBar()
                sb.setValue(sb.maximum())
        elif phase == "done":
            self.view.scrollToAnchor("summary")
        elif str(st["stage"]) in st["answers"]:
            sb = self.view.verticalScrollBar()
            sb.setValue(sb.maximum())
        else:
            self.view.scrollToAnchor(f"stage{st['stage']}")

    # ------------------------------------------------------------- actions
    def submit(self):
        if _phase(self.state) == "test":
            k = self.state["test_idx"]
            j = self.choice_group.checkedId() if self.choice_group else -1
            if j < 0:
                QMessageBox.information(self, "Seçim yok", "Bir seçenek işaretleyin.")
                return
            q = self.case["final_test"][k]
            self.state["test_answers"][str(k)] = {"answer": j, "score": 1.0 if j == q["answer_index"] else 0.0}
            self._save()
            self.render()
            return
        i = self.state["stage"]
        task = self.case["stages"][i]["task"]
        if task["type"] == "open":
            ans = self.answer_te.toPlainText().strip()
            if not ans:
                QMessageBox.information(self, "Boş cevap", "Bir cevap yazın ya da 'Bilmiyorum, cevabı göster'e basın.")
                return
            self.answer_btn.setEnabled(False)
            self.reveal_btn.setEnabled(False)
            self.answer_btn.setText("Değerlendiriliyor…")
            self.answer_te.setReadOnly(True)
            engine: CaseEngine = self.ctx.case_engine
            case, cid = self.case, self.case_id

            def done(g):
                if self.case_id != cid:
                    return
                self.state["answers"][str(i)] = {"answer": ans, "score": g["score"] / 100.0, "grade": g}
                self.state.get("draft", {}).pop(str(i), None)
                self._save()
                self.render()

            def fail(e):
                self.answer_btn.setEnabled(True)
                self.reveal_btn.setEnabled(True)
                self.answer_btn.setText("Cevapla")
                self.answer_te.setReadOnly(False)
                QMessageBox.warning(self, "Değerlendirilemedi", e)

            self.ctx.runner.start(lambda p, t: engine.grade_open_stage(case, i, ans), on_result=done, on_error=fail)
            return

        if task["type"] == "mcq":
            j = self.choice_group.checkedId() if self.choice_group else -1
            if j < 0:
                QMessageBox.information(self, "Seçim yok", "Bir seçenek işaretleyin.")
                return
            answer = j
        else:
            answer = [j for j, wdg in enumerate(self.choice_widgets) if wdg.isChecked()]
            if not answer:
                QMessageBox.information(self, "Seçim yok", "En az bir seçenek işaretleyin.")
                return
        score = CaseEngine.score_choice(task, answer)
        self.state["answers"][str(i)] = {"answer": answer, "score": score}
        self._save()
        self.render()

    def reveal(self):
        if _phase(self.state) == "test":
            k = self.state["test_idx"]
            self.state["test_answers"][str(k)] = {"answer": None, "score": 0.0, "revealed": True}
            self._save()
            self.render()
            return
        i = self.state["stage"]
        task = self.case["stages"][i]["task"]
        draft = self.answer_te.toPlainText().strip() if task["type"] == "open" else None
        self.state["answers"][str(i)] = {"answer": draft or None, "score": None, "revealed": True}
        self._save()
        self.render()

    def next_stage(self):
        st = self.state
        n = len(self.case["stages"])
        tests = self.case.get("final_test") or []
        phase = _phase(st)
        if phase == "stages":
            if st["stage"] < n - 1:
                st["stage"] += 1
            elif tests:
                st["phase"], st["test_idx"] = "test", 0
            else:
                st["phase"] = "done"
        elif phase == "test":
            if st["test_idx"] < len(tests) - 1:
                st["test_idx"] += 1
            else:
                st["phase"] = "done"
        self._save()
        self.render()

    # ---------------------------------------------------------------- chat
    def ask(self):
        q = self.chat_in.text().strip()
        if not q or self.chat_busy or not self.case:
            return
        if not self.ctx.settings.has_any_key:
            QMessageBox.warning(self, "API anahtarı gerekli", "Lütfen önce Ayarlar sekmesinden API anahtarınızı girin.")
            return
        self.chat_in.clear()
        prior = [{"role": m["role"], "content": m["content"]} for m in self.chat_history if m["content"]]
        self.chat_history.append({"role": "user", "content": q})
        target = {"role": "assistant", "content": ""}
        self.chat_history.append(target)
        self.chat_busy = True
        self._chat_flush.start()
        engine: CaseEngine = self.ctx.case_engine
        case = self.case
        revealed = len(self.case["stages"]) if _phase(self.state) != "stages" else self.state["stage"] + 1
        finished = _phase(self.state) == "done"

        def job(progress, task):
            try:
                for piece in engine.ask_stream(case, revealed, finished, q, prior):
                    if task.cancelled:
                        break
                    progress(piece)
            finally:
                llm = getattr(engine, "last_chat_llm", None)
                if llm is not None:
                    target["model"] = getattr(llm, "label", "")

        def on_err(e):
            target["content"] += f"\n\n⚠️ {e}"

        def fin():
            self.chat_busy = False
            self._chat_flush.stop()
            self._render_chat()

        self.ctx.runner.start(job, on_progress=lambda s: target.__setitem__("content", target["content"] + s),
                              on_error=on_err, on_finished=fin)
        self._render_chat()

    def _render_chat(self):
        if not self.chat_history:
            self.chat_view.setHtml(f"<p style='color:{style.MUTED}'>Henüz soru yok.</p>")
            return
        md = []
        for m in self.chat_history:
            who = "**Sen:**" if m["role"] == "user" else (
                f"**Eğitmen** _({m['model']})_**:**" if m.get("model") else "**Eğitmen:**")
            body = m["content"] or ("_yazıyor…_" if self.chat_busy else "")
            md.append(f"{who} {body}\n")
        self.chat_view.setMarkdown("\n\n".join(md))
        sb = self.chat_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ----------------------------------------------------------------- pdf
    def export_pdf(self):
        if not self.case:
            return
        full = self.state.get("finished", False)
        if not full:
            if QMessageBox.question(
                    self, "Vaka bitmedi",
                    "Vaka henüz bitmedi. Tüm aşamalar, cevaplar ve özet dahil edilsin mi?\n\n"
                    "(Hayır: sadece şu ana kadar açılan aşamalar kaydedilir.)") == QMessageBox.Yes:
                full = True
        safe = re.sub(r'[\\/:*?"<>|]+', "", self.case["public_title"])[:60].strip() or "vaka"
        path, _ = QFileDialog.getSaveFileName(self, "PDF olarak kaydet", str(Path.home() / f"{safe}.pdf"), "PDF (*.pdf)")
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        state = dict(self.state)
        if full:
            state["finished"] = True
            state["phase"] = "done"
        ok = write_case_pdf(self.case, state, path, full=full)
        if ok:
            QMessageBox.information(self, "Kaydedildi", f"PDF kaydedildi:\n{path}")
        else:
            QMessageBox.warning(self, "Hata", "PDF yazılamadı. Dosya başka bir programda açık olabilir.")


def write_case_pdf(case: dict, state: dict, path: str, full: bool = True) -> bool:
    doc = QTextDocument()
    doc.setDefaultStyleSheet("body { font-family: 'Segoe UI', 'Noto Sans', Arial; font-size: 10pt; } "
                             "h2 { font-size: 15pt; } h3 { font-size: 12pt; margin-top: 10px; }")
    doc.setHtml(render_case_html(case, state, full=full, for_pdf=True))
    writer = QPdfWriter(path)
    writer.setPageSize(QPageSize(QPageSize.A4))
    writer.setPageMargins(QMarginsF(16, 16, 16, 16), QPageLayout.Millimeter)
    writer.setResolution(96)  # QTextDocument yazı boyutları ekran dpi'sına göre ölçeklenir
    writer.setTitle(case.get("public_title", "Vaka"))
    doc.setPageSize(writer.pageLayout().paintRectPixels(writer.resolution()).size().toSizeF())
    doc.print_(writer)
    return Path(path).exists() and Path(path).stat().st_size > 0
