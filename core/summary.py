"""Kaynaktan ayrıntılı özet / analiz (kullanıcının hazır komutlarıyla).

PDF kaynaklarda seçili sayfalar ALT-PDF olarak yapay zekâya gönderilir: model sayfayı görür, tabloları ve
şekilleri gerçekten okur. Diğer kaynaklarda (Word, metin, web) kayıtlı metin gönderilir.
"""
from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from .db import Database
from .llm import LLMError

log = logging.getLogger(__name__)

MAX_PAGES = 120           # bir analizde en fazla
PAGES_PER_PART = 25       # bir istekte en fazla (uzun aralıklar parçalara bölünür)
MAX_PDF_BYTES = 15_000_000
MAX_TEXT_CHARS = 70_000

SYSTEM_SUMMARY = """Sen titiz bir tıp eğitimi ve araştırma asistanısın. Sana verilen KAYNAĞI (PDF sayfaları ya da metin)
dikkatle oku ve kullanıcının talimatını eksiksiz uygula.
GENEL KURALLAR:
- Yalnızca kaynaktaki bilgiyi kullan; veri uydurma. Kaynakta olmayan bir şey istenirse "kaynakta yok" de.
- Çıktıyı MARKDOWN olarak yaz: ana bölümler "## ", alt bölümler "### ", maddeler "- ".
- TABLOLAR: kaynaktaki her tabloyu, kaynaktaki sırayla, Türkçeye çevirerek GERÇEK MARKDOWN TABLOSU olarak ver
  (| Sütun | Sütun | ve |---| satırı). İçeriği ve satır sırasını koru, her satırda bir öğe. Alt kategori
  başlıklarını tablo içinde **kalın** yaz, altındaki öğeleri ayrı satırlarda "↳ " ile girintili göster.
  Tablonun üstüne "**Tablo N: başlık**" yaz.
- Grafik, şema, akış diyagramı, harita gibi görselleri ilişkileriyle birlikte açıklayıcı Türkçe paragrafla anlat;
  üstüne "**Şekil N: başlık**" yaz.
- Birimler: ağırlık kg, boy cm, hacim mL; diğer birimleri de uygun SI birimine çevir.
- Sade, açık ve öz Türkçe kullan (talimat başka dil istemedikçe)."""

QUESTION_RULES = """SORULAR İÇİN ZORUNLU KURALLAR:
1. Her soruya konuyu açan, bilgi veren bir cümleyle başla, sonra soruyu sor.
2. Yaş, ağırlık, boy, yüzde, ilaç dozu ve zaman birimleri dışında kısaltma kullanma.
3. Yalnızca kaynaktaki bilgiyi kullan; veri uydurma."""

_STEP_CLASS = """### 1. Sınıflama
Kaynaktaki her kategori, sınıf ve genel terimi belirle. Her birinin altına, kaynakta açıkça geçen alt
kategorilerini, örneklerini veya üyelerini kaynaktaki ifadeyle listele."""
_STEP_PROTOCOL = """### 2. Klinik protokol
Yalnızca kaynağa dayanan kapsamlı, uygulanabilir bir klinik protokol yaz. Kaynak verdiyse şunları kapsa: tanım,
nedenler ve risk faktörleri, belirti ve bulgular, tanı, tedavi (ilaç dozlarıyla), izlem, komplikasyonlar, takip."""
_STEP_VISUALS = """### 3. Tablolar, şekiller ve diğer görseller
Kaynaktaki her görseli kaynaktaki sırayla işle: tabloları Türkçe Markdown tablo olarak, grafik/şema/akış
diyagramlarını ilişkileri açıklayan Türkçe paragraf olarak ver. Hiçbirini atlama."""
_STEP_REVIEW = """### 4. Tekrar soruları (tam 10)
Bilişsel düzeylerde sırayla ilerle: bilgi, kavrama, uygulama, analiz, sentez, değerlendirme (10 soruyu altı düzeye
yay). Kaynaktaki konu sırasını izle; her soru farklı bir yönü ölçsün. Her soruyu madde olarak yaz; hemen altında
ayrı satırlarda **Cevap:** ve **Açıklama:** ver."""
_STEP_PICTURE = """### 5. Görsele dayalı sorular (en fazla 5)
Yalnızca kaynakta resim/şekil/diyagram varsa: her biri belirli bir görsele dayanan en fazla 5 soru yaz. Önce
görselin adını (ör. "Şekil 2") ve ne gösterdiğini yaz, sonra soruyu sor. Görsel yoksa bu adımı atla ve bunu belirt."""
_STEP_PEDS = """### 6. Pediatrik klinik vakalar (5)
Kaynağa dayanan 5 pediatrik klinik senaryo yaz. Her senaryoya 5 alt soru ver. Her alt soru, cevabı ve ayrıntılı
açıklaması ayrı satırlarda olsun."""
_STEP_ILLUSTRATION = """### 7. Hastalık şeması
Kaynak bir hastalığı anlatıyorsa, onu gösteren etiketli bir şemanın (ör. basit mekanizma ya da tipik klinik
bulgular) ayrıntılı metin tarifini yaz ve kaynaktaki ilgili şekle işaret et."""

