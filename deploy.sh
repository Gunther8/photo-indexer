#!/bin/bash
#
# Photo Indexer 一键部署脚本（CentOS/RHEL/Alma/Rocky/Ubuntu/Debian）
#
#   sudo bash deploy.sh
#
# 全程交互式：安装依赖 → 填写密钥 → 网盘授权 → 装服务 → 起 Web
# 重复运行是安全的，已有配置会作为默认值带出来。
#
set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="$HOME/.photo_indexer"
CONFIG_FILE="$CONFIG_DIR/config.json"
VENV="$INSTALL_DIR/venv"
PY="$VENV/bin/python"

RED=$'\e[31m'; GRN=$'\e[32m'; YLW=$'\e[33m'; BLU=$'\e[36m'; BLD=$'\e[1m'; RST=$'\e[0m'

info() { echo "${BLU}==>${RST} $*"; }
ok()   { echo "${GRN}  ✓${RST} $*"; }
warn() { echo "${YLW}  !${RST} $*"; }
die()  { echo "${RED}错误:${RST} $*" >&2; exit 1; }

banner() {
  echo
  echo "${BLD}╔══════════════════════════════════════════════╗${RST}"
  echo "${BLD}║   Photo Indexer  一键部署                    ║${RST}"
  echo "${BLD}╚══════════════════════════════════════════════╝${RST}"
  echo
}

# 读取输入：ask <变量名> <提示> [默认值] [secret]
ask() {
  local __var=$1 prompt=$2 default=${3:-} secret=${4:-}
  local input display
  if [[ -n "$default" ]]; then
    if [[ -n "$secret" ]]; then display="已设置，回车保持不变"; else display="$default"; fi
    prompt="$prompt [${display}]"
  fi
  if [[ -n "$secret" ]]; then
    read -rsp "$prompt: " input; echo
  else
    read -rp "$prompt: " input
  fi
  printf -v "$__var" '%s' "${input:-$default}"
}

ask_yn() {  # ask_yn "提示" [Y|N] → 返回 0=yes
  local prompt=$1 default=${2:-Y} ans
  read -rp "$prompt [$([[ $default == Y ]] && echo 'Y/n' || echo 'y/N')]: " ans
  ans=${ans:-$default}
  [[ ${ans,,} == y* ]]
}

json_get() {  # 从已有 config.json 取值，不存在返回空
  [[ -f "$CONFIG_FILE" ]] || { echo ""; return; }
  python3 - "$1" <<'EOF' 2>/dev/null || echo ""
import json, os, sys
p = os.path.expanduser("~/.photo_indexer/config.json")
try:
    print(json.load(open(p)).get(sys.argv[1], "") or "")
except Exception:
    print("")
EOF
}

# ──────────────────────────────────────────────────────────────────────────────
banner
[[ $EUID -eq 0 ]] || die "请用 root 运行：sudo bash deploy.sh"

# ── 1. 系统依赖 ───────────────────────────────────────────────────────────────
info "安装系统依赖"
if command -v dnf &>/dev/null;   then PKG="dnf -y install"
elif command -v yum &>/dev/null; then PKG="yum -y install"
elif command -v apt-get &>/dev/null; then apt-get update -qq; PKG="apt-get -y install"
else die "不支持的系统（需要 dnf/yum/apt）"; fi

$PKG python3 python3-pip nginx screen sqlite util-linux bzip2 >/dev/null 2>&1 || \
  $PKG python3 python3-pip nginx screen sqlite3 util-linux bzip2 >/dev/null 2>&1 || \
  warn "部分系统包安装失败，继续尝试"
ok "nginx / screen / sqlite"

# ── 2. Python 运行环境 ────────────────────────────────────────────────────────
# 本项目用了 PEP 604 (X | None) 注解，需要 Python >= 3.10。
# 很多发行版（如 CentOS 7/8）自带的 python3 只有 3.6，会在运行时报 TypeError，
# 所以这里必须显式挑一个够新的解释器，挑不到就装 Miniconda。
MIN_MINOR=10
BASE_PY=""

info "查找 Python >= 3.$MIN_MINOR"
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
  command -v "$cand" &>/dev/null || continue
  if "$cand" -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, $MIN_MINOR) else 1)" 2>/dev/null; then
    BASE_PY=$(command -v "$cand"); break
  fi
done

