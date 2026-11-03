"""Bilgi kartları: özetten ya da çözülen sorudan soru–cevap kartları üretir."""
from __future__ import annotations

from . import normalize as nz

SYSTEM_CARDS = """Sen tıp öğrencileri için bilgi kartı (ön yüz: soru, arka yüz: kısa cevap) hazırlayan bir eğitimcisin.
Sana verilen ÖZETTEN, ezberlenmesi ve hatırlanması gereken bilgileri kartlara dönüştür.
KURALLAR:
- Her kart TEK bir bilgiyi sorsun (atomik). Ön yüz net bir soru olsun; arka yüz kısa olsun (birkaç kelime ya da
  1-2 kısa cümle). Uzun paragraf yazma.
- Ön yüz tek başına anlaşılır olsun: konuyu belirt ("Allerjik rinitte…", "Diyabetik ketoasidozda…").
- Öncelik: tanı kriterleri, eşik değerler, ilaç ve dozlar, ilk tercih tedaviler, ayırıcı tanıda ayırt ettirici
  bulgular, sınıflamalar, komplikasyonlar, mekanizmalar. Tablolardaki her önemli satırı ayrı karta çevir.
- Sınıflama/liste sorularında arka yüzde maddeleri satır satır yaz (en fazla 6 madde).
- Yalnızca özetteki bilgiyi kullan; bilgi uydurma. Özet İngilizceyse kartları da İngilizce yaz.
- Aynı bilgiyi iki kez sorma; önemsiz ayrıntıdan kart yapma.
YALNIZCA şu JSON'u döndür:
{"cards": [{"front": "soru", "back": "kısa cevap", "topic": "kısa konu"}]}"""


def cards_from_summary(llm, markdown: str, title: str = "", n: int = 30) -> list[dict]:
    user = (f"ÖZET ({title}):\n\n{markdown[:60000]}\n\n=====\n"
            f"Bu özetten yaklaşık {n} bilgi kartı hazırla (özet kısaysa daha az, uzunsa en fazla {n + 15}).")
    data = llm.complete_json(SYSTEM_CARDS, user, max_tokens=12000, temperature=0.3)
    out = []
    for c in nz.items(data, "cards", "kartlar", "flashcards", "items"):
        if not isinstance(c, dict):
            continue
        front = nz.text(nz.first(c, "front", "question", "soru", "q"))
        back = nz.text(nz.first(c, "back", "answer", "cevap", "a"))
        if front and back:
            out.append({"front": front, "back": back, "topic": nz.text(c.get("topic") or "")})
    return out


def card_from_question(q: dict) -> dict:
    """Quizde çözülen bir sorudan kart (yapay zekâ gerekmez)."""
    t = q.get("type")
    front = q.get("question", "")
    if t == "mcq" and q.get("options"):
        back = q["options"][q["answer_index"]]
    elif t == "fill" and q.get("blanks"):
        parts = front.split("____")
        front = parts[0] + "".join(f"({i + 1}) ____" + p for i, p in enumerate(parts[1:]))
        back = "\n".join(f"({i + 1}) {b['answer']}" for i, b in enumerate(q["blanks"]))
    elif t == "match" and q.get("pairs"):
        back = "\n".join(f"{p['left']} → {p['right']}" for p in q["pairs"])
    else:
        back = q.get("model_answer", "")
    exp = (q.get("explanation") or "").strip()
    if exp:
        back += "\n\n" + (exp if len(exp) < 600 else exp[:600] + "…")
    return {"front": front, "back": back, "topic": q.get("topic", ""), "image": q.get("image", "")}


SYSTEM_CARDS_SRC = SYSTEM_CARDS.replace(
    "Sana verilen ÖZETTEN", "Sana verilen KAYNAKTAN (PDF sayfaları ya da metin)").replace(
    "Yalnızca özetteki bilgiyi kullan; bilgi uydurma. Özet İngilizceyse kartları da İngilizce yaz.",
    "Yalnızca kaynaktaki bilgiyi kullan; bilgi uydurma. Kartları Türkçe yaz (kaynak İngilizce olsa bile; terimlerin "
    "İngilizcesini gerekirse parantezde ver). Kaynağın tamamını kapsa, yalnızca ilk sayfalarda kalma.")


def cards_from_source(engine, src, llm_factory, n: int = 25, topic: str = "", should_stop=None) -> list[dict]:
    """Kaynaktan (PDF sayfaları/metin) doğrudan bilgi kartı. Uzun kaynaklarda her bölümden orantılı sayıda."""
    from .quiz import make_llm
    total_pages = sum(max(1, len(p.pages) or sum(len(m.pages) for m in p.members) or 1) for p in src.packets)
    out: list[dict] = []
    seen: set[str] = set()
    for pk in src.packets:
        if should_stop and should_stop():
            break
        share = max(1, len(pk.pages) or sum(len(m.pages) for m in pk.members) or 1)
        k = max(4, round(n * share / total_pages))
        instr = (f"Bu kaynaktan yaklaşık {k} bilgi kartı hazırla."
                 + (f" Yalnızca şu konuya odaklan: {topic}." if topic.strip() else ""))
        llm = make_llm(llm_factory, "heavy")
        data = llm.complete_json(SYSTEM_CARDS_SRC, engine._parts(src, pk, instr), max_tokens=12000, temperature=0.3)
        for c in nz.items(data, "cards", "kartlar", "flashcards", "items"):
            if not isinstance(c, dict):
                continue
            front = nz.text(nz.first(c, "front", "question", "soru", "q"))
            back = nz.text(nz.first(c, "back", "answer", "cevap", "a"))
            if front and back and front.lower() not in seen:
                seen.add(front.lower())
                out.append({"front": front, "back": back, "topic": nz.text(c.get("topic") or "")})
    return out
