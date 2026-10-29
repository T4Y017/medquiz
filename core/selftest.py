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
    from .config import PROVIDERS, Settings, data_dir
    from .db import Database
    from .ingest import chunk_doc, load_any
    from .retrieval import Embedder, Retriever

    db = Database(data_dir() / "medquiz.db")
    from . import usage as _usage
    _usage.attach(db)
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

    def clients():
        """Paketlenmiş sürümde her sağlayıcının SDK istemcisi gerçekten kurulabiliyor mu (anahtar gerekmez)."""
        from .llm import LLMClient
        out = {}
        for p in ("anthropic", "openai", "gemini"):
            st = Settings()
            setattr(st, f"{p}_api_key", "test-key")
            st.provider = p
            c = LLMClient(st, p)
            out[p] = type(getattr(c, "_client", None) or c).__name__
        return out

    _step(results, "Yapay zekâ istemcileri (Claude/OpenAI/Gemini) kurulumu", clients)

    def fill_logic():
        from . import normalize as nz
        sent, bl = nz.blanks({"question": "DKA'da ilk tedavi [1] ve ardından ____ infüzyonudur.",
                              "blanks": ["izotonik sıvı", {"answer": "insülin", "alternatives": ["insulin"]}]})
        oks = [nz.blank_ok("Izotonik sivi", bl[0]), nz.blank_ok("insulın", bl[1]), not nz.blank_ok("potasyum", bl[1])]
        if not all(oks):
            raise RuntimeError(f"Boşluk kontrolü hatalı: {oks}")
        return {"sentence": sent, "checks": oks}

    _step(results, "Boşluk doldurma kontrolü", fill_logic)

    def account_sync():
        """İki 'bilgisayar' arasında şifreli hesap eşitlemesi (ağ gerekmez)."""
        import tempfile
        from .db import Database
        from .sync import Account, SyncEngine
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Database(Path(tmp) / "a.db"), Database(Path(tmp) / "b.db")
            sa, sb = Settings(), Settings()
            sa.gemini_api_key, sa.provider, sa.user_name = "test-key", "gemini", "Test"
            a.add_vocab("mucosa", "mukoza", profile="Test")
            qid = a.save_question("mcq", "T", "Orta", {"type": "mcq", "question": "q"}, [], "Test")
            a.record_answer(qid, "x", 1.0)
            cloud = Path(tmp) / "cloud"
            cloud.mkdir()
            SyncEngine(a, sa).sync(Account.create(str(cloud), "pw1234"), initial=False)
            st = SyncEngine(b, sb).sync(Account.open(str(cloud), "pw1234"), initial=True)
            ok = (sb.gemini_api_key == "test-key" and len(b.list_vocab("Test")) == 1
                  and b.stats("Test")["total"] == 1)
            a.conn.close()
            b.conn.close()
            if not ok:
                raise RuntimeError(f"Eşitleme eksik: {st}")
            return {"received": {k: v for k, v in st.items() if k in ("added", "settings", "devices")}}

    _step(results, "Hesap eşitleme (şifreli, iki bilgisayar)", account_sync)

    def google_ready():
        from . import gdrive
        cid, _ = gdrive.client_config()
        import cryptography  # noqa: F401  (paketlenmiş sürümde şifreleme kütüphanesi var mı)
        return {"google_sign_in": bool(cid), "client": (cid[:12] + "…") if cid else ""}

    _step(results, "Google ile giriş hazırlığı", google_ready)

    def update_check():
        from . import updater
        real = updater.__version__
        info_now = updater.check()
        # Eski bir sürümmüş gibi davranıp tam akışı (bul → indir → boyut + SHA-256 doğrula) sına; kurma
        updater.__version__ = "1.0.0"
        try:
            info = updater.check()
            if info is None:
                raise RuntimeError(f"Eski sürüm taklidinde güncelleme bulunamadı: {updater.last_check}")
            t0 = time.time()
            path = updater.download(info)
            size = path.stat().st_size
            path.unlink(missing_ok=True)
        finally:
            updater.__version__ = real
        return {"current": real, "newer_now": info_now.version if info_now else None,
                "as_1.0.0_found": info.version, "downloaded_mb": round(size / 1e6, 1),
                "sha256_verified": bool(info.sha256), "download_secs": round(time.time() - t0, 1),
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
        if prov == "gemini":
            # Kullanıcının güçlü modeldeki küçük günlük ücretsiz kotasını (ör. 20 istek) CI tüketmesin:
            # testler hafif modelle yapılır. Yerel API istemcisi her iki model için aynı koddur.
            s.gemini_model = PROVIDERS["gemini"]["backup_model"]
        from .case import CaseEngine, CaseRequest
        from .llm import LLMClient, RoutedLLM
        from .quiz import GenRequest, QuizEngine

        retr2 = Retriever(db, None)
        def factory(task="heavy", s=s):
            return RoutedLLM(s, task=task)

        qe = QuizEngine(db, retr2, factory)
        ce = CaseEngine(db, retr2, qe, factory)

        def quiz(qe=qe):
            qs = qe.generate(GenRequest(counts={"mcq": 2, "fill": 2, "match": 1}, difficulty="Orta",
                                        topic="diyabetik ketoasidoz"))
            types = [q["type"] for q in qs]
            if "fill" not in types:
                raise RuntimeError(f"Boşluk doldurma sorusu gelmedi: {types}")
            f = next(q for q in qs if q["type"] == "fill")
            return {"questions": len(qs), "types": types, "model": qs[0].get("model") if qs else "",
                    "stems": [q["question"][-70:] for q in qs if q["type"] == "mcq"],
                    "fill": f["question"][:120], "fill_answers": [b["answer"] for b in f["blanks"]]}

        def summary():
            from .summary import TEMPLATES, SummaryEngine
            se = SummaryEngine(db, retr2, factory)
            doc = db.list_documents()[0]
            from .mdfix import count_tables, normalize_markdown
            src = se.build_source(doc["id"], "6-7")       # test kitabında 2 gerçek ızgara tablosu olan sayfalar
            md = normalize_markdown("".join(se.run(src, TEMPLATES["detayli"]["prompt"], "diyabetik ketoasidoz",
                                                   want_tables=True)))
            n_tab = count_tables(md)
            if len(md) < 300:
                raise RuntimeError(f"Özet çok kısa: {md[:200]!r}")
            if n_tab < 2:
                raise RuntimeError(f"Özette tablo eksik ({n_tab}); rapor: {se.table_report}; {md[:300]!r}")
            first = md[md.index("|"):][:240] if "|" in md else ""
            return {"pdf_packet": bool(src.packets[0].pdf), "captions": [c for _, c in src.packets[0].captions],
                    "tables": n_tab, "report": se.table_report, "chars": len(md), "first_table": first,
                    "model": se.last_label}

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
            """gemini-3.8-flash: yalnızca model bilgisi (kota harcamaz) + yerel istemcinin Türkçe çıktısı."""
            from . import usage
            from .gemini_native import GeminiNative
            info = GeminiNative(key, "gemini-3.8-flash").model_info()
            c = LLMClient(s, "gemini", s.gemini_model)
            txt = c.complete("Kısa yanıt ver.", [{"role": "user", "content": "Türkçe tek cümle: şeker, ığdır, göğüs."}],
                             max_tokens=200, temperature=0)
            if any(bad in txt for bad in ("Ã", "Ä", "Å")):
                raise RuntimeError(f"Türkçe karakter bozulması: {txt!r}")
            return {"model_info": info, "turkish_ok": True, "sample": txt[:80], "usage_today": usage.today()}

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
            tr = Translator(db, lambda s=s: RoutedLLM(s, task="light"))
            t = tr.translate(q)
            turkish = any(ch in t for ch in "çğışöüÇĞİŞÖÜ")
            if not turkish:
                raise RuntimeError(f"Çeviri Türkçe görünmüyor: {t[:120]!r}")
            d = tr.define("diaphoresis", "He presented with chest pain, diaphoresis and dyspnea.")
            return {"en_question": q[:120], "english_like": round(ascii_ratio, 2), "translation": t[:120],
                    "term": d["term"], "meaning": d["meaning"], "ipa": d["ipa"], "cached": tr.cached(q) is not None}

        def review_and_short_answer(qe=qe):
            # 2 yanlış cevap kaydı oluştur → tekrar quizi: 5 test + 5 eşleştirme
            qs = qe.generate(GenRequest(qtype="mcq", count=2, difficulty="Orta", topic="diyabetik ketoasidoz"))
            for q in qs:
                qe.record(q, "x", 0.0)
            res = qe.generate_review(profile="", n_mcq=5, n_match=5)
            types = [q["type"] for q in res["questions"]]
            m = next((q for q in res["questions"] if q["type"] == "match"), None)
            # kısa ama doğru klasik cevap esnek puanlanmalı
            g = qe.grade_open({"question": "Diyabetik ketoasidozda insülin başlamadan önce neye bakılır, neden?",
                               "key_points": ["Serum potasyum düzeyi", "K < 3.3 mEq/L ise önce replasman",
                                              "İnsülin potasyumu hücre içine sokar; hipokalemi/aritmi riski"],
                               "model_answer": "İnsülin başlamadan önce serum potasyumu ölçülmelidir. K < 3.3 mEq/L ise "
                                               "insülin ertelenir ve potasyum replasmanı yapılır, çünkü insülin "
                                               "potasyumu hücre içine sokarak ciddi hipokalemi ve aritmiye yol açabilir."},
                              "Potasyum. 3.3 altıysa önce K ver, insülin K'yı hücre içine sokar.")
            if g["score"] < 75:
                raise RuntimeError(f"Kısa doğru cevaba düşük puan: {g['score']} — {g.get('feedback', '')[:150]}")
            return {"review_types": {t: types.count(t) for t in set(types)}, "topics": res["topics"][:3],
                    "match_pairs": len(m["pairs"]) if m else 0, "short_answer_score": g["score"],
                    "feedback": g.get("feedback", "")[:120]}

        # Gerçek kullanım gibi adımlar arasında kısa ara (ücretsiz katmanın dakikalık sınırı)
        steps = [(f"{prov}: Ayarlar > Test düğmesi", settings_test), (f"{prov}: quiz üretimi (test + boşluk + eşleştirme)", quiz),
                 (f"{prov}: PDF sayfalarından özet (tablolu)", summary),
                         (f"{prov}: İngilizce soru + çeviri + kelime tanımı", english_and_translate),
                         (f"{prov}: tekrar quizi (5 test + 5 eşleştirme) + kısa cevap puanı", review_and_short_answer), (f"{prov}: klasik cevap puanlama", grade),
                         (f"{prov}: vaka üretimi (paraklinik + test)", case),
                         (f"{prov}: sohbet (akış + takip sorusu)", chat), (f"{prov}: vaka sohbeti", case_chat)]
        if prov == "gemini":
            steps.insert(0, ("gemini: model bilgisi + Türkçe karakter + kullanım sayacı", gemini38_direct))
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
