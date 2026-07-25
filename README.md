# Photo Indexer — 百度网盘照片/视频 AI 索引器

用多模态大模型给百度网盘里的照片和视频生成中文描述、场景标签和拍摄信息，建立可全文检索与语义检索的本地索引。适合媒体素材量大、靠文件名根本找不到东西的场景（作者本人索引了约 14 万个文件）。

## 能做什么

- **自然语言搜索**：搜「蜗牛」「雪山 日出」「戴红帽子的小孩」，而不是靠文件名
- **多关键词**：空格分隔，数量不限，AND 逻辑（`海边 傍晚 逆光`）
- **结构化筛选**：媒体类型、季节、是否有人物、是否适合当封面、拍摄日期
- **语义搜索**：基于 text-embedding-v4 向量的相似内容推荐
- **拍摄地点**：从 GPS EXIF 反查到省市区街道
- **两套界面**：本地 PyQt6 桌面端，以及可部署到服务器的 Web 端

## 工作原理

```
扫描网盘 → 下载并压缩 → 读 EXIF/视频元数据 → GPS 反查地址
        → 提交多模态大模型（Batch 模式）→ 回收结果写入 SQLite + FTS5
        → 生成 embedding 向量 → 检索
```

- **图片**：压缩到 1024px 再送模型，省 token
- **视频**：PySceneDetect 按场景切换抽帧（最多 20 帧），多帧结果合并取视觉冲击力最高的描述
- **RAW 文件**（CR2/CR3/ARW/NEF/ORF/RW2/DNG）不单独分析，作为配对 JPEG 记录的元数据存储
- **Batch API** 相比实时调用省约 50% 费用，代价是结果异步返回（几分钟到几小时）

## 安装

```bash
git clone https://github.com/<your-name>/photo-indexer.git
cd photo-indexer
pip install -r requirements.txt
```

服务器（无图形界面）上把 `opencv-python` 换成 headless 版，否则会报缺 `libGL.so.1`：

```bash
pip uninstall -y opencv-python && pip install opencv-python-headless
```

## 配置

复制 `config.example.json` 到 `~/.photo_indexer/config.json` 并填入你自己的密钥：

```bash
mkdir -p ~/.photo_indexer && cp config.example.json ~/.photo_indexer/config.json
```

需要申请的三个凭据：

| 配置项 | 用途 | 申请地址 |
|---|---|---|
| `baidu_app_key` / `baidu_app_secret` | 读取网盘文件 | [百度网盘开放平台](https://pan.baidu.com/union/) |
| `qwen_api_key` | 多模态分析 + 向量生成 | [阿里云百炼](https://bailian.console.aliyun.com/) |
| `amap_api_key` | GPS 反查地址（可选） | [高德开放平台](https://lbs.amap.com/) |

`config.json` 已在 `.gitignore` 里，不会被提交。

## 使用

### 桌面端

```bash
python main.py
```

首次运行在设置里完成百度网盘 OAuth 授权（oob 模式，复制授权码粘贴回来即可，不需要回调服务器）。

### 命令行（服务器索引用）

```bash
python indexer_cli.py scan     # 扫描网盘，写入待处理记录，同时清理已删除文件
python indexer_cli.py process  # 下载 + 分析，循环处理所有待处理项
python indexer_cli.py check    # 回收 Batch 任务结果
python indexer_cli.py status   # 查看进度统计
```

长时间任务建议放在 `screen` 里跑，避免 SSH 断开中断：

```bash
screen -S indexer
python indexer_cli.py process
# Ctrl+A D 脱离，screen -r indexer 恢复
```

配合 cron 自动回收结果：

```cron
*/10 * * * * flock -n /tmp/indexer.lock /path/to/run.sh check
0 3 * * * /path/to/python build_embeddings.py
```

`flock` 是必须的——并发跑多个索引进程会触发 SQLite `database is locked`。

### Web 端

```bash
python web_server.py
```

HTTP Basic 认证，同一 IP 连续 5 次密码错误永久封锁（封锁列表持久化在 `~/.photo_indexer/blocked_ips.json`）。生产环境建议放在 nginx 反代后面并配置 HTTPS，nginx 需要传 `X-Real-IP` 头，否则限流会把所有人当成同一个 IP。

误封自己的话：

```bash
echo '[]' > ~/.photo_indexer/blocked_ips.json && systemctl restart photo-web
```

## 辅助脚本

- `build_embeddings.py` — 为已有记录批量生成语义向量（约 300 RPM，自动处理 429 限流）
- `backfill_video_dates.py` — 回填视频拍摄日期，先解析文件名再查网盘 API 的 `local_mtime`，不需要重新下载视频
- `reset_errors.py` — 把失败记录重置为待处理

## 项目结构

```
core/
  database.py        SQLite + FTS5，线程本地连接
  baidu_pan.py       OAuth2、文件遍历、下载，token 自动刷新
  media_processor.py 图片压缩、视频抽帧
  ai_analyzer.py     多模态分析（实时/Batch）、embedding 生成
  geo_coder.py       GPS 反查地址，带数据库缓存
  indexer.py         调度：扫描 → 下载 → EXIF → 地理 → AI → 写库
ui/                  PyQt6 桌面界面
static/              Web 前端页面
utils/               EXIF 读取、视频元数据解析、通用工具
```

## 成本参考

Batch 模式下约 ¥0.5/百万输入 token + ¥5/百万输出 token。14 万个文件（含视频多帧）实际花费约几百元人民币，具体取决于视频占比。

## 已知限制

- 超过 `max_video_size_mb` / `max_photo_size_mb` 的文件会跳过
- Nominatim（海外 GPS 反查）在国内服务器上不可达，代码里已改成重试 1 次直接跳过
- Batch 任务偶尔会丢结果，记录会卡在 `processing` 状态，需要手动重置为 `pending` 重跑

## License

MIT
