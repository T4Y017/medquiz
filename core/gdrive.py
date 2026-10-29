"""Google ile giriş (OAuth, masaüstü akışı) ve Google Drive'da uygulamaya özel gizli alan (appDataFolder).

appDataFolder kullanıcının Drive'ında görünmez; yalnızca MedQuiz (bu OAuth istemcisi) erişebilir. Kullanılan
izinler Google'ın 'hassas olmayan' kapsamlarıdır (drive.appdata, openid, email): doğrulama gerekmez.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import logging
import os
import secrets
import socketserver
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

import requests

log = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
DRIVE = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3"
SCOPES = "openid email https://www.googleapis.com/auth/drive.appdata"


class GoogleError(Exception):
    pass


def client_config() -> tuple[str, str]:
    """İstemci kimliği: derleme sırasında core/google_client.py'ye yazılır (GitHub sırları) ya da ortam değişkeni."""
    cid = os.environ.get("MEDQUIZ_GOOGLE_CLIENT_ID", "")
    sec = os.environ.get("MEDQUIZ_GOOGLE_CLIENT_SECRET", "")
    if not cid:
        try:
            from . import google_client
            cid, sec = google_client.CLIENT_ID, google_client.CLIENT_SECRET
        except ImportError:
            pass
    return cid.strip(), sec.strip()


def available() -> bool:
    return bool(client_config()[0])


# ------------------------------------------------------------------ giriş
_DONE_HTML = """<!doctype html><meta charset="utf-8"><title>MedQuiz</title>
<body style="font-family:Segoe UI,Arial;background:#f0fdfa;display:flex;align-items:center;justify-content:center;
height:100vh;margin:0"><div style="background:white;padding:32px 40px;border-radius:14px;box-shadow:0 4px 20px #0001">
<h2 style="color:#0f766e;margin-top:0">{title}</h2><p>{msg}</p></div></body>"""


def sign_in(open_browser=webbrowser.open, timeout: float = 300, cancelled=lambda: False) -> dict:
    """Tarayıcıda Google hesabı seçilir; yerel bir adrese dönen kodla belirteçler alınır."""
    cid, sec = client_config()
    if not cid:
        raise GoogleError("Bu sürümde Google ile giriş henüz etkin değil (istemci kimliği yok).")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    result: dict = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" not in q and "error" not in q:
                self.send_response(404)
                self.end_headers()
                return
            if q.get("state", [""])[0] != state:
                result["error"] = "state"
            elif "error" in q:
                result["error"] = q["error"][0]
            else:
                result["code"] = q["code"][0]
            ok = "code" in result
            body = _DONE_HTML.format(
                title="✓ Giriş tamamlandı" if ok else "Giriş yapılmadı",
                msg="MedQuiz'e dönebilirsin; bu sekmeyi kapatabilirsin." if ok else
                "İzin verilmedi. MedQuiz'e dönüp tekrar deneyebilirsin.").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = socketserver.TCPServer(("127.0.0.1", 0), H)
    srv.timeout = 0.5
    redirect = f"http://127.0.0.1:{srv.server_address[1]}"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": cid, "redirect_uri": redirect, "response_type": "code", "scope": SCOPES,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
        "access_type": "offline", "prompt": "consent select_account"})
    try:
        open_browser(url)
        end = time.time() + timeout
        while not result and time.time() < end and not cancelled():
            srv.handle_request()
    finally:
        srv.server_close()
    if cancelled():
        raise GoogleError("Giriş iptal edildi.")
    if "code" not in result:
        raise GoogleError("Google girişi tamamlanmadı" + (f" ({result['error']})." if result.get("error") else
                                                          " (süre doldu)."))
    r = requests.post(TOKEN_URL, data={"code": result["code"], "client_id": cid, "client_secret": sec,
                                       "redirect_uri": redirect, "grant_type": "authorization_code",
                                       "code_verifier": verifier}, timeout=30)
    if r.status_code != 200:
        raise GoogleError(f"Google belirteci alınamadı: {r.text[:200]}")
    tok = r.json()
    if not tok.get("refresh_token"):
        raise GoogleError("Google uzun süreli oturum vermedi; lütfen tekrar deneyin.")
    email = ""
    try:
        payload = tok["id_token"].split(".")[1]
        email = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["email"]
    except (KeyError, IndexError, ValueError):
        pass
    return {"refresh_token": tok["refresh_token"], "access_token": tok.get("access_token", ""),
            "expires_at": time.time() + float(tok.get("expires_in", 3000)) - 60, "email": email}


def revoke(refresh_token: str) -> None:
    try:
        requests.post(REVOKE_URL, params={"token": refresh_token}, timeout=10)
    except requests.RequestException:
        pass


