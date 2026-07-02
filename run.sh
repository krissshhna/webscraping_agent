#!/usr/bin/env bash
# run.sh — Start the WebScrape Agent backend server
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"

echo ""
echo "  ╔══════════════════════════════════════╗"
echo "  ║   WebScrape Agent — Starting Up      ║"
echo "  ╚══════════════════════════════════════╝"
echo ""

# Check Python
if ! command -v python3 &>/dev/null; then
  echo "  ✗ Python 3 not found. Please install Python 3.8+"
  exit 1
fi

echo "  ✓ Python: $(python3 --version)"
echo ""

# Install dependencies if needed
if ! python3 -c "import fastapi, uvicorn, httpx, bs4, openpyxl" 2>/dev/null; then
  echo "  Installing dependencies..."
  pip3 install -r "$SCRIPT_DIR/requirements.txt" -q
  echo "  ✓ Dependencies installed"
fi

echo "  ▶ Starting server on http://localhost:8000"
echo "  ▶ Open your browser at: http://localhost:8000"
echo "  ▶ Press Ctrl+C to stop"
echo ""

cd "$BACKEND_DIR"
python3 -m uvicorn main:app --host localhost --port 8000 --reload
