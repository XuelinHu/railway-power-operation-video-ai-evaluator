#!/usr/bin/env bash
#
# 备份：数据库 + 媒体。
#
# **绝不要用 `cp app.db`。** 数据库跑在 WAL 模式下，已有内容可能还留在
# `-wal` 文件里没落盘，`cp` 出来的副本可能直接是损坏的——而"备份是坏的"
# 这件事通常要到真的需要恢复时才发现。`VACUUM INTO` 会生成一份一致的快照。
#
# 媒体（视频、证据帧）用 rsync 增量同步。它们是不可变文件，
# 只在写入完成后才出现在目录里，所以增量拷贝是安全的。
#
# 用法：
#   scripts/backup.sh                    # 备份到 $DATA_DIR/backups
#   scripts/backup.sh /mnt/usb/railway   # 备份到外部磁盘
#
# 建议 cron：每天 22:00 一次全量（数据库每小时一次太频繁，没意义——
# 100 人的系统一天也就几百次写入）。
#   0 22 * * * /opt/railway-power/scripts/backup.sh /mnt/backup/railway >> /var/log/railway-backup.log 2>&1

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/railway-power}"
DATA_DIR="${DATA_DIR:-$APP_DIR/data}"
DB_PATH="$DATA_DIR/app.db"

TARGET="${1:-$DATA_DIR/backups}"
STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="$TARGET/$STAMP"

if [[ ! -f "$DB_PATH" ]]; then
    echo "找不到数据库：$DB_PATH" >&2
    exit 1
fi

mkdir -p "$DEST"

# --- 数据库 ---------------------------------------------------------------
if ! command -v sqlite3 >/dev/null 2>&1; then
    echo "缺少 sqlite3 命令。Debian/Ubuntu：apt install sqlite3" >&2
    exit 1
fi

sqlite3 "$DB_PATH" "VACUUM INTO '$DEST/app.db'"

# 验证快照真的可用。**不做这一步的备份等于没有备份**：
# 一个 0 字节或损坏的文件同样会安安静静地躺在那里。
if ! sqlite3 "$DEST/app.db" "PRAGMA integrity_check;" | grep -q '^ok$'; then
    echo "备份完整性校验失败：$DEST/app.db" >&2
    rm -rf "$DEST"
    exit 1
fi

JOB_COUNT="$(sqlite3 "$DEST/app.db" "SELECT COUNT(*) FROM analysisjob;" 2>/dev/null || echo '?')"
echo "数据库已备份：$DEST/app.db（包含 $JOB_COUNT 条分析任务）"

# --- 媒体 -----------------------------------------------------------------
# 视频最大，用 rsync 增量；证据帧很小但是评分依据，必须一起备。
for name in uploads frames; do
    source_dir="$DATA_DIR/$name"
    [[ -d "$source_dir" ]] || continue
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --delete "$source_dir/" "$DEST/$name/"
    else
        cp -a "$source_dir" "$DEST/$name"
    fi
    echo "已备份 $name/"
done

# --- 清理旧备份 -----------------------------------------------------------
# 只保留最近 14 份。磁盘满了会让 worker 写不出证据帧，
# 而那时故障现象是"分析总是失败"，跟备份看起来毫无关系。
if [[ "$TARGET" == "$DATA_DIR/backups" ]]; then
    ls -1dt "$TARGET"/*/ 2>/dev/null | tail -n +15 | xargs -r rm -rf
fi

echo "备份完成：$DEST"
