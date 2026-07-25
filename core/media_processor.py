import logging
import sys
import tempfile
import os
from pathlib import Path
from typing import Optional

from PIL import Image

from config import get_config
from utils.helpers import get_ext, PHOTO_EXTS, VIDEO_EXTS

logger = logging.getLogger(__name__)


def _register_heif():
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except ImportError:
        logger.warning("pillow-heif not installed; HEIC/HEIF files may not open")


_register_heif()


class MediaProcessor:
    def __init__(self):
        self._cfg = get_config()
        self._max_size: int = self._cfg.get("image_max_size", 1024)

    def process_image(self, source_path: str) -> Optional[str]:
        """
        Compress image to max_size px on long edge, save as JPEG to a temp file.
        Returns temp file path (caller must delete).
        """
        try:
            img = Image.open(source_path)

            # Convert to RGB (handles RGBA, palette, etc.)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")

            img = self._resize(img)
            tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            img.save(tmp.name, "JPEG", quality=85, optimize=True)
            return tmp.name
        except Exception as e:
            logger.error(f"Image processing failed for {source_path}: {e}")
            return None

    def process_video(self, source_path: str) -> list[str]:
        """
        Extract key frames from video.
        Uses ffmpeg for frame extraction (handles H.265/HEVC reliably).
        Falls back to OpenCV+PySceneDetect if ffmpeg is not available.
        Returns list of temp JPEG file paths (caller must delete all).
        """
        paths = self._extract_frames_ffmpeg(source_path)
        if paths is not None:
            return paths
        return self._extract_frames_opencv(source_path)

    def _extract_frames_ffmpeg(self, source_path: str) -> list[str] | None:
        """用 ffmpeg 按时间均匀抽帧，最多20帧。返回 None 表示 ffmpeg 不可用。"""
        import shutil, subprocess
        # 优先用 conda 环境内的 ffmpeg，兼容 PATH 未设置的情况
        ffmpeg_bin = shutil.which("ffmpeg") or shutil.which(
            "ffmpeg", path=str(Path(sys.executable).parent)
        )
        if not ffmpeg_bin:
            return None
        ffprobe_bin = ffmpeg_bin.replace("ffmpeg", "ffprobe")
        try:
            # 先获取视频时长
            probe = subprocess.run(
                [ffprobe_bin, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", source_path],
                capture_output=True, text=True, timeout=30
            )
            duration = float(probe.stdout.strip() or 0)
            if duration <= 0:
                return None

            # 均匀取最多20个时间点
            n_frames = min(20, max(1, int(duration / 30)))  # 每30秒一帧，最多20帧
            timestamps = [duration * (i + 0.5) / n_frames for i in range(n_frames)]

            paths: list[str] = []
            for ts in timestamps:
                tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
                tmp.close()
                result = subprocess.run(
                    [ffmpeg_bin, "-ss", str(ts), "-i", source_path,
                     "-frames:v", "1", "-q:v", "2", "-y", tmp.name],
                    capture_output=True, timeout=60
                )
                if result.returncode == 0 and os.path.getsize(tmp.name) > 0:
                    # 缩放
                    try:
                        img = Image.open(tmp.name)
                        img = self._resize(img)
                        img.save(tmp.name, "JPEG", quality=85)
                        paths.append(tmp.name)
                    except Exception:
                        os.unlink(tmp.name)
                else:
                    os.unlink(tmp.name)

            logger.info(f"ffmpeg 抽帧: {source_path} → {len(paths)} 帧")
            return paths
        except Exception as e:
            logger.warning(f"ffmpeg 抽帧失败，回退到 opencv: {e}")
            return None

    def _extract_frames_opencv(self, source_path: str) -> list[str]:
        """OpenCV + PySceneDetect 抽帧（备用）。"""
        try:
            from scenedetect import open_video, SceneManager
            from scenedetect.detectors import ContentDetector
            import cv2

            video = open_video(source_path)
            scene_mgr = SceneManager()
            scene_mgr.add_detector(ContentDetector(threshold=27))
            scene_mgr.detect_scenes(video)
            scene_list = scene_mgr.get_scene_list()

            cap = cv2.VideoCapture(source_path)
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

            if not scene_list:
                frame_numbers = [total_frames // 2]
            else:
                frame_numbers = []
                for start, end in scene_list:
                    mid = (start.get_frames() + end.get_frames()) // 2
                    frame_numbers.append(mid)
                frame_numbers = frame_numbers[:20]

            paths: list[str] = []
            for fn in frame_numbers:
                cap.set(cv2.CAP_PROP_POS_FRAMES, fn)
                ret, frame = cap.read()
                if not ret:
                    continue
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                img = Image.fromarray(rgb)
                img = self._resize(img)
                tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
                img.save(tmp.name, "JPEG", quality=85)
                paths.append(tmp.name)

            cap.release()
            return paths

        except ImportError:
            logger.error("scenedetect or opencv not installed. Video frames cannot be extracted.")
            return []
        except Exception as e:
            logger.error(f"Video processing failed for {source_path}: {e}")
            return []

    def _resize(self, img: Image.Image) -> Image.Image:
        w, h = img.size
        max_side = max(w, h)
        if max_side <= self._max_size:
            return img
        ratio = self._max_size / max_side
        new_w = int(w * ratio)
        new_h = int(h * ratio)
        return img.resize((new_w, new_h), Image.LANCZOS)

    @staticmethod
    def cleanup(paths: list[str]):
        for p in paths:
            try:
                if p and os.path.exists(p):
                    os.unlink(p)
            except OSError:
                pass
