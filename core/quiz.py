"""Quiz üretimi, klasik cevap puanlama ve kaynaklı soru-cevap (RAG sohbet)."""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable, Iterator

from .db import Database
import json
import logging

from . import normalize as nz
from .llm import LLMClient, LLMError
from .retrieval import Retriever

log = logging.getLogger(__name__)

DIFFICULTIES = {
    "Kolay": "Temel tanım, sınıflama ve doğrudan bilgi soruları. Tek adımlı hatırlama.",
    "Orta": "Mekanizma, patofizyoloji, ayırıcı tanı ve ilişkilendirme gerektiren sorular.",
    "Zor": "TUS/USMLE düzeyi: çok adımlı akıl yürütme, istisnalar, ince ayrımlar, tuzak çeldiriciler.",
    "Klinik vaka": "Hasta öyküsü, fizik muayene ve laboratuvar bulgularıyla başlayan vaka temelli sorular; "
                   "tanı, sonraki en uygun adım veya tedavi sorulur.",
}

QTYPES = {"mcq": "Çoktan seçmeli", "open": "Klasik (uzun cevaplı)", "mixed": "Karışık"}

BATCH = 5

SYSTEM_QUIZ = """Sen tıp eğitimi için deneyimli bir soru yazarı ve öğretim üyesisin.
Görevin, sana verilen KAYNAK METİNLERE dayanarak yüksek kaliteli Türkçe sınav soruları hazırlamak.

Kurallar:
- Soruların doğru cevabı mutlaka kaynak metinlerde dayanağı olan bilgiye dayanmalı. Kaynakta olmayan
  bilgiyi doğru cevap yapma. Çeldiricilerde genel tıp bilgisini kullanabilirsin ama hepsi açıkça yanlış olmalı.
- Çoktan seçmeli sorular TUS formatında 5 şıklı olmalı, tek bir kesin doğru cevap olmalı.
  Çeldiriciler makul, aynı kategoriden ve benzer uzunlukta olmalı. "Hepsi/Hiçbiri" şıkkı kullanma.
- Klasik sorular düşünmeyi gerektirmeli (açıklayınız, karşılaştırınız, yaklaşımı tartışınız...).
  Örnek cevap kapsamlı olmalı ve puanlama için 3-6 anahtar nokta içermeli.
- Açıklamalar öğretici olsun: doğru cevabın neden doğru, önemli çeldiricilerin neden yanlış olduğunu söyle.
- Tıbbi terimleri doğru kullan; gerekirse parantez içinde Latince/İngilizce karşılığını ver.
- Daha önce sorulan sorularla aynı ya da çok benzer soru yazma.
- YALNIZCA geçerli JSON döndür, başka metin yazma."""

JSON_SPEC = """Şu JSON yapısını döndür:
{
  "questions": [
    {
      "type": "mcq",
      "topic": "kısa konu başlığı",
      "question": "soru kökü",
      "options": ["şık 1", "şık 2", "şık 3", "şık 4", "şık 5"],
      "answer_index": 0,
      "explanation": "neden doğru / çeldiriciler neden yanlış",
      "source_keys": ["K1"]
    },
    {
      "type": "open",
      "topic": "kısa konu başlığı",
      "question": "klasik soru",
      "model_answer": "kapsamlı örnek cevap",
      "key_points": ["puanlanacak anahtar nokta 1", "nokta 2", "nokta 3"],
      "source_keys": ["K2"]
    }
  ]
}
Şıklarda "A)", "B)" gibi harf önekleri kullanma."""

SYSTEM_GRADE = """Sen adil ve titiz bir tıp eğitimcisisin. Öğrencinin klasik (uzun cevaplı) sınav cevabını,
verilen anahtar noktalar, örnek cevap ve kaynak metne göre değerlendir.
- Anlamca doğru olan farklı ifadeleri kabul et; yazım hatalarını puandan düşme.
- Tıbben yanlış ya da tehlikeli ifadeleri açıkça belirt.
- 0-100 arası puan ver: anahtar noktaların kapsanma oranı esas, yanlış bilgi puan düşürür.
YALNIZCA şu JSON'u döndür:
{"score": 0-100, "covered": ["kapsanan nokta"], "missing": ["eksik nokta"],
 "errors": ["yanlış ifade ve düzeltmesi"], "feedback": "kısa, yapıcı geri bildirim"}"""

SYSTEM_CHAT = """Sen tıp öğrencilerine ve sağlık çalışanlarına yardımcı olan bir çalışma asistanısın.
Kullanıcının sorusunu ÖNCELİKLE verilen kaynak metinlere dayanarak Türkçe yanıtla.
- Kaynaklardan aldığın bilgiyi [K1], [K2] gibi etiketlerle belirt.
- Kaynaklarda cevap yoksa bunu açıkça söyle; genel tıp bilgisi eklersen "(kaynak dışı genel bilgi)" diye işaretle.
- Kısa ve düzenli yaz; gerekirse madde işaretleri kullan.
- Bu bir eğitim aracıdır: kişisel teşhis/tedavi isteyenlere bir hekime başvurmalarını hatırlat."""


