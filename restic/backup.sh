#!/bin/bash
# =============================================================================
# Docker Projects Restic Backup Script
# =============================================================================
# 用途：对 ~/docker_projects 进行加密增量备份，上传到 Cloudflare R2。
#
# 依赖：
#   - restic（sudo apt install -y restic && sudo restic self-update）
#   - ~/.restic.env（凭据文件，600 权限）
#   - ~/docker_projects/.restic/excludes.txt（排除规则，按服务器定制）
#   - ~/docker_projects/.restic/db-containers.conf（数据库容器映射，可为空）
#   - ~/docker_projects/.restic/prometheus-snapshots.conf（Prometheus 映射，可为空）
#
# ~/.restic.env 格式：
#   export AWS_ACCESS_KEY_ID=<R2 Access Key ID>
#   export AWS_SECRET_ACCESS_KEY=<R2 Secret Access Key>
#   export RESTIC_REPOSITORY=s3:https://<account>.r2.cloudflarestorage.com/jnxyp-docker-backup/<hostname>
#   export RESTIC_PASSWORD=<加密密码，丢失后数据永久无法解密>
#
# db-containers.conf 格式（空行和 # 开头行忽略）：
#   容器名=mongodb
#   容器名=postgresql:用户名:数据库名
#   容器名=mysql[:数据库名]
#   容器名=mariadb[:数据库名]
#
# prometheus-snapshots.conf 格式（空行和 # 开头行忽略）：
#   名称|管理 API URL|宿主机 TSDB snapshots 目录
#   prometheus|http://127.0.0.1:9090|/home/ubuntu/docker_projects/observability/prometheus/data/snapshots
#
# 数据库备份策略：
#   运行中的数据库数据目录不直接备份（有损坏风险）。
#   本脚本先 dump 到 .restic/_dumps/，打包进快照后立即删除。
#   原始数据目录（data-node、pgdata 等）在 excludes.txt 中排除。
#
# 日志：输出由 cron 重定向到 /var/log/restic/backup.log（日志走 stderr）
#   成功标记：Backup complete
#   失败标记：BACKUP FAILED: <原因>   ← 告警按这个关键字匹配，比"25h 没成功"及时
#
# 可调环境变量（默认值见脚本内）：
#   BACKUP_TIMEOUT=2h    restic backup 超时
#   FORGET_TIMEOUT=1h    restic forget --prune 超时
#   DUMP_TIMEOUT=30m     单个数据库 dump 超时
#   MIN_FREE_MB=2048     dump 前要求的最小剩余磁盘（MB）
#
# 保护机制：
#   - flock（/tmp/restic-backup.lock）：上次没跑完时，本次直接退出，不叠加
#   - timeout：任何阶段卡死都会在限时后失败退出，不再无限重试
#   - restic unlock：清理上次被 kill 留下的陈旧仓库锁
#   - 磁盘预检：避免 dump 写一半被截断（比直接失败更危险）
#
# 部署步骤（首次）：
#   1. sudo apt install -y restic && sudo restic self-update
#   2. vim ~/.restic.env && chmod 600 ~/.restic.env
#   3. source ~/.restic.env && restic init
#   4. sudo mkdir -p /var/log/restic && sudo chown $(whoami) /var/log/restic
#   5. sudo ln -s ~/docker_projects/.restic/restic.alloy /etc/alloy/restic.alloy
#   6. sudo install -o root -g root -m 644 ~/docker_projects/.restic/cron /etc/cron.d/restic-backup
#   7. sudo systemctl reload alloy
# =============================================================================

set -euo pipefail

DOCKER_PROJECTS="$HOME/docker_projects"
RESTIC_DIR="$DOCKER_PROJECTS/.restic"
DUMPS_DIR="$RESTIC_DIR/_dumps"
DB_CONF="$RESTIC_DIR/db-containers.conf"
PROM_CONF="$RESTIC_DIR/prometheus-snapshots.conf"
EXCLUDES="$RESTIC_DIR/excludes.txt"
LOCK_FILE="/tmp/restic-backup.lock"
SNAPSHOTS_DIR="$RESTIC_DIR/_snapshots"

