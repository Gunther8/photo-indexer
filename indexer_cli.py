"""
无界面命令行索引器，用于在服务器上运行。
用法:
  python indexer_cli.py scan          # 扫描网盘，写入 pending 记录
  python indexer_cli.py process       # 下载+分析，循环处理所有 pending
  python indexer_cli.py check         # 检查批处理任务结果
  python indexer_cli.py status        # 显示当前进度统计
  python indexer_cli.py reset_errors  # 重置 error 为 pending
"""
import sys
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from utils.helpers import setup_logging
setup_logging(Path.home() / ".photo_indexer")

logger = logging.getLogger(__name__)


def cmd_scan():
    from core.indexer import Indexer
    indexer = Indexer()

    def cb(cur, tot, msg):
        print(f"\r{msg}", end="", flush=True)

    count = indexer.scan(progress_cb=cb)
    print(f"\n扫描完成，新增 {count} 个文件")


def cmd_process():
    from core.indexer import Indexer
    indexer = Indexer()

    def cb(cur, tot, msg):
        if tot > 0:
            print(f"\r[{cur}/{tot}] {msg}", end="", flush=True)
        else:
            print(f"\r{msg}", end="", flush=True)

    indexer.process_pending(progress_cb=cb)
    print("\n处理完成")


def cmd_check():
    from core.indexer import Indexer
    indexer = Indexer()

    def cb(cur, tot, msg):
        print(f"\r{msg}", end="", flush=True)

    indexer.check_batch_jobs(progress_cb=cb)
    print("\n检查完成")


def cmd_status():
    from core.database import get_db
    db = get_db()
    counts = db.count_by_status()
    total = sum(counts.values())
    done = counts.get("done", 0)
    pending = counts.get("pending", 0)
    processing = counts.get("processing", 0)
    error = counts.get("error", 0)

    from core.database import get_db
    jobs = db.get_pending_batch_jobs()

    print(f"""
=== 索引进度 ===
总计:      {total:>8,}
已完成:    {done:>8,}  ({f'{done/total*100:.1f}%' if total else '--'})
待处理:    {pending:>8,}
处理中:    {processing:>8,}
出错:      {error:>8,}
AI批处理任务未完成: {len(jobs)} 个
""")


def cmd_reset_errors():
    from core.database import get_db
    db = get_db()
    conn = db.get_conn()
    cur = conn.execute(
        "UPDATE media SET index_status='pending', index_error=NULL "
        "WHERE index_status IN ('error', 'processing')"
    )
    conn.commit()
    print(f"已重置 {cur.rowcount} 条记录为 pending")


COMMANDS = {
    "scan": cmd_scan,
    "process": cmd_process,
    "check": cmd_check,
    "status": cmd_status,
    "reset_errors": cmd_reset_errors,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[sys.argv[1]]()
