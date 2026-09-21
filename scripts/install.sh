#!/usr/bin/env bash
#
# fan-video-dl - Web 视频下载器(基于 yt-dlp) 安装脚本
# 将源码安装为 systemd 服务（Python venv + gunicorn），并安装内置 CLI 命令。
# 可重复执行，升级等同于重新安装（覆盖程序并重启服务，数据目录保留）。
#
# Usage:
#   交互式安装（将提示端口与数据目录）:
#     bash scripts/install.sh
#   参数静默安装（-p 端口 / -d 数据目录 / -s 源码目录）:
#     bash scripts/install.sh -p 5200 -d /var/lib/fan-video-dl -y
#     bash scripts/install.sh -p 5200 -d /var/lib/fan-video-dl -s /data/fan-video-dl.repo
#   国内网络可用镜像仓库:
#     FAN_VIDEO_DL_REPO=https://ghfast.top/https://github.com/meimolihan/fan-video-dl.git bash scripts/install.sh -y

set -euo pipefail

# ================== terminal colors ==================
list_color_init() {
    export gl_hui=$'\033[38;5;59m'
    export gl_hong=$'\033[38;5;9m'
    export gl_lv=$'\033[38;5;10m'
    export gl_huang=$'\033[38;5;11m'
    export gl_lan=$'\033[38;5;32m'
    export gl_bai=$'\033[38;5;15m'
    export gl_zi=$'\033[38;5;13m'
    export gl_bufan=$'\033[38;5;14m'
    export reset=$'\033[0m'
}
list_color_init

sep_line() {
  printf '%s' "$gl_bufan"
  printf '—%.0s' {1..32}
  printf '%s\n' "$reset"
}

section() {
  printf "  %s %s\n" "${gl_zi}▶${reset}" "$1"
}

ok() {
  printf "  %s %s\n" "${gl_lv}>>>${reset}" "$1"
}

skip() {
  printf "  %s %s\n" "${gl_hui}--${reset}" "$1"
}

print_banner() {
  local z="$gl_zi" r="$reset" b="$gl_bai" l="$gl_lan"
  printf '%s\n' \
    "" \
    "  ${z}┌─────────────────────────────────────────┐${r}" \
    "  ${z}│${r}   ${b}fan-video-dl${r}  ${l}视频下载器 · 安装${r}     ${z}│${r}" \
    "  ${z}└─────────────────────────────────────────┘${r}" \
    ""
}

error() { printf "  %s %s\n" "${gl_hong}[错误]${reset}" "$1" >&2; exit 1; }

# ================== customize me ==================
APP_NAME="fan-video-dl"
DEFAULT_PORT=5200
APP_DIR="/var/lib/${APP_NAME}"
DEFAULT_DATA_DIR="${APP_DIR}/data"
CONFIG_FILE="/etc/${APP_NAME}.conf"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
CLI_BIN="/usr/local/bin/${APP_NAME}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-}")" && pwd)"
DEFAULT_SRC_DIR="${SCRIPT_DIR}/.."
GITHUB_REPO="https://github.com/meimolihan/fan-video-dl.git"
GITHUB_BIN_REPO="meimolihan/fan-video-dl"

# ================== GitHub 下载加速镜像 ==================
# 原始 GitHub 地址超时/失败时，按下列顺序依次尝试（末尾必须带斜杠）
GITHUB_MIRRORS=(
  "https://ghfast.top/"
  "https://ghproxy.net/"
  "https://gh.xxooo.cf/"
  "https://v6.gh-proxy.org/"
  "https://githubproxy.cc/"
)
# v6.gh-proxy.org 为纯 IPv6 代理：本机未配置 IPv6 地址时剔除，避免每次空等超时
if [ ! -s /proc/net/if_inet6 ]; then
  _no_v6=()
  for _m in "${GITHUB_MIRRORS[@]}"; do
    case "${_m}" in
      *v6.gh-proxy.org*) continue ;;
    esac
    _no_v6+=("${_m}")
  done
  GITHUB_MIRRORS=("${_no_v6[@]}")
