"""
回填视频录制日期：先从文件名解析，解析不到的查百度网盘 filemetas API 取 local_mtime。
用法: python backfill_video_dates.py [--dry-run]
"""
import sys
import re
import time
import json
import logging
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).parent))

from core.database import get_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

_CST = timezone(timedelta(hours=8))

# ── 文件名日期解析 ──────────────────────────────────────────────────────────

_PATTERNS = [
    # VID_20191012_234809.mp4 / IMG_20210329_141830.mp4 / InShot_20190102_212719435.mp4
    re.compile(r'(\d{4})(0[1-9]|1[0-2])([0-2]\d|3[01])[\s_-]?([01]\d|2[0-3])([0-5]\d)([0-5]\d)'),
    # 2024-07-29 113636.mov / 2024-04-02_233430.mov
    re.compile(r'(\d{4})-(0[1-9]|1[0-2])-([0-2]\d|3[01])[\s_-]?([01]\d|2[0-3])([0-5]\d)([0-5]\d)'),
    # 2020_11_22_11_40_33_Cache.mp4
    re.compile(r'(\d{4})_(0[1-9]|1[0-2])_([0-2]\d|3[01])_([01]\d|2[0-3])_([0-5]\d)_([0-5]\d)'),
    # 2016_0302_230352_557.MP4
    re.compile(r'(\d{4})_(0[1-9]|1[0-2])([0-2]\d|3[01])_([01]\d|2[0-3])([0-5]\d)([0-5]\d)'),
]

# Unix 时间戳: go_compose_video_1602138513571.mp4, wx_camera_1620306560530.mp4, mmexport1699964045790.mp4
_TS_PATTERN = re.compile(r'(\d{10})(\d{3})?')


def parse_date_from_filename(filename: str):
    stem = Path(filename).stem
    for pat in _PATTERNS:
        m = pat.search(stem)
        if m:
            y, mo, d, h, mi, s = m.groups()
            try:
                dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s))
                if 2000 <= dt.year <= 2030:
                    return dt.strftime("%Y-%m-%dT%H:%M:%S")
            except ValueError:
                continue

    for m in _TS_PATTERN.finditer(stem):
        ts = int(m.group(1))
        if 946684800 <= ts <= 1893456000:  # 2000-01-01 ~ 2030-01-01
            dt = datetime.fromtimestamp(ts, tz=_CST)
            return dt.strftime("%Y-%m-%dT%H:%M:%S")

    return None


# ── 百度网盘 filemetas ────────────────────────────────────────────────────────

def fetch_mtime_batch(fs_ids: list[str], token: str) -> dict[str, str]:
    """批量查 filemetas，返回 {fs_id: taken_at_str}"""
    import requests
    result = {}
    id_list = json.dumps([int(fid) for fid in fs_ids if fid])
    r = requests.get(
        "https://pan.baidu.com/rest/2.0/xpan/multimedia",
        params={
            "method": "filemetas",
            "access_token": token,
            "fsids": id_list,
            "dlink": 0,
        },
        timeout=30,
    )
    data = r.json()
    if data.get("errno") != 0:
        logger.warning(f"filemetas errno={data.get('errno')}: {data}")
        return result
    for item in data.get("list", []):
        fs_id = str(item.get("fs_id", ""))
        local_mtime = item.get("local_mtime", 0)
        if local_mtime and local_mtime > 946684800:
            dt = datetime.fromtimestamp(local_mtime, tz=_CST)
            if 2000 <= dt.year <= 2030:
                result[fs_id] = dt.strftime("%Y-%m-%dT%H:%M:%S")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只统计不写库")
    args = parser.parse_args()

    db = get_db()
    conn = db.get_conn()

    rows = conn.execute(
        "SELECT id, filename, pan_fs_id FROM media "
        "WHERE media_type='video' AND (taken_at IS NULL OR taken_at='')"
    ).fetchall()
    total = len(rows)
    logger.info(f"共 {total} 个视频需要回填日期")

    if total == 0:
        return

    # ── 第一轮：文件名解析 ──────────────────────────────────────────────────
    parsed = 0
    remaining = []
    for row in rows:
        mid, fname, fs_id = row[0], row[1], row[2]
        dt = parse_date_from_filename(fname)
        if dt:
            if not args.dry_run:
                conn.execute("UPDATE media SET taken_at=? WHERE id=?", (dt, mid))
            parsed += 1
        else:
            remaining.append((mid, fname, fs_id))

    if not args.dry_run:
        conn.commit()
    logger.info(f"文件名解析：{parsed}/{total} 成功，剩余 {len(remaining)} 需查 API")

    if not remaining or args.dry_run:
        logger.info(f"完成。文件名解析={parsed}，剩余={len(remaining)}")
        return

    # ── 第二轮：百度网盘 filemetas ──────────────────────────────────────────
    cfg_path = Path.home() / ".photo_indexer" / "config.json"
    cfg = json.loads(cfg_path.read_text())
    token = cfg.get("baidu_access_token", "")
    if not token:
        logger.warning("无百度网盘 token，跳过 API 回填")
        return

    api_filled = 0
    batch_size = 100
    for i in range(0, len(remaining), batch_size):
        batch = remaining[i:i + batch_size]
        fs_ids = [str(r[2]) for r in batch if r[2]]
        if not fs_ids:
            continue
        mtime_map = fetch_mtime_batch(fs_ids, token)

        for mid, fname, fs_id in batch:
            dt = mtime_map.get(str(fs_id))
            if dt:
                conn.execute("UPDATE media SET taken_at=? WHERE id=?", (dt, mid))
                api_filled += 1

        conn.commit()
        done = min(i + batch_size, len(remaining))
        logger.info(f"API 进度 {done}/{len(remaining)}，已回填 {api_filled}")
        time.sleep(0.3)

    logger.info(f"完成。文件名解析={parsed}，API回填={api_filled}，总计={parsed + api_filled}/{total}")


if __name__ == "__main__":
    main()
