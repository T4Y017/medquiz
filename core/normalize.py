"""Yapay zekâ çıktılarını hoşgörülü biçimde okuma yardımcıları.

Farklı modeller (Claude, Gemini, OpenAI) aynı şemayı küçük farklılıklarla döndürebilir:
"multiple_choice" yerine "mcq", 0 yerine "B" ya da şıkkın metni, şıklarda "A) " öneki gibi.
Bu yardımcılar bu farkları tek bir biçime çevirir; böylece geçerli sorular boşuna atılmaz.
"""
from __future__ import annotations

import re
import unicodedata

LETTERS = "ABCDEFGHIJ"

_TYPE_MAP = {
    "mcq": "mcq", "multiplechoice": "mcq", "multiple_choice": "mcq", "single": "mcq", "singlechoice": "mcq",
    "single_choice": "mcq", "choice": "mcq", "test": "mcq", "coktansecmeli": "mcq", "çoktanseçmeli": "mcq",
    "open": "open", "openended": "open", "open_ended": "open", "essay": "open", "classic": "open", "klasik": "open",
    "shortanswer": "open", "short_answer": "open", "longanswer": "open", "long_answer": "open", "free": "open",
    "freetext": "open", "free_text": "open", "written": "open",
    "match": "match", "matching": "match", "eslestirme": "match", "eşleştirme": "match", "pairs": "match",
    "matchingpairs": "match", "matching_pairs": "match",
    "fill": "fill", "fillin": "fill", "fill_in": "fill", "fillblank": "fill", "fill_blank": "fill",
    "fillintheblank": "fill", "fill_in_the_blank": "fill", "fill_in_the_blanks": "fill", "fillintheblanks": "fill",
    "cloze": "fill", "gap": "fill", "gapfill": "fill", "gap_fill": "fill", "blank": "fill", "blanks": "fill",
    "bosluk": "fill", "boşluk": "fill", "boslukdoldurma": "fill", "boşlukdoldurma": "fill",
    "bosluk_doldurma": "fill", "boşluk_doldurma": "fill", "completion": "fill",
    "multi": "multi", "multiselect": "multi", "multi_select": "multi", "multipleselect": "multi",
    "multiple_select": "multi", "multipleanswer": "multi", "multiple_answer": "multi", "checkbox": "multi",
    "coklusecim": "multi", "çokluseçim": "multi", "selectall": "multi", "select_all": "multi",
}

_OPT_PREFIX = re.compile(r"^\s*(?:\(?([A-Ja-j])[\)\.\:\-]|\(([A-Ja-j])\)|([1-9])[\)\.])\s+")


def first(d: dict, *keys, default=None):
    """Sözlükte verilen anahtarlardan ilk dolu olanın değerini döndürür."""
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return default


def text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return "\n".join(text(x) for x in v if text(x))
    if isinstance(v, dict):
        return text(first(v, "text", "content", "value", "description", default=""))
    return str(v).strip()


def str_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        parts = [p.strip(" -•*\t") for p in re.split(r"\n|;", v)]
        return [p for p in parts if p]
    if isinstance(v, dict):
        return [text(x) for x in v.values() if text(x)]
    return [text(x) for x in v if text(x)]


def norm_type(v, default: str | None = None) -> str | None:
    if not v:
        return default
    key = unicodedata.normalize("NFKC", str(v)).lower().replace(" ", "").replace("-", "_")
    return _TYPE_MAP.get(key) or _TYPE_MAP.get(key.replace("_", "")) or default


def clean_option(s: str) -> str:
    return _OPT_PREFIX.sub("", str(s).strip(), count=1).strip()


def options_with_flags(raw) -> tuple[list[str], set[int]]:
    """Şıkları listeye çevirir. [{'text':..., 'correct': true}] biçiminden doğru şıkları da çıkarır."""
    flagged: set[int] = set()
    if isinstance(raw, dict):
        items = [raw[k] for k in sorted(raw, key=lambda x: str(x))]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    elif isinstance(raw, str):
        items = [ln for ln in raw.splitlines() if ln.strip()]
    else:
        items = []
    opts: list[str] = []
    for it in items:
        if isinstance(it, dict):
            t = clean_option(text(it))
            if not t:
                continue
            if it.get("correct") is True or it.get("is_correct") is True or it.get("dogru") is True:
                flagged.add(len(opts))
            opts.append(t)
        else:
            t = clean_option(str(it))
            if t:
                opts.append(t)
    return opts, flagged