fi

# 根据原始 GitHub URL 生成候选地址列表：原始地址优先，然后依次套用各镜像
make_url_candidates() {
  local github_url="$1"
  local p
  printf '%s\n' "$github_url"
  for p in "${GITHUB_MIRRORS[@]}"; do
    printf '%s\n' "${p}${github_url}"
  done
}
# ==========================================================

# 经 curl|bash 远程执行时，SCRIPT_DIR 指向 bash 抽取的临时目录，本地源码仓库
# 需按常见目录回退探测（当前目录 / 上一级目录 / 上级的上级），否则会误判"无本地源码"。
resolve_local_src() {
  local candidates=(
    "${SCRIPT_DIR:-}/.."
    "$(pwd)"
    "$(pwd)/.."
    "$(dirname "$(pwd)")"
    "$(dirname "$(dirname "$(pwd)")")"
  )
  local c cc app_canon
  app_canon="$(cd "${APP_DIR}" 2>/dev/null && pwd)"
  for c in "${candidates[@]}"; do
    [ -n "${c}" ] || continue
    cc="$(cd "${c}" 2>/dev/null && pwd)" || continue
    # 安装目录本身是旧版程序目录, 不能当作"本地源码"使用, 否则会 cp 自身且永远拉不到新版
    [ -n "${app_canon}" ] && [ "${cc}" = "${app_canon}" ] && continue
    if [ -f "${c}/app.py" ] && [ -f "${c}/requirements.txt" ]; then
      printf '%s' "${c}"
      return 0
    fi
  done
  return 1
}

is_valid_src() {
  [ -f "$1/app.py" ] && [ -f "$1/requirements.txt" ]
}
# ==================================================

PORT=""
DATA_DIR=""
SRC_DIR=""
SRC_DIR_EXPLICIT=0
INSTALL_YES=0

# ---- bootstrap: support `bash -c "$(curl ...)" -p ... -d ...` ----
case "$0" in
  -*) set -- "$0" "$@" ;;
esac

# ---- parse command-line args (silent install) ----
while [ "$#" -gt 0 ]; do
  case "$1" in
    -p|--port)
      shift
      [ -n "${1:-}" ] || error "缺少 -p/--port 的值"
      PORT="$1"
      ;;
    -d|--data)
      shift
      [ -n "${1:-}" ] || error "缺少 -d/--data 的值"
      DATA_DIR="$1"
      ;;
    -s|--src)
      shift
      [ -n "${1:-}" ] || error "缺少 -s/--src 的值"
      SRC_DIR="$1"
      SRC_DIR_EXPLICIT=1
      ;;
    -y|--yes)
      INSTALL_YES=1
      ;;
    -h|--help)
      printf "%s\n" "${gl_lan}fan-video-dl${reset} - ${gl_bai}Web 视频下载器(基于 yt-dlp) 安装脚本${reset}"
      printf "  %-13s %s\n" "${gl_bai}用法:${reset}" "bash scripts/install.sh [-p PORT] [-d DATA_DIR] [-s SRC] [-y]"
      printf "  %-13s %s\n" "${gl_bai}-p, --port${reset}" "监听端口（默认 ${gl_lan}${DEFAULT_PORT}${reset}）"
      printf "  %-13s %s\n" "${gl_bai}-d, --data${reset}" "数据目录（默认 ${gl_lan}${DEFAULT_DATA_DIR}${reset}）"
      printf "  %-13s %s\n" "${gl_bai}-s, --src${reset}" "源码仓库路径（默认 ${gl_lan}${DEFAULT_SRC_DIR}${reset}）"
      printf "  %-13s %s\n" "${gl_bai}-y, --yes${reset}" "免交互，未指定项全部使用默认值"
      printf "  %-13s %s\n" "${gl_bai}-h, --help${reset}" "显示本帮助"
      printf "%s\n" "${gl_hui}指定任意参数即进入静默安装；不带参数则为交互式安装。${reset}"
      printf "%s\n" "${gl_hui}未指定 -s 且本地无源码时，自动从 GitHub 拉取源码（可用 FAN_VIDEO_DL_REPO 自定义仓库/镜像）。${reset}"
      printf "%s\n" "${gl_hui}国内网络可设 FAN_VIDEO_DL_REPO=https://ghfast.top/https://github.com/meimolihan/fan-video-dl.git${reset}"
      printf "%s\n" "${gl_hui}登录用户名/密码默认 admin/admin123，可用环境变量 AUTH_USERNAME / AUTH_PASSWORD 覆盖。${reset}"
      exit 0
      ;;
    *)
      error "未知参数: $1（使用 -h 查看帮助）"
      ;;
  esac
  shift
