import base64
import hashlib
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.flac import Picture

EXTENSIONS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac"}


class Library:
    def __init__(self, path, artwork_dir=None):
        self.path = path
        self.artwork_dir = Path(artwork_dir or Path(path).with_name("artwork")).expanduser()
        parent = Path(path).expanduser().parent
        if str(parent) != ".":
            parent.mkdir(parents=True, exist_ok=True)
        self.artwork_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            self._ensure_schema(db)

    @staticmethod
    def _ensure_schema(db):
        db.execute("""CREATE TABLE IF NOT EXISTS tracks (
                id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL, artist TEXT, album TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP, last_played TEXT
            )""")
        columns = {row[1] for row in db.execute("PRAGMA table_info(tracks)")}
        if "last_played" not in columns:
            db.execute("ALTER TABLE tracks ADD COLUMN last_played TEXT")
        db.execute("""CREATE TABLE IF NOT EXISTS track_images (
                id INTEGER PRIMARY KEY, track_id INTEGER NOT NULL,
                path TEXT NOT NULL UNIQUE, mime_type TEXT NOT NULL,
                FOREIGN KEY(track_id) REFERENCES tracks(id) ON DELETE CASCADE
            )""")

    def _connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        self._ensure_schema(db)
        return db

    @staticmethod
    def _metadata(path):
        title = Path(path).stem.replace("_", " ")
        artist = album = None
        try:
            audio = MutagenFile(path, easy=True)
            if audio:
                title = (audio.get("title") or [title])[0]
                artist = (audio.get("artist") or [None])[0]
                album = (audio.get("album") or [None])[0]
        except Exception:
            pass
        return title, artist, album

    @staticmethod
    def _embedded_images(path):
        images = []
        try:
            audio = MutagenFile(path)
            if not audio:
                return images
            for picture in getattr(audio, "pictures", []) or []:
                images.append((bytes(picture.data), picture.mime or "image/jpeg"))
            tags = getattr(audio, "tags", None)
            if tags:
                for key, value in tags.items():
                    if key == "metadata_block_picture":
                        values = value if isinstance(value, list) else [value]
                        for encoded in values:
                            try:
                                picture = Picture(base64.b64decode(encoded))
                                images.append((bytes(picture.data), picture.mime or "image/jpeg"))
                            except Exception:
                                pass
                        continue
                    values = value if key == "covr" and isinstance(value, list) else [value]
                    for item in values:
                        if hasattr(item, "data") and hasattr(item, "mime"):
                            images.append((bytes(item.data), item.mime or "image/jpeg"))
                        elif key == "covr" and isinstance(item, (bytes, bytearray)):
                            images.append((bytes(item), "image/jpeg"))
        except Exception:
            pass
        unique = {}
        for data, mime in images:
            unique[hashlib.sha256(data).hexdigest()] = (data, mime)
        return list(unique.values())

    def _save_images(self, db, track_id, path):
        db.execute("DELETE FROM track_images WHERE track_id = ?", (track_id,))
        saved = 0
        for index, (data, mime) in enumerate(self._embedded_images(path)):
            digest = hashlib.sha256(data).hexdigest()
            extension = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp"}.get(mime, "img")
            target = self.artwork_dir / f"{track_id}-{index}-{digest[:16]}.{extension}"
            target.write_bytes(data)
            db.execute("INSERT OR REPLACE INTO track_images(track_id,path,mime_type) VALUES(?,?,?)",
                       (track_id, str(target), mime))
            saved += 1
        return saved

    def scan(self, roots, progress=None):
        result = {"files_seen": 0, "tracks_updated": 0, "tracks_removed": 0, "artwork_saved": 0, "errors": []}
        normalized_roots = [os.path.realpath(root).rstrip(os.sep) for root in roots if os.path.isdir(root)]
        seen_paths = set()

        def report():
            if progress:
                progress(dict(result))

        def onerror(error):
            result["errors"].append({"path": getattr(error, "filename", ""), "message": str(error)})
            report()

        with self._connect() as db:
            for root in roots:
                if not os.path.isdir(root):
                    continue
                for base, directories, names in os.walk(root, onerror=onerror, followlinks=False):
                    directories[:] = [directory for directory in directories
                                      if not directory.startswith(".") and
                                      not os.path.islink(os.path.join(base, directory))]
                    for name in names:
                        if name.startswith("."):
                            continue
                        path = os.path.realpath(os.path.join(base, name))
                        if Path(path).suffix.lower() not in EXTENSIONS:
                            continue
                        seen_paths.add(path)
                        result["files_seen"] += 1
                        try:
                            title, artist, album = self._metadata(path)
                            cur = db.execute("""INSERT INTO tracks(path,title,artist,album)
                                VALUES(?,?,?,?) ON CONFLICT(path) DO UPDATE SET
                                title=excluded.title, artist=excluded.artist, album=excluded.album,
                                updated_at=CURRENT_TIMESTAMP""", (path, title, artist, album))
                            result["tracks_updated"] += cur.rowcount
                            track_id = db.execute("SELECT id FROM tracks WHERE path = ?", (path,)).fetchone()[0]
                            result["artwork_saved"] += self._save_images(db, track_id, path)
                            if result["files_seen"] % 25 == 0:
                                db.commit()
                                report()
                        except (OSError, ValueError, RuntimeError) as error:
                            result["errors"].append({"path": path, "message": str(error)})
                            report()
            for row in db.execute("SELECT id, path FROM tracks").fetchall():
                path = row[1]
                inside_scanned_root = any(path == root or path.startswith(root + os.sep) for root in normalized_roots)
                if inside_scanned_root and path not in seen_paths:
                    db.execute("DELETE FROM track_images WHERE track_id = ?", (row[0],))
                    db.execute("DELETE FROM tracks WHERE id = ?", (row[0],))
                    result["tracks_removed"] += 1
            db.commit()
        report()
        return result

    def all(self):
        with self._connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM tracks ORDER BY artist, title")]

    def get(self, track_id):
        with self._connect() as db:
            row = db.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
            return dict(row) if row else None

    def images(self, track_id):
        with self._connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM track_images WHERE track_id = ? ORDER BY id", (track_id,))]

    def image(self, image_id):
        with self._connect() as db:
            row = db.execute("SELECT * FROM track_images WHERE id = ?", (image_id,)).fetchone()
            return dict(row) if row else None

    def random_next(self):
        with self._connect() as db:
            row = db.execute("SELECT * FROM tracks WHERE last_played IS NULL ORDER BY RANDOM() LIMIT 1").fetchone()
            if not row:
                row = db.execute("SELECT * FROM tracks ORDER BY RANDOM() LIMIT 1").fetchone()
            return dict(row) if row else None

    def mark_played(self, track_id):
        timestamp = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute("UPDATE tracks SET last_played = ? WHERE id = ?", (timestamp, track_id))
