"""Uygulama içinden şikayet / istek: GitHub'da (geliştiricinin deposunda) 'geri-bildirim' etiketli bir konu açılır.

Gönderim, kullanıcının kendi GitHub oturumuyla tarayıcıda yapılır (uygulamanın içinde parola ya da anahtar yok).
Her kayıtta görünmez bir işaret (marker) bulunur; uygulama bu işaretle konuyu bulup durumunu ve geliştiricinin
cevabını gösterir. Depo herkese açık olduğundan gönderilen metin ve tanılama bilgisi herkes tarafından görülebilir:
tanılama yalnızca sürüm, sistem ve hata satırlarını içerir; API anahtarları, e-postalar ve belge adları çıkarılır.
"""
from __future__ import annotations

import platform
import re
import urllib.parse
import uuid

import requests

from .config import data_dir
from .diagnostics import redact
from .version import UPDATE_REPO, __version__

LABEL = "geri-bildirim"
KINDS = {"hata": "🐞 Hata / şikayet", "istek": "✨ Yeni özellik isteği", "oneri": "💡 İyileştirme önerisi"}
AREAS = ["Genel", "Quiz", "Vaka", "Özet", "Kartlar", "Sohbet", "Kütüphane", "Kelimeler", "İstatistik", "Ayarlar",
         "Hesap / eşitleme", "Güncelleme"]
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PATH = re.compile(r"[A-Za-z]:\\\\?[^\s\"']+|/(?:home|Users)/[^\s\"']+")
MAX_URL = 7000


def new_marker() -> str:
    return uuid.uuid4().hex[:12]


def diagnostics(ctx) -> str:
    """Herkese açık konuya eklenecek kısa tanılama: kişisel/gizli bilgi içermez."""
    s = ctx.settings
    lines = [f"Sürüm: {__version__}", f"Sistem: {platform.system()} {platform.release()}",
             f"Yapay zekâ: {s.provider} ({s.model_for(s.provider)}), yedek {'açık' if s.fallback_enabled else 'kapalı'}"]
    log = data_dir() / "medquiz.log"
    if log.exists():
        try:
            tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
        except OSError:
            tail = []
        errs = [ln for ln in tail if " ERROR " in ln or " WARNING " in ln or "Traceback" in ln or "Error" in ln][-15:]
        if errs:
            lines.append("Son uyarı/hata satırları:")
            lines += [ln[:300] for ln in errs]
    return clean("\n".join(lines))


def clean(text: str) -> str:
    text = redact(text)
    text = _EMAIL.sub("[e-posta]", text)
    return _PATH.sub("[dosya yolu]", text)


def issue_title(kind: str, area: str, text: str) -> str:
    first = " ".join(text.split())[:70]
    return f"[{KINDS.get(kind, kind).split(' ', 1)[1]}] {area}: {first}"


def issue_body(kind: str, area: str, text: str, diag: str, marker: str) -> str:
    body = (f"**Tür:** {KINDS.get(kind, kind)}\n**Bölüm:** {area}\n\n### Açıklama\n\n{text.strip()}\n\n")
    if diag:
        body += f"<details><summary>Tanılama</summary>\n\n```\n{diag}\n```\n</details>\n\n"
    body += f"<!-- medquiz-feedback:{marker} v{__version__} -->"
    return body


def new_issue_url(kind: str, area: str, text: str, diag: str, marker: str) -> str:
    base = f"https://github.com/{UPDATE_REPO}/issues/new?"
    title = issue_title(kind, area, text)
    body = issue_body(kind, area, text, diag, marker)
    url = base + urllib.parse.urlencode({"title": title, "body": body, "labels": LABEL})
    if len(url) > MAX_URL:        # tarayıcı adres sınırı: önce tanılamayı, sonra metni kısalt
        body = issue_body(kind, area, text[:3000], "", marker)
        url = base + urllib.parse.urlencode({"title": title, "body": body, "labels": LABEL})
    return url


def fetch_statuses(markers: list[str], timeout: float = 15) -> dict[str, dict]:
    """İşaretlere göre GitHub'daki konuları bul: {marker: {number, state, reply, url}} (kimlik doğrulamasız)."""
    out: dict[str, dict] = {}
    if not markers:
        return out
    r = requests.get(f"https://api.github.com/repos/{UPDATE_REPO}/issues",
                     params={"labels": LABEL, "state": "all", "per_page": 100},
                     headers={"Accept": "application/vnd.github+json"}, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"GitHub'a ulaşılamadı ({r.status_code}).")
    want = set(markers)
    for it in r.json():
        m = re.search(r"medquiz-feedback:([0-9a-f]{12})", it.get("body") or "")
        if not m or m.group(1) not in want:
            continue
        info = {"number": it["number"], "state": it["state"], "url": it["html_url"], "reply": "",
                "labels": [lb["name"] for lb in it.get("labels", [])]}
        if it.get("comments"):
            try:
                cs = requests.get(it["comments_url"], params={"per_page": 100}, timeout=timeout).json()
                owner = UPDATE_REPO.split("/")[0].lower()
                def trusted(c):
                    u = c.get("user") or {}
                    return (u.get("login", "").lower() == owner or u.get("type") == "Bot") and \
                        "<!-- medquiz-bot -->" in (c.get("body") or "")
                own = [c for c in cs if trusted(c)]
                if own:
                    info["reply"] = re.sub(r"<!--.*?-->", "", own[-1]["body"], flags=re.S).strip()
            except (requests.RequestException, ValueError):
                pass
        out[m.group(1)] = info
    return out
