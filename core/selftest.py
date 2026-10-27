"""Paketlenmiş uygulamanın kendi kendini sınaması (CI'da gerçek Windows makinesinde çalışır).

Kullanım:  MedQuiz.exe --selftest --pdf kitap.pdf --out sonuc.json
Ortamda GEMINI_API_KEY / ANTHROPIC_API_KEY varsa gerçek yapay zekâ ile quiz ve vaka üretimi de denenir.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import traceback
from pathlib import Path


def _step(results: list, name: str, fn, required: bool = True):
    t0 = time.time()
    try:
        info = fn()
        results.append({"step": name, "ok": True, "secs": round(time.time() - t0, 2), "info": info,
                        "required": required})
        return info
    except Exception as e:  # noqa: BLE001
        results.append({"step": name, "ok": False, "secs": round(time.time() - t0, 2),
                        "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1500:],
                        "required": required})
        return None


def run(pdf: str | None, out: str) -> int:
    os.environ["MEDQUIZ_DATA_DIR"] = tempfile.mkdtemp(prefix="medquiz_selftest_")
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(Path(os.environ["MEDQUIZ_DATA_DIR"]) / "medquiz.log",
                                                      encoding="utf-8")], force=True)
    results: list[dict] = []
    from .config import Settings, data_dir
    from .db import Database
    from .ingest import chunk_doc, load_any
    from .retrieval import Embedder, Retriever

    db = Database(data_dir() / "medquiz.db")
    state: dict = {}

    def ingest():
        import importlib.util
        doc = load_any(pdf)
        chunks = chunk_doc(doc)
        db.add_document(doc.title, doc.source, doc.kind, chunks, topic="Test")
        state["n_chunks"] = len(chunks)
        return {"pages": len(doc.sections), "chunks": len(chunks), "warnings": doc.warnings,
                "pdfium": importlib.util.find_spec("pypdfium2") is not None}

    if pdf:
        _step(results, "PDF okuma + bölümleme", ingest)

    emb = Embedder(Settings().embedding_model, cache_dir=str(data_dir() / "models"))
    retr = Retriever(db, emb)
    _step(results, "Anahtar kelime dizini", lambda: {"chunks": retr.size})
    _step(results, "Arama", lambda: {"hits": len(retr.search("diyabetik ketoasidoz tedavisi", k=5))})

    def model_load():
        ok = emb.available()
        if not ok:
            raise RuntimeError(emb.error or "yüklenemedi")
        return {"model": emb.model_name}

    _step(results, "Anlamsal model indirme/yükleme", model_load, required=False)

    def embed_rate():
        texts = [c["text"] for c in db.all_chunks()[:400]]
        t0 = time.time()
        v = emb.embed(texts)
        if v is None:
            raise RuntimeError("embedding başarısız")
        secs = time.time() - t0
        rate = len(texts) / max(secs, 1e-6)
        n = state.get("n_chunks", len(texts))
        return {"chunks_per_sec": round(rate, 1), "dim": int(v.shape[1]),
                "estimated_full_book_secs": round(n / rate, 1)}

    if emb.ready:
        _step(results, "Embedding hızı", embed_rate, required=False)

    def search_while_embedding():
        # Arka planda vektörler hesaplanırken arama beklememeli
        import threading
        retr.invalidate()
        th = threading.Thread(target=retr.embed_missing, daemon=True)
        th.start()
        time.sleep(0.5)
        t0 = time.time()
        hits = retr.search("akut pankreatit", k=5)
        wait = time.time() - t0
        th.join(timeout=1)
        if wait > 5:
            raise RuntimeError(f"arama {wait:.1f} sn bekledi")
        return {"search_secs_during_embedding": round(wait, 3), "hits": len(hits),
                "embedded": list(retr.embed_progress)}

    if emb.ready and pdf:
        _step(results, "Vektörleme sürerken arama", search_while_embedding, required=False)

    def gui():
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        from app import MainWindow  # type: ignore
        from ui.context import AppContext
        from ui.case_page import write_case_pdf

        ctx = AppContext()
        win = MainWindow(ctx)
        win.show()
        app.processEvents()
        pdf_out = Path(data_dir()) / "vaka.pdf"
        case = {"public_title": "Test", "patient": "x", "stages": [{"title": "A", "narrative": "n",
                "findings": [{"category": "Lab", "test": "Na", "result": "128", "unit": "mEq/L", "ref": "135-145"}],
                "task": {"type": "open", "prompt": "p", "expected_points": ["e"], "model_answer": "m"},
                "teaching": "t", "sources": []}], "final_test": [], "final_diagnosis": "d"}
        ok = write_case_pdf(case, {"phase": "done", "finished": True}, str(pdf_out))
        win.close()
        return {"window": True, "pdf_export": ok}

    _step(results, "Arayüz + PDF dışa aktarma", gui)

    def update_check():
        from . import updater
        from .version import __version__
        info = updater.check()
        return {"current": __version__, "newer_available": info.version if info else None,
                "can_self_install": updater.can_self_install()}

    _step(results, "Güncelleme kontrolü (GitHub Releases)", update_check, required=False)

    # --- gerçek yapay zekâ (anahtar varsa)
    for prov, env in (("gemini", "GEMINI_API_KEY"), ("anthropic", "ANTHROPIC_API_KEY")):
        key = os.environ.get(env, "").strip()
        if not key or not pdf:
            continue
        s = Settings()
        setattr(s, f"{prov}_api_key", key)
        s.provider = prov
        s.fallback_enabled = True    # yalnızca bu sağlayıcının anahtarı var: birincil + hafif yedek modeli
        from .case import CaseEngine, CaseRequest
        from .llm import RoutedLLM
        from .quiz import GenRequest, QuizEngine

        retr2 = Retriever(db, None)
        qe = QuizEngine(db, retr2, lambda s=s: RoutedLLM(s))
        ce = CaseEngine(db, retr2, qe, lambda s=s: RoutedLLM(s))

        def quiz(qe=qe):
            qs = qe.generate(GenRequest(qtype="mixed", count=3, difficulty="Orta", topic="diyabetik ketoasidoz"))
            return {"questions": len(qs), "types": [q["type"] for q in qs], "model": qs[0].get("model") if qs else "",
                    "sample": qs[0]["question"][:160] if qs else ""}

        def case(ce=ce):
            r = ce.generate(CaseRequest(topic="diyabetik ketoasidoz", level="Öğrenci", paraclinic=True, test_count=3))
            c = r["payload"]
            return {"stages": len(c["stages"]), "findings": sum(len(st.get("findings", [])) for st in c["stages"]),
                    "final_test": len(c["final_test"]), "title": c["public_title"][:100], "model": c.get("model")}

        def grade(qe=qe):
            g = qe.grade_open({"question": "DKA tedavisini açıklayınız.", "key_points": ["sıvı", "insülin", "potasyum"],
                               "model_answer": "İzotonik sıvı, insülin infüzyonu, potasyum takibi."},
                              "Önce serum fizyolojik verilir, sonra insülin başlanır.")
            return {"score": g["score"], "missing": g.get("missing", [])[:3]}



        def chat(qe=qe):
            # Sohbet: akış (stream) + çok turlu takip sorusu
            stream, hits, llm = qe.chat_stream("Diyabetik ketoasidozda potasyum neden önemli?", [])
            a1 = "".join(stream)
            if len(a1.strip()) < 20:
                raise RuntimeError(f"Sohbet yanıtı boş/kısa: {a1!r}")
            hist = [{"role": "user", "content": "Diyabetik ketoasidozda potasyum neden önemli?"},
                    {"role": "assistant", "content": a1}]
            stream2, _, llm2 = qe.chat_stream("Peki insüline ne zaman başlanır?", hist)
            a2 = "".join(stream2)
            if len(a2.strip()) < 20:
                raise RuntimeError(f"Takip yanıtı boş/kısa: {a2!r}")
            return {"chars": [len(a1), len(a2)], "cites_sources": "[K" in a1, "sources": len(hits),
                    "model": llm.label, "sample": a1[:160]}

        def case_chat(ce=ce):
            c = ce.generate(CaseRequest(topic="akut pankreatit", level="Öğrenci", paraclinic=True,
                                        test_count=2))["payload"]
            ans = "".join(ce.ask_stream(c, 1, False, "Bu hastada ilk ne düşünmeliyim?", []))
            if len(ans.strip()) < 20:
                raise RuntimeError(f"Vaka sohbeti boş: {ans!r}")
            return {"chars": len(ans), "sample": ans[:160]}

        def gemini38_direct():
            """gemini-3.8-flash'ı YEDEKSİZ dener: model bilgisi, küçük JSON, quiz boyutunda istek, akış."""
            from .gemini_native import GeminiAPIError, GeminiNative
            from .llm import LLMClient
            out: dict = {}
            m38 = "gemini-3.8-flash"
            try:
                out["model_info"] = GeminiNative(key, m38).model_info()
            except GeminiAPIError as e:
                out["model_info_error"] = f"{e.status} {e.quota_id} {e.message[:150]}"
            for name, mt, prompt in (
                    ("small_json", 600, 'Tek kısa soru: {"question": "...", "options": ["a","b","c","d"], "answer_index": 0}'),
                    ("quiz_size", 8000, "Diyabetik ketoasidoz hakkında 5 adet 5 şıklı TUS sorusu yaz. JSON: "
                                        '{"questions":[{"question":"...","options":["..."],"answer_index":0,'
                                        '"explanation":"..."}]}')):
                t0 = time.time()
                c = LLMClient(s, "gemini", m38)
                try:
                    d = c.complete_json("Sen tıp eğitimcisisin. Yalnızca JSON döndür.", prompt, max_tokens=mt,
                                        temperature=0.5)
                    out[name] = {"ok": True, "secs": round(time.time() - t0, 1), "usage": c.last_usage,
                                 "items": len(d.get("questions", [])) if isinstance(d, dict) else None}
                except Exception as e:  # noqa: BLE001
                    cause = getattr(e, "__cause__", None)
                    out[name] = {"ok": False, "secs": round(time.time() - t0, 1), "error": str(e)[:200],
                                 "status": getattr(cause, "status", None), "quota": getattr(cause, "quota_id", ""),
                                 "retry_after": getattr(cause, "retry_after", None)}
                time.sleep(5)
            if not any(isinstance(v, dict) and v.get("ok") for v in out.values()):
                raise RuntimeError(json.dumps(out, ensure_ascii=False)[:800])
            return out

        def settings_test():
            from .llm import test_provider
            r = test_provider(s, prov)
            return {"label": r["label"], "secs": round(r["secs"], 1), "notes": r["notes"], "q": r["question"][:100]}

        def english_and_translate(qe=qe):
            from .translate import Translator
            qs = qe.generate(GenRequest(qtype="open", count=1, difficulty="Orta", topic="diyabetik ketoasidoz",
                                        language="en"))
            q = qs[0]["question"]
            ascii_ratio = sum(c.isascii() for c in q) / max(1, len(q))
            tr = Translator(db, lambda s=s: RoutedLLM(s))
            t = tr.translate(q)
            turkish = any(ch in t for ch in "çğışöüÇĞİŞÖÜ")
            if not turkish:
                raise RuntimeError(f"Çeviri Türkçe görünmüyor: {t[:120]!r}")
            d = tr.define("diaphoresis", "He presented with chest pain, diaphoresis and dyspnea.")
            return {"en_question": q[:120], "english_like": round(ascii_ratio, 2), "translation": t[:120],
                    "term": d["term"], "meaning": d["meaning"], "ipa": d["ipa"], "cached": tr.cached(q) is not None}

        # Gerçek kullanım gibi adımlar arasında kısa ara (ücretsiz katmanın dakikalık sınırı)
        steps = [(f"{prov}: Ayarlar > Test düğmesi", settings_test), (f"{prov}: quiz üretimi", quiz),
                         (f"{prov}: İngilizce soru + çeviri + kelime tanımı", english_and_translate), (f"{prov}: klasik cevap puanlama", grade),
                         (f"{prov}: vaka üretimi (paraklinik + test)", case),
                         (f"{prov}: sohbet (akış + takip sorusu)", chat), (f"{prov}: vaka sohbeti", case_chat)]
        if prov == "gemini":
            steps.insert(0, ("gemini: 3.8-flash doğrudan (yedeksiz) tanılama", gemini38_direct))
        for name, fn in steps:
            _step(results, name, fn, required=False)
            time.sleep(10)

    log_tail = ""
    lp = Path(data_dir()) / "medquiz.log"
    if lp.exists():
        log_tail = "\n".join(lp.read_text(encoding="utf-8", errors="replace").splitlines()[-40:])
    ok = all(r["ok"] for r in results if r["required"])
    Path(out).write_text(json.dumps({"ok": ok, "results": results, "log_tail": log_tail}, ensure_ascii=False,
                                    indent=2), encoding="utf-8")
    return 0 if ok else 1
