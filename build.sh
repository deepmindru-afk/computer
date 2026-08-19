#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "Building frontend..."
(cd cptr/frontend && bun install && bun run build)

echo "Building wheel..."
uv build --wheel

echo "Done. Wheel in dist/"
ls dist/*.whl
