"""Taranmış (resim) PDF sayfalarını ve sayfa fotoğraflarını yapay zekâyla metne çevirme.

Sayfalar görüntüye çevrilip birkaç sayfalık gruplar hâlinde gönderilir; model metni okuma sırasıyla yazar (tablolar
satır satır). Kotayı korumak için hafif model kullanılır (task='light')."""
from __future__ import annotations

import io
import logging
import re
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

PAGES_PER_CALL = 4
MAX_PX = 1600

SYSTEM_OCR = """Sen bir OCR (metin tanıma) aracısın. Sana kitap sayfalarının görüntüleri verilir. Her sayfadaki
metni EKSİKSİZ ve OKUMA SIRASIYLA, olduğu dilde aynen yaz (çevirme, özetleme, yorum ekleme). Başlıkları ayrı satıra
yaz. Tabloları satır satır "hücre | hücre | hücre" biçiminde yaz. Şekil içindeki yazıları "[Şekil: ...]" diye kısaca
belirt. Okunamayan yerlere "[okunamadı]" yaz. Her sayfanın başına tam olarak şu satırı koy:
=== SAYFA n ===
(n, sana verilen sayfa numarasıdır). Başka hiçbir şey yazma."""


def page_image(pdf, page_no: int) -> bytes:
    page = pdf[page_no - 1]
    try:
        w, h = page.get_size()
        scale = max(1.0, min(3.0, MAX_PX / max(w, h)))
        img = page.render(scale=scale).to_pil().convert("L")
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=80, optimize=True)
        return buf.getvalue()
    finally:
        page.close()


def image_file_jpeg(path: Path) -> bytes:
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(path)).convert("L")
    if max(img.size) > MAX_PX:
        img.thumbnail((MAX_PX, MAX_PX))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=82, optimize=True)
    return buf.getvalue()


def _parse(text: str, pages: list[int]) -> dict[int, str]:
    out: dict[int, str] = {}
    parts = re.split(r"^\s*=+\s*SAYFA\s+(\d+)\s*=+\s*$", text, flags=re.M | re.I)
    if len(parts) >= 3:
        for i in range(1, len(parts) - 1, 2):
            try:
                n = int(parts[i])
            except ValueError:
                continue
            body = parts[i + 1].strip()
            if n in pages and body:
                out[n] = body
    elif len(pages) == 1 and text.strip():
        out[pages[0]] = text.strip()
    return out


def ocr_images(llm, images: list[tuple[int, bytes]]) -> dict[int, str]:
    """[(sayfa no, jpeg)] → {sayfa no: metin}."""
    parts: list[dict] = []
    for n, data in images:
        parts.append({"type": "text", "text": f"Sayfa {n}:"})
        parts.append({"type": "image", "data": data, "mime": "image/jpeg"})
    parts.append({"type": "text", "text": "Bu sayfaların metnini yaz."})
    text = llm.complete(SYSTEM_OCR, [{"role": "user", "content": parts}], max_tokens=16000, temperature=0)
    return _parse(text, [n for n, _ in images])


def ocr_pdf(path: str, pages: list[int], llm_factory, progress: Callable[[int, int], None] | None = None,
            should_stop: Callable[[], bool] | None = None) -> dict[int, str]:
    import pypdfium2 as pdfium

    from .quiz import make_llm
    pdf = pdfium.PdfDocument(path)
    out: dict[int, str] = {}
    try:
        for i in range(0, len(pages), PAGES_PER_CALL):
            if should_stop and should_stop():
                break
            group = pages[i:i + PAGES_PER_CALL]
            imgs = [(n, page_image(pdf, n)) for n in group]
            got = ocr_images(make_llm(llm_factory, "light"), imgs)
            missing = [n for n in group if n not in got]
            if missing and len(group) > 1:           # grup cevabı bölünemediyse tek tek dene
                for n in missing:
                    got.update(ocr_images(make_llm(llm_factory, "light"), [(n, dict(imgs)[n])]))
            out.update(got)
            if progress:
                progress(min(len(pages), i + len(group)), len(pages))
    finally:
        pdf.close()
    return out


def ocr_doc(doc, llm_factory, progress=None, should_stop=None) -> dict[int, str]:
    """NeedsOCR ile gelen belgenin metinsiz sayfalarını okur."""
    from .quiz import make_llm
    if doc.kind == "image":
        got = ocr_images(make_llm(llm_factory, "light"), [(1, image_file_jpeg(Path(doc.source)))])
        if progress:
            progress(1, 1)
        return got
    return ocr_pdf(doc.source, doc.empty_pages, llm_factory, progress, should_stop)


def estimate_calls(n_pages: int) -> int:
    return max(1, -(-n_pages // PAGES_PER_CALL))
