"""Hesap bağlama: aynı kullanıcıyı (API anahtarları, ayarlar, istatistikler, kelimeler, özetler, sohbetler, vakalar ve
kütüphane) birden çok bilgisayarda kullanmak.

Sunucu yok. Eşitleme, kullanıcının zaten kullandığı bir bulut klasörü (OneDrive, Google Drive, Dropbox…) üzerinden
yapılır; dosyalar hesap şifresiyle şifrelenir (scrypt + Fernet/AES). Her bilgisayar yalnızca KENDİ dosyasını yazar
(devices/<cihaz>.mqd); diğerlerini okuyup birleştirir. Böylece iki bilgisayar aynı dosyayı aynı anda yazıp
çakışmaz. Birleştirme satır düzeyindedir (uid + değişiklik zamanı); hiçbir bilgisayarın verisi silinmez,
yalnızca kullanıcının sildiği öğeler (iz kaydıyla) her yerde silinir.

Klasör yapısı:
    <bulut>/MedQuiz Hesap/
        account.json                 şifre doğrulama (sır içermez)
        devices/<cihaz-id>.mqd        şifreli anlık görüntü (ayarlar + kullanıcı verisi)
        library/<belge-uid>.mql       şifreli belge metni (bölümler)
        library/<belge-uid>.<uzantı>  orijinal dosya (PDF/Word…; özette PDF sayfaları için)
"""
from __future__ import annotations

import base64
import gzip
import json
import logging
import os
import platform
import shutil
import socket
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from .config import Settings, data_dir

log = logging.getLogger(__name__)

FOLDER_NAME = "MedQuiz Hesap"
FORMAT = 1
MAX_LIB_FILE = 300 * 1024 * 1024
# Bilgisayara özgü, eşitlenmeyen ayarlar
LOCAL_ONLY_EXTRA = {"sync", "_saved_at", "sidebar_collapsed"}
LOCAL_ONLY_FIELDS = {"font_size"}
KEY_FIELDS = ("anthropic_api_key", "openai_api_key", "gemini_api_key")


class SyncError(Exception):
    pass


