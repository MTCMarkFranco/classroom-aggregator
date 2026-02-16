#!/bin/bash
# ============================================================
# Raspberry Pi Setup Script — Classroom Assignment Aggregator
# ============================================================
# Copy this entire project folder to the Pi (via USB, SCP, etc.)
# then run:   bash pi_setup.sh
#
# It will:
#   1. Install system dependencies (Python 3, Chromium libs)
#   2. Create a Python virtual environment
#   3. Install Python packages + Playwright Chromium
#   4. Set up a daily cron job (default: 7:00 AM)
#   5. Create a convenience wrapper to run manually
# ============================================================

set -e  # Exit on error

# ── Configuration ───────────────────────────────────────────
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$PROJECT_DIR/venv"
CRON_HOUR="${CRON_HOUR:-7}"          # Override: CRON_HOUR=6 bash pi_setup.sh
CRON_MINUTE="${CRON_MINUTE:-0}"
OUTPUT_DIR="$PROJECT_DIR/reports"
RUN_SCRIPT="$PROJECT_DIR/run_daily.sh"

echo "============================================"
echo "  Classroom Aggregator — Raspberry Pi Setup"
echo "============================================"
echo ""
echo "Project directory : $PROJECT_DIR"
echo "Cron schedule     : ${CRON_MINUTE} ${CRON_HOUR} * * *  (daily)"
echo ""

# ── 1. System packages ─────────────────────────────────────
echo "[1/5] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3 \
    python3-venv \
    python3-pip \
    libglib2.0-0 \
    libnss3 \
    libnspr4 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libatspi2.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libx11-xcb1 \
    curl \
    gnupg 2>/dev/null
echo "   ✓ System packages installed"

# Install latest Google Chrome
if ! command -v google-chrome-stable &>/dev/null; then
    echo "   Installing Google Chrome..."
    curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
        | sudo gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/google-chrome.gpg] \
https://dl.google.com/linux/chrome/deb/ stable main" \
        | sudo tee /etc/apt/sources.list.d/google-chrome.list >/dev/null
    sudo apt-get update -qq
    sudo apt-get install -y -qq google-chrome-stable
    echo "   ✓ Google Chrome installed ($(google-chrome-stable --version))"
else
    echo "   ✓ Google Chrome already installed ($(google-chrome-stable --version))"
fi

# ── 2. Python virtual environment ──────────────────────────
echo "[2/5] Setting up Python virtual environment..."
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
    echo "   ✓ Virtual environment created at $VENV_DIR"
else
    echo "   ✓ Virtual environment already exists"
fi

# Activate venv
source "$VENV_DIR/bin/activate"

# ── 3. Python dependencies ─────────────────────────────────
echo "[3/5] Installing Python dependencies..."
pip install --upgrade pip -q
pip install -r "$PROJECT_DIR/requirements.txt" -q
echo "   ✓ Python packages installed"

echo "   Installing Playwright Chromium browser..."
playwright install chromium 2>&1 | tail -1
echo "   ✓ Playwright Chromium installed"

# ── 4. Create the daily runner script ──────────────────────
echo "[4/5] Creating daily runner script..."
mkdir -p "$OUTPUT_DIR"

cat > "$RUN_SCRIPT" << 'RUNNER_EOF'
#!/bin/bash
# Daily runner for Classroom Assignment Aggregator
# Called by cron or manually: bash run_daily.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"
OUTPUT_DIR="$SCRIPT_DIR/reports"
LOG_FILE="$OUTPUT_DIR/latest_run.log"
REPORT_FILE="$OUTPUT_DIR/latest_report.txt"

# Activate virtual environment
source "$VENV_DIR/bin/activate"

# Create output directory if needed
mkdir -p "$OUTPUT_DIR"

# Timestamp
echo "=== Run started: $(date '+%Y-%m-%d %H:%M:%S') ===" > "$LOG_FILE"

# Run the aggregator (headless, non-interactive)
# The .env file provides credentials, semester classes, and HEADLESS=true
cd "$SCRIPT_DIR"
python main.py --headless 2>>"$LOG_FILE" | tee "$REPORT_FILE"
EXIT_CODE=${PIPESTATUS[0]}

echo "" >> "$LOG_FILE"
echo "=== Run finished: $(date '+%Y-%m-%d %H:%M:%S') — exit code: $EXIT_CODE ===" >> "$LOG_FILE"

# Also save a dated copy
DATE_STAMP=$(date '+%Y-%m-%d_%H%M')
cp "$REPORT_FILE" "$OUTPUT_DIR/report_${DATE_STAMP}.txt" 2>/dev/null

# Clean up reports older than 14 days
find "$OUTPUT_DIR" -name "report_*.txt" -mtime +14 -delete 2>/dev/null

exit $EXIT_CODE
RUNNER_EOF

chmod +x "$RUN_SCRIPT"
echo "   ✓ Created $RUN_SCRIPT"

# ── 5. Set up cron job ─────────────────────────────────────
echo "[5/5] Setting up daily cron job..."
CRON_CMD="${CRON_MINUTE} ${CRON_HOUR} * * * /bin/bash ${RUN_SCRIPT} >> ${OUTPUT_DIR}/cron.log 2>&1"

# Remove any existing cron entry for this project, then add the new one
( crontab -l 2>/dev/null | grep -v "classroom-aggregator" ; echo "$CRON_CMD" ) | crontab -
echo "   ✓ Cron job installed: $CRON_CMD"

# ── Done ────────────────────────────────────────────────────
echo ""
echo "============================================"
echo "  Setup complete!"
echo "============================================"
echo ""
echo "  Reports saved to : $OUTPUT_DIR/"
echo "  Run manually      : bash $RUN_SCRIPT"
echo "  View cron jobs    : crontab -l"
echo "  View last report  : cat $OUTPUT_DIR/latest_report.txt"
echo "  View last log     : cat $OUTPUT_DIR/latest_run.log"
echo ""
echo "  Make sure your .env file has the correct credentials:"
echo "    $PROJECT_DIR/.env"
echo ""
echo "  To change the schedule, edit cron:"
echo "    crontab -e"
echo ""