TEMPLATES: dict[str, dict] = {
    "detayli": {
        "name": "Detaylı özet (sınıflama + klinik protokol + tablolar)",
        "desc": "Kaynaktaki sınıflamalar, kapsamlı klinik protokol ve tüm tablolar/şekiller Türkçe.",
        "prompt": "[TOPIC] konusundaki kaynağı sistematik ve eksiksiz analiz et. Şu bölümleri yaz:\n\n"
                  + "\n\n".join([_STEP_CLASS, _STEP_PROTOCOL, _STEP_VISUALS, _STEP_ILLUSTRATION]),
    },
    "detayli_sorular": {
        "name": "Detaylı özet + 10 tekrar sorusu",
        "desc": "Detaylı özetin üstüne Bloom düzeylerine yayılmış 10 soru ve görsele dayalı sorular.",
        "prompt": "[TOPIC] konusundaki kaynağı sistematik ve eksiksiz analiz et. Şu bölümleri yaz:\n\n"
                  + "\n\n".join([_STEP_CLASS, _STEP_PROTOCOL, _STEP_VISUALS, _STEP_REVIEW, _STEP_PICTURE])
                  + "\n\n" + QUESTION_RULES,
    },
    "pediatrik": {
        "name": "Pediatrik vaka seti (5 vaka × 5 soru)",
        "desc": "Kaynağa dayalı 5 pediatrik senaryo, her birinde 5 alt soru, cevap ve ayrıntılı açıklama.",
        "prompt": "[TOPIC] konusundaki kaynağa dayanarak:\n\n" + _STEP_PEDS + "\n\n" + QUESTION_RULES,
    },
    "tam": {
        "name": "Tam analiz (7 adım + son kontrol)",
        "desc": "Eklediğin komutun tamamı: sınıflama, protokol, görseller, 10 soru, görsel soruları, 5 pediatrik vaka, şema.",
        "prompt": "[TOPIC] konusundaki kaynağı sistematik ve eksiksiz analiz et. Adımları sırayla uygula:\n\n"
                  + "\n\n".join([_STEP_CLASS, _STEP_PROTOCOL, _STEP_VISUALS, _STEP_REVIEW, _STEP_PICTURE,
                                 _STEP_PEDS, _STEP_ILLUSTRATION])
                  + "\n\n" + QUESTION_RULES
                  + "\n\nSON KONTROL (yanıtlamadan önce): tam 10 tekrar sorusu ve 5×5 vaka sorusu olduğundan, her "
                    "sorunun bilgi veren bir cümleyle başladığından, izin verilenler dışında kısaltma olmadığından, "
                    "birimlerin kg/cm/mL olduğundan ve kaynaktaki her tablo/şeklin sırayla işlendiğinden emin ol.",
    },
    "en_cloze": {
        "name": "İngilizce: boşluk doldurma (cloze) testi",
        "desc": "Akademik İngilizce özet paragrafı + 5 numaralı boşluk ve her biri için 5 şıklı soru.",
        "prompt": "You are an English language test writer. Using the source, create a cloze test. Write in ENGLISH "
                  "(explanations may be in Turkish).\n\nSTEP 1 – PARAGRAPH: Write an original 100–150 word summary "
                  "of the source in academic English with exactly 5 numbered blanks [1]–[5], in this order: [1] "
                  "academic vocabulary word, [2] conjunction, [3] preposition, [4] verb tense, [5] grammatical element "
                  "(relative pronoun, article or quantifier). The paragraph must read naturally once filled. Do not "
                  "reveal answers in the paragraph.\n\nSTEP 2 – QUESTIONS: For each blank, one question with options "
                  "A–E, exactly one correct answer, four plausible distractors of the same category; randomize the "
                  "correct letter.\n\nSTEP 3 – Immediately after each question: **Correct answer:** letter + word, "
                  "and an explanation as bullet points (grammar rule, meaning or collocation).",
    },
    "en_awl": {
        "name": "İngilizce: akademik kelimeler (TR–EN tablo)",
        "desc": "Kaynaktaki akademik kelimeler; Türkçe anlam, İngilizce tanım, örnek cümle. Tek tıkla kelime havuzuna eklenir.",
        "prompt": "Kaynaktaki akademik ve tıbbi İngilizce kelimeleri (Academic Word List ve sık tıbbi terimler) çıkar. "
                  "Önce kaynağın 120-180 kelimelik özgün İngilizce özetini bu kelimelerle yaz. Sonra şu sütunlarla "
                  "TEK bir Markdown tablosu ver: | Kelime | Türkçe anlam | İngilizce anlam | Örnek cümle |. "
                  "En az 20 kelime; kaynaktaki geçiş sırasıyla.",
    },
}


