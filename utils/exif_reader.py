import logging
import struct
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


def _parse_gps_coord(values, ref: str) -> Optional[float]:
    """Convert EXIF GPS IFDRational tuple (deg, min, sec) + ref to decimal degrees."""
    try:
        def to_float(v):
            if hasattr(v, "num"):
                return v.num / v.den if v.den else 0.0
            return float(v)

        deg, mins, sec = [to_float(v) for v in values]
        decimal = deg + mins / 60 + sec / 3600
        if ref in ("S", "W"):
            decimal = -decimal
        return decimal
    except Exception:
        return None


def read_exif(file_path: str) -> dict:
    """
    Extract EXIF data from image file.
    Returns dict with keys: taken_at, camera_make, camera_model, gps_lat, gps_lng
    """
    result: dict = {}
    try:
        import exifread
        with open(file_path, "rb") as f:
            tags = exifread.process_file(f, details=False)

        # Timestamp
        for tag in ("EXIF DateTimeOriginal", "EXIF DateTimeDigitized", "Image DateTime"):
            if tag in tags:
                try:
                    dt_str = str(tags[tag])
                    result["taken_at"] = datetime.strptime(dt_str, "%Y:%m:%d %H:%M:%S").isoformat()
                    break
                except ValueError:
                    pass

        # Camera
        if "Image Make" in tags:
            result["camera_make"] = str(tags["Image Make"]).strip()
        if "Image Model" in tags:
            result["camera_model"] = str(tags["Image Model"]).strip()

        # GPS
        lat_tag = tags.get("GPS GPSLatitude")
        lat_ref = str(tags.get("GPS GPSLatitudeRef", "N"))
        lng_tag = tags.get("GPS GPSLongitude")
        lng_ref = str(tags.get("GPS GPSLongitudeRef", "E"))

        if lat_tag and lng_tag:
            lat = _parse_gps_coord(lat_tag.values, lat_ref)
            lng = _parse_gps_coord(lng_tag.values, lng_ref)
            if lat is not None and lng is not None:
                result["gps_lat"] = lat
                result["gps_lng"] = lng

    except ImportError:
        logger.warning("exifread not installed, falling back to Pillow EXIF")
        result = _read_exif_pillow(file_path)
    except Exception as e:
        logger.debug(f"EXIF read failed for {file_path}: {e}")

    return result


def _read_exif_pillow(file_path: str) -> dict:
    result: dict = {}
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS, GPSTAGS

        img = Image.open(file_path)
        raw_exif = img._getexif()
        if not raw_exif:
            return result

        exif: dict = {}
        for tag_id, value in raw_exif.items():
            tag = TAGS.get(tag_id, tag_id)
            exif[tag] = value

        for key in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
            if key in exif:
                try:
                    result["taken_at"] = datetime.strptime(exif[key], "%Y:%m:%d %H:%M:%S").isoformat()
                    break
                except ValueError:
                    pass

        if "Make" in exif:
            result["camera_make"] = str(exif["Make"]).strip()
        if "Model" in exif:
            result["camera_model"] = str(exif["Model"]).strip()

        gps_info = exif.get("GPSInfo")
        if gps_info:
            gps: dict = {GPSTAGS.get(k, k): v for k, v in gps_info.items()}
            lat_vals = gps.get("GPSLatitude")
            lat_ref = gps.get("GPSLatitudeRef", "N")
            lng_vals = gps.get("GPSLongitude")
            lng_ref = gps.get("GPSLongitudeRef", "E")

            if lat_vals and lng_vals:
                def ratio(v):
                    return v[0] / v[1] if isinstance(v, tuple) else float(v)

                def dms_to_decimal(vals, ref):
                    d, m, s = [ratio(v) for v in vals]
                    dec = d + m / 60 + s / 3600
                    return -dec if ref in ("S", "W") else dec

                result["gps_lat"] = dms_to_decimal(lat_vals, lat_ref)
                result["gps_lng"] = dms_to_decimal(lng_vals, lng_ref)

    except Exception as e:
        logger.debug(f"Pillow EXIF read failed for {file_path}: {e}")

    return result


# MP4/MOV epoch: 1904-01-01 00:00:00 UTC
_MP4_EPOCH = datetime(1904, 1, 1, tzinfo=timezone.utc)
_CST = timezone(timedelta(hours=8))


def read_video_metadata(file_path: str) -> dict:
    """Extract creation_time from MP4/MOV moov/mvhd atom (pure Python, no ffprobe)."""
    result: dict = {}
    try:
        with open(file_path, "rb") as f:
            creation_ts = _find_mvhd_creation_time(f)
            if creation_ts and creation_ts > 0:
                dt = _MP4_EPOCH + timedelta(seconds=creation_ts)
                dt_cst = dt.astimezone(_CST)
                if dt_cst.year >= 2000:
                    result["taken_at"] = dt_cst.strftime("%Y-%m-%dT%H:%M:%S")
    except Exception as e:
        logger.debug(f"Video metadata read failed for {file_path}: {e}")
    return result


def _find_mvhd_creation_time(f) -> Optional[int]:
    """Walk MP4 box tree to find moov→mvhd and return creation_time seconds."""
    f.seek(0, 2)
    file_size = f.tell()
    f.seek(0)
    return _scan_boxes(f, file_size, depth=0)


def _scan_boxes(f, end: int, depth: int) -> Optional[int]:
    if depth > 5:
        return None
    while f.tell() < end - 8:
        pos = f.tell()
        header = f.read(8)
        if len(header) < 8:
            return None
        size = struct.unpack(">I", header[:4])[0]
        box_type = header[4:8]
        if size == 1:
            ext = f.read(8)
            if len(ext) < 8:
                return None
            size = struct.unpack(">Q", ext)[0]
        if size < 8:
            return None
        box_end = pos + size
        if box_type == b"moov":
            result = _scan_boxes(f, box_end, depth + 1)
            if result is not None:
                return result
        elif box_type == b"mvhd":
            data = f.read(min(size - 8, 32))
            if len(data) >= 8:
                version = data[0]
                if version == 0:
                    return struct.unpack(">I", data[4:8])[0]
                elif version == 1 and len(data) >= 12:
                    return struct.unpack(">Q", data[4:12])[0]
        if box_end > f.tell():
            f.seek(box_end)
    return None