done

# ---- firewall: automatically open the listen port ----
FW_OPENED="n"
open_firewall_port() {
  local PORT="$1"
  if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    if ! firewall-cmd --query-port="${PORT}/tcp" >/dev/null 2>&1; then
      firewall-cmd --permanent --add-port="${PORT}/tcp" >/dev/null 2>&1 || true
      firewall-cmd --reload >/dev/null 2>&1 || true
    fi
    ok "已通过 ${gl_bai}firewalld${reset} 开放端口 ${gl_lan}${PORT}/tcp${reset}"
    FW_OPENED="y"
    return 0
  fi

  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
    if ! ufw status 2>/dev/null | grep -q "${PORT}/tcp"; then
      ufw allow "${PORT}/tcp" >/dev/null 2>&1 || true
    fi
    ok "已通过 ${gl_bai}ufw${reset} 开放端口 ${gl_lan}${PORT}/tcp${reset}"
    FW_OPENED="y"
    return 0
  fi

  if command -v iptables >/dev/null 2>&1; then
    if iptables -C INPUT -p tcp --dport "${PORT}" -j ACCEPT >/dev/null 2>&1; then
      ok "端口 ${gl_lan}${PORT}/tcp${reset} 已在 iptables 中放行"
      FW_OPENED="y"
      return 0
    fi
    if iptables -L INPUT -n 2>/dev/null | grep -qE 'policy (DROP|REJECT)|REJECT|DROP'; then
      if iptables -I INPUT -p tcp --dport "${PORT}" -j ACCEPT >/dev/null 2>&1; then
        ok "已通过 ${gl_bai}iptables${reset} 开放端口 ${gl_lan}${PORT}/tcp${reset}"
        FW_OPENED="y"
        return 0
      fi
    fi
  fi
  printf "  %s %s\n" "${gl_huang}[提示]${reset}" "未检测到活跃的防火墙（firewalld/ufw/iptables），跳过端口开放。"
}

[ "$(id -u)" != "0" ] && error "请以 root 身份运行（例如 sudo bash scripts/install.sh）"

print_banner
sep_line
section "安装信息"
printf "  %-14s %s\n" "${gl_lan}系统${reset}" "$(uname -s) $(uname -m)"
printf "  %-14s %s\n" "${gl_lan}程序${reset}" "${gl_bai}${APP_NAME}${reset}"
sep_line

# ---- Python 运行时检查 ----
check_python_runtime() {
  if ! command -v python3 >/dev/null 2>&1; then
    error "未检测到 Python3，请先安装（要求 >= 3.9）：apt install python3 python3-venv python3-pip"
  fi
  if ! python3 -c 'import venv' >/dev/null 2>&1; then
    error "Python3 缺少 venv 模块，请安装：apt install python3-venv"
  fi
  PYTHON_BIN="$(command -v python3)"
  ok "Python$(python3 --version 2>&1 | awk '{print $2}') / ${gl_bai}${PYTHON_BIN}${reset}"
}

