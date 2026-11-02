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