if [[ -n "$BASE_PY" ]]; then
  ok "使用 $BASE_PY ($($BASE_PY -V 2>&1 | cut -d' ' -f2))"
  info "创建虚拟环境"
  [[ -d "$VENV" ]] && ! "$VENV/bin/python" -c "import sys; sys.exit(0 if sys.version_info[:2]>=(3,$MIN_MINOR) else 1)" 2>/dev/null && {
    warn "已有 venv 版本过低，重建"; rm -rf "$VENV"; }
  if [[ ! -d "$VENV" ]]; then
    "$BASE_PY" -m venv "$VENV" 2>/dev/null || {
      $PKG python3-venv >/dev/null 2>&1 || true
      "$BASE_PY" -m venv "$VENV"; }
  fi
else
  SYS_VER=$(python3 -V 2>&1 | cut -d' ' -f2 || echo '未安装')
  warn "系统 python3 版本过低（$SYS_VER），本项目需要 >= 3.$MIN_MINOR"
  info "安装 Miniconda 独立 Python（不影响系统 python3）"
  CONDA_DIR=/opt/miniconda3
  if [[ ! -x "$CONDA_DIR/bin/conda" ]]; then
    ARCH=$(uname -m); case "$ARCH" in
      x86_64)  MC_ARCH=x86_64 ;;
      aarch64) MC_ARCH=aarch64 ;;
      *) die "不支持的架构 $ARCH，请手动安装 Python >= 3.$MIN_MINOR 后重跑" ;;
    esac
    MC_URL="https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-${MC_ARCH}.sh"
    curl -fsSL "$MC_URL" -o /tmp/miniconda.sh || die "Miniconda 下载失败，请检查网络"
    bash /tmp/miniconda.sh -b -p "$CONDA_DIR" >/dev/null
    rm -f /tmp/miniconda.sh
  fi
  # 不执行 conda init：会污染 .bashrc，某些云主机的安全监控每隔几秒执行一次
  # shell 检查，会因此拉起大量 python 子进程把 CPU 打满。这里只用绝对路径。
  rm -rf "$VENV"
  "$CONDA_DIR/bin/conda" create -y -q -p "$VENV" "python=3.11" >/dev/null
  ok "已装独立 Python 3.11 到 $VENV"
fi

"$PY" -c "import sys; assert sys.version_info[:2] >= (3, $MIN_MINOR)" \
  || die "Python 环境准备失败"
"$PY" -m pip install -q --upgrade pip
ok "Python $("$PY" -V 2>&1 | cut -d' ' -f2) @ $VENV"

info "安装 Python 依赖（约 1-3 分钟）"
"$PY" -m pip install -q -r "$INSTALL_DIR/requirements.txt"
# 服务器无图形界面，必须用 headless 版 opencv，否则报 libGL.so.1 缺失
"$PY" -m pip uninstall -qy opencv-python 2>/dev/null || true
"$PY" -m pip install -q opencv-python-headless
ok "依赖安装完成"

# ── 3. Swap（防 OOM）──────────────────────────────────────────────────────────
if [[ $(swapon --show --noheadings 2>/dev/null | wc -l) -eq 0 ]]; then
  info "未检测到 swap（小内存机器容易被 OOM 杀进程）"
  if ask_yn "  创建 4GB swap 文件?"; then
    fallocate -l 4G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=4096 status=none
    chmod 600 /swapfile && mkswap -q /swapfile && swapon /swapfile
    grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    sysctl -qw vm.swappiness=10
    grep -q 'vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=10' >> /etc/sysctl.conf
    ok "已启用 4GB swap"
  fi
else
  ok "swap 已存在"
fi

# ── 4. 配置密钥 ───────────────────────────────────────────────────────────────
mkdir -p "$CONFIG_DIR"
echo
echo "${BLD}────── 配置 ──────${RST}"
echo "留空可跳过可选项；重复部署时回车保持原值。"
echo

echo "${BLD}[1/4] 百度网盘${RST}  申请: https://pan.baidu.com/union/  （创建「软件」类应用）"
ask BAIDU_KEY    "  AppKey"    "$(json_get baidu_app_key)"
ask BAIDU_SECRET "  SecretKey" "$(json_get baidu_app_secret)" secret
echo

