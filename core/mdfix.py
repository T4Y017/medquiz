"""Yapay zekâ çıktısındaki Markdown tablolarını onarır, böylece tablolar ekranda ve PDF'te gerçekten tablo olur.

Uygulamanın Markdown görüntüleyicisi (Qt / md4c) katıdır: tablo bir paragrafın hemen altında başlarsa
("**Tablo 1: ...**" satırından sonra boş satır yoksa), bir madde işaretinin içinde kalırsa, ``` kod bloğuna
sarılırsa ya da ayırıcı satırı (|---|) eksikse tablo yerine düz metin gösterir. Modeller bunları sık yapar.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

_SEP_CELL = re.compile(r"^\s*:?-{1,}:?\s*$")
_CAPTION = re.compile(r"\b(?:Tablo|Table|Tab\.|Çizelge)\s*(\d{1,3}(?:[.\-]\d{1,3})?)", re.IGNORECASE)


def _cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip() for c in re.split(r"(?<!\\)\|", s)]


def _is_row(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.count("|") >= 2


def _is_sep(line: str) -> bool:
    if "-" not in line or "|" not in line:
        return False
    cells = [c for c in _cells(line)]
    return bool(cells) and all(_SEP_CELL.match(c) for c in cells if c != "") and any(c for c in cells)


# ------------------------------------------------------------------ HTML <table> → Markdown
class _HT(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _html_tables(md: str) -> str:
    def conv(m):
        p = _HT()
        p.feed(m.group(0))
        return "\n\n" + rows_to_markdown(p.rows[0], p.rows[1:]) + "\n\n" if p.rows else m.group(0)
    return re.sub(r"<table[\s\S]*?</table>", conv, md, flags=re.IGNORECASE)


def _esc(c) -> str:
    return " ".join(str(c if c is not None else "").replace("|", "/").split())


def rows_to_markdown(headers: list, rows: list[list]) -> str:
    n = max([len(headers)] + [len(r) for r in rows]) if (headers or rows) else 0
    if n == 0:
        return ""
    h = [_esc(x) for x in headers] + [""] * (n - len(headers))
    out = ["| " + " | ".join(x or " " for x in h) + " |", "|" + "|".join(["---"] * n) + "|"]
    for r in rows:
        r = [_esc(x) for x in r] + [""] * (n - len(r))
        out.append("| " + " | ".join(r[:n]) + " |")
    return "\n".join(out)


# ------------------------------------------------------------------ ana onarım
def normalize_markdown(md: str) -> str:
    if not md:
        return md
    md = md.replace("\r\n", "\n")
    md = _html_tables(md)
    # Tablo ya da Markdown içeren kod bloklarını aç (```markdown ... ``` / ```...``` içinde tablo)
    def unfence(m):
        body = m.group(2)
        lang = (m.group(1) or "").strip().lower()
        if lang in ("markdown", "md", "") and any(_is_row(l) for l in body.splitlines()):
            return "\n" + body.strip("\n") + "\n"
        return m.group(0)
    md = re.sub(r"```[ \t]*([A-Za-z]*)[ \t]*\n([\s\S]*?)\n[ \t]*```", unfence, md)
    # Tüm yanıt tek bir ```markdown bloğuna sarılmış ve kapanmamışsa
    md = re.sub(r"^\s*```(?:markdown|md)?[ \t]*\n", "", md, count=1, flags=re.IGNORECASE)

    lines = md.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # madde işareti içinde ya da girintili tablo satırı: "- | a | b |" / "   | a |"
        stripped = re.sub(r"^\s*(?:[-*+]\s+)?(?=\|)", "", line) if re.match(r"^\s*(?:[-*+]\s+)?\|", line) else line
        if _is_row(stripped):
            block = []
            while i < len(lines):
                l2 = re.sub(r"^\s*(?:[-*+]\s+)?(?=\|)", "", lines[i]) \
                    if re.match(r"^\s*(?:[-*+]\s+)?\|", lines[i]) else lines[i]
                if _is_row(l2) or (_is_sep(l2) and block):
                    block.append(l2.strip())
                    i += 1
                else:
                    break
            if len(block) >= 2 or (len(block) == 1 and not _is_sep(block[0])):
                out.extend(_fix_block(block, out))
                continue
            out.extend(block)
            continue
        out.append(line)
        i += 1
    text = "\n".join(out)
    return re.sub(r"\n{4,}", "\n\n\n", text)


def _fix_block(block: list[str], out: list[str]) -> list[str]:
    rows = [b for b in block if not _is_sep(b)]
    if len(rows) < 2 and len(block) < 2:          # tek satırlık "tablo" → düz metin olarak bırak
        return block
    header = _cells(rows[0])
    body = [_cells(r) for r in rows[1:]]
    n = max([len(header)] + [len(r) for r in body])
    res = []
    if out and out[-1].strip():
        res.append("")                              # tablodan önce boş satır şart
    res.append(rows_to_markdown(header, body) if n > 1 else "\n".join(block))
    res.append("")
    return res


def count_tables(md: str) -> int:
    lines = md.split("\n")
    return sum(1 for i in range(1, len(lines)) if _is_sep(lines[i]) and _is_row(lines[i - 1]))


def table_numbers_in(md: str) -> set[str]:
    """Çıktıda tablo olarak verilmiş 'Tablo N' başlıkları (tablonun hemen üstündeki 3 satırda geçenler)."""
    lines = md.split("\n")
    found = set()
    for i in range(1, len(lines)):
        if _is_sep(lines[i]) and _is_row(lines[i - 1]):
            for back in lines[max(0, i - 5):i - 1]:
                for m in _CAPTION.finditer(back):
                    found.add(m.group(1))
    return found


def captions_in_source(text: str) -> list[tuple[str, str]]:
    """Kaynak metindeki tablo başlıkları: [(numara, başlık satırı)], ilk geçiş sırasıyla, tekrarsız."""
    seen, out = set(), []
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r"^(?:Tablo|Table|Tab\.|Çizelge)\s*(\d{1,3}(?:[.\-]\d{1,3})?)\s*[.:\-–]?\s*(.*)$", s,
                     re.IGNORECASE)
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            out.append((m.group(1), s[:140]))
    return out
