"""Vaka çalışması: kaynaklara dayalı, adım adım ilerleyen interaktif klinik vakalar."""
from __future__ import annotations

import json
import logging
import random
import re
from dataclasses import dataclass
from typing import Callable, Iterator

from . import normalize as nz
from .db import Database
from .llm import LLMClient, LLMError
from .quiz import QuizEngine, format_context, make_llm, source_label
from .retrieval import Retriever

log = logging.getLogger(__name__)

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
- Laboratuvar/görüntüleme sonuçlarını "findings" listesinde YAPILANDIRILMIŞ ver (test, sonuç, birim, referans aralığı);
  bu değerleri narrative içinde tekrar yazma ve yorumunu önceden verme — yorumu öğrenci yapacak.
- YALNIZCA geçerli JSON döndür."""

PARACLINIC_RULES = """PARAKLİNİK AĞIRLIK (zorunlu):
- En az 2 aşama paraklinik yorum aşaması olsun. Uygun olanları seç: tam kan sayımı, biyokimya, elektrolitler,
  arter/venöz kan gazı, idrar tetkiki, koagülasyon, kardiyak/inflamatuvar belirteçler, hormonlar, BOS/plevral sıvı,
  EKG, akciğer grafisi, USG/BT/MR raporu, ekokardiyografi, patoloji, mikrobiyoloji/kültür.
- Bu aşamalarda sonuçlar "findings" listesinde verilsin; görev sonuçları YORUMLAMAYI istesin (hangi değerler anormal,
  hangi tabloyu gösteriyor, ör. anyon açığı hesabı, asit-baz bozukluğunun tipi ve kompanzasyonu, EKG bulgusunun
  lokalizasyonu, görüntüleme bulgusunun anlamı).
