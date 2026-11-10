#!/usr/bin/env bash
#
# 在一台【已激活的麒麟 V11 主机】上执行，自制 docker 基础镜像 kylin-server:v11。
# 做完后，其它机器即可运行：
#   ./scripts/build-bin-kylin.sh
#
# 用法：
#   ./make-base-image.sh
#   IMAGE_TAG=kylin-server:v11-custom ./make-base-image.sh
#
set -euo pipefail

IMAGE_TAG="${IMAGE_TAG:-kylin-server:v11}"
ENGINE="${ENGINE:-$(command -v docker || command -v podman || true)}"
[ -n "$ENGINE" ] || { echo "[fail] 未找到 docker / podman" >&2; exit 1; }

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

# 0) 确认当前主机是麒麟（防止在其它系统上误打 rootfs）
[ -r /etc/os-release ] || die "读不到 /etc/os-release"
# shellcheck disable=SC1091
. /etc/os-release
PRETTY="${PRETTY_NAME:-unknown}"
echo "$PRETTY" | grep -qiE 'kylin|麒麟' || die "当前主机不是麒麟（识别为：$PRETTY）。
本脚本必须在已激活的麒麟 V11 主机上执行；若只想快速得到可用产物，
可在构建机上让 build-bin-kylin.sh 自动回退到 openeuler/openeuler:24.03-lts。"

log "麒麟版本: $PRETTY"
log "容器引擎: $ENGINE"

# 1) 打根文件系统（排除虚拟/临时目录，避免镜像过大与内容污染）
TAR_PATH="${TAR_PATH:-/tmp/kylin-v11-rootfs.tar.gz}"
log "打包根文件系统到 $TAR_PATH（需要几分钟）"
tar --numeric-owner \
    --one-file-system \
    --exclude=./proc/'*' \
    --exclude=./sys/'*' \
    --exclude=./dev/'*' \
    --exclude=./run/'*' \
    --exclude=./tmp/'*' \
    --exclude=./boot/'*' \
    --exclude=./mnt/'*' \
    --exclude=./media/'*' \
    --exclude=./var/cache/dnf \
    --exclude=./var/cache/yum \
    --exclude="$TAR_PATH" \
    -C / -czf "$TAR_PATH" .

# 2) 导入为镜像
log "导入镜像 $IMAGE_TAG"
"$ENGINE" import "$TAR_PATH" "$IMAGE_TAG"

# 3) 体检：dnf / python3 是后续 PyInstaller 构建的硬依赖
log "体检基础镜像"
"$ENGINE" run --rm "$IMAGE_TAG" bash -lc '
  set -e
  python3 -V
  dnf --version | head -1
  command -v gcc >/dev/null 2>&1 || echo "note: gcc 尚未安装（构建阶段会 dnf 安装）"
' || die "基础镜像体检失败"

rm -f "$TAR_PATH"
log "完成：$IMAGE_TAG"
log "现在可在任意构建机上执行 ./scripts/build-bin-kylin.sh"
