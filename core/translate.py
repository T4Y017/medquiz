"""Tıbbi metin çevirisi ve kelime/ifade tanımı (kelime havuzu için)."""
from __future__ import annotations

import hashlib
import re
from typing import Callable, Iterator

from . import normalize as nz
from .db import Database
from .llm import LLMError

SYSTEM_TRANSLATE = """Sen tıp alanında uzman, İngilizce-Türkçe çevirmen ve eğitimcisin.
Verilen metni anlamı tam koruyarak, akıcı ve doğal TÜRKÇE'ye çevir.
- Tıbbi terimlerin Türkçe karşılığını kullan; ilk geçtiği yerde İngilizce aslını parantez içinde ver
  (örn. "taşikardi (tachycardia)"). Yaygın kısaltmaları (EKG, BUN, ARDS) koru.
- Sayıları, birimleri, laboratuvar değerlerini ve şık harflerini (A, B, C…) aynen koru.
- Biçimi koru (satırlar, madde işaretleri). Açıklama, yorum veya not EKLEME; yalnızca çeviriyi yaz.
- Metin zaten Türkçe ise İngilizce'ye çevir (aynı kurallarla)."""

SYSTEM_DEFINE = """Sen tıp öğrencilerine İngilizce tıbbi terminoloji öğreten bir eğitimcisin.
Verilen kelime/ifadeyi, verilen bağlam cümlesindeki anlamıyla açıkla. YALNIZCA şu JSON'u döndür:
{"term": "kelimenin sözlük biçimi (çoğulsa tekil, çekimliyse yalın)",
 "meaning": "kısa Türkçe karşılık (1-4 kelime)",
 "definition": "Türkçe, 1-2 cümlelik tıbbi açıklama",
 "pos": "noun / verb / adjective / adverb / phrase / abbreviation",
 "ipa": "IPA okunuşu (bilmiyorsan boş)",
 "example": "kelimeyi içeren kısa İngilizce örnek cümle (bağlamdakini kısaltarak kullanabilirsin)"}"""


def _key(text: str, kind: str = "tr") -> str:
    return hashlib.sha1(f"{kind}\n{text.strip()}".encode("utf-8")).hexdigest()


class Translator:
    def __init__(self, db: Database, llm_factory: Callable):
        self.db = db
        self.llm_factory = llm_factory
        self.last_label = ""

    def cached(self, text: str) -> str | None:
        return self.db.get_translation(_key(text))

    def translate(self, text: str) -> str:
        text = text.strip()
        if not text:
            return ""
        hit = self.cached(text)
        if hit:
            return hit
        llm = self.llm_factory()
        out = llm.complete(SYSTEM_TRANSLATE, [{"role": "user", "content": text}],
                           max_tokens=min(8000, 400 + len(text)), temperature=0.2).strip()
        self.last_label = getattr(llm, "label", "")
        if not out:
            raise LLMError("Çeviri boş döndü; lütfen tekrar deneyin.")
        self.db.put_translation(_key(text), out)
        return out

    def translate_stream(self, text: str) -> Iterator[str]:
        """Önbellekte varsa tek seferde, yoksa akışla verir ve sonunda önbelleğe yazar."""
        text = text.strip()
        hit = self.cached(text)
        if hit:
            yield hit
            return
        llm = self.llm_factory()
        parts: list[str] = []
        for piece in llm.stream(SYSTEM_TRANSLATE, [{"role": "user", "content": text}],
                                max_tokens=min(8000, 400 + len(text)), temperature=0.2):
            parts.append(piece)
            yield piece
        self.last_label = getattr(llm, "label", "")
        out = "".join(parts).strip()
        if out:
            self.db.put_translation(_key(text), out)

    def define(self, term: str, context: str = "") -> dict:
        term = clean_term(term)
        if not term:
            raise LLMError("Kelime boş.")
        ctx = sentence_around(context, term) if context else ""
        llm = self.llm_factory()
        data = llm.complete_json(SYSTEM_DEFINE, f"Kelime/ifade: {term}\nBağlam: {ctx or '(yok)'}",
                                 max_tokens=700, temperature=0.2)
        if isinstance(data.get("items"), list) and data["items"]:
            data = data["items"][0] if isinstance(data["items"][0], dict) else data
        out = {
            "term": nz.text(data.get("term")) or term,
            "meaning": nz.text(nz.first(data, "meaning", "turkish", "translation", "anlam")),
            "definition": nz.text(nz.first(data, "definition", "explanation", "aciklama", "tanim")),
            "pos": nz.text(data.get("pos")),
            "ipa": nz.text(data.get("ipa")).strip("/[] "),
            "example": nz.text(data.get("example")) or ctx,
        }
        # Kullanıcının kelimeyi gördüğü gerçek cümle varsa onu tercih et (hatırlamayı kolaylaştırır)
        if ctx and term.lower() in ctx.lower():
            out["example"] = ctx
        if not out["meaning"]:
            raise LLMError("Kelimenin anlamı alınamadı; lütfen tekrar deneyin.")
        # model tekil/yalın biçime çevirdiyse ama tamamen farklı bir kelime döndürdüyse kullanıcınınkini koru
        if out["term"].lower()[:3] != term.lower()[:3]:
            out["term"] = term
        return out


def clean_term(t: str) -> str:
    t = re.sub(r"\s+", " ", (t or "").replace(" ", " ")).strip()
    return t.strip(" .,;:!?\"'()[]{}“”‘’«»")[:80]


def sentence_around(text: str, term: str, max_len: int = 300) -> str:
    """Bağlam metninden, terimi içeren cümleyi çıkarır."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return ""
    i = text.lower().find(term.lower())
    if i < 0:
        return text[:max_len]
    start = max(text.rfind(". ", 0, i), text.rfind("? ", 0, i), text.rfind("\n", 0, i))
    start = 0 if start < 0 else start + 2
    end_candidates = [p for p in (text.find(". ", i), text.find("? ", i)) if p >= 0]
    end = min(end_candidates) + 1 if end_candidates else len(text)
    s = text[start:end].strip()
    if len(s) > max_len:
        a = max(0, i - start - max_len // 2)
        s = "…" + s[a:a + max_len] + "…"
    return s
