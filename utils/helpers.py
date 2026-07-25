import logging
import time
import functools
from pathlib import Path

logger = logging.getLogger(__name__)

PHOTO_EXTS = {"jpg", "jpeg", "png", "heic", "heif", "webp", "tiff", "tif"}
RAW_EXTS = {"cr2", "cr3", "arw", "nef", "orf", "rw2", "dng"}
VIDEO_EXTS = {"mp4", "mov", "avi", "mkv", "m4v"}
ALL_MEDIA_EXTS = PHOTO_EXTS | VIDEO_EXTS


def get_ext(filename: str) -> str:
    return Path(filename).suffix.lstrip(".").lower()


def is_photo(filename: str) -> bool:
    return get_ext(filename) in PHOTO_EXTS


def is_raw(filename: str) -> bool:
    return get_ext(filename) in RAW_EXTS


def is_video(filename: str) -> bool:
    return get_ext(filename) in VIDEO_EXTS


def is_media(filename: str) -> bool:
    return get_ext(filename) in ALL_MEDIA_EXTS


def retry(max_attempts: int = 3, base_delay: float = 1.0, exceptions=(Exception,)):
    """Decorator for exponential-backoff retry."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    if attempt == max_attempts - 1:
                        raise
                    delay = base_delay * (2 ** attempt)
                    logger.warning(f"{func.__name__} attempt {attempt+1} failed: {e}. Retrying in {delay}s")
                    time.sleep(delay)
        return wrapper
    return decorator


def setup_logging(log_dir: Path):
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "app.log"

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # 文件：DEBUG 级别，记录所有细节，按 10MB 轮转保留 3 个文件
    from logging.handlers import RotatingFileHandler
    file_handler = RotatingFileHandler(
        str(log_file), maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)

    # 控制台：INFO 级别，只显示关键信息
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(fmt)

    # 屏蔽第三方库的 DEBUG 噪音
    for noisy in ("urllib3", "httpcore", "httpx", "openai._base_client",
                  "PIL", "exifread", "hpack", "h2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    logging.info(f"日志文件: {log_file}")
