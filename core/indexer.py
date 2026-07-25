import json
import logging
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional

WORK_DIR = Path.home() / ".photo_indexer" / "work"

from config import get_config
from core.database import get_db
from core.baidu_pan import BaiduPan
from core.media_processor import MediaProcessor
from core.ai_analyzer import AIAnalyzer
from core.geo_coder import GeoCoder
from utils.exif_reader import read_exif, read_video_metadata
from utils.helpers import get_ext, PHOTO_EXTS, VIDEO_EXTS, RAW_EXTS

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, str], None]


class Indexer:
    def __init__(self):
        self._cfg = get_config()
        self._db = get_db()
        self._pan = BaiduPan()
        self._processor = MediaProcessor()
        self._ai = AIAnalyzer()
        self._geo = GeoCoder()

    # ── Scan ─────────────────────────────────────────────────────────────────

    def scan(self, progress_cb: Optional[ProgressCallback] = None) -> int:
        """
        Enumerate all media files in the configured scan path and insert new ones as 'pending'.
        Returns count of newly discovered files.
        """
        scan_path = self._cfg.get("scan_path", "/")
        existing_paths = self._db.get_all_paths()
        max_video_mb = self._cfg.get("max_video_size_mb", 500)
        max_photo_mb = self._cfg.get("max_photo_size_mb", 50)
        exclude_paths: list[str] = self._cfg.get("exclude_paths", [])
        logger.info(f"开始扫描: {scan_path}，数据库已有 {len(existing_paths)} 个文件")

        if progress_cb:
            progress_cb(0, 0, f"正在遍历网盘目录: {scan_path} ...")

        # Collect files with live progress updates
        all_files: list[dict] = []
        all_pan_paths: set[str] = set()
        skipped_size = 0
        skipped_path = 0
        found_count = 0
        for entry in self._pan.list_all_media(scan_path):
            path = entry.get("path", "")
            fname = entry.get("server_filename", "")
            size_bytes = entry.get("size", 0)
            size_mb = size_bytes / (1024 * 1024)
            ext = get_ext(fname)

            all_pan_paths.add(path)

            # 排除路径过滤
            if exclude_paths and any(path.startswith(ep) for ep in exclude_paths):
                skipped_path += 1
                logger.debug(f"跳过（排除路径）: {path}")
                continue

            # 体积过滤
            if ext in VIDEO_EXTS and max_video_mb > 0 and size_mb > max_video_mb:
                skipped_size += 1
                logger.info(f"跳过大视频 ({size_mb:.0f}MB): {path}")
                continue
            if ext in PHOTO_EXTS and max_photo_mb > 0 and size_mb > max_photo_mb:
                skipped_size += 1
                logger.info(f"跳过大图片 ({size_mb:.0f}MB): {path}")
                continue

            all_files.append(entry)
            found_count += 1
            if progress_cb and found_count % 50 == 0:
                progress_cb(0, 0, f"正在扫描，已发现 {found_count} 个媒体文件（跳过 {skipped_size} 个超大文件）...")

        logger.info(f"网盘遍历完成，共找到 {len(all_files)} 个媒体文件，跳过 {skipped_size} 个超大文件，跳过 {skipped_path} 个排除路径文件")

        if progress_cb:
            progress_cb(0, 0, f"正在识别 RAW/JPEG 配对...")

        pairs = self._pan.find_raw_jpeg_pairs(all_files)

        new_count = 0
        for entry in all_files:
            path = entry["path"]
            fname = entry.get("server_filename", "")
            ext = get_ext(fname)

            if path in existing_paths:
                continue

            # Skip RAW files — they're stored as metadata on the JPEG record
            if ext in RAW_EXTS:
                continue

            media_type = "video" if ext in VIDEO_EXTS else "photo"
            raw_path = pairs.get(path)

            self._db.upsert_media({
                "pan_path": path,
                "pan_fs_id": str(entry.get("fs_id", "")),
                "filename": fname,
                "media_type": media_type,
                "file_ext": ext,
                "has_raw_pair": 1 if raw_path else 0,
                "raw_pan_path": raw_path,
                "index_status": "pending",
            })
            new_count += 1

        # 清理网盘上已不存在的记录
        orphan_paths = existing_paths - all_pan_paths
        removed_count = 0
        if orphan_paths:
            removed_count = self._db.delete_media_by_paths(orphan_paths)
            logger.info(f"清理了 {removed_count} 条已删除文件的记录")

        logger.info(f"扫描完成，新增 {new_count} 个文件，清理 {removed_count} 个失效记录")
        if progress_cb:
            progress_cb(0, 0, f"扫描完成，共 {len(all_files)} 个媒体文件，新增 {new_count}，清理 {removed_count}")

        return new_count

    # ── Process ──────────────────────────────────────────────────────────────

    def process_pending(self, progress_cb: Optional[ProgressCallback] = None):
        """Process all pending media items."""
        mode = self._cfg.get("analysis_mode", "batch")
        if mode == "batch":
            self._process_batch(progress_cb)
        else:
            self._process_realtime(progress_cb)

    def _process_realtime(self, progress_cb: Optional[ProgressCallback] = None):
        batch_size = self._cfg.get("batch_size", 100)
        pending = self._db.get_pending_media(limit=batch_size)
        total = len(pending)

        for idx, media in enumerate(pending):
            if progress_cb:
                progress_cb(idx, total, f"处理中: {media['filename']}")

            self._process_one(media)

        if progress_cb:
            progress_cb(total, total, "实时分析完成")

    def _process_one(self, media: dict):
        media_id = media["id"]
        self._db.update_media(media_id, {"index_status": "processing"})

        tmp_files: list[str] = []
        try:
            # 1. Download
            fs_id = media.get("pan_fs_id")
            if not fs_id:
                raise ValueError("No fs_id for media")

            suffix = f".{media['file_ext']}"
            tmp_original = tempfile.mktemp(suffix=suffix)
            self._pan.download_file(fs_id, tmp_original)
            tmp_files.append(tmp_original)

            # 2. EXIF / video metadata
            exif_data = {}
            if media["media_type"] == "photo":
                exif_data = read_exif(tmp_original)
            else:
                exif_data = read_video_metadata(tmp_original)

            # 3. Geo
            geo_data = {}
            lat = exif_data.get("gps_lat")
            lng = exif_data.get("gps_lng")
            if lat is not None and lng is not None:
                geo_data = self._geo.reverse_geocode(lat, lng)

            # 4. Process image/video
            ai_result = None
            if media["media_type"] == "photo":
                compressed = self._processor.process_image(tmp_original)
                if compressed:
                    tmp_files.append(compressed)
                    ai_result = self._ai.analyze_image(compressed)
            else:
                frames = self._processor.process_video(tmp_original)
                tmp_files.extend(frames)
                if frames:
                    frame_results = self._ai.analyze_images(frames)
                    ai_result = self._ai.merge_video_frame_results(frame_results)

            # 5. Build update record
            update: dict = {"index_status": "done"}
            update.update(exif_data)
            update.update(geo_data)

            if ai_result:
                update.update({
                    "description": ai_result.get("description"),
                    "objects": json.dumps(ai_result.get("objects", []), ensure_ascii=False),
                    "scene_tags": json.dumps(ai_result.get("scene_tags", []), ensure_ascii=False),
                    "weather": ai_result.get("weather"),
                    "season": ai_result.get("season"),
                    "mood": ai_result.get("mood"),
                    "has_person": ai_result.get("has_person"),
                    "person_count": ai_result.get("person_count"),
                    "person_desc": ai_result.get("person_desc"),
                    "is_cover_worthy": ai_result.get("is_cover_worthy"),
                    "cover_reason": ai_result.get("cover_reason"),
                    "is_caption_worthy": ai_result.get("is_caption_worthy"),
                    "caption_reason": ai_result.get("caption_reason"),
                    "is_broll": ai_result.get("is_broll"),
                    "visual_impact": ai_result.get("visual_impact"),
                    "is_vertical_comp": ai_result.get("is_vertical_comp"),
                    "quality_score": ai_result.get("quality_score"),
                })

            self._db.update_media(media_id, update)

            # 生成描述向量
            desc = (ai_result or {}).get("description") or ""
            if desc:
                vec = self._ai.generate_embedding(desc)
                if vec:
                    self._db.set_embedding(media_id, vec)

            logger.info(f"Done: {media['filename']}")

        except Exception as e:
            logger.error(f"Failed to process {media['filename']}: {e}", exc_info=True)
            self._db.update_media(media_id, {
                "index_status": "error",
                "index_error": str(e)[:500],
            })
        finally:
            self._processor.cleanup(tmp_files)

    # ── Batch mode ───────────────────────────────────────────────────────────

    def _process_batch(self, progress_cb: Optional[ProgressCallback] = None):
        # First resume any pending batch jobs
        pending_jobs = self._db.get_pending_batch_jobs()
        if pending_jobs:
            logger.info(f"轮询 {len(pending_jobs)} 个未完成的 AI 批处理任务，请稍候...")
            if progress_cb:
                progress_cb(0, 0, f"检查 {len(pending_jobs)} 个 AI 批处理任务状态...")
        for i, job in enumerate(pending_jobs, 1):
            if progress_cb:
                progress_cb(i, len(pending_jobs), f"检查批处理任务 [{i}/{len(pending_jobs)}] {job['job_id'][:8]}...")
            self._resume_batch_job(job)
        if pending_jobs:
            logger.info(f"批处理任务轮询完毕，继续下载下一批...")

        WORK_DIR.mkdir(parents=True, exist_ok=True)
        batch_size = self._cfg.get("batch_size", 20)
        workers = self._cfg.get("download_workers", 3)

        round_num = 0
        while True:
            pending = self._db.get_pending_media(limit=batch_size)
            if not pending:
                if progress_cb:
                    progress_cb(0, 0, "没有待处理文件，全部完成")
                break

            round_num += 1
            total = len(pending)
            if progress_cb:
                progress_cb(0, total, f"第 {round_num} 批：准备 {total} 个文件...")

            batch_items, media_id_by_custom, tmp_compressed = \
                self._prepare_batch(pending, total, workers, progress_cb)

            if not batch_items:
                logger.warning(f"第 {round_num} 批全部失败，继续下一批")
                continue

            self._submit_batch(batch_items, media_id_by_custom, tmp_compressed,
                               pending, total, round_num, progress_cb)

    def _prepare_batch(self, pending, total, workers, progress_cb):
        """并行下载+EXIF+压缩，返回 (batch_items, media_id_by_custom, tmp_compressed)"""
        batch_items: list[dict] = []
        media_id_by_custom: dict[str, int] = {}
        tmp_compressed: dict[int, list[str]] = {}
        done_count = [0]
        done_lock = threading.Lock()

        def _prepare_one(media: dict, idx: int):
            self._db.update_media(media["id"], {"index_status": "processing"})
            fs_id = media.get("pan_fs_id")
            if not fs_id:
                raise ValueError("No fs_id")

            suffix = f".{media['file_ext']}"
            tmp_original = str(WORK_DIR / f"{fs_id}{suffix}")

            if os.path.exists(tmp_original):
                logger.info(f"[{idx+1}/{total}] 已缓存: {media['filename']}")
                if progress_cb:
                    progress_cb(idx, total, f"[{idx+1}/{total}] 已缓存: {media['filename']}")
            else:
                logger.info(f"[{idx+1}/{total}] 下载: {media['filename']}")
                if progress_cb:
                    progress_cb(idx, total, f"[{idx+1}/{total}] 连接中: {media['filename']} ...")

                def _dl_progress(dl, tot, _fname=media['filename'], _idx=idx):
                    if not progress_cb or tot <= 0:
                        return
                    pct = int(dl * 100 / tot)
                    progress_cb(_idx, total,
                                f"[{_idx+1}/{total}] 下载 {_fname}  "
                                f"{dl/1048576:.1f}/{tot/1048576:.1f} MB ({pct}%)")

                self._pan.download_file(fs_id, tmp_original, on_progress=_dl_progress)
                logger.info(f"[{idx+1}/{total}] 已下载: {media['filename']}")

            tmp_files = [tmp_original]

            exif_data = {}
            if media["media_type"] == "photo":
                exif_data = read_exif(tmp_original)
            else:
                exif_data = read_video_metadata(tmp_original)

            geo_data = {}
            lat, lng = exif_data.get("gps_lat"), exif_data.get("gps_lng")
            if lat is not None and lng is not None:
                geo_data = self._geo.reverse_geocode(lat, lng)

            pre_update = {**exif_data, **geo_data}
            if pre_update:
                self._db.update_media(media["id"], pre_update)

            entries = []
            if media["media_type"] == "photo":
                compressed = self._processor.process_image(tmp_original)
                if compressed:
                    tmp_files.append(compressed)
                    entries.append({"custom_id": f"media_{media['id']}", "image_path": compressed})
            else:
                frames = self._processor.process_video(tmp_original)
                for i, frame in enumerate(frames):
                    entries.append({"custom_id": f"media_{media['id']}_frame{i}", "image_path": frame})
                tmp_files.extend(frames)

            return media["id"], entries, tmp_files

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_prepare_one, media, idx): media
                for idx, media in enumerate(pending)
            }
            for future in as_completed(futures):
                media = futures[future]
                try:
                    media_id, entries, tmp_files = future.result()
                    if entries:
                        for e in entries:
                            batch_items.append(e)
                            media_id_by_custom[e["custom_id"]] = media_id
                        tmp_compressed[media_id] = tmp_files
                    else:
                        self._processor.cleanup(tmp_files)

                    with done_lock:
                        done_count[0] += 1
                    if progress_cb:
                        progress_cb(done_count[0], total,
                                    f"准备完成 {done_count[0]}/{total}: {media['filename']}")

                except Exception as e:
                    logger.error(f"跳过 {media['filename']}（失败3次）: {e}")
                    self._db.update_media(media["id"], {
                        "index_status": "error",
                        "index_error": str(e)[:500],
                    })

        return batch_items, media_id_by_custom, tmp_compressed

    def _submit_batch(self, batch_items, media_id_by_custom, tmp_compressed,
                      pending, total, round_num, progress_cb):
        try:
            if progress_cb:
                progress_cb(total, total,
                            f"第 {round_num} 批：上传 {len(batch_items)} 张图片到AI...")
            logger.info(f"正在上传第 {round_num} 批 {len(batch_items)} 张图片...")
            jsonl_path = self._ai.build_batch_file(batch_items)
            batch_id = self._ai.submit_batch(jsonl_path)
            os.unlink(jsonl_path)

            all_media_ids = list(set(media_id_by_custom.values()))
            self._db.create_batch_job(batch_id, all_media_ids)
            logger.info(f"第 {round_num} 批已提交: {batch_id}（{len(batch_items)} 张），继续下一批")

            if progress_cb:
                progress_cb(total, total,
                            f"第 {round_num} 批已提交 {batch_id}，立即开始下一批...")

            for tmp_list in tmp_compressed.values():
                self._processor.cleanup(tmp_list)

        except Exception as e:
            logger.error(f"第 {round_num} 批提交失败: {e}", exc_info=True)
            for mid in set(media_id_by_custom.values()):
                self._db.update_media(mid, {"index_status": "pending"})
            for tmp_list in tmp_compressed.values():
                # 保留 WORK_DIR 里的原始文件
                self._processor.cleanup([f for f in tmp_list if str(WORK_DIR) not in f])

    def _resume_batch_job(self, job: dict):
        batch_id = job["job_id"]
        try:
            status = self._ai.check_batch_status(batch_id)
            self._db.update_batch_job(batch_id, status)

            if status != "completed":
                logger.info(f"Batch job {batch_id} status: {status}")
                return

            results = self._ai.get_batch_results(batch_id)

            # 按 media_id 收集所有帧的结果，视频可能有多帧
            frames_by_media: dict[int, list] = {}
            errors_by_media: dict[int, str] = {}

            for custom_id, ai_result in results.items():
                # custom_id 格式: media_123 或 media_123_frame0
                parts = custom_id.split("_")
                media_id = int(parts[1])
                if ai_result is None:
                    errors_by_media.setdefault(media_id, "AI returned no result")
                else:
                    frames_by_media.setdefault(media_id, []).append(ai_result)

            # 写入没有任何成功帧的错误
            for media_id, err in errors_by_media.items():
                if media_id not in frames_by_media:
                    self._db.update_media(media_id, {
                        "index_status": "error",
                        "index_error": err,
                    })

            # 合并多帧结果后写入
            for media_id, frame_results in frames_by_media.items():
                if len(frame_results) == 1:
                    merged = frame_results[0]
                else:
                    # 多帧视频：合并（取视觉冲击力最高帧的描述，合并标签）
                    merged = self._ai.merge_video_frame_results(frame_results)

                if not merged:
                    continue

                update = {
                    "index_status": "done",
                    "description": merged.get("description"),
                    "objects": json.dumps(merged.get("objects", []), ensure_ascii=False),
                    "scene_tags": json.dumps(merged.get("scene_tags", []), ensure_ascii=False),
                    "weather": merged.get("weather"),
                    "season": merged.get("season"),
                    "mood": merged.get("mood"),
                    "has_person": merged.get("has_person"),
                    "person_count": merged.get("person_count"),
                    "person_desc": merged.get("person_desc"),
                    "is_cover_worthy": merged.get("is_cover_worthy"),
                    "cover_reason": merged.get("cover_reason"),
                    "is_caption_worthy": merged.get("is_caption_worthy"),
                    "caption_reason": merged.get("caption_reason"),
                    "is_broll": merged.get("is_broll"),
                    "visual_impact": merged.get("visual_impact"),
                    "is_vertical_comp": merged.get("is_vertical_comp"),
                    "quality_score": merged.get("quality_score"),
                }
                self._db.update_media(media_id, update)

                # 生成描述向量
                desc = merged.get("description") or ""
                if desc:
                    vec = self._ai.generate_embedding(desc)
                    if vec:
                        self._db.set_embedding(media_id, vec)

            logger.info(f"Batch job {batch_id} completed: {len(frames_by_media)} 个媒体写入DB")

        except Exception as e:
            logger.error(f"Failed to resume batch job {batch_id}: {e}")

    def check_batch_jobs(self, progress_cb: Optional[ProgressCallback] = None):
        """Check status of all pending batch jobs and write results if completed."""
        jobs = self._db.get_pending_batch_jobs()
        for job in jobs:
            if progress_cb:
                progress_cb(0, len(jobs), f"检查任务 {job['job_id']}...")
            self._resume_batch_job(job)
        if progress_cb:
            progress_cb(len(jobs), len(jobs), "批处理检查完成")