# 超时保护（可用环境变量覆盖）。背景：2026-08-06 cn-hk-fossic 因机房限速导致
# 上传 R2 持续失败，restic 卡在同几个数据块上无限重试，跑了 6h48m 仍未结束
# （正常仅 19-22 秒），既没完成也没退出，还挡住了当天的备份。
DUMP_TIMEOUT="${DUMP_TIMEOUT:-30m}"      # 单个数据库 dump
BACKUP_TIMEOUT="${BACKUP_TIMEOUT:-2h}"   # restic backup
FORGET_TIMEOUT="${FORGET_TIMEOUT:-1h}"   # restic forget --prune
MIN_FREE_MB="${MIN_FREE_MB:-2048}"       # dump 前要求的最小剩余磁盘

# 日志一律走 stderr：dump 阶段 stdout 会被重定向进 .sql 文件，
# 日志若走 stdout 就会污染备份数据。cron 已 2>&1，收集不受影响。
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >&2; }

# 失败时打一行固定标记，便于 Loki 告警按关键字匹配，而不是只靠
# "25 小时内没有出现 Backup complete" 这种滞后判据。
fail() {
    log "BACKUP FAILED: $*"
    exit 1
}

# timeout 超时退出码为 124；把它翻译成明确的失败信息，避免静默卡死。
run_with_timeout() {
    local limit="$1" desc="$2"; shift 2
    local rc=0
    timeout --signal=TERM --kill-after=60s "$limit" "$@" || rc=$?
    if [[ $rc -eq 124 || $rc -eq 137 ]]; then
        fail "$desc timed out after $limit (rc=$rc)"
    elif [[ $rc -ne 0 ]]; then
        fail "$desc exited with rc=$rc"
    fi
}

cleanup_transients() {
    if [[ -d "$DUMPS_DIR" ]]; then
        rm -rf "$DUMPS_DIR"
        log "Cleaned up temporary dumps."
    fi
    if [[ -d "$SNAPSHOTS_DIR" ]]; then
        [[ "$SNAPSHOTS_DIR" == "$HOME/docker_projects/.restic/_snapshots" ]] \
            || fail "refusing to clean unexpected snapshot path: $SNAPSHOTS_DIR"
        sudo rm -rf -- "$SNAPSHOTS_DIR"
        log "Cleaned up temporary Prometheus snapshots."
    fi
}

get_container_env() {
    local container="$1"
    local key="$2"

    docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$container" \
        | grep -m1 "^${key}=" \
        | cut -d= -f2-
}

# --------------------------------------------------------------------------
# 并发保护
# 上一次跑挂住时，第二天 cron 会再起一个，两个 restic 抢同一个仓库锁只会更糟。
# 用 flock 保证同一时刻只有一个实例；抢不到就直接退出并报错。
# --------------------------------------------------------------------------
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    log "BACKUP FAILED: another backup is still running (holding $LOCK_FILE), aborting."
    exit 1
fi

# --------------------------------------------------------------------------
# 加载凭据
# --------------------------------------------------------------------------
source ~/.restic.env
trap cleanup_transients EXIT

# --------------------------------------------------------------------------
# 清理陈旧仓库锁
# 上一次被 kill / OOM 掉时会在仓库里留下锁，下一次运行会直接失败。
# restic unlock 默认只删除已过期的非独占锁，正常运行中的实例不受影响。
# --------------------------------------------------------------------------
restic unlock 2>&1 | grep -v '^successfully removed 0 locks$' || true

# --------------------------------------------------------------------------
# 磁盘空间预检
# dump 会先落盘再打包，磁盘满会导致 dump 截断（比失败更危险）。
# --------------------------------------------------------------------------
if [[ -s "$DB_CONF" ]]; then
    free_mb=$(df -Pm "$RESTIC_DIR" | awk 'NR==2{print $4}')
    if [[ -n "$free_mb" && "$free_mb" -lt "$MIN_FREE_MB" ]]; then
        fail "only ${free_mb}MB free on $(df -P "$RESTIC_DIR" | awk 'NR==2{print $6}'), need >= ${MIN_FREE_MB}MB for dumps"
    fi
