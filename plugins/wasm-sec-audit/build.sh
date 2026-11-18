#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

cargo build --target wasm32-wasip1 --release --target-dir "$SCRIPT_DIR/target"
cp "$SCRIPT_DIR/target/wasm32-wasip1/release/wasm-sec-audit.wasm" "$SCRIPT_DIR/wasm-sec-audit.wasm"
echo "Built $SCRIPT_DIR/wasm-sec-audit.wasm ($(wc -c < "$SCRIPT_DIR/wasm-sec-audit.wasm" | tr -d ' ') bytes)"
