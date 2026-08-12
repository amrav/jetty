#!/usr/bin/env bash
# Build dist/jetty-orc.pyz — a single-file, dependency-free distribution of
# the orchestrator. The package is stdlib-only, so the zipapp runs on any
# Linux box with Python 3.11+: `scp` it somewhere and `./jetty-orc.pyz doctor`.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$STAGE/jetty_orc"
cp "$ROOT"/src/jetty/orchestrator/*.py "$STAGE/jetty_orc/"
cat > "$STAGE/__main__.py" <<'EOF'
from jetty_orc.cli import main

main()
EOF

mkdir -p "$ROOT/dist"
python3 -m zipapp "$STAGE" \
  --output "$ROOT/dist/jetty-orc.pyz" \
  --python "/usr/bin/env python3" \
  --compress
echo "built $ROOT/dist/jetty-orc.pyz ($(du -h "$ROOT/dist/jetty-orc.pyz" | cut -f1))"