echo "${BLD}[2/4] 阿里云百炼${RST}（多模态分析 + 向量）  申请: https://bailian.console.aliyun.com/"
ask QWEN_KEY "  API Key (sk-...)" "$(json_get qwen_api_key)" secret
echo

echo "${BLD}[3/4] 高德地图${RST}（可选，GPS 反查地址）  申请: https://lbs.amap.com/"
ask AMAP_KEY "  Web服务 API Key（可留空）" "$(json_get amap_api_key)"
echo

echo "${BLD}[4/4] Web 访问${RST}"
ask WEB_USER "  登录用户名" "$(json_get web_user || echo admin)"
EXIST_PASS="$(json_get web_pass)"
ask WEB_PASS "  登录密码" "$EXIST_PASS" secret
[[ -n "$WEB_PASS" ]] || die "Web 密码不能为空"
ask WEB_PORT   "  内部监听端口" "$(json_get web_port || echo 8080)"
ask WEB_DOMAIN "  域名或公网IP（可留空）" "$(json_get web_domain)"
echo
ask SCAN_PATH "  网盘扫描起始目录" "$(json_get scan_path || echo /)"

[[ -n "$BAIDU_KEY" && -n "$BAIDU_SECRET" ]] || die "百度网盘 AppKey/SecretKey 必填"
[[ -n "$QWEN_KEY" ]] || die "百炼 API Key 必填"

info "写入配置"
BAIDU_KEY="$BAIDU_KEY" BAIDU_SECRET="$BAIDU_SECRET" QWEN_KEY="$QWEN_KEY" \
AMAP_KEY="$AMAP_KEY" WEB_USER="$WEB_USER" WEB_PASS="$WEB_PASS" \
WEB_PORT="$WEB_PORT" WEB_DOMAIN="$WEB_DOMAIN" SCAN_PATH="$SCAN_PATH" \
CONFIG_FILE="$CONFIG_FILE" python3 <<'EOF'
import json, os
p = os.environ["CONFIG_FILE"]
cfg = {}
if os.path.exists(p):
    try:
        cfg = json.load(open(p))
    except Exception:
        cfg = {}
cfg.update({
    "baidu_app_key":    os.environ["BAIDU_KEY"],
    "baidu_app_secret": os.environ["BAIDU_SECRET"],
    "qwen_api_key":     os.environ["QWEN_KEY"],
    "amap_api_key":     os.environ["AMAP_KEY"],
    "web_user":         os.environ["WEB_USER"],
    "web_pass":         os.environ["WEB_PASS"],
    "web_port":         int(os.environ["WEB_PORT"] or 8080),
    "web_domain":       os.environ["WEB_DOMAIN"],
    "scan_path":        os.environ["SCAN_PATH"] or "/",
})
cfg.setdefault("baidu_access_token", "")
cfg.setdefault("baidu_refresh_token", "")
cfg.setdefault("qwen_model", "qwen3-vl-plus")
cfg.setdefault("analysis_mode", "batch")
cfg.setdefault("batch_size", 20)
cfg.setdefault("download_workers", 3)
cfg.setdefault("image_max_size", 1024)
cfg.setdefault("max_video_size_mb", 500)
cfg.setdefault("max_photo_size_mb", 50)
cfg.setdefault("exclude_paths", [])
cfg.setdefault("http_proxy", "")
json.dump(cfg, open(p, "w"), ensure_ascii=False, indent=2)
EOF
chmod 600 "$CONFIG_FILE"
ok "配置已保存到 $CONFIG_FILE (权限 600)"

# ── 5. 百度网盘授权 ───────────────────────────────────────────────────────────
echo
if [[ -n "$(json_get baidu_access_token)" ]]; then
  ok "百度网盘已授权"
  ask_yn "  重新授权?" N && "$PY" "$INSTALL_DIR/indexer_cli.py" auth
else
  info "百度网盘授权（必须完成，否则无法读取文件）"
  "$PY" "$INSTALL_DIR/indexer_cli.py" auth
fi

# ── 6. run.sh ─────────────────────────────────────────────────────────────────
info "生成管理脚本"
cat > "$INSTALL_DIR/run.sh" <<EOF
#!/bin/bash
# 由 deploy.sh 生成
cd "$INSTALL_DIR"
PYTHON="$PY"
case "\$1" in
  auth)    \$PYTHON indexer_cli.py auth ;;
  scan)    \$PYTHON indexer_cli.py scan ;;
  process) \$PYTHON indexer_cli.py process ;;
  check)   \$PYTHON indexer_cli.py check ;;
  status)  \$PYTHON indexer_cli.py status ;;
  reset)   \$PYTHON indexer_cli.py reset_errors ;;
  embed)   \$PYTHON build_embeddings.py ;;
  *)       echo "用法: ./run.sh [auth|scan|process|check|status|reset|embed]" ;;
