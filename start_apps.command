#!/bin/bash
# ═══════════════════════════════════════════════════════════════
#  Carnival Test Automation Platform — Startup Script
#  Starts Django backend + Streamlit UI
# ═══════════════════════════════════════════════════════════════

PROJECT_ROOT="/Users/arvind.kumar1/Downloads/carnival/playwright-carnival-framework-final-updated"
STREAMLIT_PATH="$PROJECT_ROOT/streamlet-ui"

echo ""
echo "🚀  XT-Forge Agent - Streamlit UI"
echo "══════════════════════════════════════"

# ── Kill existing processes on ports ──────────────────────────
echo "→ Cleaning port 8501…"
lsof -ti:8501 | xargs kill -9 2>/dev/null
sleep 1

# ── Detect Python venv ────────────────────────────────────────
VENV_CANDIDATES=(
  "$PROJECT_ROOT/venv/bin/activate"
  "$PROJECT_ROOT/.venv/bin/activate"
  "$PROJECT_ROOT/ai-healer-django/venv/bin/activate"
  "$PROJECT_ROOT/ai-healer-django/.venv/bin/activate"
)
VENV_ACTIVATED=""
for venv in "${VENV_CANDIDATES[@]}"; do
  if [ -f "$venv" ]; then
    echo "→ Activating venv: $venv"
    source "$venv"
    VENV_ACTIVATED="$venv"
    break
  fi
done

# Set the python command correctly
PYTHON_CMD="python3"
if [ -n "$VENV_ACTIVATED" ]; then
  PYTHON_CMD="python"
fi

# ── Install Streamlit deps (if needed) ────────────────────────
echo "→ Checking Streamlit dependencies…"
$PYTHON_CMD -m pip install -q -r "$STREAMLIT_PATH/requirements.txt"

# ── Start Streamlit ───────────────────────────────────────────
echo ""
echo "→ Starting XT-Forge Agent UI on :8501"
echo "  (Django must be started manually if needed)"
echo ""

# Open Chrome in the background after a short delay
(sleep 2; open -a "Google Chrome" "http://localhost:8501" 2>/dev/null || open "http://localhost:8501") &

cd "$PROJECT_ROOT"
$PYTHON_CMD -m streamlit run "$STREAMLIT_PATH/app.py" --server.port 8501