# ---- ffmpeg 检查（视频合并/转码必需）----
ensure_ffmpeg() {
  if command -v ffmpeg >/dev/null 2>&1; then
    ok "已检测到 ${gl_bai}ffmpeg${reset}"
    return 0
  fi
  printf "  %s %s\n" "${gl_huang}[提示]${reset}" "未检测到 ffmpeg，正在尝试自动安装 ..."
  if command -v apt-get >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null 2>&1 || true
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ffmpeg >/dev/null 2>&1 || true
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y -q ffmpeg >/dev/null 2>&1 || true
  elif command -v yum >/dev/null 2>&1; then
    yum install -y -q ffmpeg >/dev/null 2>&1 || true
  elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache ffmpeg >/dev/null 2>&1 || true
  fi
  if command -v ffmpeg >/dev/null 2>&1; then
    ok "ffmpeg 安装完成"
  else
    printf "  %s %s\n" "${gl_huang}[警告]${reset}" "ffmpeg 未安装成功，分离流视频将无法合并；请手动安装后重启服务。"
  fi
}

check_python_runtime

# ---- silent install detection ----
SILENT="n"
if [ -n "${PORT}" ]; then
  case "${PORT}" in
    ''|*[!0-9]*) error "PORT 无效（需为 1‑65535 的数字）: ${PORT}" ;;
    *) [ "${PORT}" -ge 1 ] && [ "${PORT}" -le 65535 ] || error "PORT 超出范围（1‑65535）: ${PORT}" ;;
  esac
  SILENT="y"
fi
[ -n "${DATA_DIR}" ] && SILENT="y"
[ -n "${SRC_DIR}" ] && SILENT="y"
[ ! -t 0 ] && SILENT="y"

section "配置参数"
# port prompt
if [ -z "${PORT}" ]; then
  if [ "$INSTALL_YES" = "1" ] || [ ! -t 0 ]; then
    PORT="${DEFAULT_PORT}"
  else
    while :; do
      read -r -p "${gl_bai}请输入监听端口${reset} ${gl_hui}[默认: ${DEFAULT_PORT}]${reset}: " PORT
      PORT="${PORT:-$DEFAULT_PORT}"
      case "$PORT" in
        ''|*[!0-9]*) printf "  %s\n" "${gl_huang}端口无效，请重新输入。${reset}" ;;
        *)
          if [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ]; then break; fi
          printf "  %s\n" "${gl_huang}端口超出范围（1‑65535），请重新输入。${reset}"
          ;;
      esac
    done
  fi
else
  printf "  %-14s %s\n" "${gl_lan}监听端口${reset}" "${gl_bai}${PORT}${reset}（参数指定）"
fi
PORT="${PORT:-$DEFAULT_PORT}"

# data dir prompt
if [ -z "${DATA_DIR}" ]; then
  if [ "$INSTALL_YES" = "1" ] || [ ! -t 0 ]; then
    DATA_DIR="${DEFAULT_DATA_DIR}"
  else
    read -r -p "${gl_bai}请输入数据目录${reset} ${gl_hui}[默认: ${DEFAULT_DATA_DIR}]${reset}: " DATA_DIR
    DATA_DIR="${DATA_DIR:-$DEFAULT_DATA_DIR}"
  fi
else
  printf "  %-14s %s\n" "${gl_lan}数据目录${reset}" "${gl_bai}${DATA_DIR}${reset}（参数指定）"
fi
DATA_DIR="${DATA_DIR:-$DEFAULT_DATA_DIR}"

# ---- 获取源码 ----
if [ "${SRC_DIR_EXPLICIT}" != "1" ]; then
  DISCOVERED_SRC="$(resolve_local_src)" || true
  if [ -n "${DISCOVERED_SRC:-}" ]; then
    SRC_DIR="${DISCOVERED_SRC}"
  fi
fi

