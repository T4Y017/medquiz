"""Quiz sayfası: soru üretimi, cevaplama, geri bildirim, sürekli mod."""
from __future__ import annotations

import html
import json

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QCompleter, QDialog, QDialogButtonBox, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QScrollArea, QSpinBox, QTextBrowser, QVBoxLayout, QWidget,
)

from core.quiz import BATCH, DIFFICULTIES, QTYPES, GenRequest

from . import style
from .context import AppContext

LETTERS = "ABCDEFGH"


def esc(s: str) -> str:
    return html.escape(s or "").replace("\n", "<br>")


class DocPicker(QDialog):
    def __init__(self, docs: list[dict], selected: set[int] | None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Kaynak seç")
        self.resize(520, 480)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Sorular hangi belgelerden üretilsin?"))
        self.list = QListWidget()
        for d in docs:
            it = QListWidgetItem(f"{d['title']}" + (f"   [{d['topic']}]" if d.get("topic") else ""))
            it.setData(Qt.UserRole, d["id"])
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if (selected is None or d["id"] in selected) else Qt.Unchecked)
            self.list.addItem(it)
        lay.addWidget(self.list)
        row = QHBoxLayout()
        b_all, b_none = QPushButton("Tümü"), QPushButton("Hiçbiri")
        b_all.clicked.connect(lambda: self._set_all(Qt.Checked))
        b_none.clicked.connect(lambda: self._set_all(Qt.Unchecked))
        row.addWidget(b_all)
        row.addWidget(b_none)
        row.addStretch()
        lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Tamam")
        bb.button(QDialogButtonBox.Cancel).setText("Vazgeç")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _set_all(self, state):
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(state)

    def selected_ids(self) -> list[int]:
        return [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.Checked]


