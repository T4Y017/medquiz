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


SYSTEM_FIG_CARDS = """Sen tıp öğrencileri için ŞEKİLLİ bilgi kartı hazırlayan bir eğitimcisin. Sana kaynak kitaptan kesilmiş
şekiller (röntgen, EKG, histoloji, klinik fotoğraf, şema…) ve geçtikleri sayfanın metni verilir. Her uygun şekil
için TEK bir kart yaz: ön yüzde şekle bakarak cevaplanacak kısa bir soru ("Bu görüntüdeki bulgu nedir?", "İşaretli
yapı hangisidir?", "Bu EKG hangi tanıyla uyumludur?"), arka yüzde kısa cevap ve şekilde nereye bakılacağı (1-2
cümle). Ön yüzde cevabı ya da şeklin başlığını SÖYLEME. Sayfa metniyle uyuşmayan bilgi uydurma. Şekil tıbbi
açıdan anlamsızsa (logo, süs, okunmayan küçük resim) "usable": false yap.
YALNIZCA şu JSON'u döndür: {"cards": [{"figure": 1, "usable": true, "front": "soru", "back": "cevap", "topic": "konu"}]}"""


def figure_cards(engine, src, llm_factory, n: int = 3, language: str = "tr", should_stop=None) -> list[dict]:
    """Seçilen PDF sayfalarındaki şekillerden görüntülü kart (ön yüzde görüntü + soru)."""
    import base64
    from pathlib import Path

    from . import figures
    from .quiz import make_llm
    if n <= 0:
        return []
    members = [m for pk in src.packets for m in (pk.members or [pk])]
    hint: dict[int, list[int]] = {}
    docs: dict[int, dict] = {}
    for did in src.doc_ids or [src.doc_id]:
        d = engine.db.get_document(did)
        if d and d["kind"] == "pdf" and Path(d["source"]).is_file():
            docs[did] = dict(d)
    for m in members:
        did = next((k for k, d in docs.items() if d["title"] == m.title), None)
        if did is not None and m.pages:
            hint.setdefault(did, []).extend(m.pages)
    if not docs:
        return []
    figs = figures.find_figures(list(docs.values()), n + 1, pages_hint=hint or None)
    out: list[dict] = []
    for i in range(0, len(figs), 4):
        if len(out) >= n or (should_stop and should_stop()):
            break
        group = figs[i:i + 4]
        parts: list[dict] = [{"type": "text", "text": f"{len(group)} şekil var; her biri için (uygunsa) 1 kart yaz."
                              + (" LANGUAGE: write the cards in ENGLISH." if language == "en" else "")}]
        for k, f in enumerate(group, 1):
            parts.append({"type": "text", "text": f"--- ŞEKİL {k} — s. {f.page}. Sayfa metni: {f.context[:1500]}"})
            parts.append({"type": "image", "data": f.image, "mime": "image/jpeg"})
        data = make_llm(llm_factory, "heavy").complete_json(SYSTEM_FIG_CARDS, parts, max_tokens=6000, temperature=0.3)
        for c in nz.items(data, "cards", "kartlar", "items"):
            if not isinstance(c, dict) or c.get("usable") is False:
                continue
            try:
                k = int(c.get("figure", 0))
            except (TypeError, ValueError):
                continue
            front, back = nz.text(c.get("front")), nz.text(c.get("back"))
            if 1 <= k <= len(group) and front and back:
                f = group[k - 1]
                out.append({"front": front, "back": back + f"\n\n🖼 {docs[f.doc_id]['title'][:50]} — s. {f.page}",
                            "topic": nz.text(c.get("topic") or ""), "image": base64.b64encode(f.image).decode()})
                if len(out) >= n:
                    break
    return out


def cards_from_source(engine, src, llm_factory, n: int = 25, topic: str = "", should_stop=None,
                      language: str = "tr", n_images: int = 0) -> list[dict]:
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
                 + (f" Yalnızca şu konuya odaklan: {topic}." if topic.strip() else "")
                 + (" LANGUAGE: write every card (front and back) in ENGLISH, not Turkish." if language == "en" else ""))
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
    if n_images and not (should_stop and should_stop()):
        try:
            out = figure_cards(engine, src, llm_factory, n_images, language, should_stop) + out
        except Exception as e:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning("Şekilli kart üretilemedi: %s", e)
    return out
