# Photo Indexer — 百度网盘照片/视频 AI 索引器

用多模态大模型给百度网盘里的照片和视频生成中文描述、场景标签和拍摄信息，建成一个能用大白话搜索的媒体库。

装在一台小服务器上，跑一次索引，之后打开网页搜「蜗牛」「雪山 日出」「戴红帽子的小孩」就能找到对应的照片——而不是对着一堆 `IMG_20190312_155919.jpg` 发呆。作者本人索引了约 14 万个文件。

## 能做什么

- **自然语言搜索** — 按画面内容搜，不是按文件名
- **多关键词** — 空格分隔，数量不限，AND 逻辑（`海边 傍晚 逆光`）
- **条件筛选** — 媒体类型、季节、有无人物、是否适合当封面、拍摄日期
- **相似推荐** — 基于语义向量找内容相近的素材
- **拍摄地点** — 从 GPS 信息反查到省市区街道

## 快速开始

一台 2核2G 起步的云服务器（CentOS / Alma / Rocky / Ubuntu / Debian），root 登录后一条命令下载：

```bash
mkdir -p ~/photo-indexer && curl -fsSL https://codeload.github.com/Gunther8/photo-indexer/tar.gz/refs/heads/main | tar xz -C ~/photo-indexer --strip-components=1
```

然后运行部署脚本：

```bash
cd ~/photo-indexer && sudo bash deploy.sh
```

（装了 git 的话，`git clone https://github.com/Gunther8/photo-indexer.git` 也一样。）

脚本会全程交互式引导你：装依赖 → 填三个密钥 → 网盘授权 → 装服务 → 起 Web，结束后直接给你访问地址。

> 项目需要 **Python ≥ 3.10**。CentOS 等系统自带的是 3.6，脚本检测到版本过低会自动装一份独立的 Python，不动系统环境。

然后跑首次索引（放 `screen` 里，避免 SSH 断开中断）：

```bash
screen -S indexer
./run.sh scan       # 扫描网盘，建立文件清单
./run.sh process    # 下载 + AI 分析（耗时较长，Ctrl+A D 可脱离）
```

`process` 提交完就可以不管了——定时任务每 10 分钟自动回收 AI 结果写库，每天凌晨 3 点自动生成检索向量。用 `./run.sh status` 随时看进度。

### 需要准备的密钥

| 密钥 | 用途 | 申请地址 |
|---|---|---|
| 百度网盘 AppKey / SecretKey | 读取网盘文件 | [百度网盘开放平台](https://pan.baidu.com/union/) — 创建「软件」类应用 |
| 阿里云百炼 API Key | 图像分析 + 向量生成 | [百炼控制台](https://bailian.console.aliyun.com/) |
| 高德地图 Web 服务 Key | GPS 反查地址（**可选**） | [高德开放平台](https://lbs.amap.com/) |

deploy.sh 会逐项提示你粘贴，写进 `~/.photo_indexer/config.json`（权限 600），不会进 git。

## 日常命令

```bash
./run.sh status     # 索引进度统计
./run.sh scan       # 重新扫描（新增文件入库，网盘已删除的清理掉）
./run.sh process    # 处理待分析文件
./run.sh check      # 手动回收一次 AI 批处理结果
./run.sh reset      # 把出错/卡住的记录重置为待处理
./run.sh embed      # 手动生成语义向量
./run.sh auth       # 重新授权百度网盘

journalctl -u photo-web -f   # Web 服务日志
screen -r indexer            # 回到索引任务
```

## 工作原理

```
扫描网盘 → 下载并压缩 → 读 EXIF / 视频元数据 → GPS 反查地址
        → 提交多模态大模型（Batch 模式）→ 回收结果写入 SQLite + FTS5
        → 生成语义向量 → 检索
```

- **图片**：压缩到 1024px 再送模型，省 token
- **视频**：按场景切换抽帧（最多 20 帧），多帧结果合并取视觉冲击力最高的描述
- **RAW 文件**（CR2/CR3/ARW/NEF 等）不单独分析，作为配对 JPEG 记录的元数据存储
- **Batch API** 比实时调用省约 50% 费用，代价是结果异步返回（几分钟到几小时），所以才需要定时任务回收

## 安全

Web 端是 HTTP Basic 认证，同一 IP 连续 5 次密码错误**永久封锁**，封锁列表存在 `~/.photo_indexer/blocked_ips.json`，重启不丢。

误封了自己：

```bash
echo '[]' > ~/.photo_indexer/blocked_ips.json && systemctl restart photo-web
```

deploy.sh 生成的 nginx 配置已经带了 `X-Real-IP` 转发——这个不能省，否则限流会把所有访客当成同一个 IP，一个人输错密码全网都进不来。

默认是 80 端口明文 HTTP。生产环境建议套 Cloudflare 代理，或者自己配 HTTPS 证书。

## 成本参考

Batch 模式约 ¥0.5/百万输入 token + ¥5/百万输出 token。14 万个文件（含视频多帧）实际花费几百元人民币，视频占比越高越贵。

## 项目结构

```
deploy.sh            一键部署（装依赖、配置、systemd、nginx、cron）
run.sh               日常命令入口（由 deploy.sh 生成）
indexer_cli.py       索引命令行
web_server.py        FastAPI Web 服务
build_embeddings.py  批量生成语义向量
backfill_video_dates.py  回填视频拍摄日期（不用重新下载视频）
core/
  database.py        SQLite + FTS5，线程本地连接
  baidu_pan.py       OAuth2、文件遍历、下载，token 自动刷新
  media_processor.py 图片压缩、视频抽帧
  ai_analyzer.py     多模态分析（实时/Batch）、embedding 生成
  geo_coder.py       GPS 反查地址，带数据库缓存
  indexer.py         调度：扫描 → 下载 → EXIF → 地理 → AI → 写库
static/              Web 前端页面
utils/               EXIF 读取、视频元数据解析、通用工具
```

## 踩过的坑

部署脚本里已经处理掉了，列在这儿供参考：

- **服务器必须用 `opencv-python-headless`**，普通版依赖 `libGL.so.1`，无图形界面的机器会直接报错
- **cron 必须加 `flock`**，并发跑多个索引进程会触发 SQLite `database is locked`
- **小内存机器要开 swap**，否则索引跑到一半会被 OOM Killer 杀掉
- **Nominatim（海外 GPS 反查）在国内服务器不可达**，代码里已改成重试 1 次直接跳过

## 已知限制

- 超过 `max_video_size_mb` / `max_photo_size_mb` 的文件会跳过（默认 500MB / 50MB）
- Batch 任务偶尔丢结果，记录会卡在 `processing`，跑 `./run.sh reset` 重置后重新处理即可

## License

MIT
