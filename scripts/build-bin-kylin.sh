#!/usr/bin/env bash
#
# 一键产出麒麟 V11 可直接运行的单文件二进制
# 用法：
#   ./scripts/build-bin-kylin.sh
#   BASE_IMAGE=kylin-server:v11 ./scripts/build-bin-kylin.sh
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

BASE_IMAGE="${BASE_IMAGE:-kylin-server:v11}"
FALLBACK_IMAGE="${FALLBACK_IMAGE:-openeuler/openeuler:24.03-lts}"
IMAGE_NAME="${IMAGE_NAME:-icu-qc-kylin-bin}"
DOCKERFILE="${DOCKERFILE:-deploy/kylin-v11/Dockerfile.bin}"
OUT_DIR="${OUT_DIR:-dist-kylin}"
PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

ENGINE="${ENGINE:-$(command -v docker || command -v podman || true)}"
[ -n "$ENGINE" ] || die "未找到 docker / podman"
log "容器引擎: $ENGINE  构建机架构: $(uname -m)"

# 1) 前端：没构建过就现构建（PyInstaller 会把 frontend_dist 打进二进制）
if [ ! -f icu-quality-backend/frontend_dist/index.html ] || [ "${SYNC_FRONTEND:-0}" = "1" ]; then
  command -v npm >/dev/null 2>&1 || die "需要先构建前端，但本机没有 npm"
  log "构建前端并同步到 frontend_dist"
  ( cd icu-quality-dashboard && { [ -f package-lock.json ] && npm ci || npm install; } && npm run build )
  rm -rf icu-quality-backend/frontend_dist
  mkdir -p icu-quality-backend/frontend_dist
  cp -a icu-quality-dashboard/dist/. icu-quality-backend/frontend_dist/
fi

# 2) 基础镜像
#    首选自制 kylin-server:v11；本机没有时自动回退到同源的 openEuler 24.03
#    （glibc 2.38 / Python 3.11，与麒麟 V11 同源，产物兼容性一致）。
if [ "$BASE_IMAGE" = "kylin-server:v11" ] \
   && ! "$ENGINE" image inspect "$BASE_IMAGE" >/dev/null 2>&1; then
  log "本地无自制镜像 $BASE_IMAGE，自动回退 $FALLBACK_IMAGE"
  log "（openEuler 系，与麒麟 V11 同源；若要纯麒麟环境，先在已激活的麒麟 V11 主机执行 deploy/kylin-v11/make-base-image.sh）"
  BASE_IMAGE="$FALLBACK_IMAGE"
fi
"$ENGINE" image inspect "$BASE_IMAGE" >/dev/null 2>&1 || {
  log "本地无 $BASE_IMAGE，尝试拉取"
  "$ENGINE" pull "$BASE_IMAGE" || die "拉取基础镜像失败：$BASE_IMAGE
可指定 BASE_IMAGE=... 覆盖，或在已激活的麒麟 V11 主机上执行
deploy/kylin-v11/make-base-image.sh 自制 kylin-server:v11 后重跑。"
}
log "使用基础镜像: $BASE_IMAGE"

# 3) 编译
log "编译二进制（首次约 3-6 分钟）"
"$ENGINE" build \
  --build-arg BASE_IMAGE="$BASE_IMAGE" \
  --build-arg PIP_INDEX_URL="$PIP_INDEX_URL" \
  -t "$IMAGE_NAME" -f "$DOCKERFILE" .

# 4) 取出唯一产物
mkdir -p "$OUT_DIR"
CID="$("$ENGINE" create "$IMAGE_NAME")"
trap '"$ENGINE" rm -f "$CID" >/dev/null 2>&1 || true' EXIT
"$ENGINE" cp "$CID:/out/icu-quality-dashboard" "$OUT_DIR/"
chmod +x "$OUT_DIR/icu-quality-dashboard"
( cd "$OUT_DIR" && sha256sum icu-quality-dashboard > icu-quality-dashboard.sha256 )

BIN_ARCH="$(uname -m)"
echo
log "完成，产物就这一个文件："
ls -lh "$OUT_DIR/icu-quality-dashboard"
cat "$OUT_DIR/icu-quality-dashboard.sha256"
cat <<EOF

拿去麒麟 V11（${BIN_ARCH}）服务器上跑，三条命令：
  scp $OUT_DIR/icu-quality-dashboard root@SERVER:/opt/icu/
  ssh root@SERVER 'chmod +x /opt/icu/icu-quality-dashboard'
  ssh root@SERVER 'cd /opt/icu && ./icu-quality-dashboard'

浏览器打开 http://SERVER_IP:8091
数据库/LLM 配置：在二进制同目录放 .env（模板见 deploy/env.template），
或直接用环境变量启动，例如：
  SMARTCARE_DB_HOST=10.0.0.5 PORT=8091 ./icu-quality-dashboard
EOF
