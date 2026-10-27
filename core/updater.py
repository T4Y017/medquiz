"""Uygulama içi güncelleme: GitHub Releases'tan yeni sürümü bulur, indirir, doğrular ve kurar.

Akış:
  1. check()      → https://api.github.com/repos/<repo>/releases/latest (anahtar gerekmez; depo herkese açık olmalı)
  2. download()   → kurulum dosyasını geçici klasöre indirir, boyut + SHA-256 (sürüm notundaki) doğrular
  3. install()    → Inno Setup kurulumunu sessiz modda başlatır; uygulama kapanır, kurulum bitince yeniden açılır
Kullanıcı verileri (%APPDATA%\\MedQuiz) kurulumdan etkilenmez.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .version import INSTALLER_ASSET, UPDATE_REPO, __version__

log = logging.getLogger(__name__)
API = "https://api.github.com/repos/{repo}/releases/latest"
UA = {"User-Agent": f"MedQuiz/{__version__}", "Accept": "application/vnd.github+json"}


class UpdateError(Exception):
    pass


@dataclass
class UpdateInfo:
    version: str
    url: str
    size: int
    sha256: str
    notes: str
    page: str
    alt_url: str = ""


def parse_version(v: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:4]) or (0,)


def is_newer(remote: str, local: str = __version__) -> bool:
    return parse_version(remote) > parse_version(local)


def can_self_install() -> bool:
    """Yalnızca paketlenmiş Windows sürümünde kendini güncelleyebilir (geliştirme ortamında değil)."""
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


def _clean_notes(body: str) -> str:
    body = re.sub(r"\n?---\s*\nsha256:.*", "", body or "", flags=re.S | re.I)
    body = re.sub(r"\n(Co-Authored-By|Claude-Session):.*", "", body)
    return body.strip()


def check(repo: str = UPDATE_REPO, timeout: float = 10.0, get=None) -> UpdateInfo | None:
    """Daha yeni bir sürüm varsa bilgisini, yoksa None döndürür."""
    import requests

    get = get or requests.get
    try:
        r = get(API.format(repo=repo), headers=UA, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        raise UpdateError(f"Güncelleme sunucusuna ulaşılamadı: {e}") from e
    if r.status_code == 404:
        return None                                    # henüz yayın yok ya da depo gizli
    if r.status_code == 403:
        raise UpdateError("GitHub geçici olarak istek sınırına ulaştı; daha sonra tekrar denenecek.")
    if r.status_code != 200:
        raise UpdateError(f"Güncelleme bilgisi alınamadı (HTTP {r.status_code}).")
    data = r.json()
    tag = str(data.get("tag_name", ""))
    if not tag or not is_newer(tag):
        return None
    asset = next((a for a in data.get("assets", []) if a.get("name") == INSTALLER_ASSET), None)
    if not asset:
        return None
    body = data.get("body") or ""
    m = re.search(r"sha256:\s*([0-9a-fA-F]{64})", body)
    return UpdateInfo(
        version=tag.lstrip("vV"), url=asset.get("url") or asset["browser_download_url"],
        size=int(asset.get("size") or 0), sha256=m.group(1).lower() if m else "", notes=_clean_notes(body),
        page=data.get("html_url", ""), alt_url=asset.get("browser_download_url", ""),
    )


def download(info: UpdateInfo, progress: Callable[[int, int], None] | None = None,
             should_stop: Callable[[], bool] | None = None, get=None) -> Path:
    import requests

    get = get or requests.get
    dest_dir = Path(tempfile.gettempdir()) / "MedQuizUpdate"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"MedQuiz_Kurulum_{info.version}.exe"
    part = dest.with_suffix(".part")
    urls = [u for u in (info.url, info.alt_url) if u]
    last: UpdateError | None = None
    for url in urls:
        try:
            return _download_one(url, info, dest, part, progress, should_stop, get)
        except UpdateError as e:
            last = e
            if "iptal" in str(e) or "SHA-256" in str(e):
                raise
            log.warning("İndirme %s başarısız: %s", url, e)
    raise last or UpdateError("İndirme başarısız.")


def _download_one(url, info, dest, part, progress, should_stop, get) -> Path:
    h = hashlib.sha256()
    done = 0
    headers = {"User-Agent": UA["User-Agent"]}
    if "api.github.com" in url:
        headers["Accept"] = "application/octet-stream"      # API varlık adresi → dosyanın kendisi
    try:
        with get(url, headers=headers, stream=True, timeout=30) as r:
            if r.status_code != 200:
                raise UpdateError(f"İndirme başarısız (HTTP {r.status_code}).")
            total = int(r.headers.get("content-length") or info.size or 0)
            with open(part, "wb") as f:
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    if should_stop and should_stop():
                        raise UpdateError("İndirme iptal edildi.")
                    if not chunk:
                        continue
                    f.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
    except UpdateError:
        part.unlink(missing_ok=True)
        raise
    except Exception as e:  # noqa: BLE001
        part.unlink(missing_ok=True)
        raise UpdateError(f"İndirme yarıda kesildi: {e}") from e
    if info.size and done != info.size:
        part.unlink(missing_ok=True)
        raise UpdateError("İndirilen dosya eksik; lütfen tekrar deneyin.")
    if info.sha256 and h.hexdigest() != info.sha256:
        part.unlink(missing_ok=True)
        raise UpdateError("İndirilen dosya doğrulanamadı (SHA-256 uyuşmuyor); güvenlik için kurulmadı.")
    part.replace(dest)
    return dest


def install(installer: Path) -> None:
    """Kurulumu sessiz modda başlatır. Çağıran taraf hemen ardından uygulamayı kapatmalıdır."""
    if not can_self_install():
        raise UpdateError("Otomatik kurulum yalnızca Windows'ta, paketlenmiş uygulamada çalışır.")
    flags = 0x00000008 | 0x00000200          # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([str(installer), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS",
                      "/FORCECLOSEAPPLICATIONS"], creationflags=flags, close_fds=True, cwd=str(installer.parent))
    log.info("Güncelleme kurulumu başlatıldı: %s", installer)


def cleanup_old_downloads() -> None:
    d = Path(tempfile.gettempdir()) / "MedQuizUpdate"
    if d.exists():
        for f in d.glob("MedQuiz_Kurulum_*"):
            try:
                os.remove(f)          # kurulmuş ya da yarım kalmış eski indirmeler
            except OSError:
                pass