# 源码目录即安装目录(旧版安装残留)时: 显式 -s 直接报错; 否则改为远程拉取最新代码
if [ -n "${SRC_DIR:-}" ]; then
  if [ "$(cd "${SRC_DIR}" 2>/dev/null && pwd)" = "$(cd "${APP_DIR}" 2>/dev/null && pwd)" ]; then
    if [ "${SRC_DIR_EXPLICIT}" = "1" ]; then
      error "安装目录 ${APP_DIR} 是旧版程序目录, 不能作为 -s 源码; 请去掉 -s 让脚本自动拉取最新代码"
    fi
    printf "  %s %s\n" "${gl_huang}[提示]${reset}" "检测到源码目录为已有安装目录(旧版残留), 改为远程拉取最新代码"
    SRC_DIR=""
  fi
fi

if ! is_valid_src "${SRC_DIR:-}"; then
  if [ "${SRC_DIR_EXPLICIT}" = "1" ]; then
    error "未找到源码仓库 ${SRC_DIR}（-s 显式指定，需包含 app.py 与 requirements.txt）"
  fi
  ok "本地无源码仓库，尝试获取源码（可用环境变量 FAN_VIDEO_DL_REPO 自定义仓库地址）"
  TMP_ROOT="$(mktemp -d)"
  TMP_SRC="${TMP_ROOT}/fan-video-dl"

  # 候选仓库源：自定义 > 原始 GitHub > 各镜像
  REPO_CANDIDATES=()
  [ -n "${FAN_VIDEO_DL_REPO:-}" ] && REPO_CANDIDATES+=("${FAN_VIDEO_DL_REPO}")
  while IFS= read -r u; do
    REPO_CANDIDATES+=("$u")
  done < <(make_url_candidates "${GITHUB_REPO}")

  FETCHED="n"
  if command -v timeout >/dev/null 2>&1; then CLONE_TIMEOUT="timeout 90"; else CLONE_TIMEOUT=""; fi

  if command -v git >/dev/null 2>&1; then
    for repo in "${REPO_CANDIDATES[@]}"; do
      [ -n "${repo}" ] || continue
      skip "尝试 git clone ${gl_bai}${repo}${reset}"
      if ${CLONE_TIMEOUT} git clone --depth=1 "${repo}" "${TMP_SRC}" 2>"${TMP_ROOT}/clone.err"; then
        FETCHED="y"
        break
      fi
      printf "  %s %s\n" "${gl_huang}[警告]${reset}" "克隆失败：$(tail -n 1 "${TMP_ROOT}/clone.err" 2>/dev/null)"
      rm -rf "${TMP_SRC}"
    done
  else
    printf "  %s %s\n" "${gl_huang}[警告]${reset}" "未检测到 git，跳过 git clone，改用源码压缩包"
  fi

  # 回退：下载源码压缩包并解压（无需 git，走与脚本下载一致的加速线路）
  if [ "${FETCHED}" != "y" ]; then
    ARCHIVE_URLS=()
    while IFS= read -r u; do
      ARCHIVE_URLS+=("$u")
    done < <(make_url_candidates "https://github.com/${GITHUB_BIN_REPO}/archive/refs/heads/main.tar.gz")
    ARCHIVE_URLS+=("https://codeload.github.com/${GITHUB_BIN_REPO}/tar.gz/refs/heads/main")

    if command -v curl >/dev/null 2>&1; then
      DL_CMD="curl -fsSL --connect-timeout 10 --max-time 120"
    elif command -v wget >/dev/null 2>&1; then
      DL_CMD="wget -qO- --timeout=120 --tries=1"
    else
      DL_CMD=""
    fi
    for url in "${ARCHIVE_URLS[@]}"; do
      [ -n "${url}" ] || continue
      [ -n "${DL_CMD}" ] || break
      skip "尝试下载源码包 ${gl_bai}${url}${reset}"
      TMP_TGZ="${TMP_ROOT}/src.tar.gz"
      if ${DL_CMD} "${url}" > "${TMP_TGZ}" 2>/dev/null \
        && tar -xzf "${TMP_TGZ}" -C "${TMP_ROOT}" 2>/dev/null; then
        EXTRACTED="$(find "${TMP_ROOT}" -maxdepth 1 -type d -name 'fan-video-dl-*' -print -quit 2>/dev/null)"
        if [ -n "${EXTRACTED}" ]; then
          mv "${EXTRACTED}" "${TMP_SRC}"
          FETCHED="y"
          break
        fi
      fi
      printf "  %s %s\n" "${gl_huang}[警告]${reset}" "下载失败：${url}"
      rm -f "${TMP_TGZ}"
    done
  fi

  [ "${FETCHED}" = "y" ] || error "获取源码仓库失败，请检查服务器网络，或使用 -s 指定本地源码目录"
  SRC_DIR="${TMP_SRC}"
  ok "已获取源码仓库"
