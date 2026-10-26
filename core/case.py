"""Vaka çalışması: kaynaklara dayalı, adım adım ilerleyen interaktif klinik vakalar."""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable, Iterator

from .db import Database
from .llm import LLMClient, LLMError
from .quiz import QuizEngine, format_context, source_label
from .retrieval import Retriever

LEVELS = {
    "Öğrenci": "Dönem 4-6 tıp öğrencisi: temel yaklaşım, klasik bulgular, ilk basamak tetkik ve tedavi.",
    "Asistan / pratisyen": "Asistan hekim/pratisyen: kılavuz temelli yönetim, doz ve zamanlama, komplikasyonlar.",
    "Uzman": "Uzman düzeyi: atipik prezentasyon, ince ayırıcı tanı, tartışmalı yönetim noktaları, istisnalar.",
}

SYSTEM_CASE = """Sen deneyimli bir klinisyen ve tıp eğitimcisisin. Verilen KAYNAK METİNLERE dayanarak,
öğrencinin adım adım çözeceği gerçekçi ve öğretici bir klinik vaka hazırlıyorsun (Türkçe).

İlkeler:
- Vaka aşama aşama ilerler. Her aşamanın "narrative" kısmı yalnızca o ana kadar bilinen bilgiyi içerir;
  sonraki aşamaların cevabını (tanı, tedavi vb.) ÖNCEDEN açık etme. Başlık da tanıyı ele vermemeli.
- Her aşamada öğrenciye bir görev ver (ayırıcı tanı, istenecek tetkikler, bulgu yorumu, tanı, tedavi, takip).
- Doğru cevaplar ve öğretim noktaları kaynak metinlere dayanmalı ve [K1] gibi source_keys ile işaretlenmeli.
- Vital bulgular, laboratuvar değerleri gibi vakayı gerçekçi kılan kurgusal ayrıntılar ekleyebilirsin;
  bunlar tutarlı ve klinik olarak inandırıcı olmalı (birimleriyle yaz).
- Kaynakta olmayan ama vaka için gerekli tıbbi bilgi kullandıysan o aşamada "general_knowledge": true yap.
- Vakadaki hasta tamamen kurgusaldır.
- "teaching" kısmı öğretici olsun: neden-sonuç, patofizyoloji, sık yapılan hatalar.
- YALNIZCA geçerli JSON döndür."""

CASE_SPEC = """Şu JSON yapısını döndür:
{
  "public_title": "tanıyı ele vermeyen başlık (örn. 'Nefes darlığıyla gelen 67 yaşında kadın')",
  "patient": "yaş, cinsiyet, başvuru yeri — tek cümle",
  "stages": [
    {
      "title": "Başvuru ve öykü",
      "narrative": "bu aşamada öğrenilenler (öykü, şikayet, ilk izlenim...)",
      "task": {
        "type": "open",
        "prompt": "öğrenciye soru/görev",
        "expected_points": ["beklenen nokta 1", "nokta 2", "nokta 3"],
        "model_answer": "ideal cevap"
      },
      "teaching": "cevaptan sonra gösterilecek öğretim notu",
      "source_keys": ["K1"],
      "general_knowledge": false
    },
    {
      "title": "Tetkikler",
      "narrative": "...",
      "task": {
        "type": "multi",
        "prompt": "Hangi tetkikleri istersiniz? (birden fazla seçilebilir)",
        "options": ["...", "...", "...", "...", "...", "..."],
        "correct_indices": [0, 2, 3]
      },
      "teaching": "...", "source_keys": ["K2"], "general_knowledge": false
    },
    {
      "title": "Tanı",
      "narrative": "tetkik sonuçları ...",
      "task": {
        "type": "mcq",
        "prompt": "En olası tanı nedir?",
        "options": ["...", "...", "...", "...", "..."],
        "answer_index": 0
      },
      "teaching": "...", "source_keys": ["K1", "K3"], "general_knowledge": false
    }
  ],
  "final_diagnosis": "kesin tanı",
  "differential": [{"diagnosis": "ayırıcı tanı", "for": "lehine bulgular", "against": "aleyhine bulgular"}],
  "discussion": "patofizyoloji, tanı ve tedavi yaklaşımını bağlayan kapsamlı tartışma (birkaç paragraf)",
  "teaching_points": ["akılda kalması gereken nokta", "..."],
  "pitfalls": ["sık yapılan hata / kaçırılan nokta", "..."],
  "source_keys": ["K1", "K2"]
}
Kurallar: 5-7 aşama olsun; görev türlerini karıştır (open, mcq, multi). mcq 4-5 şıklı ve tek doğru;
multi 5-8 seçenekli, en az 2 doğru. Şıklarda harf öneki kullanma."""