esac
EOF
chmod +x "$INSTALL_DIR/run.sh"
ok "run.sh"

# ── 7. systemd ────────────────────────────────────────────────────────────────
info "安装 systemd 服务"
TOTAL_MB=$(free -m | awk '/^Mem:/{print $2}')
MEM_MAX=$(( TOTAL_MB / 2 )); (( MEM_MAX < 512 )) && MEM_MAX=512
cat > /etc/systemd/system/photo-web.service <<EOF
[Unit]
Description=Photo Indexer Web Server
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
ExecStart=$PY $INSTALL_DIR/web_server.py
Restart=always
RestartSec=5
MemoryMax=${MEM_MAX}M
MemorySwapMax=200M

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable -q photo-web
systemctl restart photo-web
sleep 2
systemctl is-active --quiet photo-web && ok "photo-web 已启动 (MemoryMax=${MEM_MAX}M)" \
  || warn "photo-web 启动异常，查看: journalctl -u photo-web -n 30"

# ── 8. nginx ──────────────────────────────────────────────────────────────────
info "配置 nginx 反向代理"
NGINX_DIR=/etc/nginx/conf.d
[[ -d $NGINX_DIR ]] || NGINX_DIR=/etc/nginx/sites-enabled
cat > "$NGINX_DIR/photo.conf" <<EOF
server {
    listen 80;
    server_name ${WEB_DOMAIN:-_};

    client_max_body_size 100M;

    location / {
        proxy_pass         http://127.0.0.1:${WEB_PORT};
        proxy_set_header   Host \$host;
        # X-Real-IP 必须传，否则登录限流会把所有访客当成同一个 IP
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300;
        proxy_send_timeout 300;
    }
}
EOF
if nginx -t &>/dev/null; then
  systemctl enable -q nginx && systemctl restart nginx
  ok "nginx 已配置（80 → 127.0.0.1:${WEB_PORT}）"
else
  warn "nginx 配置检查失败，跳过：nginx -t"
fi

# ── 9. cron ───────────────────────────────────────────────────────────────────
info "安装定时任务"
# flock 防止并发跑多个索引进程，否则 SQLite 会 database is locked
CRON_CHECK="*/10 * * * * flock -n /tmp/indexer_check.lock $INSTALL_DIR/run.sh check >> $CONFIG_DIR/cron.log 2>&1"
CRON_EMBED="0 3 * * * flock -n /tmp/indexer_embed.lock $PY $INSTALL_DIR/build_embeddings.py >> $CONFIG_DIR/embedding.log 2>&1"
( crontab -l 2>/dev/null | grep -vF "$INSTALL_DIR/run.sh check" | grep -vF "build_embeddings.py"
  echo "$CRON_CHECK"; echo "$CRON_EMBED" ) | crontab -
ok "每 10 分钟回收 AI 结果，每天 3:00 生成向量"

# ── 完成 ──────────────────────────────────────────────────────────────────────
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "${GRN}${BLD}部署完成${RST}"
echo
echo "  访问地址   http://${WEB_DOMAIN:-$IP}      用户名 ${WEB_USER}"
echo
echo "  ${BLD}下一步${RST} — 首次索引（放 screen 里跑，避免 SSH 断开中断）:"
echo "    screen -S indexer"
echo "    $INSTALL_DIR/run.sh scan       # 扫描网盘建立文件清单"
echo "    $INSTALL_DIR/run.sh process    # 下载分析（耗时长，可 Ctrl+A D 脱离）"
echo
echo "  ${BLD}常用${RST}"
echo "    $INSTALL_DIR/run.sh status     # 看进度"
echo "    journalctl -u photo-web -f     # 看 Web 日志"
echo "    screen -r indexer              # 回到索引任务"
echo
[[ -z "$WEB_DOMAIN" ]] && warn "未配置域名，当前是 HTTP 明文。建议套 Cloudflare 或配 HTTPS 证书。"
echo
