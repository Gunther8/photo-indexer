import logging
import time
import json
import urllib.parse
from pathlib import Path
from typing import Optional, Iterator

import requests

from config import get_config
from utils.helpers import retry, is_media, is_raw, get_ext, PHOTO_EXTS, RAW_EXTS, VIDEO_EXTS

logger = logging.getLogger(__name__)

AUTH_BASE = "https://openapi.baidu.com/oauth/2.0"
PAN_BASE = "https://pan.baidu.com/rest/2.0/xpan"
PAN_API_BASE = "https://pan.baidu.com/api"


class BaiduPanError(Exception):
    pass


class BaiduPan:
    def __init__(self):
        self._cfg = get_config()

    # ── OAuth ────────────────────────────────────────────────────────────────

    def get_auth_url(self) -> str:
        params = {
            "response_type": "code",
            "client_id": self._cfg.get("baidu_app_key"),
            "redirect_uri": "oob",
            "scope": "basic,netdisk",
            "display": "page",
        }
        return f"{AUTH_BASE}/authorize?" + urllib.parse.urlencode(params)

    def exchange_code(self, code: str) -> dict:
        """Exchange authorization code for tokens. Returns token dict."""
        resp = requests.post(
            f"{AUTH_BASE}/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self._cfg.get("baidu_app_key"),
                "client_secret": self._cfg.get("baidu_app_secret"),
                "redirect_uri": "oob",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise BaiduPanError(f"Auth error: {data}")
        self._cfg.update({
            "baidu_access_token": data["access_token"],
            "baidu_refresh_token": data.get("refresh_token", ""),
        })
        return data

    def refresh_token(self) -> bool:
        refresh = self._cfg.get("baidu_refresh_token")
        if not refresh:
            return False
        try:
            resp = requests.post(
                f"{AUTH_BASE}/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                    "client_id": self._cfg.get("baidu_app_key"),
                    "client_secret": self._cfg.get("baidu_app_secret"),
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            if "access_token" in data:
                self._cfg.update({
                    "baidu_access_token": data["access_token"],
                    "baidu_refresh_token": data.get("refresh_token", refresh),
                })
                return True
        except Exception as e:
            logger.error(f"Token refresh failed: {e}")
        return False

    def is_authorized(self) -> bool:
        return bool(self._cfg.get("baidu_access_token"))

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _token(self) -> str:
        token = self._cfg.get("baidu_access_token")
        if not token:
            raise BaiduPanError("Not authorized. Please authenticate first.")
        return token

    def _proxies(self):
        proxy = self._cfg.get("http_proxy") or None
        return {"http": proxy, "https": proxy} if proxy else None

    @retry(max_attempts=3, base_delay=2.0, exceptions=(requests.RequestException,))
    def _get(self, url: str, params: dict) -> dict:
        params["access_token"] = self._token()
        resp = requests.get(url, params=params, timeout=30, proxies=self._proxies())
        if resp.status_code == 429:
            time.sleep(10)
            raise requests.RequestException("Rate limited")
        # 注意：token过期时百度返回 HTTP 400 + errno=-6，必须先解析JSON再raise_for_status
        try:
            data = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise BaiduPanError(f"非JSON响应: {resp.text[:200]}")
        errno = data.get("errno", 0)
        if errno in (-6, 110, 111):  # token expired/invalid
            logger.info(f"Token失效 (errno={errno})，尝试刷新...")
            if self.refresh_token():
                params["access_token"] = self._token()
                resp2 = requests.get(url, params=params, timeout=30, proxies=self._proxies())
                data = resp2.json()
                errno = data.get("errno", 0)
                if errno not in (0, None):
                    raise BaiduPanError(f"刷新token后仍失败 errno={errno}: {data.get('errmsg', '')}")
            else:
                raise BaiduPanError(f"Token expired and refresh failed. errno={errno}")
        elif errno not in (0, None):
            raise BaiduPanError(f"API error errno={errno}: {data.get('errmsg', '')}")
        elif resp.status_code >= 400:
            resp.raise_for_status()
        return data

    # ── File listing ─────────────────────────────────────────────────────────

    def list_dir(self, path: str, page: int = 1, limit: int = 1000) -> dict:
        data = self._get(
            f"{PAN_BASE}/file",
            {"method": "list", "dir": path, "limit": limit, "start": (page - 1) * limit, "order": "name"},
        )
        logger.debug(f"list_dir {path} page={page} → {len(data.get('list', []))} 条")
        return data

    def list_all_media(self, root_path: str = "/") -> Iterator[dict]:
        """Recursively yield all media file entries under root_path."""
        dirs_to_visit = [root_path]
        while dirs_to_visit:
            current = dirs_to_visit.pop(0)
            page = 1
            while True:
                try:
                    data = self.list_dir(current, page=page, limit=1000)
                except BaiduPanError as e:
                    logger.error(f"Failed to list {current}: {e}")
                    break

                entries = data.get("list", [])
                if not entries:
                    break

                for entry in entries:
                    if entry.get("isdir"):
                        dirs_to_visit.append(entry["path"])
                    else:
                        fname = entry.get("server_filename", "")
                        ext = get_ext(fname)
                        if ext in PHOTO_EXTS or ext in VIDEO_EXTS or ext in RAW_EXTS:
                            yield entry

                if len(entries) < 1000:
                    break
                page += 1

    def find_raw_jpeg_pairs(self, file_list: list[dict]) -> dict[str, str]:
        """
        Given a list of file entries, return a mapping from jpeg pan_path -> raw pan_path.
        """
        by_stem: dict[str, dict[str, str]] = {}
        for f in file_list:
            path = f["path"]
            ext = get_ext(f["server_filename"])
            stem = Path(path).stem.lower()
            parent = str(Path(path).parent)
            key = f"{parent}/{stem}"
            if key not in by_stem:
                by_stem[key] = {}
            if ext in RAW_EXTS:
                by_stem[key]["raw"] = path
            elif ext in PHOTO_EXTS:
                by_stem[key]["jpeg"] = path

        pairs: dict[str, str] = {}
        for entry in by_stem.values():
            if "raw" in entry and "jpeg" in entry:
                pairs[entry["jpeg"]] = entry["raw"]
        return pairs

    # ── Download ─────────────────────────────────────────────────────────────

    def get_download_url(self, fs_id: str) -> str:
        # filemetas must use the multimedia endpoint, not xpan/file
        data = self._get(
            "https://pan.baidu.com/rest/2.0/xpan/multimedia",
            {"method": "filemetas", "fsids": json.dumps([int(fs_id)]), "dlink": 1, "thumb": 0},
        )
        files = data.get("list", [])
        if not files:
            raise BaiduPanError(f"No file info for fs_id={fs_id}, response={data}")
        dlink = files[0].get("dlink")
        if not dlink:
            raise BaiduPanError(f"No dlink for fs_id={fs_id}, file_info={files[0]}")
        return dlink

    @retry(max_attempts=3, base_delay=2.0, exceptions=(requests.RequestException,))
    def download_file(self, fs_id: str, dest_path: str, on_progress=None) -> str:
        """Download file to dest_path. Returns dest_path.
        on_progress(downloaded_bytes, total_bytes) called every chunk if provided.
        """
        url = self.get_download_url(fs_id)
        headers = {"User-Agent": "pan.baidu.com"}
        resp = requests.get(
            url,
            headers=headers,
            params={"access_token": self._token()},
            stream=True,
            timeout=(30, 300),  # (connect_timeout, read_timeout)
            proxies=self._proxies(),
        )
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
                downloaded += len(chunk)
                if on_progress:
                    on_progress(downloaded, total)
        return dest_path

    # ── Thumbnail ────────────────────────────────────────────────────────────

    def get_thumbnail_url(self, path: str, width: int = 300, height: int = 300) -> Optional[str]:
        try:
            data = self._get(
                f"{PAN_BASE}/file",
                {
                    "method": "thumbnail",
                    "path": path,
                    "width": width,
                    "height": height,
                    "quality": 80,
                },
            )
            return data.get("url") or data.get("thumbs", {}).get("url2")
        except BaiduPanError:
            return None
