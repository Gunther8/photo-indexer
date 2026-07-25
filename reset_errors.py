import sys
sys.path.insert(0, ".")
from core.database import get_db

db = get_db()
conn = db.get_conn()
cur = conn.execute("UPDATE media SET index_status='pending', index_error=NULL WHERE index_status='error'")
conn.commit()
print(f"已重置 {cur.rowcount} 条错误记录为 pending")