fi

if command -v systemctl >/dev/null 2>&1; then
  USE_SYSTEMD="y"
else
  USE_SYSTEMD="n"
  printf "  %s\n" "${gl_huang}[警告]${reset} 未检测到 systemd（容器或受限环境）。"
  printf "  %s\n" "${gl_hui}    已回退为后台运行模式，重启或崩溃后服务不会自动恢复。${reset}"
fi

sep_line
section "安装程序"
ok "正在安装 ${gl_bai}${APP_NAME}${reset} 程序 ${gl_hong}.${gl_huang}.${gl_lv}.${gl_bai}"

# 1) 拷贝源码到安装目录（保留已有 data/downloads 链接与数据）
mkdir -p "${APP_DIR}"
cp -f "${SRC_DIR}/app.py" "${APP_DIR}/app.py"
cp -f "${SRC_DIR}/douyin_downloader.py" "${APP_DIR}/douyin_downloader.py"
[ -f "${SRC_DIR}/version.txt" ] && cp -f "${SRC_DIR}/version.txt" "${APP_DIR}/version.txt"
[ -f "${SRC_DIR}/requirements.txt" ] && cp -f "${SRC_DIR}/requirements.txt" "${APP_DIR}/requirements.txt"
[ -f "${SRC_DIR}/cli.py" ] && cp -f "${SRC_DIR}/cli.py" "${APP_DIR}/cli.py"
rm -rf "${APP_DIR}/templates"
mkdir -p "${APP_DIR}/templates"
cp -rf "${SRC_DIR}/templates/." "${APP_DIR}/templates/"
ok "已拷贝程序源码至 ${gl_bai}${APP_DIR}${reset}"