fi

# --------------------------------------------------------------------------
# 阶段一：数据库预转储
# 读取 db-containers.conf，对每个运行中的容器执行对应的 dump。
# dump 输出到 .restic/_dumps/，会被 restic 一并打包进快照。
# 没有 db-containers.conf 或文件为空时直接跳过。
# --------------------------------------------------------------------------
if [[ -s "$DB_CONF" ]]; then
    while IFS='=' read -r container spec || [[ -n "$container" ]]; do
        [[ -z "$container" || "$container" == \#* ]] && continue

        type=$(echo "$spec" | cut -d: -f1)
        params=$(echo "$spec" | cut -d: -f2-)

        # 容器未运行则跳过，不中断整体备份
        if ! docker ps --format '{{.Names}}' | grep -q "^${container}$"; then
            log "WARNING: container '$container' not running, skipping dump."
            continue
        fi

        case "$type" in
            mongodb)
                # 在容器内 dump 到 /tmp，再 docker cp 到宿主机，避免权限问题
                log "Dumping MongoDB: $container ..."
                dest="$DUMPS_DIR/mongodb/$container"
                mkdir -p "$dest"
                run_with_timeout "$DUMP_TIMEOUT" "mongodump ($container)" \
                    docker exec "$container" mongodump --out /tmp/_restic_dump --quiet
                docker cp "$container:/tmp/_restic_dump/." "$dest/"
                docker exec "$container" rm -rf /tmp/_restic_dump
                log "MongoDB dump complete: $dest"
                ;;
            postgresql)
                # pg_dump 输出到 stdout，直接重定向到宿主机文件
                user=$(echo "$params" | cut -d: -f1)
                dbname=$(echo "$params" | cut -d: -f2)
                log "Dumping PostgreSQL: $container ($dbname) ..."
                dest="$DUMPS_DIR/postgresql/$container"
                mkdir -p "$dest"
                run_with_timeout "$DUMP_TIMEOUT" "pg_dump ($container/$dbname)" \
                    docker exec "$container" pg_dump -U "$user" "$dbname" \
                    > "$dest/${dbname}.sql"
                log "PostgreSQL dump complete: $dest"
                ;;
            mysql|mariadb)
                # 优先使用容器环境变量中的 root 凭据；数据库名可由配置覆盖
                dbname="$params"
                if [[ -z "$dbname" ]]; then
                    dbname="$(get_container_env "$container" MYSQL_DATABASE || true)"
                fi

                root_pw="$(get_container_env "$container" MYSQL_ROOT_PASSWORD || true)"
                user="$(get_container_env "$container" MYSQL_USER || true)"
                user_pw="$(get_container_env "$container" MYSQL_PASSWORD || true)"

                if [[ -n "$root_pw" ]]; then
                    dump_user="root"
                    dump_pw="$root_pw"
                elif [[ -n "$user" && -n "$user_pw" ]]; then
                    dump_user="$user"
                    dump_pw="$user_pw"
                else
                    log "WARNING: no MySQL credentials found for '$container', skipping dump."
                    continue
                fi

                dest="$DUMPS_DIR/mysql/$container"
                mkdir -p "$dest"

                if [[ -n "$dbname" ]]; then
                    log "Dumping MySQL: $container ($dbname) ..."
                    run_with_timeout "$DUMP_TIMEOUT" "mysqldump ($container/$dbname)" \
                        docker exec \
                        -e MYSQL_DUMP_USER="$dump_user" \
                        -e MYSQL_DUMP_PASSWORD="$dump_pw" \
                        -e MYSQL_DUMP_DATABASE="$dbname" \
                        "$container" sh -lc \
                        'exec mysqldump --single-transaction --quick --routines --events --triggers --set-gtid-purged=OFF -u"$MYSQL_DUMP_USER" -p"$MYSQL_DUMP_PASSWORD" "$MYSQL_DUMP_DATABASE"' \
                        > "$dest/${dbname}.sql"
                    log "MySQL dump complete: $dest/${dbname}.sql ($(du -h "$dest/${dbname}.sql" | cut -f1))"
                else
                    log "Dumping MySQL: $container (all databases) ..."
                    run_with_timeout "$DUMP_TIMEOUT" "mysqldump ($container/all)" \
                        docker exec \
                        -e MYSQL_DUMP_USER="$dump_user" \
                        -e MYSQL_DUMP_PASSWORD="$dump_pw" \
                        "$container" sh -lc \
                        'exec mysqldump --single-transaction --quick --routines --events --triggers --set-gtid-purged=OFF -u"$MYSQL_DUMP_USER" -p"$MYSQL_DUMP_PASSWORD" --all-databases' \
                        > "$dest/all-databases.sql"
                    log "MySQL dump complete: $dest/all-databases.sql ($(du -h "$dest/all-databases.sql" | cut -f1))"
                fi
                ;;
            *)
                log "WARNING: unknown db type '$type' for '$container', skipping."
                ;;
        esac
    done < "$DB_CONF"