class QuizPage(QWidget):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.setObjectName("page")
        self.ctx = ctx
        self.questions: list[dict] = []
        self.results: dict[int, dict] = {}   # soru indeksi → {"answer", "score", "grade"}
        self.idx = -1
        self.gen_token = 0
        self.task = None
        self.req: GenRequest | None = None
        self.selected_docs: set[int] | None = None
        self.review_mode = False
        self._fb_note = ""
        self._build()
        ctx.library_changed.connect(self._refresh_topics)
        self._refresh_topics()
        self._render()

    # ================================================================ UI
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 22, 28, 18)
        root.setSpacing(12)

        head = QHBoxLayout()
        t = QLabel("Quiz")
        t.setObjectName("h1")
        head.addWidget(t)
        head.addStretch()
        self.score_lbl = QLabel("")
        self.score_lbl.setObjectName("muted")
        head.addWidget(self.score_lbl)
        root.addLayout(head)

        # --- ayar kartı
        cfg = QFrame()
        cfg.setObjectName("card")
        g = QGridLayout(cfg)
        g.setContentsMargins(16, 14, 16, 14)
        g.setHorizontalSpacing(12)
        g.setVerticalSpacing(8)

        self.type_cb = QComboBox()
        for k, v in QTYPES.items():
            self.type_cb.addItem(v, k)
        self.type_cb.setCurrentIndex(2)
        self.count_sp = QSpinBox()
        self.count_sp.setRange(1, 50)
        self.count_sp.setValue(self.ctx.settings.default_count)
        self.diff_cb = QComboBox()
        for k, v in DIFFICULTIES.items():
            self.diff_cb.addItem(k)
            self.diff_cb.setItemData(self.diff_cb.count() - 1, v, Qt.ToolTipRole)
        self.diff_cb.setCurrentText(self.ctx.settings.default_difficulty)
        self.topic_le = QLineEdit()
        self.topic_le.setPlaceholderText("Konu (boş = tüm kütüphaneden rastgele) — örn. akut koroner sendrom")
        self.docs_btn = QPushButton("Kaynaklar: Tümü")
        self.docs_btn.clicked.connect(self._pick_docs)
        self.cont_cb = QCheckBox("Sürekli mod")
        self.cont_cb.setToolTip("Sorular bittikçe arka planda yenileri otomatik üretilir.")
        self.start_btn = QPushButton("Başlat")
        self.start_btn.setObjectName("primary")
        self.start_btn.clicked.connect(self.start)
        self.review_btn = QPushButton("Yanlışlarımı tekrar et")
        self.review_btn.clicked.connect(self.start_review)

        def lab(s):
            l = QLabel(s)
            l.setObjectName("muted")
            return l

        g.addWidget(lab("Soru türü"), 0, 0)
        g.addWidget(lab("Sayı"), 0, 1)
        g.addWidget(lab("Zorluk"), 0, 2)
        g.addWidget(lab("Konu"), 0, 3)
        g.addWidget(self.type_cb, 1, 0)
        g.addWidget(self.count_sp, 1, 1)
        g.addWidget(self.diff_cb, 1, 2)
        g.addWidget(self.topic_le, 1, 3)
        g.setColumnStretch(3, 1)
        row2 = QHBoxLayout()
        row2.addWidget(self.docs_btn)
        row2.addWidget(self.cont_cb)
        row2.addStretch()
        row2.addWidget(self.review_btn)
        row2.addWidget(self.start_btn)
        g.addLayout(row2, 2, 0, 1, 4)
        root.addWidget(cfg)

        # --- durum
        st = QHBoxLayout()
        self.busy = QProgressBar()
        self.busy.setRange(0, 0)
        self.busy.setFixedWidth(140)
        self.busy.setTextVisible(False)
        self.busy.hide()
        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.stop_btn = QPushButton("Durdur")
        self.stop_btn.clicked.connect(self.stop_generation)
        self.stop_btn.hide()
        st.addWidget(self.busy)
        st.addWidget(self.status, 1)
        st.addWidget(self.stop_btn)
        root.addLayout(st)

        # --- soru kartı
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        holder = QWidget()
        holder.setObjectName("page")
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(0, 0, 4, 0)
        self.card = QFrame()
        self.card.setObjectName("card")
        cl = QVBoxLayout(self.card)
        cl.setContentsMargins(22, 18, 22, 20)
        cl.setSpacing(12)

        meta = QHBoxLayout()
        self.counter_lbl = QLabel("")
        self.counter_lbl.setObjectName("h2")
        self.badge_type = QLabel()
        self.badge_type.setObjectName("badge")
        self.badge_topic = QLabel()
        self.badge_topic.setObjectName("badge")
        self.badge_diff = QLabel()
        self.badge_diff.setObjectName("badge")
        meta.addWidget(self.counter_lbl)
        meta.addSpacing(8)
        meta.addWidget(self.badge_type)
        meta.addWidget(self.badge_diff)
        meta.addWidget(self.badge_topic)
        meta.addStretch()
        cl.addLayout(meta)

        self.q_lbl = QLabel()
        self.q_lbl.setWordWrap(True)
        self.q_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        f = self.q_lbl.font()
        f.setPointSize(f.pointSize() + 2)
        self.q_lbl.setFont(f)
        cl.addWidget(self.q_lbl)

        self.opts_box = QWidget()
        ol = QVBoxLayout(self.opts_box)
        ol.setContentsMargins(0, 0, 0, 0)
        ol.setSpacing(8)
        self.opt_btns: list[QPushButton] = []
        for i in range(5):
            b = QPushButton()
            b.setObjectName("option")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, i=i: self.answer_mcq(i))
            b.setMinimumHeight(44)
            ol.addWidget(b)
            self.opt_btns.append(b)
        cl.addWidget(self.opts_box)

        self.open_box = QWidget()
        obl = QVBoxLayout(self.open_box)
        obl.setContentsMargins(0, 0, 0, 0)
        self.answer_te = QPlainTextEdit()
        self.answer_te.setPlaceholderText("Cevabınızı buraya yazın…")
        self.answer_te.setMinimumHeight(150)
        obl.addWidget(self.answer_te)
        r = QHBoxLayout()
        r.addStretch()
        self.grade_btn = QPushButton("Cevabı değerlendir")
        self.grade_btn.setObjectName("primary")
        self.grade_btn.clicked.connect(self.grade_open)
        self.show_ans_btn = QPushButton("Cevabı göster")
        self.show_ans_btn.clicked.connect(self.reveal_open)
        r.addWidget(self.show_ans_btn)
        r.addWidget(self.grade_btn)
        obl.addLayout(r)
        cl.addWidget(self.open_box)

        self.fb = QFrame()
        fbl = QVBoxLayout(self.fb)
        fbl.setContentsMargins(16, 12, 16, 12)
        self.fb_lbl = QLabel()
        self.fb_lbl.setWordWrap(True)
        self.fb_lbl.setTextFormat(Qt.RichText)
        self.fb_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        fbl.addWidget(self.fb_lbl)
        self.src_btn = QPushButton("Kaynak metni göster")
        self.src_btn.setCheckable(True)
        self.src_btn.toggled.connect(lambda on: (self.src_view.setVisible(on),
                                                 self.src_btn.setText("Kaynak metni gizle" if on else "Kaynak metni göster")))
        fbl.addWidget(self.src_btn, 0, Qt.AlignLeft)
        self.src_view = QTextBrowser()
        self.src_view.setMinimumHeight(180)
        self.src_view.hide()
        fbl.addWidget(self.src_view)
        cl.addWidget(self.fb)

        self.empty_lbl = QLabel()
        self.empty_lbl.setWordWrap(True)
        self.empty_lbl.setAlignment(Qt.AlignCenter)
        self.empty_lbl.setObjectName("muted")
        self.empty_lbl.setMinimumHeight(220)
        cl.addWidget(self.empty_lbl)

        hl.addWidget(self.card)
        hl.addStretch()
        self.scroll.setWidget(holder)
        root.addWidget(self.scroll, 1)

        # --- gezinme
        nav = QHBoxLayout()
        self.prev_btn = QPushButton("← Önceki")
        self.prev_btn.clicked.connect(lambda: self.go(self.idx - 1))
        self.next_btn = QPushButton("Sonraki →")
        self.next_btn.setObjectName("primary")
        self.next_btn.clicked.connect(self.next)
        disc = QLabel("Eğitim amaçlıdır; tanı/tedavi kararı için hekime danışın.")
        disc.setObjectName("disclaimer")
        nav.addWidget(self.prev_btn)
        nav.addStretch()
        nav.addWidget(disc)
        nav.addStretch()
        nav.addWidget(self.next_btn)
        root.addLayout(nav)

        # kısayollar: A-E / 1-5 şık seçimi, Enter sonraki
        for i in range(5):
            for key in (LETTERS[i], str(i + 1)):
                sc = QShortcut(QKeySequence(key), self)
                sc.setContext(Qt.WidgetWithChildrenShortcut)
                sc.activated.connect(lambda i=i: self._shortcut_option(i))
        for key in ("Return", "Enter"):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(self._shortcut_next)

    # ========================================================= helpers
    def _refresh_topics(self):
        topics = self.ctx.db.topics()
        comp = QCompleter(topics, self)
        comp.setCaseSensitivity(Qt.CaseInsensitive)
        comp.setFilterMode(Qt.MatchContains)
        self.topic_le.setCompleter(comp)
        if self.selected_docs is not None:
            valid = {d["id"] for d in self.ctx.db.list_documents()}
            self.selected_docs &= valid
            self._update_docs_btn()

    def _update_docs_btn(self):
        if self.selected_docs is None:
            self.docs_btn.setText("Kaynaklar: Tümü")
        else:
            self.docs_btn.setText(f"Kaynaklar: {len(self.selected_docs)} belge")

    def _pick_docs(self):
        docs = self.ctx.db.list_documents()
        if not docs:
            QMessageBox.information(self, "Kütüphane boş", "Önce Kütüphane sekmesinden belge ekleyin.")
            return
        dlg = DocPicker(docs, self.selected_docs, self)
        if dlg.exec():
            ids = dlg.selected_ids()
            self.selected_docs = None if len(ids) == len(docs) else set(ids)
            self._update_docs_btn()

    def _set_busy(self, on: bool, text: str = ""):
        self.busy.setVisible(on)
        self.stop_btn.setVisible(on)
        if text or not on:
            self.status.setText(text)

    def _shortcut_option(self, i):
        q = self.current()
        if q and q["type"] == "mcq" and not self.answer_te.hasFocus() and not self.topic_le.hasFocus():
            if i < len(q["options"]):
                self.answer_mcq(i)

    def _shortcut_next(self):
        if self.answer_te.hasFocus() or self.topic_le.hasFocus():
            return
        if self.current() is not None and self.idx in self.results:
            self.next()

    def current(self) -> dict | None:
        return self.questions[self.idx] if 0 <= self.idx < len(self.questions) else None

    # ====================================================== generation
    def _build_request(self) -> GenRequest:
        return GenRequest(
            qtype=self.type_cb.currentData(), count=self.count_sp.value(), difficulty=self.diff_cb.currentText(),
            topic=self.topic_le.text().strip(),
            doc_ids=sorted(self.selected_docs) if self.selected_docs is not None else None,
            profile=self.ctx.profile,
        )

    def start(self):
        if not self.ctx.settings.has_any_key:
            QMessageBox.warning(self, "API anahtarı gerekli", "Lütfen önce Ayarlar sekmesinden API anahtarınızı girin.")
            return
        if self.selected_docs is not None and not self.selected_docs:
            QMessageBox.information(self, "Kaynak yok", "En az bir kaynak belge seçin.")
            return
        self.stop_generation(silent=True)
        self.review_mode = False
        self.questions, self.results, self.idx = [], {}, -1
        self.req = self._build_request()
        self._render()
        self._generate(self.req.count)

    def _generate(self, count: int):
        if self.req is None:
            return
        self.gen_token += 1
        token = self.gen_token
        req = GenRequest(**{**self.req.__dict__, "count": count})
        self._fb_note = ""
        engine = self.ctx.engine

        def job(progress, task):
            return engine.generate(req, on_batch=progress, should_stop=lambda: task.cancelled)

        self._set_busy(True, "Sorular hazırlanıyor… (ilk sorular birkaç saniye içinde gelir)")
        self.task = self.ctx.runner.start(
            job,
            on_progress=lambda batch, t=token: self._on_batch(batch, t),
            on_result=lambda res, t=token: self._on_gen_done(t),
            on_error=lambda e, t=token: self._on_gen_error(e, t),
        )

    def _on_batch(self, batch: list[dict], token: int):
        if token != self.gen_token:
            return
        self.questions.extend(batch)
        if self.idx == -1:
            self.go(0)
        else:
            self._render_nav()
        if batch and batch[0].get("fell_back"):
            self._fb_note = f"  ·  ⚠ Birincil yapay zekâ yanıt vermedi, {batch[0].get('model', 'yedek')} ile devam edildi."
        self.status.setText(f"{len(self.questions)} soru hazır…{self._fb_note}")

    def _on_gen_done(self, token: int):
        if token != self.gen_token:
            return
        self.task = None
        self._set_busy(False, f"{len(self.questions)} soru hazır.{self._fb_note}" if self.questions else "")
        self._render_nav()

    def _on_gen_error(self, err: str, token: int):
        if token != self.gen_token:
            return
        self.task = None
        self._set_busy(False, "")
        if not self.questions:
            self._render()
        QMessageBox.warning(self, "Soru üretilemedi", err)

    def stop_generation(self, silent: bool = False):
        if self.task:
            self.task.cancel()
            self.task = None
        self.gen_token += 1
        self._set_busy(False, "" if silent else "Durduruldu.")
        self._render_nav()

    def start_review(self):
        rows = self.ctx.db.wrong_questions(limit=self.count_sp.value(), profile=self.ctx.profile or None)
        if not rows:
            QMessageBox.information(self, "Harika!", "Tekrar edilecek yanlış/düşük puanlı soru yok.")
            return
        self.stop_generation(silent=True)
        qs = []
        for r in rows:
            try:
                q = json.loads(r["payload"])
            except json.JSONDecodeError:
                continue
            q["id"] = r["id"]
            qs.append(q)
        self.review_mode = True
        self.req = None
        self.questions, self.results, self.idx = qs, {}, -1
        self.status.setText(f"Tekrar modu: {len(qs)} soru")
        self.go(0)

    # ======================================================= answering
    def answer_mcq(self, i: int):
        q = self.current()
        if not q or q["type"] != "mcq" or self.idx in self.results:
            return
        ok = i == q["answer_index"]
        self.results[self.idx] = {"answer": i, "score": 1.0 if ok else 0.0}
        self.ctx.engine.record(q, q["options"][i], 1.0 if ok else 0.0)
        self._render()

    def grade_open(self):
        q = self.current()
        if not q or q["type"] != "open" or self.idx in self.results:
            return
        ans = self.answer_te.toPlainText().strip()
        if not ans:
            QMessageBox.information(self, "Boş cevap", "Değerlendirme için bir cevap yazın ya da 'Cevabı göster'e basın.")
            return
        idx = self.idx
        engine = self.ctx.engine
        self.grade_btn.setEnabled(False)
        self.show_ans_btn.setEnabled(False)
        self.grade_btn.setText("Değerlendiriliyor…")

        def done(res):
            self.results[idx] = {"answer": ans, "score": res["score"] / 100.0, "grade": res}
            engine.record(q, ans, res["score"] / 100.0)
            self.grade_btn.setText("Cevabı değerlendir")
            if idx == self.idx:
                self._render()

        def fail(e):
            self.grade_btn.setText("Cevabı değerlendir")
            self.grade_btn.setEnabled(True)
            self.show_ans_btn.setEnabled(True)
            QMessageBox.warning(self, "Değerlendirilemedi", e)

        self.ctx.runner.start(lambda p, t: engine.grade_open(q, ans), on_result=done, on_error=fail)

    def reveal_open(self):
        q = self.current()
        if not q or self.idx in self.results:
            return
        self.results[self.idx] = {"answer": self.answer_te.toPlainText(), "score": None, "revealed": True}
        self._render()

    # ====================================================== navigation
    def go(self, i: int):
        if not 0 <= i < len(self.questions):
            return
        if self.current() and self.current()["type"] == "open" and self.idx not in self.results:
            self.questions[self.idx]["_draft"] = self.answer_te.toPlainText()
        self.idx = i
        self._render()
        self.scroll.verticalScrollBar().setValue(0)
        self._maybe_prefetch()

    def next(self):
        if self.idx < len(self.questions) - 1:
            self.go(self.idx + 1)
        elif self.cont_cb.isChecked() and not self.review_mode:
            if not self.task:
                self._generate(BATCH)
            self.status.setText("Yeni sorular hazırlanıyor…")
        else:
            self._show_summary()

    def _maybe_prefetch(self):
        if (self.cont_cb.isChecked() and not self.review_mode and self.req and not self.task
                and len(self.questions) - self.idx <= 2):
            self._generate(BATCH)

    # ======================================================== rendering
    def _score_summary(self) -> tuple[int, float, int, int]:
        scored = [r["score"] for r in self.results.values() if r.get("score") is not None]
        correct = sum(1 for s in scored if s >= 0.6)
        avg = sum(scored) / len(scored) if scored else 0.0
        return len(scored), avg, correct, len(self.questions)

    def _render_nav(self):
        n, avg, correct, total = self._score_summary()
        self.prev_btn.setEnabled(self.idx > 0)
        has_q = self.current() is not None
        last = self.idx >= len(self.questions) - 1
        if not has_q:
            self.next_btn.setEnabled(False)
            self.next_btn.setText("Sonraki →")
        elif last and self.cont_cb.isChecked() and not self.review_mode:
            self.next_btn.setEnabled(True)
            self.next_btn.setText("Yeni sorular →")
        elif last:
            self.next_btn.setEnabled(bool(self.questions) and not self.task)
            self.next_btn.setText("Sonucu gör")
        else:
            self.next_btn.setEnabled(True)
            self.next_btn.setText("Sonraki →")
        self.score_lbl.setText(f"Cevaplanan: {n}   ·   Doğru: {correct}   ·   Ortalama: %{avg * 100:.0f}" if n else "")
        if has_q:
            extra = " (üretiliyor…)" if self.task else ""
            self.counter_lbl.setText(f"Soru {self.idx + 1} / {total}{extra}")

    def _show_widgets(self, q: bool):
        for w in (self.counter_lbl, self.badge_type, self.badge_topic, self.badge_diff, self.q_lbl):
            w.setVisible(q)
        self.empty_lbl.setVisible(not q)

    def _render(self):
        q = self.current()
        self.fb.hide()
        self.opts_box.hide()
        self.open_box.hide()
        if q is None:
            self._show_widgets(False)
            if self.task:
                self.empty_lbl.setText("Sorular hazırlanıyor…\n\nKaynak metinler seçiliyor ve yapay zeka soruları yazıyor.")
            elif self.ctx.retriever.db.list_documents():
                self.empty_lbl.setText("Başlamak için soru türünü, sayısını ve zorluğu seçip “Başlat”a basın.\n\n"
                                       "İpucu: Konu alanını boş bırakırsanız sorular kütüphanenin tamamından, "
                                       "henüz sorulmamış bölümlere öncelik verilerek üretilir.")
            else:
                self.empty_lbl.setText("Kütüphaneniz boş.\n\nSoldaki “Kütüphane” sekmesinden PDF, Word, metin dosyası "
                                       "veya web sayfası ekleyin; sorular bu kaynaklardan üretilecek.")
            self._render_nav()
            return

        self._show_widgets(True)
        self.badge_type.setText("Çoktan seçmeli" if q["type"] == "mcq" else "Klasik")
        self.badge_topic.setText(q.get("topic") or "Genel")
        self.badge_topic.setVisible(bool(q.get("topic")))
        diff = self.req.difficulty if self.req else "Tekrar"
        self.badge_diff.setText(diff)
        self.q_lbl.setText(q["question"])
        res = self.results.get(self.idx)

        if q["type"] == "mcq":
            self.opts_box.show()
            for i, b in enumerate(self.opt_btns):
                if i < len(q["options"]):
                    b.show()
                    b.setText(f"{LETTERS[i]})  {q['options'][i]}")
                    state = ""
                    if res is not None:
                        if i == q["answer_index"]:
                            state = "correct"
                        elif i == res["answer"]:
                            state = "wrong"
                        else:
                            state = "dim"
                    b.setProperty("state", state)
                    b.setEnabled(res is None or state in ("correct", "wrong"))
                    b.style().unpolish(b)
                    b.style().polish(b)
                else:
                    b.hide()
            if res is not None:
                self._render_mcq_feedback(q, res)
        else:
            self.open_box.show()
            answered = res is not None
            self.answer_te.setReadOnly(answered)
            self.answer_te.setPlainText(res["answer"] if answered else q.get("_draft", ""))
            self.grade_btn.setVisible(not answered)
            self.show_ans_btn.setVisible(not answered)
            self.grade_btn.setEnabled(not answered)
            self.show_ans_btn.setEnabled(not answered)
            if answered:
                self._render_open_feedback(q, res)
        self._render_nav()

    def _set_sources(self, q: dict):
        srcs = q.get("sources") or []
        texts = q.get("source_texts") or []
        parts = []
        for s, t in zip(srcs, texts):
            parts.append(f"<p><b>{esc(s)}</b></p><p style='color:#334155'>{esc(t)}</p><hr>")
        self.src_view.setHtml("".join(parts) or "<i>Kaynak metin yok.</i>")
        self.src_btn.setChecked(False)
        self.src_view.hide()

    def _src_line(self, q: dict) -> str:
        srcs = q.get("sources") or []
        parts = []
        if srcs:
            parts.append(f"📖 Kaynak: {esc('; '.join(dict.fromkeys(srcs)))}")
        if q.get("model"):
            parts.append(f"🤖 {esc(q['model'])}")
        return f"<p style='color:{style.MUTED}'>{' &nbsp;·&nbsp; '.join(parts)}</p>" if parts else ""

    def _render_mcq_feedback(self, q: dict, res: dict):
        ok = res["score"] >= 1.0
        self.fb.setObjectName("feedbackOk" if ok else "feedbackBad")
        self.fb.style().unpolish(self.fb)
        self.fb.style().polish(self.fb)
        letter = LETTERS[q["answer_index"]]
        head = (f"<h3 style='color:{style.OK}'>✓ Doğru!</h3>" if ok else
                f"<h3 style='color:{style.BAD}'>✗ Yanlış — doğru cevap {letter}</h3>")
        body = f"<p>{esc(q.get('explanation', ''))}</p>" if q.get("explanation") else ""
        self.fb_lbl.setText(head + body + self._src_line(q))
        self._set_sources(q)
        self.fb.show()

    def _render_open_feedback(self, q: dict, res: dict):
        if res.get("revealed"):
            self.fb.setObjectName("feedbackMid")
            head = "<h3>Örnek cevap</h3>"
        else:
            s = res["score"]
            self.fb.setObjectName("feedbackOk" if s >= 0.7 else "feedbackMid" if s >= 0.4 else "feedbackBad")
            color = style.OK if s >= 0.7 else "#a16207" if s >= 0.4 else style.BAD
            head = f"<h3 style='color:{color}'>Puan: {s * 100:.0f} / 100</h3>"
        self.fb.style().unpolish(self.fb)
        self.fb.style().polish(self.fb)
        parts = [head]
        g = res.get("grade")
        if g:
            if g.get("feedback"):
                parts.append(f"<p>{esc(g['feedback'])}</p>")
            if g.get("covered"):
                parts.append("<p><b>✓ Doğru değindikleriniz:</b></p><ul>" +
                             "".join(f"<li>{esc(x)}</li>" for x in g["covered"]) + "</ul>")
            if g.get("missing"):
                parts.append("<p><b>○ Eksik kalanlar:</b></p><ul>" +
                             "".join(f"<li>{esc(x)}</li>" for x in g["missing"]) + "</ul>")
            if g.get("errors"):
                parts.append(f"<p style='color:{style.BAD}'><b>⚠ Hatalı ifadeler:</b></p><ul>" +
                             "".join(f"<li>{esc(x)}</li>" for x in g["errors"]) + "</ul>")
        if q.get("key_points"):
            parts.append("<p><b>Anahtar noktalar:</b></p><ul>" +
                         "".join(f"<li>{esc(x)}</li>" for x in q["key_points"]) + "</ul>")
        parts.append(f"<p><b>Örnek cevap:</b><br>{esc(q.get('model_answer', ''))}</p>")
        parts.append(self._src_line(q))
        self.fb_lbl.setText("".join(parts))
        self._set_sources(q)
        self.fb.show()

    def _show_summary(self):
        n, avg, correct, total = self._score_summary()
        unanswered = total - len(self.results)
        msg = (f"Toplam {total} soru · Cevaplanan {n} · Doğru (≥%60) {correct}\n"
               f"Ortalama başarı: %{avg * 100:.0f}")
        if unanswered:
            msg += f"\n\n{unanswered} soru cevaplanmadı."
        box = QMessageBox(self)
        box.setWindowTitle("Quiz sonucu")
        box.setText(msg)
        again = box.addButton("Yeni quiz", QMessageBox.AcceptRole)
        review = box.addButton("Yanlışları tekrar et", QMessageBox.ActionRole)
        box.addButton("Kapat", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is again:
            self.start()
        elif box.clickedButton() is review:
            self.start_review()