def _norm_cmp(s: str) -> str:
    s = unicodedata.normalize("NFKC", clean_option(s)).lower()
    return re.sub(r"[\s\.\,;:]+", " ", s).strip()


def resolve_index(v, opts: list[str]) -> int | None:
    """Tek bir cevap değerini şık indeksine çevirir: 0 tabanlı sayı, harf ('B', 'B)'), ya da şık metni."""
    n = len(opts)
    if v is None or n == 0:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        i = int(v)
        if 0 <= i < n:
            return i
        if i == n:          # 1 tabanlı yazılmış son şık
            return n - 1
        return None
    s = str(v).strip()
    if not s:
        return None
    if re.fullmatch(r"-?\d+", s):
        return resolve_index(int(s), opts)
    m = re.fullmatch(r"\(?([A-Ja-j])[\)\.\:]?", s) or re.match(r"^\(?([A-Ja-j])[\)\.\:]\s", s)
    if m:
        i = LETTERS.index(m.group(1).upper())
        if i < n:
            return i
    target = _norm_cmp(s)
    for i, o in enumerate(opts):
        if _norm_cmp(o) == target:
            return i
    for i, o in enumerate(opts):            # kısmi eşleşme (model şıkkı kısaltmış olabilir)
        oc = _norm_cmp(o)
        if target and (target in oc or oc in target) and min(len(target), len(oc)) >= 4:
            return i
    return None


def resolve_indices(v, opts: list[str]) -> set[int]:
    if v is None:
        return set()
    if isinstance(v, str):
        parts = [p for p in re.split(r"[,;/\s]+(?=[A-Ja-j0-9]\b)|,|;", v) if p.strip()]
        if len(parts) <= 1 and re.fullmatch(r"[A-Ja-j]{2,}", v.strip()):
            parts = list(v.strip())
    elif isinstance(v, (list, tuple, set)):
        parts = list(v)
    else:
        parts = [v]
    out = set()
    for p in parts:
        i = resolve_index(p, opts)
        if i is not None:
            out.add(i)
    return out


def mcq_answer(q: dict, opts: list[str], flagged: set[int]) -> int | None:
    if len(flagged) == 1:
        return next(iter(flagged))
    for key in ("answer_index", "correct_index", "correctIndex", "answerIndex", "correct_option", "correct_answer",
                "correctAnswer", "answer", "correct", "dogru_cevap", "cevap"):
        if key in q:
            i = resolve_index(q[key], opts)
            if i is not None:
                return i
    return None


def multi_answer(q: dict, opts: list[str], flagged: set[int]) -> set[int]:
    if flagged:
        return flagged
    for key in ("correct_indices", "correctIndices", "answer_indices", "answers", "correct_answers",
                "correctAnswers", "correct", "answer", "dogru_cevaplar"):
        if key in q:
            got = resolve_indices(q[key], opts)
            if got:
                return got
    return set()


