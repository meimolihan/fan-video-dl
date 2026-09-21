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
# 默认值
BACKUP_DIR=""
KEEP_NUM=6
SERVICE="fan-video-dl"
CONFIG_FILE="/etc/fan-video-dl.conf"
APP_DIR="/var/lib/fan-video-dl"
DATA_DIR="/var/lib/fan-video-dl/data"

# 从安装记录读取安装目录/数据目录/备份目录（不存在时使用默认值）
if [ -f "${CONFIG_FILE}" ]; then
  while IFS='=' read -r KEY VALUE; do
    KEY=$(printf '%s' "$KEY" | tr -d ' ')
    [ "$KEY" = "APP_DIR" ] && [ -n "$VALUE" ] && APP_DIR="${VALUE}"
    [ "$KEY" = "DATA_DIR" ] && [ -n "$VALUE" ] && DATA_DIR="${VALUE}"
    [ "$KEY" = "BACKUP_DIR" ] && [ -n "$VALUE" ] && BACKUP_DIR="${VALUE}"
  done < "${CONFIG_FILE}"
fi
BACKUP_DIR="${BACKUP_DIR:-${APP_DIR}/backup}"

# 参数解析，支持两种传参顺序可互换
# 用法示例：
# ./fan-video-dl_backup.sh                     # 使用默认
# ./fan-video-dl_backup.sh 10                  # 只传保留份数
# ./fan-video-dl_backup.sh /data/bak           # 只传备份目录
# ./fan-video-dl_backup.sh /data/bak 8         # 目录 保留数
# ./fan-video-dl_backup.sh 8 /data/bak         # 保留数 目录，顺序互换兼容
parse_args() {
    local p1="${1:-}"
    local p2="${2:-}"

    if [[ -z "${p1}" ]]; then
        return
    fi

    if [[ "${p1}" =~ ^[0-9]+$ ]]; then
        KEEP_NUM="${p1}"
        if [[ -n "${p2}" ]]; then
            BACKUP_DIR="${p2}"
        fi
    else
        BACKUP_DIR="${p1}"
        if [[ -n "${p2}" && "${p2}" =~ ^[0-9]+$ ]]; then
            KEEP_NUM="${p2}"
        fi
    fi
}
parse_args "$@"

echo -e "${gl_zi}>>> 备份 fan-video-dl${gl_bai}"
echo -e "${gl_bufan}————————————————————————————————————————————————${gl_bai}"
echo -e "${gl_huang}保存目录：${gl_lv}${BACKUP_DIR}${gl_bai}"
echo -e "${gl_huang}保留数量：${gl_lv}${KEEP_NUM}${gl_bai}"
echo -e "${gl_huang}数据目录：${gl_lv}${DATA_DIR}${gl_bai}"

[ -d "${DATA_DIR}" ] || { echo -e "${gl_hong}❌ 数据目录不存在: ${DATA_DIR}${gl_bai}"; exit 1; }

# 文件名精确到秒 YYYY-MM-DD_HH-MM-SS
NOW=$(date +%Y-%m-%d_%H-%M-%S)
BACKUP_FILE="${BACKUP_DIR}/FanVideoDL-${NOW}.tar.gz"

mkdir -p "${BACKUP_DIR}"

# 在线备份：不停止服务，避免面板自身被 systemctl stop 连带终止（面板与脚本同属系统服务）
echo -e ""
echo -e "${gl_lan}>>> 执行备份：${BACKUP_FILE}${gl_bai}"
if tar -czf "${BACKUP_FILE}" -C "${DATA_DIR}" .; then
    echo -e "${gl_lv}>>> 备份完成：${BACKUP_FILE}${gl_bai}"
else
    echo -e "${gl_hong}❌ 备份失败${gl_bai}"
    exit 1
fi

cd "${BACKUP_DIR}" || exit 1

# 正则适配新格式：FanVideoDL-YYYY-MM-DD_HH-MM-SS.tar.gz
old_files=$(find . -maxdepth 1 -type f -name "FanVideoDL-*.tar.gz" -printf "%f\n" \
| sed -E 's/^FanVideoDL-([0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]{2})\.tar\.gz$/\1 &/' \
| sort -k1,1 \
| head -n -${KEEP_NUM} \
| awk '{print $2}')

if [ -n "${old_files}" ];then
    echo -e ""
    echo -e "${gl_hong}>>> 将删除过期备份文件：${gl_bai}"
    echo "${old_files}"
    echo "${old_files}" | xargs -I {} rm -f "./{}"
else
    echo -e "${gl_lv}>>> 无过期备份需要删除${gl_bai}"
fi

echo -e "${gl_bufan}————————————————————————————————————————————————${gl_bai}"
echo -e "${gl_lv}✔ 备份完成：${BACKUP_FILE}${gl_bai}"
echo -e "${gl_huang}还原命令：bash scripts/fan-video-dl_recover.sh${gl_bai}"
