"""PDF'lerdeki görüntüleri (röntgen, EKG, histoloji, klinik fotoğraf, çizim…) bulup kesme — şekilli sorular için.

Görüntüler PDF'in içine gömülü resim nesnelerinden bulunur; bitişik paneller tek şekil olarak birleştirilir.
Şeklin altındaki/üstündeki başlık (cevabı ele verebilir) KESİLMEZ; yalnızca görüntünün kendisi alınır.
Sayfanın tamamını kaplayan resimler (taranmış sayfa) ve küçük süsler (logo, ikon) atlanır.
"""
from __future__ import annotations

import io
import logging
import random
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

MIN_SIDE_PT = 70           # en küçük kenar (pt); daha küçükler ikon/logo
MIN_AREA_FRAC = 0.025      # sayfa alanının en az %2,5'i
MAX_AREA_FRAC = 0.85       # bundan büyüğü taranmış sayfa
MERGE_GAP_PT = 14          # bu kadar yakın resimler aynı şeklin panelleri
MAX_PX = 1100              # kesilen görüntünün en uzun kenarı (piksel)
_CAPTION = re.compile(r"(?:Şekil|Sekil|Resim|Figür|Figure|Fig\.|Görüntü)\s*\d+[\w.\-]*", re.IGNORECASE)


@dataclass
class Figure:
    doc_id: int
    page: int                      # 1 tabanlı
    box: tuple[float, float, float, float]   # left, bottom, right, top (pt)
    page_size: tuple[float, float]
    caption: str = ""
    context: str = ""
    image: bytes = b""            # JPEG

    @property
    def key(self) -> str:
        return f"{self.doc_id}:{self.page}:{int(self.box[0])}:{int(self.box[1])}"


def _merge(boxes: list[list[float]]) -> list[list[float]]:
    changed = True
    while changed:
        changed = False
        out: list[list[float]] = []
        for b in boxes:
            for o in out:
                if (b[0] <= o[2] + MERGE_GAP_PT and o[0] <= b[2] + MERGE_GAP_PT and
                        b[1] <= o[3] + MERGE_GAP_PT and o[1] <= b[3] + MERGE_GAP_PT):
                    o[0], o[1], o[2], o[3] = min(o[0], b[0]), min(o[1], b[1]), max(o[2], b[2]), max(o[3], b[3])
                    changed = True
                    break
            else:
                out.append(list(b))
        boxes = out
    return boxes


def page_figures(pdf, page_no: int, doc_id: int = 0) -> list[Figure]:
    """Bir sayfadaki şekil adayları (kesmeden)."""
    import pypdfium2.raw as raw
    page = pdf[page_no - 1]
    try:
        w, h = page.get_size()
        boxes = []
        for obj in page.get_objects(filter=(raw.FPDF_PAGEOBJ_IMAGE,), max_depth=4):
            try:
                l, b, r, t = (obj.get_bounds() if hasattr(obj, "get_bounds") else obj.get_pos())
            except Exception:  # noqa: BLE001
                continue
            l, b, r, t = max(0, l), max(0, b), min(w, r), min(h, t)
            if r - l > 2 and t - b > 2:
                boxes.append([l, b, r, t])
        out = []
        for l, b, r, t in _merge(boxes):
            area = (r - l) * (t - b) / (w * h)
            if min(r - l, t - b) < MIN_SIDE_PT or area < MIN_AREA_FRAC or area > MAX_AREA_FRAC:
                continue
            out.append(Figure(doc_id, page_no, (l, b, r, t), (w, h)))
        if out:
            text = page.get_textpage().get_text_range() or ""
            caps = [m.group(0) for m in _CAPTION.finditer(text)]
            for i, f in enumerate(out):
                f.caption = caps[i] if i < len(caps) else (caps[0] if caps else "")
                f.context = " ".join(text.split())[:2500]
        return out
    finally:
        page.close()


def render(pdf, fig: Figure) -> bytes:
    """Şekli yüksek çözünürlükte keser, JPEG döndürür."""
    page = pdf[fig.page - 1]
    try:
        w, h = fig.page_size
        l, b, r, t = fig.box
        pad = 3
        crop = (max(0, l - pad), max(0, b - pad), max(0, w - r - pad), max(0, h - t - pad))
        longest = max(r - l, t - b)
        scale = max(1.0, min(4.0, MAX_PX / max(1.0, longest)))
        img = page.render(scale=scale, crop=crop).to_pil().convert("RGB")
        if max(img.size) > MAX_PX:
            img.thumbnail((MAX_PX, MAX_PX))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85, optimize=True)
        return buf.getvalue()
    finally:
        page.close()


def is_blank(jpeg: bytes) -> bool:
    """Neredeyse tek renkli (boş/beyaz) görüntüleri ele."""
    from PIL import Image, ImageStat
    im = Image.open(io.BytesIO(jpeg)).convert("L")
    return ImageStat.Stat(im).stddev[0] < 6


def find_figures(docs: list[dict], n: int, pages_hint: dict[int, list[int]] | None = None,
                 skip: set[str] | None = None, rng: random.Random | None = None,
                 max_pages: int = 400) -> list[Figure]:
    """Belgelerde n şekil bul ve kes. pages_hint: {doc_id: [sayfa]} (konuyla ilgili sayfalar önce taranır)."""
    import pypdfium2 as pdfium
    rng = rng or random.Random()
    skip = skip or set()
    found: list[Figure] = []
    scanned = 0
    order = list(docs)
    rng.shuffle(order)
    for d in order:
        try:
            pdf = pdfium.PdfDocument(d["source"])
        except Exception as e:  # noqa: BLE001
            log.warning("PDF açılamadı %s: %s", d.get("source"), e)
            continue
        try:
            n_pages = len(pdf)
            hint = [p for p in (pages_hint or {}).get(d["id"], []) if 1 <= p <= n_pages]
            if pages_hint is not None:
                pages = hint                       # konu verildi: yalnızca ilgili sayfalar
            else:
                pages = list(range(1, n_pages + 1))
                rng.shuffle(pages)
            per_doc_cap = max(1, -(-n // max(1, len(order)))) + 1
            got_here = 0
            for p in pages:
                if len(found) >= n or scanned >= max_pages or got_here >= per_doc_cap * 2:
                    break
                scanned += 1
                for f in page_figures(pdf, p, d["id"]):
                    if f.key in skip or len(found) >= n:
                        continue
                    try:
                        f.image = render(pdf, f)
                    except Exception as e:  # noqa: BLE001
                        log.warning("Şekil kesilemedi s.%s: %s", p, e)
                        continue
                    if is_blank(f.image):
                        continue
                    found.append(f)
                    got_here += 1
        finally:
            pdf.close()
        if len(found) >= n:
            break
    return found
