#!/usr/bin/env bash
#
# build.sh —— 一键构建 icu-quality-dashboard 并导出产物
# 用法：  ./build.sh
#

set -euo pipefail   # 任一步出错立即终止，避免「错了还往下跑」

# ---------- 可配置变量 ----------
IMAGE_NAME="icu-dashboard-builder"
ARTIFACT_NAME="icu-quality-dashboard-oel8.2-x86_64.tar.gz"
ARTIFACT_PATH_IN_IMAGE="/artifact/${ARTIFACT_NAME}"
OUTPUT_DIR="./dist"
TMP_CONTAINER="icu-export-tmp"
# --------------------------------

# 让脚本无论从哪里调用，都以脚本所在目录为工作目录（Dockerfile 在这里）
cd "$(dirname "$0")"

echo "==> [1/4] 构建镜像: ${IMAGE_NAME}"
docker build -t "${IMAGE_NAME}" .

echo "==> [2/4] 创建临时容器以提取产物"
# 若上次残留同名容器，先清掉，保证幂等
docker rm -f "${TMP_CONTAINER}" >/dev/null 2>&1 || true
docker create --name "${TMP_CONTAINER}" "${IMAGE_NAME}" >/dev/null

echo "==> [3/4] 从镜像拷出产物到 ${OUTPUT_DIR}/"
mkdir -p "${OUTPUT_DIR}"
docker cp "${TMP_CONTAINER}:${ARTIFACT_PATH_IN_IMAGE}" "${OUTPUT_DIR}/"

echo "==> [4/4] 清理临时容器"
docker rm -f "${TMP_CONTAINER}" >/dev/null

echo ""
echo "✅ 构建完成！产物位置："
ls -lh "${OUTPUT_DIR}/${ARTIFACT_NAME}"
echo ""
echo "包内容预览："
tar -tzf "${OUTPUT_DIR}/${ARTIFACT_NAME}"
