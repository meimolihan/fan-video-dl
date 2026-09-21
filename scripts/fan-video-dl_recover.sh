#!/bin/bash
set -uo pipefail

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
SERVICE="fan-video-dl"
CONFIG_FILE="/etc/fan-video-dl.conf"
APP_DIR="/var/lib/fan-video-dl"
DATA_DIR="/var/lib/fan-video-dl/data"
# 从安装记录读取安装目录/数据目录/备份目录（不存在时使用默认值）
BACKUP_DIR=""
if [ -f "${CONFIG_FILE}" ]; then
  while IFS='=' read -r KEY VALUE; do
    KEY=$(printf '%s' "$KEY" | tr -d ' ')
    [ "$KEY" = "APP_DIR" ] && [ -n "$VALUE" ] && APP_DIR="${VALUE}"
    [ "$KEY" = "DATA_DIR" ] && [ -n "$VALUE" ] && DATA_DIR="${VALUE}"
    [ "$KEY" = "BACKUP_DIR" ] && [ -n "$VALUE" ] && BACKUP_DIR="${VALUE}"
  done < "${CONFIG_FILE}"
fi
BACKUP_DIR="${BACKUP_DIR:-${APP_DIR}/backup}"

# 参数解析：1) 备份目录 2) 指定还原文件（可选，缺省取最新备份）
# 用法：
# ./fan-video-dl_recover.sh                          # 默认目录，取该目录最新备份
# ./fan-video-dl_recover.sh /data/bak [文件名]        # 指定目录，可选指定文件
parse_args() {
    local p1="${1:-}"
    local p2="${2:-}"
    if [[ -n "${p1}" ]]; then
        BACKUP_DIR="${p1}"
    fi
    if [[ -n "${p2}" ]]; then
        RESTORE_FILE_ARG="${p2}"
    fi
}
RESTORE_FILE_ARG=""
parse_args "$@"

echo -e "${gl_zi}>>> fan-video-dl 恢复脚本${gl_bai}"
echo -e "${gl_bufan}————————————————————————————————————————————————${gl_bai}"
echo -e "${gl_huang}备份目录：${gl_lv}${BACKUP_DIR}${gl_bai}"
echo -e "${gl_huang}数据目录：${gl_lv}${DATA_DIR}${gl_bai}"

command -v systemctl >/dev/null 2>&1 || { echo -e "${gl_hong}❌ 未检测到 systemctl，无法恢复服务${gl_bai}"; exit 1; }

mkdir -p "${BACKUP_DIR}"

echo -e ""
if [ -n "${RESTORE_FILE_ARG}" ]; then
    echo -e "${gl_lan}>>> 使用指定备份文件${gl_bai}"
    if [[ "${RESTORE_FILE_ARG}" =~ ^FanVideoDL-.*\.tar\.gz$ ]] && [ -f "${BACKUP_DIR}/${RESTORE_FILE_ARG}" ]; then
        f="${RESTORE_FILE_ARG}"
    else
        echo -e "${gl_hong}❌ 指定的备份文件不存在: ${RESTORE_FILE_ARG}${gl_bai}"
        exit 1
    fi
else
    echo -e "${gl_lan}>>> 查找最新备份文件${gl_bai}"
    f=$(find "$BACKUP_DIR" -maxdepth 1 -type f -name "FanVideoDL-*.tar.gz" -printf "%f\n" \
    | sed -E 's/^FanVideoDL-([0-9]{4}-[0-9]{2}-[0-9]{2}(_[0-9]{2}-[0-9]{2}-[0-9]{2})?)\.tar\.gz$/\1 &/' \
    | sort -k1,1 \
    | tail -n1 \
    | awk '{print $2}')
fi

if [ -z "$f" ];then
    echo -e "${gl_hong}❌ 无备份文件，退出${gl_bai}"
    exit 1
fi

RESTORE_FILE="${BACKUP_DIR}/${f}"
echo -e "${gl_huang}恢复文件: ${gl_lv}${RESTORE_FILE}${gl_bai}"

echo -e ""
echo -e "${gl_huang}>>> 停止 ${SERVICE} 服务${gl_bai}"
systemctl stop ${SERVICE}

echo -e ""
echo -e "${gl_huang}>>> 执行恢复${gl_bai}"
# 备份内容为数据目录下所有内容；恢复前先清空旧数据目录，避免残留文件
find "${DATA_DIR}" -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null || true
mkdir -p "${DATA_DIR}"
if tar -xzf "${RESTORE_FILE}" -C "${DATA_DIR}"; then
    echo -e "${gl_lv}>>> 恢复完成${gl_bai}"
else
    echo -e "${gl_hong}❌ 恢复失败，正在启动服务${gl_bai}"
    systemctl start ${SERVICE}
    exit 1
fi

echo -e ""
echo -e "${gl_huang}>>> 重载systemd配置并启动服务${gl_bai}"
systemctl daemon-reload
systemctl start ${SERVICE}

sleep 2
STATUS=$(systemctl is-active ${SERVICE})
case "${STATUS}" in
    active)
        echo -e "${gl_lv}✅ 服务状态：运行中${gl_bai}"
        ;;
    inactive)
        echo -e "${gl_hong}❌ 服务状态：已停止${gl_bai}"
        ;;
    failed)
        echo -e "${gl_hong}❌ 服务状态：启动失败${gl_bai}"
        ;;
    *)
        echo -e "${gl_huang}⚠️ 服务状态：${STATUS}${gl_bai}"
        ;;
esac
echo -e "${gl_bufan}————————————————————————————————————————————————${gl_bai}"