SYSTEM_CASE_CHAT = """Sen vaka tartışması yürüten bir klinik eğitimcisin. Öğrenci şu an çözmekte olduğu vaka hakkında
soru soruyor. Yalnızca ŞU ANA KADAR AÇILMIŞ aşamaları biliyormuş gibi davran: henüz açılmamış aşamalardaki tanıyı veya
tedaviyi doğrudan söyleme; bunun yerine yönlendirici ipuçları ver (Sokratik yaklaşım). Vaka tamamlandıysa her şeyi
açıkça tartışabilirsin. Kaynak metinlerden aldığın bilgiyi [K1] gibi etiketlerle belirt, kaynak dışı bilgiyi
"(genel bilgi)" diye işaretle. Kısa, net ve öğretici yaz."""


@dataclass
class CaseRequest:
    topic: str = ""
    own_case: str = ""
    level: str = "Öğrenci"
    doc_ids: list[int] | None = None
    profile: str = ""


class CaseEngine:
    def __init__(self, db: Database, retriever: Retriever, quiz: QuizEngine,
                 llm_factory: Callable[[], LLMClient]):
        self.db = db
        self.retriever = retriever
        self.quiz = quiz
        self.llm_factory = llm_factory
        self.rng = random.Random()

    # ---------------------------------------------------------- generation
    def _context(self, req: CaseRequest) -> list[dict]:
        if req.own_case.strip():
            hits = self.retriever.search(req.own_case[:600], k=8, doc_ids=req.doc_ids)
            return sorted(hits, key=lambda c: (c["doc_id"], c["idx"]))
        return self.retriever.pick_context(req.topic, n_chunks=8, doc_ids=req.doc_ids, rng=self.rng)

    def generate(self, req: CaseRequest) -> dict:
        """Vakayı üretir, doğrular, veritabanına kaydeder ve {'id', 'payload'} döner."""
        if self.retriever.size == 0 and not req.own_case.strip():
            raise LLMError("Kütüphane boş. Önce Kütüphane sekmesinden kaynak ekleyin ya da kendi vaka metninizi yazın.")
        ctx = self._context(req)
        if not ctx and not req.own_case.strip():
            raise LLMError(f"'{req.topic}' konusunda kütüphanede ilgili içerik bulunamadı.")
        context, keymap = format_context(ctx) if ctx else ("(Kütüphanede ilgili kaynak bulunamadı.)", {})

        if req.own_case.strip():
            focus = ("Aşağıdaki GERÇEK VAKA ÖZETİNİ temel al ve onu öğretici, aşamalı bir vakaya dönüştür. "
                     "Özette olmayan ayrıntıları tutarlı biçimde tamamlayabilirsin; kimlik bilgisi ekleme.\n\n"
                     f"VAKA ÖZETİ:\n{req.own_case.strip()}\n")
        elif req.topic.strip():
            focus = f"Vaka konusu: {req.topic.strip()}\n"
        else:
            focus = "Vaka konusu: kaynak metinlerdeki en öğretici klinik tabloyu seç.\n"

        user = (f"KAYNAK METİNLER:\n\n{context}\n\n=====\n\n{focus}"
                f"Hedef seviye: {req.level} — {LEVELS.get(req.level, '')}\n\n{CASE_SPEC}")
        llm = self.llm_factory()
        data = llm.complete_json(SYSTEM_CASE, user, max_tokens=12000, temperature=0.7)
        case = self._normalize(data, keymap, ctx)
        if not case["stages"]:
            raise LLMError("Model geçerli bir vaka üretemedi. Lütfen tekrar deneyin.")
        case["level"] = req.level
        case["model"] = getattr(llm, "label", "")
        case["topic"] = req.topic.strip() or ("Kendi vakam" if req.own_case.strip() else "")
        title = case["public_title"]
        cid = self.db.save_case(title, case["topic"], req.level, case, req.profile)
        if ctx:
            self.retriever.mark_used([c["id"] for c in ctx])
        return {"id": cid, "payload": case}

    def _sources(self, keys, keymap) -> list[dict]:
        out = []
        for k in keys or []:
            if isinstance(k, str):
                k = k.strip("[] ")
                if k in keymap:
                    c = keymap[k]
                    out.append({"key": k, "label": source_label(c), "text": c["text"], "chunk_id": c["id"]})
        return out

    def _normalize(self, data: dict, keymap: dict, ctx: list[dict]) -> dict:
        stages = []
        for s in data.get("stages", []):
            task = s.get("task") or {}
            ttype = task.get("type")
            prompt = str(task.get("prompt", "")).strip()
            narrative = str(s.get("narrative", "")).strip()
            if not narrative or not prompt or ttype not in ("open", "mcq", "multi"):
                continue
            t: dict = {"type": ttype, "prompt": prompt}
            if ttype == "open":
                t["expected_points"] = [str(x).strip() for x in task.get("expected_points", []) if str(x).strip()]
                t["model_answer"] = str(task.get("model_answer", "")).strip()
                if not t["expected_points"] and not t["model_answer"]:
                    continue
            else:
                opts = [str(o).strip() for o in task.get("options", []) if str(o).strip()]
                if len(opts) < 3:
                    continue
                if ttype == "mcq":
                    try:
                        ans = int(task.get("answer_index", -1))
                    except (TypeError, ValueError):
                        continue
                    if not 0 <= ans < len(opts):
                        continue
                    correct = {ans}
                else:
                    try:
                        correct = {int(i) for i in task.get("correct_indices", []) if 0 <= int(i) < len(opts)}
                    except (TypeError, ValueError):
                        continue
                    if not correct:
                        continue
                # şıkları karıştır
                order = list(range(len(opts)))
                self.rng.shuffle(order)
                t["options"] = [opts[i] for i in order]
                new_correct = sorted(order.index(i) for i in correct)
                if ttype == "mcq":
                    t["answer_index"] = new_correct[0]
                else:
                    t["correct_indices"] = new_correct
            stages.append({
                "title": str(s.get("title", "")).strip() or f"Aşama {len(stages) + 1}",
                "narrative": narrative,
                "task": t,
                "teaching": str(s.get("teaching", "")).strip(),
                "sources": self._sources(s.get("source_keys"), keymap),
                "general_knowledge": bool(s.get("general_knowledge", False)),
            })
        diff = []
        for d in data.get("differential", []) or []:
            if isinstance(d, dict) and str(d.get("diagnosis", "")).strip():
                diff.append({k: str(d.get(k, "")).strip() for k in ("diagnosis", "for", "against")})
        all_keys = list(dict.fromkeys(
            [k for s in data.get("stages", []) for k in (s.get("source_keys") or []) if isinstance(k, str)]
            + [k for k in (data.get("source_keys") or []) if isinstance(k, str)]))
        return {
            "public_title": str(data.get("public_title", "")).strip() or "Klinik vaka",
            "patient": str(data.get("patient", "")).strip(),
            "stages": stages,
            "final_diagnosis": str(data.get("final_diagnosis", "")).strip(),
            "differential": diff,
            "discussion": str(data.get("discussion", "")).strip(),
            "teaching_points": [str(x).strip() for x in data.get("teaching_points", []) if str(x).strip()],
            "pitfalls": [str(x).strip() for x in data.get("pitfalls", []) if str(x).strip()],
            "sources": self._sources(all_keys, keymap),
        }

    # ------------------------------------------------------------- scoring
    @staticmethod
    def score_choice(task: dict, answer) -> float:
        """mcq: 0/1. multi: (doğru seçilen − yanlış seçilen) / doğru sayısı, 0..1 aralığında."""
        if task["type"] == "mcq":
            return 1.0 if answer == task["answer_index"] else 0.0
        chosen = set(answer or [])
        correct = set(task["correct_indices"])
        tp = len(chosen & correct)
        fp = len(chosen - correct)
        return max(0.0, min(1.0, (tp - fp) / len(correct)))

    def grade_open_stage(self, case: dict, stage_idx: int, answer: str) -> dict:
        st = case["stages"][stage_idx]
        so_far = "\n\n".join(f"{s['title']}: {s['narrative']}" for s in case["stages"][:stage_idx + 1])
        q = {
            "question": f"VAKA (şu ana kadar):\n{so_far}\n\nGÖREV: {st['task']['prompt']}",
            "key_points": st["task"].get("expected_points", []),
            "model_answer": st["task"].get("model_answer", ""),
            "source_texts": [s["text"] for s in st.get("sources", [])],
        }
        return self.quiz.grade_open(q, answer)

    # ---------------------------------------------------------------- chat
    def ask_stream(self, case: dict, revealed: int, finished: bool, question: str,
                   history: list[dict]) -> Iterator[str]:
        stages = case["stages"][:revealed]
        lines = [f"Hasta: {case.get('patient', '')}"]
        for s in stages:
            lines.append(f"## {s['title']}\n{s['narrative']}\nGörev: {s['task']['prompt']}")
        if finished:
            lines.append(f"## Kesin tanı\n{case.get('final_diagnosis', '')}\n\n## Tartışma\n{case.get('discussion', '')}")
        else:
            lines.append(f"(Vaka devam ediyor: {len(case['stages']) - revealed} aşama henüz açılmadı.)")
        hits = self.retriever.search(question + " " + case.get("final_diagnosis", "") if finished else question, k=5)
        ctx, _ = format_context(hits) if hits else ("(ilgili kaynak yok)", {})
        msgs = [m for m in history[-6:] if m["role"] in ("user", "assistant")]
        msgs.append({"role": "user", "content": f"VAKA:\n{chr(10).join(lines)}\n\nKAYNAK METİNLER:\n{ctx}\n\n"
                                                f"ÖĞRENCİNİN SORUSU: {question}"})
        llm = self.llm_factory()
        self.last_chat_llm = llm
        return llm.stream(SYSTEM_CASE_CHAT, msgs, max_tokens=1500)