- Değerler birbiriyle ve klinikle tutarlı olsun; anormal olanları "flag" ile işaretle (H yüksek, L düşük, A anormal).
- EKG/görüntüleme gibi sayısal olmayan tetkiklerde "result" alanına bulguyu tarif eden kısa rapor yaz (yorum değil)."""

TEST_RULES = """VAKA SONU TESTİ:
"final_test" listesinde tam olarak {n} adet TUS tarzı, 5 şıklı, tek doğru cevaplı soru yaz. Sorular vakanın konusunu
pekiştirsin ama vakanın kendisini tekrar sormasın. {para}Her soruda kısa "explanation" ver.
Bir sorunun kökünde laboratuvar değerleri veriyorsan birimleriyle yaz."""

TEST_PARA = "En az yarısı paraklinik yorum sorusu olsun (soru kökünde yeni lab/kan gazı/EKG/görüntüleme verisi verip yorumlatan). "

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
      "title": "Laboratuvar sonuçları",
      "narrative": "istenen tetkiklerin sonuçları geldi",
      "findings": [
        {"category": "Kan gazı", "test": "pH", "result": "7.21", "unit": "", "ref": "7.35-7.45", "flag": "L"},
        {"category": "Kan gazı", "test": "HCO3", "result": "11", "unit": "mEq/L", "ref": "22-26", "flag": "L"},
        {"category": "EKG", "test": "12 derivasyon EKG", "result": "Sinüs taşikardisi, 118/dk; ...", "unit": "", "ref": "", "flag": "A"}
      ],
      "task": {"type": "open", "prompt": "Sonuçları yorumlayınız: ...", "expected_points": ["..."], "model_answer": "..."},
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
  "final_test": [
    {"question": "...", "options": ["...", "...", "...", "...", "..."], "answer_index": 0,
     "explanation": "...", "skill": "paraklinik"}
  ],
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
    paraclinic: bool = True
    test_count: int = 5
    language: str = "tr"       # "tr" | "en"


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

        extra = []
        if req.paraclinic:
            extra.append(PARACLINIC_RULES)
        n_test = max(0, min(10, req.test_count))
        if n_test:
            extra.append(TEST_RULES.format(n=n_test, para=TEST_PARA if req.paraclinic else ""))
        else:
            extra.append('"final_test" alanını boş liste [] olarak bırak.')
        if req.language == "en":
            extra.append("DİL: Vakanın tüm metnini (başlık, anlatım, bulgular, görevler, şıklar, öğretim notları, "
                         "tartışma, test soruları) İNGİLİZCE yaz (USMLE Step 2 CK tarzı). Kaynak Türkçe olsa bile.")
        user = (f"KAYNAK METİNLER:\n\n{context}\n\n=====\n\n{focus}"
                f"Hedef seviye: {req.level} — {LEVELS.get(req.level, '')}\n\n{CASE_SPEC}\n\n" + "\n\n".join(extra))
        llm = make_llm(self.llm_factory, "heavy")
        data = llm.complete_json(SYSTEM_CASE, user, max_tokens=16000, temperature=0.7)
        case = self._normalize(data, keymap, ctx)
        if len(case["stages"]) < 2:
            log.warning("Kullanılamayan vaka yanıtı (%s): %s", getattr(llm, "label", "?"),
                        json.dumps(data, ensure_ascii=False)[:3000])
            raise LLMError("Yapay zekâ yanıt verdi ama vaka beklenen biçimde değildi. Lütfen tekrar deneyin; "
                           "sorun sürerse Ayarlar'dan başka bir model seçin.")
        case["level"] = req.level
        case["model"] = getattr(llm, "label", "")
        case["topic"] = req.topic.strip() or ("Kendi vakam" if req.own_case.strip() else "")
        title = case["public_title"]
        cid = self.db.save_case(title, case["topic"], req.level, case, req.profile)
        if ctx:
            self.retriever.mark_used([c["id"] for c in ctx])
        return {"id": cid, "payload": case}

    def _sources(self, keys, keymap) -> list[dict]:
        if isinstance(keys, str):
            keys = re.findall(r"K\d+", keys) or [keys]
        out = []
        for k in keys or []:
            if isinstance(k, (str, int)):
                k = str(k).strip("[] ")
                if k.isdigit():
                    k = f"K{k}"
                if k in keymap:
                    c = keymap[k]
                    out.append({"key": k, "label": source_label(c), "text": c["text"], "chunk_id": c["id"]})
        return out

    def _choice_task(self, ttype: str, task: dict) -> dict | None:
        opts, flagged = nz.options_with_flags(nz.first(task, "options", "choices", "secenekler"))
        if len(opts) < 3:
            return None
        if ttype == "mcq":
            ans = nz.mcq_answer(task, opts, flagged)
            if ans is None:
                return None
            correct = {ans}
        else:
            correct = nz.multi_answer(task, opts, flagged)
            if not correct:
                return None
        order = list(range(len(opts)))
        self.rng.shuffle(order)
        t = {"type": ttype, "options": [opts[i] for i in order]}
        new_correct = sorted(order.index(i) for i in correct)
        if ttype == "mcq":
            t["answer_index"] = new_correct[0]
        else:
            t["correct_indices"] = new_correct
        return t

    @staticmethod
    def _findings(raw) -> list[dict]:
        out = []
        if isinstance(raw, dict):            # {"Hemogram": [...], "EKG": "..."} biçimi
            flat = []
            for cat, v in raw.items():
                if isinstance(v, list):
                    flat += [dict(x, category=x.get("category", cat)) if isinstance(x, dict) else
                             {"category": cat, "test": cat, "result": str(x)} for x in v]
                else:
                    flat.append({"category": cat, "test": cat, "result": nz.text(v)})
            raw = flat
        for f in raw or []:
            if isinstance(f, str):
                f = {"test": "", "result": f}
            if not isinstance(f, dict):
                continue
            test = nz.text(nz.first(f, "test", "name", "parameter", "parametre"))
            result = nz.text(nz.first(f, "result", "value", "deger", "değer", "sonuc", "sonuç"))
            if not (test or result):
                continue
            flag = str(f.get("flag", "") or "").strip().upper()[:1]
            out.append({
                "category": nz.text(nz.first(f, "category", "panel", "kategori")) or "Sonuçlar",
                "test": test, "result": result,
                "unit": nz.text(f.get("unit", "")), "ref": nz.text(nz.first(f, "ref", "reference", "referans")),
                "flag": flag if flag in ("H", "L", "A", "N") else "",
            })
        return out

    def _normalize(self, data: dict, keymap: dict, ctx: list[dict]) -> dict:
        if not isinstance(data, dict):
            data = {"stages": data}
        stages = []
        for s in nz.items(data, "stages", "steps", "asamalar", "aşamalar"):
            if not isinstance(s, dict):
                continue
            task = nz.first(s, "task", "question", "gorev", "görev", default={})
            if isinstance(task, str):
                task = {"type": "open", "prompt": task}
            prompt = nz.text(nz.first(task, "prompt", "question", "soru", "text"))
            narrative = nz.text(nz.first(s, "narrative", "description", "text", "content", "scenario", "anlatim"))
            findings = self._findings(nz.first(s, "findings", "results", "labs", "bulgular", "sonuclar"))
            if not narrative and findings:
                narrative = "Tetkik sonuçları aşağıdadır."
            if not narrative or not prompt:
                continue
            has_opts = bool(nz.first(task, "options", "choices"))
            ttype = nz.norm_type(task.get("type"), default="mcq" if has_opts else "open")
            if ttype in ("mcq", "multi"):
                t = self._choice_task(ttype, task)
                if t is None:
                    continue
                t["prompt"] = prompt
            else:
                t = {"type": "open", "prompt": prompt,
                     "expected_points": nz.str_list(nz.first(task, "expected_points", "key_points", "points",
                                                             "rubric")),
                     "model_answer": nz.text(nz.first(task, "model_answer", "answer", "ideal_answer"))}
                if not t["expected_points"] and not t["model_answer"]:
                    continue
            stages.append({
                "title": nz.text(s.get("title")) or f"Aşama {len(stages) + 1}",
                "narrative": narrative,
                "findings": findings,
                "task": t,
                "teaching": nz.text(nz.first(s, "teaching", "teaching_note", "explanation", "ogretim_notu")),
                "sources": self._sources(nz.first(s, "source_keys", "sources"), keymap),
                "general_knowledge": bool(s.get("general_knowledge", False)),
            })

        test = []
        for q in nz.items(data, "final_test", "test", "quiz", "questions") if isinstance(data, dict) else []:
            if not isinstance(q, dict):
                continue
            stem = nz.text(nz.first(q, "question", "stem", "prompt", "soru"))
            t = self._choice_task("mcq", q) if stem else None
            if t:
                test.append({"question": stem, "options": t["options"], "answer_index": t["answer_index"],
                             "explanation": nz.text(nz.first(q, "explanation", "rationale", "aciklama")),
                             "skill": nz.text(q.get("skill", ""))})

        diff = []
        for d in nz.first(data, "differential", "differential_diagnosis", "ayirici_tani", default=[]) or []:
            if isinstance(d, dict) and nz.text(nz.first(d, "diagnosis", "name", "tani")):
                diff.append({"diagnosis": nz.text(nz.first(d, "diagnosis", "name", "tani")),
                             "for": nz.text(nz.first(d, "for", "supporting", "lehine")),
                             "against": nz.text(nz.first(d, "against", "opposing", "aleyhine"))})
            elif isinstance(d, str) and d.strip():
                diff.append({"diagnosis": d.strip(), "for": "", "against": ""})
        all_keys = []
        for st in nz.items(data, "stages", "steps"):
            if isinstance(st, dict):
                k = st.get("source_keys") or []
                all_keys += k if isinstance(k, list) else [k]
        k = data.get("source_keys") or []
        all_keys = list(dict.fromkeys(all_keys + (k if isinstance(k, list) else [k])))
        return {
            "public_title": nz.text(nz.first(data, "public_title", "title")) or "Klinik vaka",
            "patient": nz.text(data.get("patient")),
            "stages": stages,
            "final_test": test,
            "final_diagnosis": nz.text(nz.first(data, "final_diagnosis", "diagnosis", "kesin_tani")),
            "differential": diff,
            "discussion": nz.text(nz.first(data, "discussion", "tartisma")),
            "teaching_points": nz.str_list(nz.first(data, "teaching_points", "key_points", "ogretim_noktalari")),
            "pitfalls": nz.str_list(nz.first(data, "pitfalls", "common_mistakes")),
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
            f = "\n".join(f"- {x['test']}: {x['result']} {x['unit']} (ref {x['ref']})".strip()
                          for x in s.get("findings", []))
            lines.append(f"## {s['title']}\n{s['narrative']}\n{f}\nGörev: {s['task']['prompt']}")
        if finished:
            lines.append(f"## Kesin tanı\n{case.get('final_diagnosis', '')}\n\n## Tartışma\n{case.get('discussion', '')}")
        else:
            lines.append(f"(Vaka devam ediyor: {len(case['stages']) - revealed} aşama henüz açılmadı.)")
        hits = self.retriever.search(question + " " + case.get("final_diagnosis", "") if finished else question, k=5)
        ctx, _ = format_context(hits) if hits else ("(ilgili kaynak yok)", {})
        msgs = [m for m in history[-6:] if m["role"] in ("user", "assistant")]
        msgs.append({"role": "user", "content": f"VAKA:\n{chr(10).join(lines)}\n\nKAYNAK METİNLER:\n{ctx}\n\n"
                                                f"ÖĞRENCİNİN SORUSU: {question}"})
        llm = make_llm(self.llm_factory, "light")
        self.last_chat_llm = llm
        return llm.stream(SYSTEM_CASE_CHAT, msgs, max_tokens=1500)
