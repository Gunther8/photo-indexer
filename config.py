import json
import os
from pathlib import Path

CONFIG_DIR = Path.home() / ".photo_indexer"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_CONFIG = {
    "baidu_app_key": "",
    "baidu_app_secret": "",
    "baidu_access_token": "",
    "baidu_refresh_token": "",
    "qwen_api_key": "",
    "amap_api_key": "",
    "thumbnail_cache_dir": str(Path.home() / ".photo_indexer" / "thumbnails"),
    "download_dir": str(Path.home() / "Downloads" / "photo_indexer"),
    "batch_size": 100,
    "image_max_size": 1024,
    "qwen_model": "qwen3-vl-plus",
    "analysis_mode": "batch",
    "max_video_size_mb": 500,
    "max_photo_size_mb": 50,
    "exclude_paths": [],
    "scan_path": "/",
    "download_workers": 3,
    "http_proxy": "",
    "web_user": "admin",
    "web_pass": "",
    "web_domain": "",
    "web_port": 8080,
}


class Config:
    def __init__(self):
        self._data: dict = {}
        self._ensure_dirs()
        self.load()

    def _ensure_dirs(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        Path(DEFAULT_CONFIG["thumbnail_cache_dir"]).mkdir(parents=True, exist_ok=True)
        Path(DEFAULT_CONFIG["download_dir"]).mkdir(parents=True, exist_ok=True)

    def load(self):
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                self._data = {**DEFAULT_CONFIG, **saved}
            except (json.JSONDecodeError, IOError):
                self._data = dict(DEFAULT_CONFIG)
        else:
            self._data = dict(DEFAULT_CONFIG)
            self.save()

    def save(self):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value):
        self._data[key] = value
        self.save()

    def update(self, data: dict):
        self._data.update(data)
        self.save()

    def __getitem__(self, key):
        return self._data[key]

    def __setitem__(self, key, value):
        self.set(key, value)


_instance: Config | None = None


def get_config() -> Config:
    global _instance
    if _instance is None:
        _instance = Config()
    return _instance