def format_context(chunks: list[dict]) -> tuple[str, dict[str, dict]]:
    """Parçaları [K1]... etiketleriyle biçimlendirir; etiket → parça sözlüğü döner."""
    keymap: dict[str, dict] = {}
    parts = []
    for i, c in enumerate(chunks, start=1):
        key = f"K{i}"
        keymap[key] = c
        loc = f", {c['location']}" if c.get("location") else ""
        parts.append(f"[{key}] (Kaynak: {c['title']}{loc})\n{c['text']}")
    return "\n\n---\n\n".join(parts), keymap


def source_label(c: dict) -> str:
    loc = f" — {c['location']}" if c.get("location") else ""
    return f"{c['title']}{loc}"


@dataclass
class GenRequest:
    qtype: str = "mixed"
    count: int = 5
    difficulty: str = "Orta"
    topic: str = ""
    doc_ids: list[int] | None = None
    profile: str = ""


class QuizEngine:
    def __init__(self, db: Database, retriever: Retriever, llm_factory: Callable[[], LLMClient]):
        self.db = db
        self.retriever = retriever
        self.llm_factory = llm_factory
        self.rng = random.Random()

    # ---------------------------------------------------------- generation
    def generate(self, req: GenRequest, on_batch: Callable[[list[dict]], None] | None = None,
                 should_stop: Callable[[], bool] | None = None) -> list[dict]:
        if self.retriever.size == 0:
            raise LLMError("Kütüphane boş. Önce Kütüphane sekmesinden PDF, Word, metin veya web sayfası ekleyin.")
        llm = self.llm_factory()
        out: list[dict] = []
        remaining = max(1, req.count)
        recent = self.db.recent_question_texts(40)
        attempts = 0
        empty_streak = 0
        last_problem = ""
        while remaining > 0 and attempts < req.count + 3:
            attempts += 1
            if should_stop and should_stop():
                break
            n = min(BATCH, remaining)
            ctx = self.retriever.pick_context(req.topic, n_chunks=4 if n <= 3 else 6,
                                              doc_ids=req.doc_ids, rng=self.rng)
            if not ctx:
                raise LLMError(f"'{req.topic}' konusunda kütüphanede ilgili içerik bulunamadı. "
                               "Farklı bir konu yazın ya da konu alanını boş bırakın.")
            batch, problem = self._generate_batch(llm, ctx, n, req, recent + [q["question"] for q in out])
            if not batch:
                empty_streak += 1
                last_problem = problem
                if empty_streak >= 2:
                    break
                continue
            empty_streak = 0
            for q in batch:
                q["model"] = getattr(llm, "label", "")
                q["fell_back"] = bool(getattr(llm, "fell_back", False))
                q["id"] = self.db.save_question(q["type"], q.get("topic", req.topic), req.difficulty, q,
                                                q["chunk_ids"], req.profile)
            self.retriever.mark_used([c["id"] for c in ctx])
            out.extend(batch)
            remaining -= len(batch)
            if on_batch:
                on_batch(batch)
        if not out and not (should_stop and should_stop()):
            raise LLMError(
                "Yapay zekâ yanıt verdi ama kullanılabilir soru çıkmadı. "
                f"{last_problem} Tekrar deneyin; sorun sürerse Ayarlar'dan başka bir model seçin "
                "veya konu alanına daha belirgin bir konu yazın.")
        return out

    def _type_instruction(self, qtype: str, n: int) -> str:
        if qtype == "mcq":
            return f"Tam olarak {n} adet çoktan seçmeli (type=\"mcq\") soru yaz."
        if qtype == "open":
            return f"Tam olarak {n} adet klasik uzun cevaplı (type=\"open\") soru yaz."
        n_open = max(1, round(n * 0.4)) if n > 1 else self.rng.choice([0, 1])
        return (f"Toplam {n} soru yaz: {n - n_open} adet çoktan seçmeli (type=\"mcq\") ve "
                f"{n_open} adet klasik uzun cevaplı (type=\"open\").")

    def _generate_batch(self, llm: LLMClient, ctx: list[dict], n: int, req: GenRequest,
                        avoid: list[str]) -> list[dict]:
        context, keymap = format_context(ctx)
        avoid_txt = "\n".join(f"- {q[:160]}" for q in avoid[-25:]) or "(yok)"
        topic_txt = f"Odak konu: {req.topic}\n" if req.topic.strip() else "Konu: kaynak metinlerdeki en önemli, sınavda çıkabilecek noktalar.\n"
        user = (
            f"KAYNAK METİNLER:\n\n{context}\n\n=====\n\n"
            f"{topic_txt}"
            f"Zorluk: {req.difficulty} — {DIFFICULTIES.get(req.difficulty, '')}\n"
            f"{self._type_instruction(req.qtype, n)}\n\n"
            f"Daha önce sorulan sorular (bunları tekrarlama):\n{avoid_txt}\n\n{JSON_SPEC}"
        )
        data = llm.complete_json(SYSTEM_QUIZ, user, max_tokens=8000, temperature=0.8)
        raw = nz.items(data, "questions", "items", "sorular", "quiz")
        questions = []
        for q in raw:
            norm = self._normalize(q, keymap, ctx) if isinstance(q, dict) else None
            if norm:
                questions.append(norm)
        problem = ""
        if not questions:
            problem = ("(Yanıtta soru listesi bulunamadı.)" if not raw else
                       f"({len(raw)} sorunun hiçbiri beklenen biçimde değildi.)")
            log.warning("Kullanılamayan quiz yanıtı (%s): %s", getattr(llm, "label", "?"),
                        json.dumps(data, ensure_ascii=False)[:3000])
        return questions[:n], problem

    def _normalize(self, q: dict, keymap: dict[str, dict], ctx: list[dict]) -> dict | None:
        text = nz.text(nz.first(q, "question", "stem", "prompt", "soru", "text"))
        if not text:
            return None
        raw_opts = nz.first(q, "options", "choices", "secenekler", "şıklar", "siklar")
        qtype = nz.norm_type(q.get("type"), default="mcq" if raw_opts else "open")
        if qtype == "multi":
            qtype = "mcq"
        keys_raw = nz.first(q, "source_keys", "sources", "source", "kaynak", default=[])
        if isinstance(keys_raw, str):
            keys_raw = [keys_raw]
        keys = [str(k).strip("[] ") for k in keys_raw if isinstance(k, (str, int))]
        src_chunks = [keymap[k] for k in keys if k in keymap] or ctx[:1]
        base = {
            "type": qtype,
            "topic": str(q.get("topic", "")).strip(),
            "question": text,
            "chunk_ids": [c["id"] for c in src_chunks],
            "sources": [source_label(c) for c in src_chunks],
            "source_texts": [c["text"] for c in src_chunks],
        }
        if qtype == "mcq":
            opts, flagged = nz.options_with_flags(raw_opts)
            ans = nz.mcq_answer(q, opts, flagged)
            if len(opts) < 3 or ans is None:
                return None
            # LLM'lerin doğru şıkkı belli konumlara koyma eğilimini kır: şıkları karıştır
            correct = opts[ans]
            order = list(range(len(opts)))
            self.rng.shuffle(order)
            opts = [opts[i] for i in order]
            base.update(options=opts, answer_index=opts.index(correct),
                        explanation=nz.text(nz.first(q, "explanation", "rationale", "aciklama", "açıklama")))
        else:
            kp = nz.str_list(nz.first(q, "key_points", "keyPoints", "points", "rubric", "anahtar_noktalar"))
            model = nz.text(nz.first(q, "model_answer", "modelAnswer", "answer", "sample_answer", "ornek_cevap"))
            if not model and kp:
                model = "\n".join(f"• {k}" for k in kp)
            base.update(model_answer=model, key_points=kp)
            if not base["model_answer"]:
                return None
        return base

    # ------------------------------------------------------------- grading
    def grade_open(self, q: dict, answer: str) -> dict:
        if not answer.strip():
            return {"score": 0, "covered": [], "missing": q.get("key_points", []), "errors": [],
                    "feedback": "Cevap boş bırakıldı."}
        llm = self.llm_factory()
        kp = "\n".join(f"- {k}" for k in q.get("key_points", []))
        src = "\n\n".join(q.get("source_texts", []))[:6000]
        user = (f"SORU:\n{q['question']}\n\nANAHTAR NOKTALAR:\n{kp}\n\nÖRNEK CEVAP:\n{q.get('model_answer', '')}\n\n"
                f"KAYNAK METİN:\n{src}\n\nÖĞRENCİ CEVABI:\n{answer}")
        res = llm.complete_json(SYSTEM_GRADE, user, max_tokens=2000, temperature=0.1)
        try:
            res["score"] = max(0.0, min(100.0, float(res.get("score", 0))))
        except (TypeError, ValueError):
            res["score"] = 0.0
        for k in ("covered", "missing", "errors"):
            if not isinstance(res.get(k), list):
                res[k] = []
        res.setdefault("feedback", "")
        return res

    def record(self, q: dict, user_answer: str, score01: float) -> None:
        if q.get("id"):
            self.db.record_answer(q["id"], user_answer, score01)

    # ---------------------------------------------------------------- chat
    def chat_stream(self, question: str, history: list[dict], k: int = 6,
                    doc_ids: list[int] | None = None):
        """(metin akışı, kullanılan kaynaklar, istemci) döner; istemcinin `label`'ı cevabı veren modeldir."""
        # Takip sorularında aramayı önceki kullanıcı mesajıyla zenginleştir
        prev_user = [m["content"] for m in history if m["role"] == "user"][-1:]
        query = " ".join(prev_user + [question])
        hits = self.retriever.search(query, k=k, doc_ids=doc_ids)
        context, _ = format_context(hits) if hits else ("(Kütüphanede ilgili kaynak bulunamadı.)", {})
        msgs = [m for m in history[-8:] if m["role"] in ("user", "assistant")]
        msgs.append({"role": "user", "content": f"KAYNAK METİNLER:\n\n{context}\n\n=====\n\nSORU: {question}"})
        llm = self.llm_factory()
        return llm.stream(SYSTEM_CHAT, msgs), hits, llm
