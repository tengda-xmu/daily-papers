from __future__ import annotations

from contextlib import contextmanager
import json
import hashlib
from pathlib import Path
import re
import secrets
import sqlite3
import time

from .document_names import name_original_pdf


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
                CREATE TABLE IF NOT EXISTS screenshots (
                  id TEXT PRIMARY KEY, paper TEXT NOT NULL, metadata TEXT NOT NULL,
                  used INTEGER NOT NULL DEFAULT 0);
                UPDATE messages SET status='interrupted' WHERE status='running';
            """)
            columns = {r[1] for r in db.execute("PRAGMA table_info(messages)")}
            if "document_hash" not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN document_hash TEXT")
            if "error" not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN error TEXT NOT NULL DEFAULT ''")
            if "model" not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN model TEXT NOT NULL DEFAULT ''")
            if "attachments" not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]'")
            if "artifact" not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN artifact TEXT NOT NULL DEFAULT '{}'")
            if 'request' not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN request TEXT NOT NULL DEFAULT '{}'")
            if 'parent_id' not in columns:
                db.execute('ALTER TABLE messages ADD COLUMN parent_id INTEGER NOT NULL DEFAULT 0')
                previous = {}
                for row in db.execute('SELECT id,paper FROM messages ORDER BY id').fetchall():
                    db.execute('UPDATE messages SET parent_id=? WHERE id=?', (previous.get(row['paper'], 0), row['id']))
                    previous[row['paper']] = row['id']
            paper_columns = {r[1] for r in db.execute('PRAGMA table_info(papers)')}
            if 'active_leaf' not in paper_columns:
                db.execute('ALTER TABLE papers ADD COLUMN active_leaf INTEGER NOT NULL DEFAULT 0')
                for row in db.execute('SELECT paper,MAX(id) AS leaf FROM messages GROUP BY paper').fetchall():
                    db.execute('INSERT INTO papers(id,active_leaf) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET active_leaf=excluded.active_leaf', (row['paper'], row['leaf']))
            db.execute('CREATE TABLE IF NOT EXISTS branch_choices (paper TEXT NOT NULL,parent_id INTEGER NOT NULL,child_id INTEGER NOT NULL,PRIMARY KEY(paper,parent_id))')
            db.execute('CREATE INDEX IF NOT EXISTS message_parent ON messages(paper,parent_id)')
            db.execute('CREATE TABLE IF NOT EXISTS annotations (paper TEXT NOT NULL,document_hash TEXT NOT NULL,revision INTEGER NOT NULL,items TEXT NOT NULL,PRIMARY KEY(paper,document_hash))')
        from .library import Library
        self.library = Library(self)
        self.library.migrate()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

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

    def paper_paths(self):
        return [self.runtime / 'recommendations.json', self.root / 'data/daily.json',
                *sorted((self.runtime / 'recommendation-history').glob('*.json'), reverse=True),
                *sorted((self.root / 'data/archive').glob('*.json'), reverse=True),
                *sorted((self.root / 'data/editions').glob('*/*.json'), reverse=True),
                *sorted((self.runtime / 'public-editions').glob('*/*.json'), reverse=True)]

    def paper(self, paper_id):
        if not ID_PATTERN.fullmatch(paper_id):
            raise ValueError("无效的论文编号。")
        paths = self.paper_paths()
        for path in paths:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for p in payload.get("core", []) + payload.get("extended", []):
                if p.get("id") == paper_id:
                    return p
        saved = self.library.metadata(paper_id)
        if saved:
            return saved
        raise ValueError("本机还没有这篇论文。请先同步 GitHub 仓库的最新数据，再打开对应文章。")

    def state(self, paper_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
        return dict(row) if row else {"id": paper_id, "thread": None, "document": None, 'active_leaf':0}

    def set_thread(self, paper_id, thread):
        with self.connect() as db:
            db.execute("INSERT INTO papers(id,thread) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET thread=excluded.thread", (paper_id, thread))

    def set_document(self, paper_id, document):
        if document:
            document = name_original_pdf(document, self.paper(paper_id))
            self.library.source(paper_id, document)
        with self.connect() as db:
            db.execute("INSERT INTO papers(id,document) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET document=excluded.document, thread=NULL", (paper_id, json.dumps(document, ensure_ascii=False)))

    def document(self, paper_id):
        value = self.state(paper_id)["document"]
        # Resolve legacy names without rewriting documents, versions or private records.
        return name_original_pdf(json.loads(value), self.paper(paper_id)) if value else None

    def history(self, paper_id, *, leaf=None, all_versions=False):
        with self.connect() as db:
            rows = [dict(r) for r in db.execute("SELECT id,role,content,status,created,document_hash,error,model,attachments,artifact,parent_id,request FROM messages WHERE paper=? ORDER BY id", (paper_id,))]
        for row in rows:
            row['attachments'] = json.loads(row['attachments'])
            row['artifact'] = json.loads(row['artifact'])
            row['request'] = json.loads(row['request'])
        if all_versions:
            return rows
        siblings = {}
        for row in rows:
            siblings.setdefault((row['parent_id'], row['role']), []).append(row['id'])
        current = self.state(paper_id)['active_leaf'] if leaf is None else leaf
        by_id = {row['id']:row for row in rows}
        chain, seen = [], set()
        while current in by_id and current not in seen:
            seen.add(current)
            row = by_id[current]
            row['versions'] = siblings[(row['parent_id'], row['role'])]
            chain.append(row)
            current = row['parent_id']
        return list(reversed(chain))

    def fork(self, paper_id, parent_id):
        with self.connect() as db:
            if parent_id and not db.execute('SELECT 1 FROM messages WHERE paper=? AND id=?', (paper_id, parent_id)).fetchone():
                raise ValueError('找不到此论文的对话位置。')
            db.execute('UPDATE papers SET active_leaf=?,thread=NULL WHERE id=?', (parent_id, paper_id))

    def select_version(self, paper_id, message_id):
        rows = {r['id']:r for r in self.history(paper_id, all_versions=True)}
        if message_id not in rows:
            raise ValueError('找不到此论文的对话版本。')
        row = rows[message_id]
        current = message_id
        with self.connect() as db:
            db.execute('INSERT INTO branch_choices VALUES(?,?,?) ON CONFLICT(paper,parent_id) DO UPDATE SET child_id=excluded.child_id', (paper_id, row['parent_id'], message_id))
            seen = set()
            while current not in seen:
                seen.add(current)
                children = [r['id'] for r in rows.values() if r['parent_id'] == current]
                if not children:
                    break
                selected = db.execute('SELECT child_id FROM branch_choices WHERE paper=? AND parent_id=?', (paper_id, current)).fetchone()
                current = selected['child_id'] if selected and selected['child_id'] in children else max(children)
            db.execute('UPDATE papers SET active_leaf=?,thread=NULL WHERE id=?', (current, paper_id))
        return current

    def set_artifact(self, message_id, artifact):
        with self.connect() as db:
            row = db.execute('SELECT paper,model,request,created FROM messages WHERE id=?', (message_id,)).fetchone()
        if row:
            self.library.artifact(row['paper'], artifact, model=row['model'],
                                  instructions=json.loads(row['request']).get('message', ''), created=row['created'])
        with self.connect() as db:
            db.execute('UPDATE messages SET artifact=? WHERE id=?', (json.dumps(artifact), message_id))

    def screenshots(self, paper_id, *, pending=False):
        with self.connect() as db:
            return [json.loads(r['metadata']) for r in db.execute(
                'SELECT metadata FROM screenshots WHERE paper=?' + (' AND used=0' if pending else '') + ' ORDER BY rowid', (paper_id,))]

    def screenshot(self, paper_id, identifier):
        from .screenshots import screenshot_path
        path = screenshot_path(self.directory(paper_id), identifier)
        found = next((r for r in self.screenshots(paper_id) if r['id'] == identifier), None)
        if not found or not path.is_file():
            raise ValueError('截图不存在或已清除，请重新上传。')
        return found, path

    def add_screenshot(self, paper_id, metadata):
        with self.connect() as db:
            db.execute('INSERT INTO screenshots(id,paper,metadata) VALUES(?,?,?)',
                       (metadata['id'], paper_id, json.dumps(metadata, ensure_ascii=False)))

    def remove_screenshot(self, paper_id, identifier):
        _, path = self.screenshot(paper_id, identifier)
        with self.connect() as db:
            if not db.execute('DELETE FROM screenshots WHERE id=? AND paper=? AND used=0', (identifier, paper_id)).rowcount:
                raise ValueError('截图已用于对话；如需删除，请清除该论文的本机记录。')
        path.unlink(missing_ok=True)

    def claim(self, request_id, paper_id):
        with self.connect() as db:
            try:
                db.execute("INSERT INTO requests VALUES(?,?)", (request_id, paper_id))
                return True
            except sqlite3.IntegrityError:
                return False

    def message(self, paper_id, role, content, status="completed", model="", attachments=(), request=None):
        doc_hash = (self.document(paper_id) or {}).get("hash")
        with self.connect() as db:
            for attachment in attachments:
                db.execute('UPDATE screenshots SET used=1 WHERE id=? AND paper=?', (attachment['id'], paper_id))
            db.execute('INSERT OR IGNORE INTO papers(id) VALUES(?)', (paper_id,))
            parent = db.execute('SELECT active_leaf FROM papers WHERE id=?', (paper_id,)).fetchone()['active_leaf']
            identifier = db.execute("INSERT INTO messages(paper,role,content,status,created,document_hash,model,attachments,parent_id,request) VALUES(?,?,?,?,?,?,?,?,?,?)",
                              (paper_id, role, content, status, time.time(), doc_hash, model, json.dumps(list(attachments), ensure_ascii=False), parent, json.dumps(request or {}, ensure_ascii=False))).lastrowid
            db.execute('UPDATE papers SET active_leaf=? WHERE id=?', (identifier, paper_id))
            db.execute('INSERT INTO branch_choices VALUES(?,?,?) ON CONFLICT(paper,parent_id) DO UPDATE SET child_id=excluded.child_id', (paper_id, parent, identifier))
            return identifier

    def update(self, message_id, content, status, error=""):
        with self.connect() as db:
            db.execute("UPDATE messages SET content=?,status=?,error=? WHERE id=?", (content, status, error, message_id))

    def clear(self, paper_id):
        with self.connect() as db:
            for table in ("messages", "requests", "translations", "screenshots", 'branch_choices', 'annotations', 'library_documents', 'library_reading'):
                db.execute(f"DELETE FROM {table} WHERE paper=?", (paper_id,))
            db.execute("DELETE FROM papers WHERE id=?", (paper_id,))
            db.execute('DELETE FROM library_papers WHERE id=?', (paper_id,))

    def clear_conversation(self, paper_id):
        screenshots = self.screenshots(paper_id)
        with self.connect() as db:
            for table in ('messages', 'requests', 'screenshots', 'branch_choices'):
                db.execute(f'DELETE FROM {table} WHERE paper=?', (paper_id,))
            db.execute('UPDATE papers SET thread=NULL,active_leaf=0 WHERE id=?', (paper_id,))
        from .screenshots import screenshot_path
        for item in screenshots:
            screenshot_path(self.directory(paper_id), item['id']).unlink(missing_ok=True)

    def translation(self, paper_id, cache_key):
        with self.connect() as db:
            row = db.execute("SELECT content FROM translations WHERE paper=? AND cache_key=?", (paper_id, cache_key)).fetchone()
        return json.loads(row[0]) if row else {}

    def save_translation(self, paper_id, cache_key, data):
        with self.connect() as db:
            db.execute("INSERT INTO translations VALUES(?,?,?) ON CONFLICT(paper,cache_key) DO UPDATE SET content=excluded.content",
                       (paper_id, cache_key, json.dumps(data, ensure_ascii=False)))
        self.library.artifact(paper_id, data.get('artifact', {}), cache_key=cache_key,
                              model=data.get('signature', {}).get('model', ''),
                              instructions=data.get('signature', {}).get('instructions', ''))

    def directory(self, paper_id):
        if not ID_PATTERN.fullmatch(paper_id):
            raise ValueError("无效的论文编号。")
        parent = (self.runtime / "documents").resolve()
        result = (parent / paper_id).resolve()
        if result.parent != parent:
            raise ValueError("资料路径不在指定目录内。")
        result.mkdir(parents=True, exist_ok=True)
        return result
