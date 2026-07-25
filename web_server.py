"""
Web查询服务器，支持HTTPS（自签名证书）
启动: python web_server.py
默认端口: 8443
"""
import json
import logging
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import threading
import time
import numpy as np
import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response, FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN

import io
import pillow_heif
pillow_heif.register_heif_opener()
from PIL import Image, ImageOps

from config import get_config
from core.database import get_db
from core.baidu_pan import BaiduPan
from utils.helpers import setup_logging

setup_logging(Path.home() / ".photo_indexer")
logger = logging.getLogger(__name__)

cfg = get_config()
db = get_db()

# ── 认证 ─────────────────────────────────────────────────────────────────────

security = HTTPBasic()
WEB_USER = cfg.get("web_user") or "admin"
WEB_PASS = cfg.get("web_pass") or "changeme"

# ── 登录限流：失败5次封锁IP ──────────────────────────────────────────────────
_MAX_FAILS = 5
_FAIL_WINDOW = 600           # 10分钟内累计失败才计数
_BLOCKED_FILE = Path.home() / ".photo_indexer" / "blocked_ips.json"

_auth_lock = threading.Lock()
_fail_records: dict[str, list[float]] = {}   # ip -> 失败时间戳列表
_blocked_ips: set[str] = set()

def _load_blocked():
    global _blocked_ips
    try:
        if _BLOCKED_FILE.exists():
            _blocked_ips = set(json.loads(_BLOCKED_FILE.read_text()))
            if _blocked_ips:
                logger.info(f"已加载 {len(_blocked_ips)} 个封锁IP")
    except Exception as e:
        logger.warning(f"加载封锁IP列表失败: {e}")

def _save_blocked():
    try:
        _BLOCKED_FILE.write_text(json.dumps(sorted(_blocked_ips)))
    except Exception as e:
        logger.warning(f"保存封锁IP列表失败: {e}")

_load_blocked()

def _client_ip(request: Request) -> str:
    # nginx 反代传来的真实IP
    ip = request.headers.get("X-Real-IP")
    if not ip:
        fwd = request.headers.get("X-Forwarded-For", "")
        ip = fwd.split(",")[0].strip() if fwd else ""
    return ip or (request.client.host if request.client else "unknown")

def require_auth(request: Request, credentials: HTTPBasicCredentials = Depends(security)):
    ip = _client_ip(request)

    with _auth_lock:
        if ip in _blocked_ips:
            raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="IP blocked")

    ok_user = secrets.compare_digest(credentials.username, WEB_USER)
    ok_pass = secrets.compare_digest(credentials.password, WEB_PASS)

    if not (ok_user and ok_pass):
        now = time.time()
        with _auth_lock:
            fails = [t for t in _fail_records.get(ip, []) if now - t < _FAIL_WINDOW]
            fails.append(now)
            _fail_records[ip] = fails
            logger.warning(f"登录失败 [{len(fails)}/{_MAX_FAILS}] IP={ip} user={credentials.username!r}")
            if len(fails) >= _MAX_FAILS:
                _blocked_ips.add(ip)
                _fail_records.pop(ip, None)
                _save_blocked()
                logger.warning(f"IP已封锁: {ip}")
                raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="IP blocked")
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )

    with _auth_lock:
        _fail_records.pop(ip, None)
    return credentials.username

# ── 缩略图缓存 ───────────────────────────────────────────────────────────────

THUMB_DIR = Path(cfg.get("thumbnail_cache_dir") or Path.home() / ".photo_indexer" / "thumbnails")
THUMB_DIR.mkdir(parents=True, exist_ok=True)

JPEG_CACHE_DIR = THUMB_DIR.parent / "jpeg_cache"
JPEG_CACHE_DIR.mkdir(parents=True, exist_ok=True)

_JPEG_MAX_SIDE = {"small": 1920, "medium": 3840, "large": None}
_JPEG_QUALITY  = {"small": 85,   "medium": 90,   "large": 95}

def get_thumbnail(media_id: int) -> bytes | None:
    cache_path = THUMB_DIR / f"{media_id}.jpg"
    if cache_path.exists():
        return cache_path.read_bytes()

    media = db.get_media_by_id(media_id)
    if not media:
        return None

    fs_id = media.get("pan_fs_id")
    if not fs_id:
        return None

    try:
        pan = BaiduPan()
        data = pan._get(
            "https://pan.baidu.com/rest/2.0/xpan/multimedia",
            {"method": "filemetas", "fsids": json.dumps([int(fs_id)]), "thumb": 1, "dlink": 0},
        )
        thumbs = (data.get("list") or [{}])[0].get("thumbs", {})
        # url3 约 850px 宽，适合预览
        url = thumbs.get("url3") or thumbs.get("url2") or thumbs.get("url1")
        if not url:
            return None
        resp = httpx.get(url, timeout=15, follow_redirects=True)
        if resp.status_code == 200:
            cache_path.write_bytes(resp.content)
            return resp.content
    except Exception as e:
        logger.warning(f"缩略图获取失败 media_id={media_id}: {e}")
    return None

