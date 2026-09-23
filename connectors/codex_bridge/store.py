from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
import time


ID_PATTERN = re.compile(r"^[a-f0-9]{12}$")


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
                UPDATE messages SET status='interrupted' WHERE status='running';
            """)
            columns = {r[1] for r in db.execute("PRAGMA table_info(messages)")}
            if "document_hash" not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN document_hash TEXT")

    def connect(self):
        db = sqlite3.connect(self.db)
        db.row_factory = sqlite3.Row
        return db

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
            return [dict(r) for r in db.execute("SELECT id,role,content,status,created,document_hash FROM messages WHERE paper=? ORDER BY id", (paper_id,))]

    def claim(self, request_id, paper_id):
        with self.connect() as db:
            try:
                db.execute("INSERT INTO requests VALUES(?,?)", (request_id, paper_id))
                return True
            except sqlite3.IntegrityError:
                return False

    def message(self, paper_id, role, content, status="completed"):
        doc_hash = (self.document(paper_id) or {}).get("hash")
        with self.connect() as db:
            return db.execute("INSERT INTO messages(paper,role,content,status,created,document_hash) VALUES(?,?,?,?,?,?)",
                              (paper_id, role, content, status, time.time(), doc_hash)).lastrowid

    def update(self, message_id, content, status):
        with self.connect() as db:
            db.execute("UPDATE messages SET content=?,status=? WHERE id=?", (content, status, message_id))

    def clear(self, paper_id):
        with self.connect() as db:
            for table in ("messages", "requests"):
                db.execute(f"DELETE FROM {table} WHERE paper=?", (paper_id,))
            db.execute("DELETE FROM papers WHERE id=?", (paper_id,))

    def directory(self, paper_id):
        if not ID_PATTERN.fullmatch(paper_id):
            raise ValueError("无效的论文编号。")
        parent = (self.runtime / "documents").resolve()
        result = (parent / paper_id).resolve()
        if result.parent != parent:
            raise ValueError("资料路径不在指定目录内。")
        result.mkdir(parents=True, exist_ok=True)
        return result
