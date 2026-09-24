from __future__ import annotations

import json
import hashlib
from pathlib import Path
import re
import secrets
import sqlite3
import time


ID_PATTERN = re.compile(r"^[a-f0-9]{12}$")
BROWSER_LIFETIME = 90 * 24 * 3600


class Store:
    def __init__(self, runtime: Path, root: Path):
        self.runtime, self.root = runtime.resolve(), root.resolve()
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.db = self.runtime / "conversations.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS papers (id TEXT PRIMARY KEY, thread TEXT, document TEXT);
                CREATE TABLE IF NOT EXISTS messages (
                  id INTEGER PRIMARY KEY, paper TEXT NOT NULL, role TEXT NOT NULL,
                  content TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, paper TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS translations (
                  paper TEXT NOT NULL, cache_key TEXT NOT NULL, content TEXT NOT NULL,
                  PRIMARY KEY(paper, cache_key));
                CREATE TABLE IF NOT EXISTS trusted_browsers (
                  token_hash TEXT PRIMARY KEY, origin TEXT NOT NULL,
                  created REAL NOT NULL, expires REAL NOT NULL);
                UPDATE messages SET status='interrupted' WHERE status='running';
            """)
            columns = {r[1] for r in db.execute("PRAGMA table_info(messages)")}
            if "document_hash" not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN document_hash TEXT")
            if "error" not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN error TEXT NOT NULL DEFAULT ''")
            if "model" not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN model TEXT NOT NULL DEFAULT ''")

    def connect(self):
        db = sqlite3.connect(self.db)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def browser_hash(credential):
        if not isinstance(credential, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", credential):
            return None
        return hashlib.sha256(credential.encode("ascii")).hexdigest()

    def remember_browser(self, origin):
        credential = secrets.token_urlsafe(32)
        token_hash = self.browser_hash(credential)
        now = time.time()
        expires = now + BROWSER_LIFETIME
        with self.connect() as db:
            db.execute("DELETE FROM trusted_browsers WHERE expires<=?", (now,))
            db.execute("INSERT INTO trusted_browsers VALUES(?,?,?,?)", (token_hash, origin, now, expires))
        # The raw credential is returned once; only its hash is stored locally.
        return credential, token_hash, expires

    def restore_browser(self, credential, origin):
        token_hash = self.browser_hash(credential)
        if not token_hash:
            return None
        now = time.time()
        expires = now + BROWSER_LIFETIME
        with self.connect() as db:
            changed = db.execute("UPDATE trusted_browsers SET expires=? WHERE token_hash=? AND origin=? AND expires>?",
                                 (expires, token_hash, origin, now)).rowcount
        return (token_hash, expires) if changed else None

    def forget_browser(self, credential, origin):
        token_hash = self.browser_hash(credential)
        if token_hash:
            with self.connect() as db:
                db.execute("DELETE FROM trusted_browsers WHERE token_hash=? AND origin=?", (token_hash, origin))
        return token_hash

    def paper(self, paper_id):
        if not ID_PATTERN.fullmatch(paper_id):
            raise ValueError("无效的论文编号。")
        paths = [self.root / "data/daily.json", *sorted((self.root / "data/archive").glob("*.json"), reverse=True)]
        for path in paths:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for p in payload.get("core", []) + payload.get("extended", []):
                if p.get("id") == paper_id:
                    return p
        raise ValueError("本机还没有这篇论文。请先同步 GitHub 仓库的最新数据，再打开对应文章。")

    def state(self, paper_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
        return dict(row) if row else {"id": paper_id, "thread": None, "document": None}

    def set_thread(self, paper_id, thread):
        with self.connect() as db:
            db.execute("INSERT INTO papers(id,thread) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET thread=excluded.thread", (paper_id, thread))

    def set_document(self, paper_id, document):
        with self.connect() as db:
            db.execute("INSERT INTO papers(id,document) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET document=excluded.document, thread=NULL", (paper_id, json.dumps(document, ensure_ascii=False)))

    def document(self, paper_id):
        value = self.state(paper_id)["document"]
        return json.loads(value) if value else None

    def history(self, paper_id):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT id,role,content,status,created,document_hash,error,model FROM messages WHERE paper=? ORDER BY id", (paper_id,))]

    def claim(self, request_id, paper_id):
        with self.connect() as db:
            try:
                db.execute("INSERT INTO requests VALUES(?,?)", (request_id, paper_id))
                return True
            except sqlite3.IntegrityError:
                return False

    def message(self, paper_id, role, content, status="completed", model=""):
        doc_hash = (self.document(paper_id) or {}).get("hash")
        with self.connect() as db:
            return db.execute("INSERT INTO messages(paper,role,content,status,created,document_hash,model) VALUES(?,?,?,?,?,?,?)",
                              (paper_id, role, content, status, time.time(), doc_hash, model)).lastrowid

    def update(self, message_id, content, status, error=""):
        with self.connect() as db:
            db.execute("UPDATE messages SET content=?,status=?,error=? WHERE id=?", (content, status, error, message_id))

    def clear(self, paper_id):
        with self.connect() as db:
            for table in ("messages", "requests", "translations"):
                db.execute(f"DELETE FROM {table} WHERE paper=?", (paper_id,))
            db.execute("DELETE FROM papers WHERE id=?", (paper_id,))

    def translation(self, paper_id, cache_key):
        with self.connect() as db:
            row = db.execute("SELECT content FROM translations WHERE paper=? AND cache_key=?", (paper_id, cache_key)).fetchone()
        return json.loads(row[0]) if row else {}

    def save_translation(self, paper_id, cache_key, data):
        with self.connect() as db:
            db.execute("INSERT INTO translations VALUES(?,?,?) ON CONFLICT(paper,cache_key) DO UPDATE SET content=excluded.content",
                       (paper_id, cache_key, json.dumps(data, ensure_ascii=False)))

    def directory(self, paper_id):
        if not ID_PATTERN.fullmatch(paper_id):
            raise ValueError("无效的论文编号。")
        parent = (self.runtime / "documents").resolve()
        result = (parent / paper_id).resolve()
        if result.parent != parent:
            raise ValueError("资料路径不在指定目录内。")
        result.mkdir(parents=True, exist_ok=True)
        return result