class GoogleAuth:
    def __init__(self, refresh_token: str):
        self.refresh_token = refresh_token
        self.access = ""
        self.expires = 0.0
        self._lock = threading.Lock()

    def token(self) -> str:
        with self._lock:
            if self.access and time.time() < self.expires:
                return self.access
            cid, sec = client_config()
            try:
                r = requests.post(TOKEN_URL, data={"client_id": cid, "client_secret": sec,
                                                   "refresh_token": self.refresh_token,
                                                   "grant_type": "refresh_token"}, timeout=30)
            except requests.RequestException as e:
                raise GoogleError(f"Google'a ulaşılamadı (internet bağlantısı?): {type(e).__name__}") from e
            if r.status_code in (400, 401):
                raise GoogleError("Google oturumu sona ermiş. Ayarlar → Hesap → “Google ile giriş yap” ile "
                                  "tekrar giriş yap.")
            if r.status_code != 200:
                raise GoogleError(f"Google oturumu yenilenemedi ({r.status_code}).")
            d = r.json()
            self.access = d["access_token"]
            self.expires = time.time() + float(d.get("expires_in", 3000)) - 60
            return self.access


# ------------------------------------------------------------------ Drive deposu
class DriveStore:
    """Dosya adı → içerik deposu; Drive'ın appDataFolder'ında. Ad başına tek dosya tutulur."""
    SIMPLE_LIMIT = 4 * 1024 * 1024

    def __init__(self, auth: GoogleAuth):
        self.auth = auth
        self._index: dict[str, dict] | None = None
        self.s = requests.Session()

    def _req(self, method: str, url: str, **kw) -> requests.Response:
        base_headers = kw.pop("headers", {}) or {}
        timeout = kw.pop("timeout", 120)
        for attempt in range(4):
            headers = dict(base_headers, Authorization=f"Bearer {self.auth.token()}")
            try:
                r = self.s.request(method, url, headers=headers, timeout=timeout, **kw)
            except requests.RequestException as e:
                if attempt == 3:
                    raise GoogleError(f"Google Drive'a ulaşılamadı: {type(e).__name__}") from e
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 401 and attempt == 0:
                self.auth.access = ""
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 403 and "storageQuotaExceeded" in r.text:
                raise GoogleError("Google Drive depolama alanın dolu.")
            if r.status_code >= 400:
                raise GoogleError(f"Google Drive hatası {r.status_code}: {r.text[:200]}")
            return r
        raise GoogleError("Google Drive yanıt vermedi.")

    def index(self, refresh: bool = False) -> dict[str, dict]:
        if self._index is not None and not refresh:
            return self._index
        out: dict[str, dict] = {}
        page = None
        while True:
            params = {"spaces": "appDataFolder", "pageSize": 1000,
                      "fields": "nextPageToken, files(id, name, modifiedTime, size)"}
            if page:
                params["pageToken"] = page
            d = self._req("GET", f"{DRIVE}/files", params=params).json()
            for f in d.get("files", []):
                ts = _ts(f.get("modifiedTime"))
                cur = out.get(f["name"])
                if cur is None or ts > cur["mtime"]:
                    out[f["name"]] = {"id": f["id"], "mtime": ts, "size": int(f.get("size") or 0)}
            page = d.get("nextPageToken")
            if not page:
                break
        self._index = out
        return out

    # --- arayüz (FolderStore ile aynı)
    def list(self, prefix: str) -> list[tuple[str, float]]:
        return [(n, m["mtime"]) for n, m in self.index().items() if n.startswith(prefix)]

    def exists(self, name: str) -> bool:
        return name in self.index()

    def read(self, name: str) -> bytes:
        m = self.index().get(name)
        if not m:
            raise FileNotFoundError(name)
        return self._req("GET", f"{DRIVE}/files/{m['id']}", params={"alt": "media"}, timeout=600).content

    def write(self, name: str, data: bytes) -> None:
        m = self.index().get(name)
        if m:
            self._upload(data, file_id=m["id"])
        else:
            fid = self._upload(data, name=name)
            self.index()[name] = {"id": fid, "mtime": time.time(), "size": len(data)}

    def download(self, name: str, dest: Path) -> None:
        dest.write_bytes(self.read(name))

    def upload_file(self, name: str, path: Path) -> None:
        self.write(name, Path(path).read_bytes())

    def _upload(self, data: bytes, name: str | None = None, file_id: str | None = None) -> str:
        meta = {} if file_id else {"name": name, "parents": ["appDataFolder"]}
        if len(data) <= self.SIMPLE_LIMIT:
            boundary = "medquiz" + secrets.token_hex(8)
            body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(meta)}\r\n"
                    f"--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n").encode() + data + \
                f"\r\n--{boundary}--".encode()
            url = f"{UPLOAD}/files/{file_id}" if file_id else f"{UPLOAD}/files"
            r = self._req("PATCH" if file_id else "POST", url, params={"uploadType": "multipart", "fields": "id"},
                          data=body, headers={"Content-Type": f"multipart/related; boundary={boundary}"})
            return r.json()["id"]
        url = f"{UPLOAD}/files/{file_id}" if file_id else f"{UPLOAD}/files"
        r = self._req("PATCH" if file_id else "POST", url, params={"uploadType": "resumable", "fields": "id"},
                      json=meta, headers={"X-Upload-Content-Type": "application/octet-stream",
                                          "X-Upload-Content-Length": str(len(data))})
        loc = r.headers["Location"]
        r2 = self._req("PUT", loc, data=data, headers={"Content-Type": "application/octet-stream"}, timeout=1800)
        return r2.json()["id"]

    def usage(self) -> int:
        return sum(m["size"] for m in self.index().values())


def _ts(s: str | None) -> float:
    if not s:
        return 0.0
    import datetime as dt
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0
