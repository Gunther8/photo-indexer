import base64
import json
import logging
import time
import tempfile
import os
from pathlib import Path
from typing import Optional

import requests
from openai import OpenAI

from config import get_config
from utils.helpers import retry

logger = logging.getLogger(__name__)

QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
QWEN_MODEL = "qwen3-vl-plus"  # 可在 config.json 的 qwen_model 字段覆盖
EMBED_MODEL = "text-embedding-v4"
EMBED_DIM = 512

ANALYSIS_PROMPT = """分析这张图片，严格按照以下JSON格式返回，不要输出任何其他内容：
{
  "description": "详细描述画面所有可见内容，包括地标名称、物品品牌型号、人物特征、环境细节，100-200字",
  "objects": ["物体1", "物体2"],
  "scene_tags": ["标签1", "标签2"],
  "weather": "晴天|阴天|雨天|雪天|黄昏|夜晚|其他",
  "season": "春|夏|秋|冬|不确定",
  "mood": "壮阔|温馨|孤独|热闹|神秘|其他",
  "has_person": true,
  "person_count": 0,
  "person_desc": "人物描述或空字符串",
  "is_cover_worthy": false,
  "cover_reason": "原因或空字符串",
  "is_caption_worthy": false,
  "caption_reason": "原因或空字符串",
  "is_broll": false,
  "visual_impact": 3,
  "is_vertical_comp": false,
  "quality_score": 3
}"""


def _encode_image(image_path: str) -> str:
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def _parse_response(text: str) -> Optional[dict]:
    text = text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1]) if len(lines) > 2 else text
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse AI response as JSON: {e}\nRaw: {text[:200]}")
        return None


class AIAnalyzer:
    def __init__(self):
        self._cfg = get_config()

    def _model(self) -> str:
        return self._cfg.get("qwen_model") or QWEN_MODEL

    def _client(self) -> OpenAI:
        api_key = self._cfg.get("qwen_api_key")
        if not api_key:
            raise ValueError("Qwen API key not configured")
        return OpenAI(api_key=api_key, base_url=QWEN_BASE_URL)

    # ── Realtime mode ────────────────────────────────────────────────────────

    @retry(max_attempts=3, base_delay=5.0, exceptions=(Exception,))
    def analyze_image(self, image_path: str) -> Optional[dict]:
        """Analyze a single image synchronously. Returns parsed dict or None."""
        client = self._client()
        b64 = _encode_image(image_path)
        resp = client.chat.completions.create(
            model=self._model(),
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                        },
                        {"type": "text", "text": ANALYSIS_PROMPT},
                    ],
                }
            ],
            max_tokens=1024,
            extra_body={"enable_thinking": False},
        )
        raw = resp.choices[0].message.content or ""
        return _parse_response(raw)

    def analyze_images(self, image_paths: list[str]) -> list[Optional[dict]]:
        """Analyze multiple images and aggregate (used for video frames)."""
        results = []
        for path in image_paths:
            results.append(self.analyze_image(path))
        return results

    def merge_video_frame_results(self, results: list[Optional[dict]]) -> Optional[dict]:
        """
        Merge analysis from multiple video frames into a single result.
        Takes description from the highest visual_impact frame; merges tags.
        """
        valid = [r for r in results if r]
        if not valid:
            return None

        best = max(valid, key=lambda r: r.get("visual_impact", 0))
        merged = dict(best)

        all_objects: list[str] = []
        all_scene_tags: list[str] = []
        for r in valid:
            all_objects.extend(r.get("objects") or [])
            all_scene_tags.extend(r.get("scene_tags") or [])

        # Deduplicate preserving order
        merged["objects"] = list(dict.fromkeys(all_objects))
        merged["scene_tags"] = list(dict.fromkeys(all_scene_tags))
        merged["has_person"] = any(r.get("has_person") for r in valid)
        merged["person_count"] = max((r.get("person_count") or 0) for r in valid)
        merged["is_cover_worthy"] = any(r.get("is_cover_worthy") for r in valid)
        merged["is_broll"] = True  # videos are always potential B-roll

        return merged

    # ── Embedding ────────────────────────────────────────────────────────────

    def generate_embedding(self, text: str) -> Optional[list[float]]:
        if not text or not text.strip():
            return None
        for attempt in range(4):
            try:
                client = self._client()
                resp = client.embeddings.create(
                    model=EMBED_MODEL,
                    input=text.strip(),
                    dimensions=EMBED_DIM,
                )
                return resp.data[0].embedding
            except Exception as e:
                err_str = str(e)
                if "429" in err_str or "limit_requests" in err_str or "insufficient_quota" in err_str:
                    wait = 60 * (attempt + 1)  # 60s → 120s → 180s
                    logger.warning(f"Embedding 限流 429，等待 {wait}s 后重试（第 {attempt+1} 次）...")
                    time.sleep(wait)
                else:
                    logger.warning(f"Embedding 生成失败: {e}")
                    return None
        logger.warning("Embedding 生成失败：超过最大重试次数")
        return None

    # ── Batch mode ───────────────────────────────────────────────────────────

    def build_batch_file(self, items: list[dict]) -> str:
        """
        items: list of {"custom_id": str, "image_path": str}
        Returns path to the JSONL batch file.
        """
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False, encoding="utf-8")
        for item in items:
            b64 = _encode_image(item["image_path"])
            req = {
                "custom_id": item["custom_id"],
                "method": "POST",
                "url": "/v1/chat/completions",
                "body": {
                    "model": QWEN_MODEL,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image_url",
                                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                                },
                                {"type": "text", "text": ANALYSIS_PROMPT},
                            ],
                        }
                    ],
                    "max_tokens": 1024,
                    "enable_thinking": False,
                },
            }
            tmp.write(json.dumps(req, ensure_ascii=False) + "\n")
        tmp.close()
        return tmp.name

    def submit_batch(self, jsonl_path: str) -> str:
        """Upload JSONL file and submit batch job. Returns batch job ID."""
        client = self._client()
        with open(jsonl_path, "rb") as f:
            file_obj = client.files.create(file=f, purpose="batch")
        batch = client.batches.create(
            input_file_id=file_obj.id,
            endpoint="/v1/chat/completions",
            completion_window="24h",
        )
        return batch.id

    def check_batch_status(self, batch_id: str) -> str:
        """Returns batch status: validating | in_progress | completed | failed | expired"""
        client = self._client()
        batch = client.batches.retrieve(batch_id)
        return batch.status

    def get_batch_results(self, batch_id: str) -> dict[str, Optional[dict]]:
        """
        Retrieve and parse batch results.
        Returns dict mapping custom_id -> parsed analysis dict (or None on error).
        """
        client = self._client()
        batch = client.batches.retrieve(batch_id)
        if batch.status != "completed":
            raise ValueError(f"Batch not completed yet: {batch.status}")

        output_file_id = batch.output_file_id
        content = client.files.content(output_file_id).text

        results: dict[str, Optional[dict]] = {}
        for line in content.strip().split("\n"):
            if not line:
                continue
            try:
                obj = json.loads(line)
                custom_id = obj["custom_id"]
                body = obj.get("response", {}).get("body", {})
                choices = body.get("choices", [])
                if choices:
                    raw_text = choices[0].get("message", {}).get("content", "")
                    results[custom_id] = _parse_response(raw_text)
                else:
                    results[custom_id] = None
            except Exception as e:
                logger.warning(f"Failed to parse batch result line: {e}")

        return results