# ── FastAPI ──────────────────────────────────────────────────────────────────

app = FastAPI(docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

@app.get("/", response_class=HTMLResponse)
def index(_: str = Depends(require_auth)):
    return FileResponse(Path(__file__).parent / "static" / "index.html")

@app.get("/detail/{media_id}", response_class=HTMLResponse)
def detail_page(media_id: int, _: str = Depends(require_auth)):
    return FileResponse(Path(__file__).parent / "static" / "detail.html")

@app.get("/api/search")
def search(
    q: str = Query(default=""),
    limit: int = Query(default=60, le=200),
    offset: int = Query(default=0),
    media_type: str = Query(default=""),
    season: str = Query(default=""),
    has_person: str = Query(default=""),
    is_cover_worthy: str = Query(default=""),
    taken_date: str = Query(default=""),
    _: str = Depends(require_auth),
):
    filters = {}
    if media_type:
        filters["media_type"] = media_type
    if season:
        filters["season"] = season
    if has_person == "1":
        filters["has_person"] = 1
    if is_cover_worthy == "1":
        filters["is_cover_worthy"] = 1
    if taken_date:
        filters["taken_date"] = taken_date

    results = db.search(query=q, filters=filters, limit=limit, offset=offset)
    return {
        "items": [
            {
                "id": r["id"],
                "filename": r["filename"],
                "media_type": r["media_type"],
                "description": r["description"],
                "scene_tags": json.loads(r["scene_tags"] or "[]"),
                "geo_city": r.get("geo_city"),
                "geo_detail": r.get("geo_detail"),
                "taken_at": r.get("taken_at"),
                "quality_score": r.get("quality_score"),
                "is_cover_worthy": r.get("is_cover_worthy"),
            }
            for r in results
        ],
        "count": len(results),
    }

@app.get("/api/media/{media_id}")
def media_detail(media_id: int, _: str = Depends(require_auth)):
    media = db.get_media_by_id(media_id)
    if not media:
        raise HTTPException(status_code=404)
    m = dict(media)
    m.pop("embedding", None)  # 二进制向量不需要返回给前端
    for field in ("objects", "scene_tags"):
        if m.get(field):
            try:
                m[field] = json.loads(m[field])
            except Exception:
                m[field] = []
    return m

@app.get("/api/thumbnail/{media_id}")
def thumbnail(media_id: int, _: str = Depends(require_auth)):
    data = get_thumbnail(media_id)
    if not data:
        raise HTTPException(status_code=404)
    return Response(content=data, media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=86400"})

@app.get("/api/jpeg/{media_id}")
def jpeg_convert(media_id: int, size: str = Query(default="large"), _: str = Depends(require_auth)):
    if size not in _JPEG_MAX_SIDE:
        raise HTTPException(400, "size 必须是 small/medium/large")

    cache_path = JPEG_CACHE_DIR / f"{media_id}_{size}.jpg"
    if cache_path.exists():
        return FileResponse(str(cache_path), media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=86400"})

    media = db.get_media_by_id(media_id)
    if not media:
        raise HTTPException(404)
    fs_id = media.get("pan_fs_id")
    if not fs_id:
        raise HTTPException(404, "无 fs_id")

    try:
        import requests as req_lib
        pan = BaiduPan()
        dlink = pan.get_download_url(fs_id)
        r = req_lib.get(dlink, headers={"User-Agent": "pan.baidu.com"},
                        params={"access_token": pan._token()}, timeout=(15, 300))
        r.raise_for_status()

        img = Image.open(io.BytesIO(r.content))
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")

        max_side = _JPEG_MAX_SIDE[size]
        if max_side:
            w, h = img.size
            if max(w, h) > max_side:
                ratio = max_side / max(w, h)
                img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)

        img.save(str(cache_path), "JPEG", quality=_JPEG_QUALITY[size], optimize=True)
        return FileResponse(str(cache_path), media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=86400"})
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"JPEG 转换失败 media_id={media_id} size={size}: {e}")
        raise HTTPException(502, str(e))

@app.get("/api/download/{media_id}")
def download(media_id: int, _: str = Depends(require_auth)):
    from urllib.parse import quote
    from fastapi.responses import StreamingResponse
    media = db.get_media_by_id(media_id)
    if not media:
        raise HTTPException(status_code=404)
    fs_id = media.get("pan_fs_id")
    if not fs_id:
        raise HTTPException(status_code=404, detail="无 fs_id")
    try:
        pan = BaiduPan()
        data = pan._get(
            "https://pan.baidu.com/rest/2.0/xpan/multimedia",
            {"method": "filemetas", "fsids": json.dumps([int(fs_id)]), "dlink": 1, "thumb": 0},
        )
        dlink = (data.get("list") or [{}])[0].get("dlink")
        if not dlink:
            raise HTTPException(status_code=502, detail="获取下载链接失败")
        filename = media.get("filename", f"media_{media_id}")
        encoded_name = quote(filename, safe="")
        content_disposition = f"attachment; filename*=UTF-8''{encoded_name}"

        import requests as req_lib
        r = req_lib.get(
            dlink,
            headers={"User-Agent": "pan.baidu.com"},
            params={"access_token": pan._token()},
            stream=True,
            timeout=(15, 300),
        )
        r.raise_for_status()

        resp_headers = {"Content-Disposition": content_disposition}
        cl = r.headers.get("content-length")
        if cl:
            resp_headers["Content-Length"] = cl

        def iter_and_close():
            try:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        yield chunk
            finally:
                r.close()

        return StreamingResponse(
            iter_and_close(),
            media_type="application/octet-stream",
            headers=resp_headers,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"下载失败 media_id={media_id}: {e}")
        raise HTTPException(status_code=502, detail=str(e))

# ── 相关图片（向量相似度）────────────────────────────────────────────────────

_emb_cache_lock = threading.Lock()
_emb_ids: list[int] = []
_emb_matrix: np.ndarray | None = None
_emb_loaded_at: float = 0.0
_EMB_TTL = 7200  # 2 小时重新加载一次（原 10 分钟，频繁重载导致内存泄漏）

def _load_embeddings():
    import gc
    global _emb_ids, _emb_matrix, _emb_loaded_at
    # 显式释放旧矩阵，防止 Python GC 延迟导致内存堆积
    _emb_ids = []
    _emb_matrix = None
    gc.collect()

    rows = db.get_all_embeddings()
    if not rows:
        _emb_loaded_at = time.time()
        return
    ids, vecs = [], []
    for mid, blob in rows:
        v = np.frombuffer(blob, dtype=np.float32).copy()
        if v.size > 0:
            ids.append(mid)
            vecs.append(v)
    mat = np.array(vecs, dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1
    _emb_ids = ids
    _emb_matrix = mat / norms
    _emb_loaded_at = time.time()
    logger.info(f"向量缓存加载完成：{len(ids)} 条")

def _get_related_ids(media_id: int, top_k: int = 12) -> list[int]:
    global _emb_matrix, _emb_ids
    with _emb_cache_lock:
        if _emb_matrix is None or (time.time() - _emb_loaded_at) > _EMB_TTL:
            _load_embeddings()
        if _emb_matrix is None or not _emb_ids:
            return []
        try:
            idx = _emb_ids.index(media_id)
        except ValueError:
            return []
        sims = _emb_matrix @ _emb_matrix[idx]
        sims[idx] = -1  # 排除自身
        top_idx = np.argsort(sims)[::-1][:top_k]
        return [_emb_ids[int(i)] for i in top_idx]

@app.get("/api/related/{media_id}")
def related(media_id: int, _: str = Depends(require_auth)):
    related_ids = _get_related_ids(media_id)
    if not related_ids:
        return {"items": []}
    placeholders = ",".join("?" * len(related_ids))
    conn = db.get_conn()
    rows = conn.execute(
        f"SELECT id, filename, media_type, geo_city FROM media WHERE id IN ({placeholders})",
        related_ids,
    ).fetchall()
    # 按相似度顺序排列
    order = {mid: i for i, mid in enumerate(related_ids)}
    items = sorted([dict(r) for r in rows], key=lambda r: order.get(r["id"], 99))
    return {"items": items}

@app.get("/api/stats")
def stats(_: str = Depends(require_auth)):
    conn = db.get_conn()
    by_status = db.count_by_status()
    rows = conn.execute(
        "SELECT media_type, COUNT(*) as cnt FROM media WHERE index_status='done' GROUP BY media_type"
    ).fetchall()
    by_type = {r["media_type"]: r["cnt"] for r in rows}
    total = conn.execute("SELECT COUNT(*) FROM media").fetchone()[0]
    return {
        "by_status": by_status,
        "done_photo": by_type.get("photo", 0),
        "done_video": by_type.get("video", 0),
        "done": by_status.get("done", 0),
        "total": total,
    }

# ── 启动 ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    port = int(cfg.get("web_port") or 80)
    domain = cfg.get("web_domain") or "localhost"

    print(f"""
╔════════════════════════════════════════╗
║   Photo Indexer Web                    ║
║   http://{domain}:{port if port != 80 else ''}
║   用户名: {WEB_USER}
║   密  码: (见 ~/.photo_indexer/config.json 的 web_pass)
╚════════════════════════════════════════╝
""")

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8080,
        log_level="warning",
    )