fi

# --------------------------------------------------------------------------
# 阶段二：Prometheus 一致性快照
# 管理 API 创建的 snapshot 对历史 block 使用硬链接；将 snapshot 在同一文件系统内
# 移到 .restic/_snapshots 后再备份，避免复制整套 TSDB，也不会直接读取活跃 WAL。
# --------------------------------------------------------------------------
if [[ -s "$PROM_CONF" ]]; then
    while IFS='|' read -r name api_url host_snapshots_dir || [[ -n "$name" ]]; do
        [[ -z "$name" || "$name" == \#* ]] && continue
        [[ -n "$api_url" && -n "$host_snapshots_dir" ]] \
            || fail "invalid Prometheus snapshot mapping for '$name'"

        log "Creating Prometheus snapshot: $name ..."
        response=$(curl --fail --silent --show-error \
            --request POST "${api_url%/}/api/v1/admin/tsdb/snapshot?skip_head=false") \
            || fail "Prometheus snapshot API failed ($name)"
        snapshot_name=$(printf '%s' "$response" \
            | sed -n 's/.*"name":"\([^"]*\)".*/\1/p')
        [[ -n "$snapshot_name" ]] \
            || fail "Prometheus snapshot API returned no snapshot name ($name)"

        source_dir="${host_snapshots_dir%/}/$snapshot_name"
        [[ -d "$source_dir" ]] \
            || fail "Prometheus snapshot directory not found: $source_dir"
        destination="$SNAPSHOTS_DIR/prometheus/$name/$snapshot_name"
        mkdir -p "$(dirname "$destination")"
        # Prometheus 官方镜像以 nobody (UID 65534) 写入 snapshot；即使父目录
        # 给了备份用户组写权限，部分宿主机仍拒绝普通用户跨目录 rename。
        # 只对已校验存在的单个 snapshot 目录使用 sudo，不改变硬链接 inode 所有者。
        sudo mv "$source_dir" "$destination"
        log "Prometheus snapshot complete: $destination"
    done < "$PROM_CONF"
fi

# --------------------------------------------------------------------------
# 阶段三：restic 备份
# --one-file-system 防止意外跨挂载点
# --------------------------------------------------------------------------
log "Starting backup: $DOCKER_PROJECTS -> $RESTIC_REPOSITORY"
run_with_timeout "$BACKUP_TIMEOUT" "restic backup" \
    restic backup "$DOCKER_PROJECTS" \
    --exclude-file "$EXCLUDES" \
    --one-file-system

# --------------------------------------------------------------------------
# 阶段四：保留策略（R2 按存储计费，prune 立即释放空间）
# 保留：最近 7 天每日 / 最近 4 周每周 / 最近 3 个月每月
# --------------------------------------------------------------------------
log "Applying retention policy..."
run_with_timeout "$FORGET_TIMEOUT" "restic forget --prune" \
    restic forget \
    --keep-daily 7 \
    --keep-weekly 4 \
    --keep-monthly 3 \
    --prune

log "Backup complete."