# 1.1) 拷贝运维脚本（备份/还原脚本供 CLI 调用，也便于手动执行）
if [ -d "${SRC_DIR}/scripts" ]; then
  mkdir -p "${APP_DIR}/scripts"
  cp -rf "${SRC_DIR}/scripts/." "${APP_DIR}/scripts/"
  chmod +x "${APP_DIR}"/scripts/*.sh 2>/dev/null || true
  ok "已部署运维脚本至 ${gl_bai}${APP_DIR}/scripts${reset}"
fi

# 2) 创建虚拟环境并安装依赖
ok "正在创建 Python 虚拟环境并安装依赖 ${gl_hong}.${gl_huang}.${gl_lv}.${gl_bai}"
if [ ! -x "${APP_DIR}/venv/bin/python" ]; then
  python3 -m venv "${APP_DIR}/venv" || error "创建虚拟环境失败，请确认已安装 python3-venv"
fi
"${APP_DIR}/venv/bin/python" -m pip install --upgrade pip -q >/dev/null 2>&1 || true
if ! "${APP_DIR}/venv/bin/pip" install -q -r "${APP_DIR}/requirements.txt"; then
  printf "  %s %s\n" "${gl_huang}[警告]${reset}" "依赖安装失败，尝试使用国内镜像源重试 ..."
  "${APP_DIR}/venv/bin/pip" install -q -i https://pypi.tuna.tsinghua.edu.cn/simple -r "${APP_DIR}/requirements.txt" \
    || error "依赖安装失败，请检查服务器网络"
fi
ok "Python 依赖安装完成"

ensure_ffmpeg

# 3) 数据目录 & 软链接（数据与程序分离，便于备份/还原）
[ -L "${APP_DIR}/data" ] && rm -f "${APP_DIR}/data"
[ -L "${APP_DIR}/downloads" ] && rm -f "${APP_DIR}/downloads"
mkdir -p "${DATA_DIR}" "${DATA_DIR}/downloads"
if [ "${DATA_DIR}" != "${APP_DIR}/data" ]; then
  ln -s "${DATA_DIR}" "${APP_DIR}/data"
fi
ln -s "${DATA_DIR}/downloads" "${APP_DIR}/downloads"
chmod 700 "${DATA_DIR}"
ok "数据目录 ${gl_lan}${DATA_DIR}${reset} 已就绪（含 users.db 与 downloads）"

# 4) 登录凭据（默认 admin/admin123，可用环境变量覆盖）
AUTH_USERNAME="${AUTH_USERNAME:-admin}"
AUTH_PASSWORD="${AUTH_PASSWORD:-admin123}"

# 5) 安装记录
mkdir -p "$(dirname "${CONFIG_FILE}")"
cat > "${CONFIG_FILE}" <<EOF
# ${APP_NAME} 安装记录（由 install.sh 生成，请勿手动修改）
APP_DIR=${APP_DIR}
PORT=${PORT}
DATA_DIR=${DATA_DIR}
BACKUP_DIR=${APP_DIR}/backup
INSTALL_METHOD=source
PYTHON_BIN=${PYTHON_BIN}
AUTH_USERNAME=${AUTH_USERNAME}
AUTH_PASSWORD=${AUTH_PASSWORD}
EOF
chmod 0600 "${CONFIG_FILE}"
ok "已写入安装记录 ${gl_bai}${CONFIG_FILE}${reset}"

# 6) 安装内置 CLI 命令
mkdir -p "$(dirname "${CLI_BIN}")"
cat > "${CLI_BIN}" <<CLI
#!/bin/sh
exec "${APP_DIR}/venv/bin/python" "${APP_DIR}/cli.py" "\$@"
CLI
chmod +x "${CLI_BIN}"
ok "已安装命令 ${gl_bai}${CLI_BIN}${reset}（运行 ${gl_bai}${APP_NAME} help${reset} 查看用法）"

sep_line
section "启动服务"
if [ "${USE_SYSTEMD}" = "y" ]; then
  cat > "${SERVICE_FILE}" <<UNIT
[Unit]
Description=${APP_NAME} - Web 视频下载器(基于 yt-dlp)
After=network-online.target local-fs.target
Wants=network-online.target

[Service]
Type=simple
# KillMode=process：systemctl stop 只终止主 gunicorn 进程，不波及面板 spawn 的下载/合并子进程
KillMode=process
ExecStart=${APP_DIR}/venv/bin/gunicorn --bind 0.0.0.0:${PORT} --workers 1 --threads 8 --timeout 600 app:app
WorkingDirectory=${APP_DIR}
Environment=PORT=${PORT}
Environment=AUTH_USERNAME=${AUTH_USERNAME}
Environment=AUTH_PASSWORD=${AUTH_PASSWORD}
Environment=TZ=Asia/Shanghai
Restart=on-failure
RestartSec=3
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
UNIT

  systemctl daemon-reload
  systemctl enable "${APP_NAME}" >/dev/null 2>&1 || true
  systemctl restart "${APP_NAME}"
  sleep 3
  if systemctl is-active "${APP_NAME}" >/dev/null 2>&1; then
    ok "${gl_bai}${APP_NAME}${reset} 服务已启动。"
    systemctl status "${APP_NAME}" --no-pager || true
  else
    printf "  %s\n" "${gl_hong}[错误]${reset} 服务启动失败，请检查：${gl_bai}journalctl -u ${APP_NAME} -n 50${reset}" >&2
    exit 1
  fi
else
  if command -v pgrep >/dev/null 2>&1 && pgrep -f "${APP_DIR}/venv/bin/gunicorn" >/dev/null 2>&1; then
    printf "  %s\n" "${gl_huang}[警告]${reset} 检测到 ${APP_NAME} 进程可能已在运行"
  else
    ( cd "${APP_DIR}" && PORT="${PORT}" AUTH_USERNAME="${AUTH_USERNAME}" AUTH_PASSWORD="${AUTH_PASSWORD}" \
        nohup "${APP_DIR}/venv/bin/gunicorn" --bind "0.0.0.0:${PORT}" --workers 1 --threads 8 --timeout 600 app:app \
        >> "${DATA_DIR}/${APP_NAME}.log" 2>&1 & )
    ok "${APP_NAME} 已在后台启动"
  fi
fi

# 取第一个IPv4
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
[ -z "${IP}" ] && IP="<服务器IP>"

open_firewall_port "${PORT}"

if [ "${FW_OPENED}" = "y" ]; then
  FW_STATUS="${gl_lv}已开放 ${PORT}/tcp${reset}"
else
  FW_STATUS="${gl_huang}未检测到活跃防火墙，已跳过${reset}"
fi

sep_line
if [ "${USE_SYSTEMD}" = "y" ]; then
  printf "  %s\n" "${gl_lv}✔ ${APP_NAME} 安装成功！${reset}"
  printf "  %-14s %s\n" "${gl_lan}访问地址${reset}" "${gl_bai}http://${IP}:${PORT}${reset}"
  printf "  %-14s %s\n" "${gl_lan}用户名${reset}" "${gl_bai}${AUTH_USERNAME}${reset}"
  printf "  %-14s %s\n" "${gl_lan}密码${reset}" "${gl_bai}${AUTH_PASSWORD}${reset}"
  printf "  %-14s %s\n" "${gl_lan}数据目录${reset}" "${gl_bai}${DATA_DIR}${reset}"
  printf "  %-14s %s\n" "${gl_lan}程序目录${reset}" "${gl_bai}${APP_DIR}${reset}"
  printf "  %-14s %s\n" "${gl_lan}防火墙状态${reset}" "$FW_STATUS"
  printf "  %-14s %s\n" "${gl_lan}运行模式${reset}" "${gl_bai}systemd 服务${reset}"
  printf "  %-14s %s\n" "${gl_lan}服务命令${reset}" "${gl_hui}systemctl status ${APP_NAME}${reset}"
  printf "  %-14s %s\n" "${gl_lan}升级方式${reset}" "${gl_hui}重新执行 scripts/install.sh（数据自动保留）${reset}"
else
  printf "  %s\n" "${gl_lv}✔ ${APP_NAME} 安装成功！${reset} ${gl_huang}（后台运行模式）${reset}"
  printf "  %-14s %s\n" "${gl_lan}访问地址${reset}" "${gl_bai}http://${IP}:${PORT}${reset}"
  printf "  %-14s %s\n" "${gl_lan}用户名${reset}" "${gl_bai}${AUTH_USERNAME}${reset}"
  printf "  %-14s %s\n" "${gl_lan}密码${reset}" "${gl_bai}${AUTH_PASSWORD}${reset}"
  printf "  %-14s %s\n" "${gl_lan}数据目录${reset}" "${gl_bai}${DATA_DIR}${reset}"
  printf "  %-14s %s\n" "${gl_lan}程序目录${reset}" "${gl_bai}${APP_DIR}${reset}"
  printf "  %s\n" "  ${gl_huang}注意：${reset}后台运行模式在系统重启后不会自动恢复。"
fi
sep_line
printf "  %s\n" "${gl_hui}提示：登录后请及时在「账户」中修改默认用户名与密码。${reset}"
sep_line