# ---------------------------------------------------------------- bulut klasörleri
def cloud_folders() -> list[tuple[str, str]]:
    """Bu bilgisayarda bulunan bulut eşitleme klasörleri: [(etiket, yol)]."""
    home = Path.home()
    cands: list[tuple[str, Path]] = []
    for env, label in (("OneDriveConsumer", "OneDrive (kişisel)"), ("OneDrive", "OneDrive"),
                       ("OneDriveCommercial", "OneDrive (iş/okul)")):
        v = os.environ.get(env)
        if v:
            cands.append((label, Path(v)))
    cands.append(("OneDrive", home / "OneDrive"))
    for root in ("G:\\", "H:\\", "I:\\"):
        for name in ("My Drive", "Drive'ım", "Benim Drive'ım"):
            cands.append(("Google Drive", Path(root) / name))
    for name in ("Google Drive", "My Drive"):
        cands.append(("Google Drive", home / name))
    try:   # Dropbox kendi yolunu info.json'da yazar
        for base in (os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA")):
            if base and (Path(base) / "Dropbox" / "info.json").exists():
                info = json.loads((Path(base) / "Dropbox" / "info.json").read_text(encoding="utf-8"))
                for v in info.values():
                    if isinstance(v, dict) and v.get("path"):
                        cands.append(("Dropbox", Path(v["path"])))
    except (OSError, ValueError):
        pass
    cands.append(("Dropbox", home / "Dropbox"))
    cands.append(("iCloud Drive", home / "iCloudDrive"))
    out, seen = [], set()
    for label, p in cands:
        try:
            if p.is_dir():
                key = str(p.resolve()).lower()
                if key not in seen:
                    seen.add(key)
                    out.append((label, str(p)))
        except OSError:
            continue
    return out


def account_dir(base: str) -> Path:
    """Kullanıcı bulut kökünü ya da doğrudan hesap klasörünü seçmiş olabilir."""
    b = Path(base)
    return b if (b / "account.json").exists() or b.name == FOLDER_NAME else b / FOLDER_NAME


# ---------------------------------------------------------------- şifreleme
def _fernet(password: str, salt: bytes):
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    key = Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(password.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(key))


class Account:
    def __init__(self, folder: Path, fernet):
        self.folder = Path(folder)
        self.f = fernet

    @staticmethod
    def exists(base: str) -> bool:
        return (account_dir(base) / "account.json").exists()

    @classmethod
    def create(cls, base: str, password: str) -> "Account":
        if len(password) < 4:
            raise SyncError("Şifre en az 4 karakter olmalı.")
        folder = account_dir(base)
        if (folder / "account.json").exists():
            return cls.open(base, password)
        (folder / "devices").mkdir(parents=True, exist_ok=True)
        (folder / "library").mkdir(exist_ok=True)
        salt = os.urandom(16)
        f = _fernet(password, salt)
        meta = {"format": FORMAT, "salt": base64.b64encode(salt).decode(), "created_at": time.time(),
                "check": f.encrypt(b"medquiz-ok").decode(),
                "note": "MedQuiz hesap klasörü. Silmeyin; dosyalar şifrelidir."}
        _atomic_write(folder / "account.json", json.dumps(meta, indent=2).encode())
        return cls(folder, f)

    @classmethod
    def open(cls, base: str, password: str) -> "Account":
        folder = account_dir(base)
        try:
            meta = json.loads((folder / "account.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise SyncError(f"Hesap klasörü okunamadı: {folder}") from e
        if meta.get("format", 1) > FORMAT:
            raise SyncError("Bu hesap daha yeni bir MedQuiz sürümüyle oluşturulmuş; önce uygulamayı güncelle.")
        f = _fernet(password, base64.b64decode(meta["salt"]))
        try:
            ok = f.decrypt(meta["check"].encode()) == b"medquiz-ok"
        except Exception:  # noqa: BLE001 (InvalidToken)
            ok = False
        if not ok:
            raise SyncError("Şifre yanlış.")
        (folder / "devices").mkdir(exist_ok=True)
        (folder / "library").mkdir(exist_ok=True)
        return cls(folder, f)

    def seal(self, obj) -> bytes:
        return self.f.encrypt(gzip.compress(json.dumps(obj, ensure_ascii=False).encode("utf-8"), 6))

    def unseal(self, data: bytes):
        return json.loads(gzip.decompress(self.f.decrypt(data)).decode("utf-8"))


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


# ---------------------------------------------------------------- eşitleme
class SyncEngine:
    def __init__(self, db, settings: Settings):
        self.db = db
        self.settings = settings

    # --- yerel kimlik
    def device_id(self) -> str:
        with self.db._lock:
            r = self.db.conn.execute("SELECT value FROM meta WHERE key='device_id'").fetchone()
            if r:
                return r[0]
            did = f"{(socket.gethostname() or 'pc')[:20]}-{uuid.uuid4().hex[:8]}".replace(" ", "_")
            self.db.conn.execute("INSERT INTO meta(key, value) VALUES ('device_id', ?)", (did,))
            self.db.conn.commit()
            return did

    def data_version(self) -> str:
        """Yerel veride değişiklik olup olmadığını anlamak için kısa özet."""
        with self.db._lock:
            parts = []
            for t in self.db.SYNC_TABLES:
                r = self.db.conn.execute(f"SELECT COUNT(*), MAX(mtime) FROM {t}").fetchone()
                parts.append(f"{r[0]}:{r[1] or 0:.3f}")
            r = self.db.conn.execute("SELECT COUNT(*), MAX(at) FROM tombstones").fetchone()
            parts.append(f"{r[0]}:{r[1] or 0:.3f}")
        return "|".join(parts) + f"|{self.settings.extra.get('_saved_at', 0)}"

    # --- anlık görüntü
    def _rows(self, table: str) -> list[dict]:
        return [dict(r) for r in self.db.conn.execute(f"SELECT * FROM {table}").fetchall()]

    def snapshot(self) -> dict:
        with self.db._lock:
            docs = self._rows("documents")
            doc_uid = {d["id"]: d["uid"] for d in docs}
            chunk_ref = {r[0]: [doc_uid.get(r[1]), r[2]]
                         for r in self.db.conn.execute("SELECT id, doc_id, idx FROM chunks").fetchall()}
            data: dict[str, list] = {}
            for t in self.db.SYNC_TABLES:
                rows = docs if t == "documents" else self._rows(t)
                for r in rows:
                    rid = r.pop("id", None)
                    if t == "questions":
                        try:
                            ids = json.loads(r.get("chunk_ids") or "[]")
                        except ValueError:
                            ids = []
                        r["chunk_ids"] = json.dumps([chunk_ref[i] for i in ids if i in chunk_ref])
                    elif t == "summaries":
                        r["doc_id"] = doc_uid.get(r.get("doc_id"))
                    elif t == "documents":
                        r["_has_file"] = Path(r["source"]).is_file()
                        r["_local_id"] = rid
                data[t] = rows
            tombs = [dict(r) for r in self.db.conn.execute("SELECT * FROM tombstones").fetchall()]
        st = asdict(self.settings)
        st["extra"] = {k: v for k, v in st.get("extra", {}).items() if k not in LOCAL_ONLY_EXTRA}
        for k in LOCAL_ONLY_FIELDS:
            st.pop(k, None)
        return {"format": FORMAT, "device": self.device_id(), "host": platform.node(), "at": time.time(),
                "settings": st, "settings_saved_at": self.settings.extra.get("_saved_at", 0),
                "data": data, "tombstones": tombs}

    # --- ayarlar
    def merge_settings(self, remote: dict, remote_saved: float, initial: bool) -> bool:
        s = self.settings
        changed = False
        newer = initial or remote_saved > (s.extra.get("_saved_at") or 0)
        for k, v in remote.items():
            if k == "extra" or k not in Settings.__dataclass_fields__ or k in LOCAL_ONLY_FIELDS:
                continue
            if k == "user_name" and not (initial and not s.user_name):
                continue          # bu bilgisayarda seçili kullanıcı yalnızca ilk bağlamada (boşsa) alınır
            cur = getattr(s, k)
            if k in KEY_FIELDS:
                take = v and (not cur or (newer and v != cur))
            else:
                take = newer and v != cur
            if take:
                setattr(s, k, v)
                changed = True
        rext = remote.get("extra") or {}
        prof = list(dict.fromkeys((s.extra.get("profiles") or []) + (rext.get("profiles") or [])))
        if prof != (s.extra.get("profiles") or []):
            s.extra["profiles"] = prof
            changed = True
        if newer:
            for k, v in rext.items():
                if k in LOCAL_ONLY_EXTRA or k == "profiles":
                    continue
                if s.extra.get(k) != v:
                    s.extra[k] = v
                    changed = True
        if changed:
            if newer:
                s.extra["_saved_at"] = max(remote_saved, s.extra.get("_saved_at") or 0)
            s.save(touch=False)
        return changed

    # --- veri birleştirme
    def merge_data(self, snap: dict, stats: dict) -> None:
        c = self.db.conn
        with self.db._lock:
            # 1) silinenler
            for t in snap.get("tombstones", []):
                if t.get("tbl") not in self.db.SYNC_TABLES:
                    continue
                row = c.execute(f"SELECT id, mtime FROM {t['tbl']} WHERE uid = ?", (t["uid"],)).fetchone()
                if row and (row["mtime"] or 0) <= t["at"]:
                    c.execute(f"DELETE FROM {t['tbl']} WHERE id = ?", (row["id"],))
                    stats["deleted"] += 1
                c.execute("INSERT INTO tombstones(uid, tbl, at) VALUES (?,?,?) "
                          "ON CONFLICT(uid) DO UPDATE SET at = MAX(at, excluded.at)", (t["uid"], t["tbl"], t["at"]))
            tomb = {r[0]: r[1] for r in c.execute("SELECT uid, at FROM tombstones").fetchall()}
            doc_id = {r[0]: r[1] for r in c.execute("SELECT uid, id FROM documents").fetchall()}
            chunk_id = {(r[0], r[1]): r[2] for r in c.execute(
                "SELECT d.uid, ch.idx, ch.id FROM chunks ch JOIN documents d ON d.id = ch.doc_id").fetchall()}
            # 2) satırlar
            for table, rows in (snap.get("data") or {}).items():
                if table not in self.db.SYNC_TABLES or table == "documents":
                    continue
                cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()} - {"id"}
                for r in rows:
                    uid, mt = r.get("uid"), r.get("mtime") or 0
                    if not uid or (uid in tomb and tomb[uid] >= mt):
                        continue
                    r = dict(r)
                    if table == "questions":
                        try:
                            refs = json.loads(r.get("chunk_ids") or "[]")
                        except ValueError:
                            refs = []
                        r["chunk_ids"] = json.dumps([chunk_id[(a, b)] for a, b in
                                                     (x for x in refs if isinstance(x, list) and len(x) == 2)
                                                     if (a, b) in chunk_id])
                    elif table == "summaries":
                        r["doc_id"] = doc_id.get(r.get("doc_id"))
                    vals = {k: v for k, v in r.items() if k in cols}
                    local = c.execute(f"SELECT id, mtime FROM {table} WHERE uid = ?", (uid,)).fetchone()
                    if local is None and table == "vocab":
                        local = self._vocab_twin(vals)
                        if local is not None and local["uid"] != uid:
                            continue          # yerel kayıt daha yeni: o kalır
                    if local is None:
                        ks = list(vals)
                        c.execute(f"INSERT INTO {table}({', '.join(ks)}) VALUES ({', '.join('?' * len(ks))})",
                                  [vals[k] for k in ks])
                        stats["added"] += 1
                    elif mt > (local["mtime"] or 0):
                        ks = [k for k in vals if k != "mtime"] + ["mtime"]
                        c.execute(f"UPDATE {table} SET {', '.join(k + ' = ?' for k in ks)} WHERE id = ?",
                                  [vals[k] for k in ks] + [local["id"]])
                        stats["updated"] += 1
            # 3) belge başlık/konu değişiklikleri
            for r in (snap.get("data") or {}).get("documents", []):
                loc = c.execute("SELECT id, mtime FROM documents WHERE uid = ?", (r.get("uid"),)).fetchone()
                if loc and (r.get("mtime") or 0) > (loc["mtime"] or 0):
                    c.execute("UPDATE documents SET title = ?, topic = ?, mtime = ? WHERE id = ?",
                              (r["title"], r.get("topic", ""), r["mtime"], loc["id"]))
            c.commit()

    def _vocab_twin(self, vals: dict):
        """Aynı kelime iki bilgisayarda ayrı ayrı eklenmişse (farklı uid) tek kayıtta birleştir."""
        c = self.db.conn
        row = c.execute("SELECT id, mtime, uid FROM vocab WHERE lower(term) = lower(?) AND profile = ?",
                        (vals.get("term", ""), vals.get("profile", ""))).fetchone()
        if row is None:
            return None
        if ((row["mtime"] or 0), row["uid"]) < ((vals.get("mtime") or 0), vals["uid"]):   # eşitlikte uid belirler
            # uzak kayıt yeni: yerel satır uzak uid'yi alır, eski uid her yerde silinsin diye iz bırakılır
            c.execute("INSERT OR REPLACE INTO tombstones(uid, tbl, at) VALUES (?, 'vocab', ?)",
                      (row["uid"], (vals.get("mtime") or 0) - 0.001))
            c.execute("UPDATE vocab SET uid = ?, mtime = ? WHERE id = ?",
                      (vals["uid"], (vals.get("mtime") or 0) - 0.0005, row["id"]))
            return c.execute("SELECT id, mtime, uid FROM vocab WHERE id = ?", (row["id"],)).fetchone()
        return row

    # --- kütüphane
    def push_library(self, acc: Account, stats: dict) -> None:
        lib = acc.folder / "library"
        with self.db._lock:
            docs = self._rows("documents")
        for d in docs:
            meta = lib / f"{d['uid']}.mql"
            if meta.exists():
                continue
            chunks = self.db.doc_chunks(d["id"])
            src = Path(d["source"])
            fname = ""
            try:
                if src.is_file() and src.stat().st_size <= MAX_LIB_FILE:
                    fname = f"{d['uid']}{src.suffix.lower()}"
                    if not (lib / fname).exists():
                        shutil.copyfile(src, lib / (fname + ".part"))
                        os.replace(lib / (fname + ".part"), lib / fname)
            except OSError as e:
                log.warning("Kütüphane dosyası kopyalanamadı (%s): %s", src, e)
                fname = ""
            doc = {k: v for k, v in d.items() if k != "id"}
            _atomic_write(meta, acc.seal({"doc": doc, "file": fname,
                                          "chunks": [{"idx": ch["idx"], "text": ch["text"],
                                                      "location": ch["location"]} for ch in chunks]}))
            stats["lib_up"] += 1

    def pull_library(self, acc: Account, remote_docs: list[dict], stats: dict) -> None:
        lib = acc.folder / "library"
        with self.db._lock:
            have = {r[0] for r in self.db.conn.execute("SELECT uid FROM documents").fetchall()}
            tomb = {r[0]: r[1] for r in self.db.conn.execute("SELECT uid, at FROM tombstones").fetchall()}
        local_lib = Path(data_dir()) / "library"
        for d in remote_docs:
            uid = d.get("uid")
            if not uid or uid in have or (uid in tomb and tomb[uid] >= (d.get("mtime") or 0)):
                continue
            meta = lib / f"{uid}.mql"
            if not meta.exists():
                continue          # dosya henüz bulut klasörüne inmemiş olabilir; sonraki eşitlemede
            try:
                pack = acc.unseal(meta.read_bytes())
            except Exception as e:  # noqa: BLE001
                log.warning("Kütüphane paketi okunamadı %s: %s", meta, e)
                continue
            doc = pack["doc"]
            source = doc["source"]
            if pack.get("file") and (lib / pack["file"]).exists():
                local_lib.mkdir(exist_ok=True)
                dst = local_lib / pack["file"]
                try:
                    if not dst.exists():
                        shutil.copyfile(lib / pack["file"], dst)
                    source = str(dst)
                except OSError as e:
                    log.warning("Belge dosyası kopyalanamadı: %s", e)
            with self.db._lock:
                c = self.db.conn
                cur = c.execute("INSERT INTO documents(title, source, kind, topic, added_at, char_count, uid, mtime) "
                                "VALUES (?,?,?,?,?,?,?,?)",
                                (doc["title"], source, doc["kind"], doc.get("topic", ""), doc.get("added_at") or
                                 time.time(), doc.get("char_count", 0), uid, doc.get("mtime") or time.time()))
                c.executemany("INSERT INTO chunks(doc_id, idx, text, location) VALUES (?,?,?,?)",
                              [(cur.lastrowid, ch["idx"], ch["text"], ch.get("location", ""))
                               for ch in pack.get("chunks", [])])
                c.commit()
            stats["lib_down"] += 1

    # --- ana işlem
    def sync(self, acc: Account, initial: bool = False, push_only: bool = False, library: bool = True) -> dict:
        stats = {"added": 0, "updated": 0, "deleted": 0, "lib_up": 0, "lib_down": 0, "devices": 0,
                 "settings": False, "errors": []}
        me = self.device_id()
        if not push_only:
            snaps = []
            for f in sorted((acc.folder / "devices").glob("*.mqd")):
                if f.stem == me:
                    continue
                try:
                    snaps.append(acc.unseal(f.read_bytes()))
                except Exception as e:  # noqa: BLE001
                    stats["errors"].append(f"{f.name}: okunamadı ({type(e).__name__})")
            snaps.sort(key=lambda s: s.get("at", 0))
            stats["devices"] = len(snaps)
            for sn in snaps:
                self.pull_library(acc, (sn.get("data") or {}).get("documents", []), stats)
            for sn in snaps:
                self.merge_data(sn, stats)
            if snaps:
                newest = max(snaps, key=lambda s: s.get("settings_saved_at") or 0)
                for sn in snaps:     # API anahtarları: herhangi bir bilgisayarda girilmişse al
                    if sn is not newest:
                        stats["settings"] |= self.merge_settings(
                            {k: sn["settings"].get(k) for k in KEY_FIELDS}, 0, False)
                stats["settings"] |= self.merge_settings(newest.get("settings") or {},
                                                         newest.get("settings_saved_at") or 0, initial)
        if library:
            self.push_library(acc, stats)
        _atomic_write(acc.folder / "devices" / f"{me}.mqd", acc.seal(self.snapshot()))
        stats["at"] = time.time()
        return stats

    def devices(self, acc: Account) -> list[dict]:
        out = []
        for f in sorted((acc.folder / "devices").glob("*.mqd")):
            try:
                st = f.stat()
                out.append({"device": f.stem, "me": f.stem == self.device_id(), "modified": st.st_mtime})
            except OSError:
                pass
        return out


# ---------------------------------------------------------------- yedek dosyası (bulut yoksa)
def export_backup(db, settings: Settings, path: str, password: str) -> dict:
    import tempfile
    import zipfile
    with tempfile.TemporaryDirectory() as tmp:
        acc = Account.create(tmp, password)
        stats = SyncEngine(db, settings).sync(acc, push_only=True)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
            for f in acc.folder.rglob("*"):
                if f.is_file():
                    z.write(f, f.relative_to(acc.folder))
    return stats


def import_backup(db, settings: Settings, path: str, password: str) -> dict:
    import tempfile
    import zipfile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / FOLDER_NAME
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                if n.startswith("/") or ".." in Path(n).parts:
                    raise SyncError("Yedek dosyası bozuk.")
            z.extractall(root)
        acc = Account.open(str(root), password)
        eng = SyncEngine(db, settings)
        # Yedekteki cihaz kimliği bizimkiyle aynıysa (aynı bilgisayardan alınmış yedek) yine de okunsun
        me = eng.device_id()
        own = acc.folder / "devices" / f"{me}.mqd"
        if own.exists():
            own.rename(own.with_name(f"{me}-yedek.mqd"))
        return eng.sync(acc, initial=not settings.has_any_key)