@dataclass
class Packet:
    label: str                       # "s. 12-36"
    pdf: bytes | None = None
    text: str = ""
    pages: list[int] = field(default_factory=list)


@dataclass
class Source:
    doc_id: int
    title: str
    scope: str
    packets: list[Packet]
    notes: list[str] = field(default_factory=list)


def _page_of(loc: str) -> int | None:
    m = re.match(r"s\.\s*(\d+)", loc or "")
    return int(m.group(1)) if m else None


def _ranges(pages: list[int]) -> str:
    if not pages:
        return ""
    out, start, prev = [], pages[0], pages[0]
    for p in pages[1:] + [None]:
        if p is not None and p == prev + 1:
            prev = p
            continue
        out.append(f"{start}" if start == prev else f"{start}-{prev}")
        if p is not None:
            start = prev = p
    return "s. " + ", ".join(out)


def parse_pages(spec: str, n_pages: int) -> list[int]:
    """'12-30, 45, 50-52' → [12..30, 45, 50, 51, 52] (1 tabanlı, sınırlar içinde)."""
    pages: set[int] = set()
    for part in re.split(r"[,;\s]+", spec.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*[-–]\s*(\d+)", part)
        if m:
            a, b = sorted((int(m.group(1)), int(m.group(2))))
            pages.update(range(a, b + 1))
        elif part.isdigit():
            pages.add(int(part))
    return sorted(p for p in pages if 1 <= p <= max(1, n_pages))


def pdf_page_count(path: str) -> int:
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(path)
        n = len(doc)
        doc.close()
        return n
    except Exception:  # noqa: BLE001
        try:
            from pypdf import PdfReader
            return len(PdfReader(path).pages)
        except Exception:  # noqa: BLE001
            return 0


def sub_pdf(path: str, pages: list[int]) -> bytes:
    """Seçili sayfalardan (1 tabanlı) yeni bir PDF üretir."""
    try:
        import pypdfium2 as pdfium
        src = pdfium.PdfDocument(path)
        new = pdfium.PdfDocument.new()
        new.import_pages(src, [p - 1 for p in pages])
        buf = io.BytesIO()
        new.save(buf)
        new.close()
        src.close()
        return buf.getvalue()
    except ImportError:
        from pypdf import PdfReader, PdfWriter
        r = PdfReader(path)
        w = PdfWriter()
        for p in pages:
            w.add_page(r.pages[p - 1])
        buf = io.BytesIO()
        w.write(buf)
        return buf.getvalue()


class SummaryEngine:
    def __init__(self, db: Database, retriever, llm_factory: Callable):
        self.db = db
        self.retriever = retriever
        self.llm_factory = llm_factory

    # ------------------------------------------------------------------ kaynak hazırlığı
    def build_source(self, doc_id: int, pages_spec: str = "", topic: str = "", send_pdf: bool = True) -> Source:
        doc = self.db.get_document(doc_id)
        if not doc:
            raise LLMError("Belge bulunamadı.")
        chunks = self.db.doc_chunks(doc_id)
        notes: list[str] = []
        path = doc["source"]
        is_pdf = doc["kind"] == "pdf" and Path(path).exists()
        if doc["kind"] in ("pdf", "web-pdf") and not is_pdf:
            notes.append("Orijinal PDF dosyası bulunamadı (taşınmış/silinmiş olabilir); kayıtlı metin kullanıldı, "
                         "tablo düzeni kısmen kaybolabilir.")
        n_pages = pdf_page_count(path) if is_pdf else 0
        all_pages = sorted({p for c in chunks if (p := _page_of(c["location"]))}) or list(range(1, n_pages + 1))

        # hangi sayfalar?
        if pages_spec.strip():
            pages = parse_pages(pages_spec, n_pages or (all_pages[-1] if all_pages else 0))
            if not pages:
                raise LLMError("Sayfa aralığı anlaşılamadı. Örnek: 12-30 veya 12-30, 45")
        elif topic.strip():
            hits = self.retriever.search(topic, k=14, doc_ids=[doc_id])
            if not hits:
                raise LLMError(f"Bu belgede '{topic}' ile ilgili bölüm bulunamadı.")
            found = sorted({p for h in hits if (p := _page_of(h["location"]))})
            pages = sorted({q for p in found for q in (p - 1, p, p + 1)
                            if q >= 1 and (not n_pages or q <= n_pages)})[:40]
        else:
            pages = all_pages
        if len(pages) > MAX_PAGES:
            notes.append(f"Seçim {len(pages)} sayfa; ilk {MAX_PAGES} sayfa işlendi. Daha dar bir aralık seçebilirsin.")
            pages = pages[:MAX_PAGES]

        packets: list[Packet] = []
        if pages and (is_pdf and send_pdf):
            for i in range(0, len(pages), PAGES_PER_PART):
                part = pages[i:i + PAGES_PER_PART]
                data = sub_pdf(path, part)
                if len(data) > MAX_PDF_BYTES:
                    notes.append(f"{_ranges(part)} çok büyük ({len(data) // 1_000_000} MB, muhtemelen taranmış); "
                                 "metin olarak gönderildi.")
                    packets.append(Packet(_ranges(part), text=self._text_for(chunks, part), pages=part))
                else:
                    packets.append(Packet(_ranges(part), pdf=data, pages=part))
        else:
            paged = any(_page_of(c["location"]) for c in chunks)
            if pages and paged:
                ps = set(pages)
                sel = [c for c in chunks if _page_of(c["location"]) in ps]
            elif topic.strip() and not pages_spec.strip():
                ids = {h["id"] for h in self.retriever.search(topic, k=20, doc_ids=[doc_id])}
                sel = [c for c in chunks if c["id"] in ids] or chunks
            else:
                sel = chunks
            text, part_no = "", 1
            for c in sel:
                piece = (f"[{c['location']}]\n" if c["location"] else "") + c["text"]
                if len(text) + len(piece) > MAX_TEXT_CHARS and text:
                    packets.append(Packet(f"bölüm {part_no}", text=text))
                    part_no += 1
                    text = ""
                text += piece + "\n\n"
            if text:
                packets.append(Packet(f"bölüm {part_no}" if packets else "tümü", text=text))
            if len(packets) > 5:
                notes.append(f"Metin uzun; ilk 5 bölüm işlendi ({len(packets)} bölümden).")
                packets = packets[:5]
        if not packets:
            raise LLMError("Seçilen aralıkta işlenecek içerik bulunamadı.")
        scope = _ranges(pages) if pages and (is_pdf or any(_page_of(c["location"]) for c in chunks)) else "tümü"
        if topic.strip() and not pages_spec.strip():
            scope = f"“{topic}” ile ilgili {scope}"
        return Source(doc_id, doc["title"], scope, packets, notes)

    @staticmethod
    def _text_for(chunks: list[dict], pages: list[int]) -> str:
        ps = set(pages)
        return "\n\n".join(f"[{c['location']}]\n{c['text']}" for c in chunks if _page_of(c["location"]) in ps)

    # ------------------------------------------------------------------ çalıştırma
    def run(self, src: Source, prompt: str, topic: str = "",
            should_stop: Callable[[], bool] | None = None) -> Iterator[str]:
        """Markdown'u parça parça üretir (akış). Birden çok paket varsa her biri ayrı başlık altında."""
        from .quiz import make_llm
        instr = prompt.replace("[TOPIC]", topic.strip() or src.title)
        multi = len(src.packets) > 1
        self.last_label = ""
        for i, pk in enumerate(src.packets, 1):
            if should_stop and should_stop():
                return
            if multi:
                yield f"\n\n# Bölüm {i}/{len(src.packets)} — {pk.label}\n\n"
            head = (f"KAYNAK: {src.title} ({pk.label})\n"
                    + (f"(Bu, uzun bir kaynağın {i}/{len(src.packets)}. bölümüdür; yalnızca bu bölümü işle.)\n"
                       if multi else ""))
            parts: list[dict] = []
            if pk.pdf:
                parts.append({"type": "pdf", "data": pk.pdf, "name": f"{src.title[:40]}.pdf"})
                parts.append({"type": "text", "text": head + "\nTALİMAT:\n" + instr})
            else:
                parts.append({"type": "text", "text": head + "\nKAYNAK METİN:\n" + pk.text + "\n\n=====\nTALİMAT:\n"
                              + instr})
            llm = make_llm(self.llm_factory, "heavy")
            for piece in llm.stream(SYSTEM_SUMMARY, [{"role": "user", "content": parts}], max_tokens=30000,
                                    temperature=0.3):
                if should_stop and should_stop():
                    return
                yield piece
            self.last_label = getattr(llm, "label", "")


def vocab_rows(markdown: str) -> list[dict]:
    """'| Kelime | Türkçe anlam | İngilizce anlam | Örnek cümle |' tablosundan kelime havuzu satırları çıkarır."""
    rows, header = [], None
    for line in markdown.splitlines():
        if not line.strip().startswith("|"):
            header = None if not line.strip() else header
            continue
        cells = [c.strip().strip("*").strip() for c in line.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue
        low = [c.replace("İ", "i").replace("I", "ı").lower() for c in cells]
        if header is None:
            if any("kelime" in c or "word" in c for c in low):
                header = low
            continue
        def col(*keys):
            for k in keys:
                for j, h in enumerate(header):
                    if k in h and j < len(cells):
                        return cells[j]
            return ""
        term = col("kelime", "word", "term")
        if term:
            rows.append({"term": term, "meaning": col("türkçe", "turkish", "anlam"),
                         "definition": col("ingilizce anlam", "english", "definition", "meaning"),
                         "example": col("örnek", "example")})
    return rows
