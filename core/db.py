"""SQLite tabanlı bilgi tabanı ve quiz geçmişi."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    topic TEXT DEFAULT '',
    added_at REAL NOT NULL,
    char_count INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    text TEXT NOT NULL,
    location TEXT DEFAULT '',
    embedding BLOB,
    emb_model TEXT,
    used_count INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_chunks_doc ON chunks(doc_id);
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    qtype TEXT NOT NULL,
    topic TEXT DEFAULT '',
    difficulty TEXT DEFAULT '',
    payload TEXT NOT NULL,
    chunk_ids TEXT DEFAULT '[]',
    user_answer TEXT,
    score REAL,
    answered_at REAL,
    profile TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    title TEXT NOT NULL,
    topic TEXT DEFAULT '',
    level TEXT DEFAULT '',
    payload TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT '{}',
    finished INTEGER DEFAULT 0,
    score REAL,
    profile TEXT DEFAULT ''
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------ documents
    def add_document(self, title: str, source: str, kind: str, chunks: list[dict], topic: str = "") -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO documents(title, source, kind, topic, added_at, char_count) VALUES (?,?,?,?,?,?)",
                (title, source, kind, topic, time.time(), sum(len(c["text"]) for c in chunks)),
            )
            doc_id = cur.lastrowid
            self.conn.executemany(
                "INSERT INTO chunks(doc_id, idx, text, location) VALUES (?,?,?,?)",
                [(doc_id, i, c["text"], c.get("location", "")) for i, c in enumerate(chunks)],
            )
            self.conn.commit()
            return doc_id

    def list_documents(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT d.*, (SELECT COUNT(*) FROM chunks c WHERE c.doc_id = d.id) AS n_chunks "
                "FROM documents d ORDER BY d.added_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def delete_document(self, doc_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
            self.conn.commit()

    def source_exists(self, source: str) -> bool:
        with self._lock:
            return self.conn.execute("SELECT 1 FROM documents WHERE source = ?", (source,)).fetchone() is not None

    def topics(self) -> list[str]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT topic FROM documents WHERE topic != '' ORDER BY topic"
            ).fetchall()
            return [r[0] for r in rows]

    # --------------------------------------------------------------- chunks
    def all_chunks(self, doc_ids: list[int] | None = None) -> list[dict]:
        with self._lock:
            sql = (
                "SELECT c.id, c.doc_id, c.idx, c.text, c.location, c.used_count, "
                "d.title, d.topic FROM chunks c JOIN documents d ON d.id = c.doc_id"
            )
            params: tuple = ()
            if doc_ids:
                sql += f" WHERE c.doc_id IN ({','.join('?' * len(doc_ids))})"
                params = tuple(doc_ids)
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def get_chunks(self, ids: list[int]) -> list[dict]:
        if not ids:
            return []
        with self._lock:
            rows = self.conn.execute(
                "SELECT c.id, c.doc_id, c.idx, c.text, c.location, d.title, d.topic "
                f"FROM chunks c JOIN documents d ON d.id = c.doc_id WHERE c.id IN ({','.join('?' * len(ids))})",
                tuple(ids),
            ).fetchall()
            by_id = {r["id"]: dict(r) for r in rows}
            return [by_id[i] for i in ids if i in by_id]

    def neighbors(self, chunk: dict, span: int = 1) -> list[dict]:
        """Aynı belgedeki komşu parçaları getirir (bağlamı genişletmek için)."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT c.id, c.doc_id, c.idx, c.text, c.location, d.title, d.topic FROM chunks c "
                "JOIN documents d ON d.id = c.doc_id WHERE c.doc_id = ? AND c.idx BETWEEN ? AND ? ORDER BY c.idx",
                (chunk["doc_id"], chunk["idx"] - span, chunk["idx"] + span),
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_used(self, ids: list[int]) -> None:
        if not ids:
            return
        with self._lock:
            self.conn.executemany("UPDATE chunks SET used_count = used_count + 1 WHERE id = ?", [(i,) for i in ids])
            self.conn.commit()

    def chunks_missing_embeddings(self, model: str) -> list[tuple[int, str]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, text FROM chunks WHERE embedding IS NULL OR emb_model IS NOT ?", (model,)
            ).fetchall()
            return [(r[0], r[1]) for r in rows]

    def set_embeddings(self, items: list[tuple[int, np.ndarray]], model: str) -> None:
        with self._lock:
            self.conn.executemany(
                "UPDATE chunks SET embedding = ?, emb_model = ? WHERE id = ?",
                [(v.astype(np.float32).tobytes(), model, cid) for cid, v in items],
            )
            self.conn.commit()

    def load_embeddings(self, model: str) -> tuple[list[int], np.ndarray | None]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, embedding FROM chunks WHERE embedding IS NOT NULL AND emb_model = ?", (model,)
            ).fetchall()
        if not rows:
            return [], None
        ids = [r[0] for r in rows]
        mat = np.vstack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
        return ids, mat

    # ------------------------------------------------------------ questions
    def save_question(self, qtype: str, topic: str, difficulty: str, payload: dict,
                      chunk_ids: list[int], profile: str = "") -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO questions(created_at, qtype, topic, difficulty, payload, chunk_ids, profile) "
                "VALUES (?,?,?,?,?,?,?)",
                (time.time(), qtype, topic, difficulty, json.dumps(payload, ensure_ascii=False),
                 json.dumps(chunk_ids), profile),
            )
            self.conn.commit()
            return cur.lastrowid

    def record_answer(self, qid: int, user_answer: str, score: float) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE questions SET user_answer = ?, score = ?, answered_at = ? WHERE id = ?",
                (user_answer, score, time.time(), qid),
            )
            self.conn.commit()

    def recent_question_texts(self, limit: int = 40) -> list[str]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT payload FROM questions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            try:
                out.append(json.loads(r[0]).get("question", ""))
            except json.JSONDecodeError:
                pass
        return [q for q in out if q]

    def stats(self, profile: str | None = None) -> dict:
        with self._lock:
            where = "WHERE score IS NOT NULL"
            params: tuple = ()
            if profile:
                where += " AND profile = ?"
                params = (profile,)
            total = self.conn.execute(f"SELECT COUNT(*), AVG(score) FROM questions {where}", params).fetchone()
            by_type = self.conn.execute(
                f"SELECT qtype, COUNT(*), AVG(score) FROM questions {where} GROUP BY qtype", params
            ).fetchall()
            by_topic = self.conn.execute(
                f"SELECT COALESCE(NULLIF(topic,''),'(Genel)'), COUNT(*), AVG(score) FROM questions {where} "
                "GROUP BY 1 ORDER BY 3 ASC", params
            ).fetchall()
            recent = self.conn.execute(
                f"SELECT qtype, topic, score, payload, user_answer, answered_at FROM questions {where} "
                "ORDER BY answered_at DESC LIMIT 50", params
            ).fetchall()
        return {
            "total": total[0] or 0,
            "avg": total[1] or 0.0,
            "by_type": [tuple(r) for r in by_type],
            "by_topic": [tuple(r) for r in by_topic],
            "recent": [dict(r) for r in recent],
        }

    def wrong_questions(self, limit: int = 20, profile: str | None = None) -> list[dict]:
        """Yanlış/düşük puanlı sorular — tekrar modu için."""
        with self._lock:
            sql = "SELECT id, qtype, topic, difficulty, payload, chunk_ids FROM questions WHERE score IS NOT NULL AND score < 0.6"
            params: tuple = ()
            if profile:
                sql += " AND profile = ?"
                params = (profile,)
            sql += " ORDER BY answered_at DESC LIMIT ?"
            rows = self.conn.execute(sql, params + (limit,)).fetchall()
        return [dict(r) for r in rows]

    def profiles(self) -> list[str]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT profile FROM questions WHERE profile != '' ORDER BY profile"
            ).fetchall()
            return [r[0] for r in rows]

    # ---------------------------------------------------------------- cases
    def save_case(self, title: str, topic: str, level: str, payload: dict, profile: str = "") -> int:
        now = time.time()
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO cases(created_at, updated_at, title, topic, level, payload, state, profile) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (now, now, title, topic, level, json.dumps(payload, ensure_ascii=False), "{}", profile),
            )
            self.conn.commit()
            return cur.lastrowid

    def update_case_state(self, case_id: int, state: dict, finished: bool, score: float | None) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE cases SET state = ?, finished = ?, score = ?, updated_at = ? WHERE id = ?",
                (json.dumps(state, ensure_ascii=False), int(finished), score, time.time(), case_id),
            )
            self.conn.commit()

    def list_cases(self, profile: str | None = None, limit: int = 200) -> list[dict]:
        with self._lock:
            sql = "SELECT id, created_at, updated_at, title, topic, level, finished, score, profile, state FROM cases"
            params: tuple = ()
            if profile:
                sql += " WHERE profile = ?"
                params = (profile,)
            sql += " ORDER BY updated_at DESC LIMIT ?"
            return [dict(r) for r in self.conn.execute(sql, params + (limit,)).fetchall()]

    def get_case(self, case_id: int) -> dict | None:
        with self._lock:
            r = self.conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        d["state"] = json.loads(d["state"] or "{}")
        return d

    def delete_case(self, case_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM cases WHERE id = ?", (case_id,))
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()
