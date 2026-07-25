import sqlite3
import json
import logging
import struct
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional, Any

logger = logging.getLogger(__name__)

DB_PATH = Path.home() / ".photo_indexer" / "media.db"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS media (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    pan_path            TEXT NOT NULL UNIQUE,
    pan_fs_id           TEXT,
    filename            TEXT NOT NULL,
    media_type          TEXT NOT NULL,
    file_ext            TEXT,
    has_raw_pair        BOOLEAN DEFAULT 0,
    raw_pan_path        TEXT,

    taken_at            DATETIME,
    camera_make         TEXT,
    camera_model        TEXT,
    gps_lat             REAL,
    gps_lng             REAL,

    geo_country         TEXT,
    geo_province        TEXT,
    geo_city            TEXT,
    geo_district        TEXT,
    geo_detail          TEXT,

    description         TEXT,
    objects             TEXT,
    scene_tags          TEXT,
    weather             TEXT,
    season              TEXT,
    mood                TEXT,
    has_person          BOOLEAN,
    person_count        INTEGER,
    person_desc         TEXT,

    is_cover_worthy     BOOLEAN,
    cover_reason        TEXT,
    is_caption_worthy   BOOLEAN,
    caption_reason      TEXT,
    is_broll            BOOLEAN,
    visual_impact       INTEGER,
    is_vertical_comp    BOOLEAN,

    quality_score       INTEGER,

    index_status        TEXT DEFAULT 'pending',
    index_error         TEXT,
    thumbnail_cache     TEXT,
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS geo_cache (
    coord_key   TEXT PRIMARY KEY,
    result      TEXT NOT NULL,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS batch_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL UNIQUE,
    status      TEXT DEFAULT 'pending',
    media_ids   TEXT,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    completed_at DATETIME
);

CREATE VIRTUAL TABLE IF NOT EXISTS media_fts USING fts5(
    description,
    objects,
    scene_tags,
    geo_detail,
    content=media,
    content_rowid=id
);

CREATE TRIGGER IF NOT EXISTS media_ai AFTER INSERT ON media BEGIN
    INSERT INTO media_fts(rowid, description, objects, scene_tags, geo_detail)
    VALUES (new.id, new.description, new.objects, new.scene_tags, new.geo_detail);
END;

CREATE TRIGGER IF NOT EXISTS media_ad AFTER DELETE ON media BEGIN
    INSERT INTO media_fts(media_fts, rowid, description, objects, scene_tags, geo_detail)
    VALUES ('delete', old.id, old.description, old.objects, old.scene_tags, old.geo_detail);
END;

CREATE TRIGGER IF NOT EXISTS media_au AFTER UPDATE ON media BEGIN
    INSERT INTO media_fts(media_fts, rowid, description, objects, scene_tags, geo_detail)
    VALUES ('delete', old.id, old.description, old.objects, old.scene_tags, old.geo_detail);
    INSERT INTO media_fts(rowid, description, objects, scene_tags, geo_detail)
    VALUES (new.id, new.description, new.objects, new.scene_tags, new.geo_detail);
END;
"""


class Database:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()  # per-thread connection storage
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self):
        conn = self._connect()
        try:
            conn.executescript(SCHEMA_SQL)
            # 迁移：旧库补加 embedding 列
            try:
                conn.execute("ALTER TABLE media ADD COLUMN embedding BLOB")
                conn.commit()
            except Exception:
                pass  # 列已存在
        finally:
            conn.close()

    def get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = self._connect()
        return self._local.conn

    def close(self):
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None

    # ── Media CRUD ──────────────────────────────────────────────────────────

    def upsert_media(self, data: dict) -> int:
        conn = self.get_conn()
        columns = list(data.keys())
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(f"{c}=excluded.{c}" for c in columns if c != "pan_path")
        sql = (
            f"INSERT INTO media ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT(pan_path) DO UPDATE SET {updates}, updated_at=CURRENT_TIMESTAMP"
        )
        cur = conn.execute(sql, list(data.values()))
        conn.commit()
        return cur.lastrowid

    def update_media(self, media_id: int, data: dict):
        if not data:
            return
        conn = self.get_conn()
        # SQLite 不支持 list/dict，强制序列化
        safe = {}
        for k, v in data.items():
            if isinstance(v, list):
                safe[k] = json.dumps(v, ensure_ascii=False) if v else ""
            elif isinstance(v, dict):
                safe[k] = json.dumps(v, ensure_ascii=False)
            else:
                safe[k] = v
        sets = ", ".join(f"{k}=?" for k in safe)
        values = list(safe.values()) + [media_id]
        conn.execute(
            f"UPDATE media SET {sets}, updated_at=CURRENT_TIMESTAMP WHERE id=?", values
        )
        conn.commit()

    def get_media_by_id(self, media_id: int) -> Optional[dict]:
        conn = self.get_conn()
        row = conn.execute("SELECT * FROM media WHERE id=?", (media_id,)).fetchone()
        return dict(row) if row else None

    def get_media_by_path(self, pan_path: str) -> Optional[dict]:
        conn = self.get_conn()
        row = conn.execute("SELECT * FROM media WHERE pan_path=?", (pan_path,)).fetchone()
        return dict(row) if row else None

    def get_pending_media(self, limit: int = 100) -> list[dict]:
        conn = self.get_conn()
        rows = conn.execute(
            "SELECT * FROM media WHERE index_status IN ('pending','error') LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_paths(self) -> set[str]:
        conn = self.get_conn()
        rows = conn.execute("SELECT pan_path FROM media").fetchall()
        return {r[0] for r in rows}

    def delete_media_by_paths(self, paths: set[str]) -> int:
        if not paths:
            return 0
        conn = self.get_conn()
        path_list = list(paths)
        deleted = 0
        for i in range(0, len(path_list), 500):
            batch = path_list[i:i + 500]
            placeholders = ",".join("?" for _ in batch)
            cur = conn.execute(
                f"DELETE FROM media WHERE pan_path IN ({placeholders})", batch
            )
            deleted += cur.rowcount
        conn.commit()
        return deleted

    def count_by_status(self) -> dict[str, int]:
        conn = self.get_conn()
        rows = conn.execute(
            "SELECT index_status, COUNT(*) FROM media GROUP BY index_status"
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def search(
        self,
        query: str = "",
        filters: Optional[dict] = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict]:
        conn = self.get_conn()
        params: list[Any] = []
        conditions: list[str] = ["m.index_status='done'"]

        query_param_count = 0
        if query:
            keywords = query.split()
            fts_expr = " AND ".join(f'"{kw}"' for kw in keywords)
            like_parts = []
            like_params = []
            for kw in keywords:
                like = f"%{kw}%"
                like_parts.append(
                    "(m.description LIKE ? OR m.objects LIKE ? OR m.scene_tags LIKE ?)"
                )
                like_params.extend([like, like, like])
            conditions.append(
                "(m.id IN (SELECT rowid FROM media_fts WHERE media_fts MATCH ?) "
                "OR (" + " AND ".join(like_parts) + "))"
            )
            params.extend([fts_expr] + like_params)
            query_param_count = 1 + len(like_params)

        if filters:
            for key, val in filters.items():
                if val is None:
                    continue
                if key == "media_type":
                    conditions.append("m.media_type=?")
                    params.append(val)
                elif key == "geo_country":
                    conditions.append("m.geo_country=?")
                    params.append(val)
                elif key == "geo_province":
                    conditions.append("m.geo_province=?")
                    params.append(val)
                elif key == "geo_city":
                    conditions.append("m.geo_city=?")
                    params.append(val)
                elif key == "camera_model":
                    conditions.append("m.camera_model=?")
                    params.append(val)
                elif key == "weather":
                    conditions.append("m.weather=?")
                    params.append(val)
                elif key == "season":
                    conditions.append("m.season=?")
                    params.append(val)
                elif key == "mood":
                    conditions.append("m.mood=?")
                    params.append(val)
                elif key == "has_person":
                    conditions.append("m.has_person=?")
                    params.append(int(val))
                elif key == "is_cover_worthy":
                    conditions.append("m.is_cover_worthy=?")
                    params.append(int(val))
                elif key == "is_broll":
                    conditions.append("m.is_broll=?")
                    params.append(int(val))
                elif key == "quality_score_min":
                    conditions.append("m.quality_score>=?")
                    params.append(val)
                elif key == "visual_impact_min":
                    conditions.append("m.visual_impact>=?")
                    params.append(val)
                elif key == "taken_date":
                    # 兼容 EXIF 格式 "2023:05:15 ..." 和 ISO 格式 "2023-05-15 ..."
                    conditions.append("SUBSTR(REPLACE(m.taken_at, ':', '-'), 1, 10)=?")
                    params.append(val)
                elif key == "date_from":
                    conditions.append("m.taken_at>=?")
                    params.append(val)
                elif key == "date_to":
                    conditions.append("m.taken_at<=?")
                    params.append(val)

        where = " AND ".join(conditions)
        sql = f"SELECT m.* FROM media m WHERE {where} ORDER BY m.taken_at DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        try:
            rows = conn.execute(sql, params).fetchall()
        except Exception:
            if query:
                fallback_conds = [c for c in conditions if "media_fts" not in c]
                fallback_conds.append("(" + " AND ".join(like_parts) + ")")
                filter_params = params[query_param_count:-2]
                fallback_params = list(like_params) + list(filter_params) + [limit, offset]
                sql2 = f"SELECT m.* FROM media m WHERE {' AND '.join(fallback_conds)} ORDER BY m.taken_at DESC LIMIT ? OFFSET ?"
                rows = conn.execute(sql2, fallback_params).fetchall()
            else:
                rows = []
        return [dict(r) for r in rows]

    def get_filter_options(self, field: str) -> list[str]:
        conn = self.get_conn()
        rows = conn.execute(
            f"SELECT DISTINCT {field} FROM media WHERE {field} IS NOT NULL AND {field}!='' ORDER BY {field}"
        ).fetchall()
        return [r[0] for r in rows]

    # ── Embedding ───────────────────────────────────────────────────────────

    def set_embedding(self, media_id: int, vector: list[float]):
        blob = struct.pack(f"{len(vector)}f", *vector)
        conn = self.get_conn()
        conn.execute("UPDATE media SET embedding=? WHERE id=?", (blob, media_id))
        conn.commit()

    def get_all_embeddings(self) -> list[tuple[int, bytes]]:
        conn = self.get_conn()
        rows = conn.execute(
            "SELECT id, embedding FROM media WHERE embedding IS NOT NULL AND index_status='done'"
        ).fetchall()
        return [(r[0], bytes(r[1])) for r in rows]

    def get_pending_embedding_ids(self) -> list[int]:
        conn = self.get_conn()
        rows = conn.execute(
            "SELECT id FROM media WHERE description IS NOT NULL AND description != '' "
            "AND embedding IS NULL AND index_status='done'"
        ).fetchall()
        return [r[0] for r in rows]

    def get_description_by_id(self, media_id: int) -> Optional[str]:
        conn = self.get_conn()
        row = conn.execute("SELECT description FROM media WHERE id=?", (media_id,)).fetchone()
        return row[0] if row else None

    # ── Geo cache ───────────────────────────────────────────────────────────

    def get_geo_cache(self, coord_key: str) -> Optional[dict]:
        conn = self.get_conn()
        row = conn.execute(
            "SELECT result FROM geo_cache WHERE coord_key=?", (coord_key,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def set_geo_cache(self, coord_key: str, result: dict):
        conn = self.get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO geo_cache (coord_key, result) VALUES (?,?)",
            (coord_key, json.dumps(result, ensure_ascii=False)),
        )
        conn.commit()

    # ── Batch jobs ──────────────────────────────────────────────────────────

    def create_batch_job(self, job_id: str, media_ids: list[int]):
        conn = self.get_conn()
        conn.execute(
            "INSERT INTO batch_jobs (job_id, media_ids) VALUES (?,?)",
            (job_id, json.dumps(media_ids)),
        )
        conn.commit()

    def update_batch_job(self, job_id: str, status: str):
        conn = self.get_conn()
        completed_at = datetime.utcnow().isoformat() if status == "completed" else None
        conn.execute(
            "UPDATE batch_jobs SET status=?, completed_at=? WHERE job_id=?",
            (status, completed_at, job_id),
        )
        conn.commit()

    def get_pending_batch_jobs(self) -> list[dict]:
        conn = self.get_conn()
        rows = conn.execute(
            "SELECT * FROM batch_jobs WHERE status NOT IN ('completed','failed')"
        ).fetchall()
        return [dict(r) for r in rows]


_instance: Optional[Database] = None


def get_db() -> Database:
    global _instance
    if _instance is None:
        _instance = Database()
    return _instance
