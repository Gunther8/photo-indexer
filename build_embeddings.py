"""
批量为已有记录生成 text-embedding-v4 向量。
用法: python build_embeddings.py [--batch 25]
"""
import sys
import time
import argparse
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core.database import get_db
from core.ai_analyzer import AIAnalyzer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=int, default=25, help="每批处理条数")
    args = parser.parse_args()

    db = get_db()
    ai = AIAnalyzer()

    pending = db.get_pending_embedding_ids()
    total = len(pending)
    logger.info(f"共 {total} 条记录需要生成向量")

    if total == 0:
        logger.info("没有需要处理的记录，退出")
        return

    done = 0
    errors = 0
    batch_size = args.batch

    for i in range(0, total, batch_size):
        chunk = pending[i: i + batch_size]
        for media_id in chunk:
            desc = db.get_description_by_id(media_id)
            if not desc:
                continue
            vec = ai.generate_embedding(desc)
            if vec:
                db.set_embedding(media_id, vec)
                done += 1
            else:
                errors += 1
            # 限流保护：约 5 QPS = 300 RPM，安全低于 text-embedding-v4 限额
            time.sleep(0.2)

        pct = min(100, int((i + len(chunk)) / total * 100))
        logger.info(f"进度 {i + len(chunk)}/{total} ({pct}%)  成功={done}  失败={errors}")

    logger.info(f"完成：成功 {done} 条，失败 {errors} 条")


if __name__ == "__main__":
    main()
