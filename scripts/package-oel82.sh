#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME="${IMAGE_NAME:-icu-quality-dashboard-oel82-builder}"
OUT_DIR="${OUT_DIR:-release}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTAINER_NAME="icu-quality-dashboard-artifact-$(date +%s)-$$"

cd "$ROOT_DIR"

mkdir -p "$OUT_DIR"

echo "Building OEL 8.2 binary package image..."
docker build --target artifact -t "$IMAGE_NAME" -f Dockerfile .

echo "Copying artifacts..."
docker create --name "$CONTAINER_NAME" "$IMAGE_NAME" >/dev/null
cleanup() {
  docker rm "$CONTAINER_NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker cp "$CONTAINER_NAME:/artifact/icu-quality-dashboard-oel8.2-x86_64.tar.gz" "$OUT_DIR/"
rm -rf "$OUT_DIR/icu-quality-dashboard"
docker cp "$CONTAINER_NAME:/artifact/icu-quality-dashboard" "$OUT_DIR/"

echo
echo "Done."
echo "Archive: $OUT_DIR/icu-quality-dashboard-oel8.2-x86_64.tar.gz"
echo "Expanded package: $OUT_DIR/icu-quality-dashboard"
