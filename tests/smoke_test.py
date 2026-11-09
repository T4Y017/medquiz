"""Uçtan uca hızlı kontrol (yapay zekâ taklit edilir, ağ gerekmez). Değişiklikten sonra çalıştır:

    QT_QPA_PLATFORM=offscreen python tests/smoke_test.py

Her sayfayı açar; quiz (test/boşluk/eşleştirme/şekilli), yanlış tekrarı, özet (tablo onarımı), bilgi kartları,
sohbet, geri bildirim ve iki 'bilgisayar' arası eşitlemeyi dener. Hata varsa çıkış kodu 1.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="mq_smoke_"))
os.environ["MEDQUIZ_DATA_DIR"] = str(TMP / "data")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
(TMP / "data").mkdir()
BOOK = TMP / "test_book.pdf"
subprocess.run([sys.executable, str(ROOT / "tools" / "make_test_book.py"), str(BOOK), "40"], check=True,
               stdout=subprocess.DEVNULL)

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtGui import QDesktopServices  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

FAILS: list[str] = []
LOG: list[str] = []


def check(name, cond, info=""):
    (LOG if cond else FAILS).append(f"{'OK  ' if cond else 'FAIL'} {name} {info}".rstrip())


class Fake:
    label = "fake"
    fell_back = False
    last_finish = "stop"

    def complete_json(self, system, user, **k):
        text = user if isinstance(user, str) else " ".join(p.get("text", "") for p in user if p["type"] == "text")
        if "bilgi kartı" in system:
            return {"cards": [{"front": "Allerjik rinitte ilk basamak?", "back": "İntranazal steroid"},
                              {"front": "DKA'da pH eşiği?", "back": "< 7,30"}]}
        if "ŞEKİL" in text or "şekil" in system.lower():
            n = sum(1 for p in user if p["type"] == "image") if isinstance(user, list) else 0
            return {"questions": [{"figure": i + 1, "usable": True, "topic": "Kardiyoloji",
                                   "question": f"Görüntüdeki bulgu hangisiyle uyumludur? ({i})",
                                   "options": ["STEMI", "Perikardit", "Normal", "Hiperkalemi", "Brugada"],
                                   "answer_index": 0} for i in range(n)]}
        if "tables" in system.lower() and "TABLOLARI" in system:
            return {"tables": []}
        qs = []
        import re
        m = re.search(r"Toplam (\d+) soru yaz: (.*?)\. Başka", text)
        plan = {}
        if m:
            for n, t in re.findall(r"(\d+) adet (çoktan|boşluk|eşleştirme)", m.group(2)):
                plan[t] = int(n)
        elif "TEKRAR" in text.upper():
            plan = {"çoktan": 5, "eşleştirme": 5}
        else:
            plan = {"çoktan": 2}
        for i in range(plan.get("çoktan", 0)):
            qs.append({"type": "mcq", "topic": "DKA", "question": f"DKA sorusu {len(LOG)}-{i}?",
                       "options": ["a", "b", "c", "d", "e"], "answer_index": 0, "explanation": "çünkü"})
        for i in range(plan.get("boşluk", 0)):
            qs.append({"type": "fill", "topic": "DKA", "question": f"DKA'da ilk tedavi ____ verilmesidir ({i}).",
                       "blanks": [{"answer": "sıvı", "alternatives": ["serum"]}]})
        for i in range(plan.get("eşleştirme", 0)):
            qs.append({"type": "match", "topic": "DKA", "question": f"Eşleştir {i}",
                       "pairs": [{"left": "pH", "right": "<7,3"}, {"left": "HCO3", "right": "<18"},
                                 {"left": "Glukoz", "right": ">250"}]})
        return {"questions": qs}

    def complete(self, system, msgs, **k):
        return "**Özet tablosu: x**\n\n| a | b |\n|---|---|\n| 1 | 2 |"

    def stream(self, system, messages, **k):
        yield "## Tedavi\n**Tablo 1: Başlangıç tedavisi**\n| Basamak | Doz |\n|---|---|\n| Sıvı | 1 L |\n"
        yield "\n" + "Açıklama metni. " * 60


def main() -> int:
    for k in ("information", "warning", "critical"):
        setattr(QMessageBox, k, staticmethod(lambda *a, **kw: QMessageBox.Ok))
    QMessageBox.question = staticmethod(lambda *a, **kw: QMessageBox.Yes)
    QMessageBox.exec = lambda self: 0
    QDialog.exec = lambda self: 1
    QDesktopServices.openUrl = staticmethod(lambda u: True)
    qa = QApplication(sys.argv)
    import app as appmod
    from core.ingest import chunk_doc, load_any
    from ui import style
    from ui.context import AppContext
    ctx = AppContext()
    ctx.embedder = None
    ctx.retriever.embedder = None
    ctx.settings.gemini_api_key = "test"
    ctx.settings.provider = "gemini"
    fac = lambda *a, **k: Fake()  # noqa: E731
    ctx.engine.llm_factory = fac
    ctx.summary_engine.llm_factory = fac
    doc = load_any(str(BOOK))
    ctx.db.add_document(doc.title, doc.source, doc.kind, chunk_doc(doc))
    ctx.notify_library_changed()
    qa.setStyleSheet(style.stylesheet(11))
    w = appmod.MainWindow(ctx)
    w.resize(1180, 820)
    w.show()
    steps = []

    def step(fn, delay=500):
        steps.append((fn, delay))

    def run():
        if not steps:
            qa.quit()
            return
        fn, delay = steps.pop(0)
        try:
            fn()
        except Exception:  # noqa: BLE001
            FAILS.append("EXC " + traceback.format_exc(limit=4))
        QTimer.singleShot(delay, run)

    for name in w.PAGES:
        step(lambda n=name: (w.go(n), check(f"sayfa açılır: {n}", w.stack.currentIndex() == w.PAGES.index(n))), 150)
    qp = w.quiz
    step(lambda: (w.go("Quiz"), qp.set_counts(mcq=2, fill=1, match=1, image=1), qp.start()), 2500)
    step(lambda: check("quiz türleri", {"mcq", "fill", "match"} <= {q["type"] for q in qp.questions}
                       and any(q.get("image") for q in qp.questions), str([q["type"] for q in qp.questions])))

    def answer_all():
        for i, q in enumerate(qp.questions):
            qp.go(i)
            if q["type"] == "mcq":
                qp.answer_mcq((q["answer_index"] + 1) % 5)
            elif q["type"] == "fill":
                for e in qp.fill_edits:
                    e.setText("Serum")
                qp.answer_fill()
            elif q["type"] == "match":
                qp.reveal_match()
        check("cevaplar kaydedildi", len(qp.results) == len(qp.questions))
        check("boşluk hoşgörülü kontrol", any(r.get("oks") == [True] for r in qp.results.values()))
        check("bu quizin yanlışları", len(qp.session_wrong_ids()) >= 1)
    step(answer_all)
    step(lambda: qp.start_review(scope="session"), 2000)
    step(lambda: check("yanlış tekrarı", qp.review_mode and len(qp.questions) > 0))
    sp = w.summary
    step(lambda: (w.go("Özet"), sp.select_docs([d["id"] for d in ctx.db.list_documents()]),
                  sp.set_pages(ctx.db.list_documents()[0]["id"], "6-7"), sp.run()), 2500)
    step(lambda: check("özet kaydedildi ve tablo onarıldı", bool(ctx.db.list_summaries()) and
                       "\n\n| Basamak" in (sp.current or {}).get("markdown", "")))
    step(lambda: sp.make_cards(), 1500)
    step(lambda: check("özetten kart", len(ctx.db.list_cards(ctx.profile)) >= 2))
    step(lambda: (w.go("Kartlar"), w.cards.start_study(due_only=True, deck=None), w.cards.reveal(), w.cards.answer(True)))
    step(lambda: check("kart çalışma", ctx.db.list_cards(ctx.profile)[0]["reviews"] >= 0))
    step(lambda: (w.go("Sohbet"), w.chat.input.setPlainText("DKA tedavisi nedir?"), w.chat.send()), 1200)
    step(lambda: check("sohbet cevabı", any(m["role"] == "assistant" for m in w.chat.history)))
    step(lambda: (w.go("Geri bildirim"), w.feedback.text.setPlainText("Deneme geri bildirimi"),
                  w.feedback.save(send=False)))
    step(lambda: check("geri bildirim kaydı", len(ctx.db.list_feedback()) == 1))
    step(lambda: (w.go("İstatistik"), check("istatistik", w.stats.t_topic.rowCount() >= 1)))

    def sync_test():
        from core.config import Settings
        from core.db import Database
        from core.sync import Account, SyncEngine
        b, sb = Database(TMP / "b.db"), Settings()
        cloud = TMP / "cloud"
        cloud.mkdir()
        SyncEngine(ctx.db, ctx.settings).sync(Account.create(str(cloud), "pw1234"))
        SyncEngine(b, sb).sync(Account.open(str(cloud), "pw1234"), initial=True)
        check("eşitleme", sb.gemini_api_key == "test" and len(b.list_documents()) == 1
              and len(b.list_cards(ctx.profile)) >= 2)
    step(sync_test)
    QTimer.singleShot(300, run)
    qa.exec()
    print("\n".join(LOG))
    if FAILS:
        print("\n".join(FAILS))
        print(f"\nSONUÇ: {len(FAILS)} HATA")
        return 1
    print(f"\nSONUÇ: hepsi tamam ({len(LOG)} kontrol)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
