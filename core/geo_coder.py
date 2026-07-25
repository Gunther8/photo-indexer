import logging
import time
from typing import Optional

import requests

from config import get_config
from core.database import get_db
from utils.helpers import retry

logger = logging.getLogger(__name__)


def is_in_china(lat: float, lng: float) -> bool:
    return 18 <= lat <= 53 and 73 <= lng <= 135


def _coord_key(lat: float, lng: float) -> str:
    return f"{round(lat, 3)},{round(lng, 3)}"


class GeoCoder:
    def __init__(self):
        self._cfg = get_config()
        self._db = get_db()
        self._last_osm_request = 0.0

    def reverse_geocode(self, lat: float, lng: float) -> dict:
        try:
            key = _coord_key(lat, lng)
            cached = self._db.get_geo_cache(key)
            if cached:
                return cached

            if is_in_china(lat, lng):
                result = self._amap_geocode(lat, lng)
            else:
                result = self._osm_geocode(lat, lng)

            if result:
                self._db.set_geo_cache(key, result)
            return result or {}
        except Exception as e:
            logger.warning(f"地理编码失败 ({lat},{lng}): {e}")
            return {}

    @retry(max_attempts=3, base_delay=2.0, exceptions=(requests.RequestException,))
    def _amap_geocode(self, lat: float, lng: float) -> Optional[dict]:
        api_key = self._cfg.get("amap_api_key")
        if not api_key:
            logger.warning("AMap API key not configured")
            return None

        # AMap uses GCJ-02, input GPS coords are WGS-84
        # For display purposes only, we accept the slight offset
        resp = requests.get(
            "https://restapi.amap.com/v3/geocode/regeo",
            params={
                "key": api_key,
                "location": f"{lng},{lat}",
                "radius": 1000,
                "extensions": "base",
                "output": "JSON",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") != "1":
            logger.warning(f"AMap geocode error: {data.get('info')}")
            return None

        regeo = data.get("regeocode", {})
        component = regeo.get("addressComponent", {})
        address = regeo.get("formatted_address", "")

        def _str(v) -> str:
            """AMap 某些字段会返回列表，强制转为字符串"""
            if isinstance(v, list):
                return v[0] if v else ""
            return str(v) if v else ""

        return {
            "geo_country": "中国",
            "geo_province": _str(component.get("province")),
            "geo_city": _str(component.get("city")) or _str(component.get("province")),
            "geo_district": _str(component.get("district")),
            "geo_detail": _str(address),
        }

    def _osm_geocode(self, lat: float, lng: float) -> Optional[dict]:
        # 优先用 BigDataCloud（从中国大陆可访问，无需Key，返回中文）
        result = self._bigdatacloud_geocode(lat, lng)
        if result:
            return result

        # 回退到 Nominatim（境外服务器可用时）
        elapsed = time.time() - self._last_osm_request
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)
        try:
            resp = requests.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={"lat": lat, "lon": lng, "format": "jsonv2", "zoom": 14},
                headers={"User-Agent": "PhotoIndexer/1.0 (personal use)"},
                timeout=3,
            )
            self._last_osm_request = time.time()
            resp.raise_for_status()
            data = resp.json()
            addr = data.get("address", {})
            return {
                "geo_country": addr.get("country", ""),
                "geo_province": addr.get("state") or addr.get("province") or addr.get("region", ""),
                "geo_city": addr.get("city") or addr.get("town") or addr.get("municipality") or addr.get("county", ""),
                "geo_district": addr.get("suburb") or addr.get("district", ""),
                "geo_detail": data.get("display_name", ""),
            }
        except Exception:
            return None

    def _bigdatacloud_geocode(self, lat: float, lng: float) -> Optional[dict]:
        try:
            resp = requests.get(
                "https://api.bigdatacloud.net/data/reverse-geocode-client",
                params={"latitude": lat, "longitude": lng, "localityLanguage": "zh"},
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()

            country = data.get("countryName", "")
            if not country:
                return None

            province = data.get("principalSubdivision", "")
            city = data.get("city") or data.get("locality", "")
            # 取最细行政区划名称（字符串，非列表）
            district_name = ""
            for item in reversed(data.get("localityInfo", {}).get("administrative", [])):
                if isinstance(item, dict) and item.get("adminLevel", 0) >= 6:
                    district_name = item.get("name", "")
                    break
            detail = ", ".join(filter(None, [district_name, city, province, country]))

            return {
                "geo_country": country,
                "geo_province": province,
                "geo_city": city,
                "geo_district": district_name,
                "geo_detail": detail,
            }
        except Exception as e:
            logger.warning(f"BigDataCloud geocode 失败: {e}")
            return None