def items(data, *keys) -> list:
    """{"questions": [...]} ya da doğrudan liste — hangisi gelirse listeyi döndürür."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            v = data.get(k)
            if isinstance(v, list):
                return v
        for v in data.values():          # tek anahtarlı sarmalayıcılar: {"quiz": {"questions": [...]}}
            if isinstance(v, dict):
                inner = items(v, *keys)
                if inner:
                    return inner
    return []


def pairs(q: dict) -> list[tuple[str, str]]:
    """Eşleştirme çiftlerini farklı biçimlerden okur: [{left,right}], {sol: sağ}, ya da paralel listeler."""
    raw = first(q, "pairs", "matches", "matching", "eslestirmeler", "items")
    out: list[tuple[str, str]] = []
    if isinstance(raw, dict):
        out = [(text(k), text(v)) for k, v in raw.items()]
    elif isinstance(raw, list):
        for it in raw:
            if isinstance(it, dict):
                l = text(first(it, "left", "term", "item", "sol", "a", "question", "key"))
                r = text(first(it, "right", "match", "definition", "sag", "sağ", "b", "answer", "value"))
                out.append((l, r))
            elif isinstance(it, (list, tuple)) and len(it) >= 2:
                out.append((text(it[0]), text(it[1])))
    if not out:
        lefts = first(q, "left", "left_items", "terms", "sol")
        rights = first(q, "right", "right_items", "definitions", "sag")
        if isinstance(lefts, list) and isinstance(rights, list) and len(lefts) == len(rights):
            out = [(text(a), text(b)) for a, b in zip(lefts, rights)]
    out = [(clean_option(l), clean_option(r)) for l, r in out if text(l) and text(r)]
    seen_l, seen_r, uniq = set(), set(), []
    for l, r in out:
        if l.lower() in seen_l or r.lower() in seen_r:
            continue
        seen_l.add(l.lower())
        seen_r.add(r.lower())
        uniq.append((l, r))
    return uniq[:6]


# ------------------------------------------------------------------ boşluk doldurma
BLANK = "____"
_BLANK_RE = re.compile(r"_{3,}|\[\s*(?:\d+|boşluk|bosluk|blank|___)\s*\]|\(\s*\d+\s*\)_*|…{2,}|\.{5,}",
                       re.IGNORECASE)


def blanks(q: dict) -> tuple[str, list[dict]]:
    """Boşluklu cümleyi '____' biçimine getirir ve [{answer, alternatives}] listesi döndürür."""
    sent = text(first(q, "question", "sentence", "text", "stem", "soru", "cumle", "cümle"))
    raw = first(q, "blanks", "answers", "answer", "gaps", "cevaplar", "cevap", "bosluklar", "boşluklar")
    if isinstance(raw, (str, int, float)):
        raw = [raw]
    if isinstance(raw, dict):
        raw = [raw[k] for k in sorted(raw, key=lambda k: str(k))]
    out: list[dict] = []
    for b in raw or []:
        if isinstance(b, dict):
            ans = text(first(b, "answer", "text", "correct", "value", "cevap", "word"))
            alts = str_list(first(b, "alternatives", "accept", "accepted", "synonyms", "alternatifler", default=[]))
        else:
            ans, alts = text(b), []
            if "/" in ans and len(ans) < 80:
                parts = [p.strip() for p in ans.split("/") if p.strip()]
                ans, alts = parts[0], parts[1:]
        if ans:
            out.append({"answer": ans, "alternatives": [a for a in alts if a and a != ans][:6]})
    sent = _BLANK_RE.sub(BLANK, sent)
    n = sent.count(BLANK)
    if n == 0 and len(out) == 1:                    # boşluk işaretlenmemiş: cevabı cümlede bul ve boşalt
        m = re.search(re.escape(out[0]["answer"]), sent, re.IGNORECASE)
        if m:
            sent = sent[:m.start()] + BLANK + sent[m.end():]
            n = 1
    if n == 0 or n != len(out):
        return sent, []
    return sent, out


_FOLD = str.maketrans("çğıöşüâîûÇĞİIÖŞÜ", "cgiosuaiucgiiosu")


def fold(s: str) -> str:
    """Karşılaştırma için: küçük harf, Türkçe karakterleri sadeleştir, noktalama/boşluk at."""
    s = unicodedata.normalize("NFKC", str(s)).replace("İ", "i").replace("I", "ı").lower().translate(_FOLD)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"\([^)]*\)", " ", s)              # parantez içi açıklamalar
    return re.sub(r"[^a-z0-9%.,]+", "", s).strip(".,")


def _lev(a: str, b: str, cap: int = 3) -> int:
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def blank_ok(user: str, blank: dict) -> bool:
    """Yazım hatası, büyük/küçük harf, Türkçe karakter ve eş anlamlılara karşı hoşgörülü kontrol."""
    u = fold(user)
    if not u:
        return False
    for target in [blank["answer"], *blank.get("alternatives", [])]:
        t = fold(target)
        if not t:
            continue
        if u == t:
            return True
        if any(ch.isdigit() for ch in t):         # sayılarda yazım toleransı yok
            continue
        tol = 0 if len(t) <= 3 else 1 if len(t) <= 7 else 2
        if _lev(u, t, tol) <= tol:
            return True
    return False
