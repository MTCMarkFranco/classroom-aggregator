#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────
# Classroom Aggregator – Raspberry Pi Setup Script
#
# Run on a fresh Raspberry Pi OS Bookworm installation.
# Sets up Python venv, Chromium + chromedriver for Selenium, and a daily
# cron job to run the aggregator automatically.
# ─────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ─── Configuration ──────────────────────────────────────────────────────
APP_DIR="$HOME/classroom-aggregator"
VENV_DIR="$APP_DIR/venv"
CRON_SCHEDULE="0 7 * * 1-5"   # 7 AM, Mon-Fri
LOG_DIR="$APP_DIR/logs"
RUNNER_SCRIPT="$APP_DIR/run_daily.sh"

echo "╔════════════════════════════════════════════════════╗"
echo "║  Classroom Aggregator – Raspberry Pi Setup         ║"
echo "╚════════════════════════════════════════════════════╝"

# ─── System packages ───────────────────────────────────────────────────
echo ""
echo "▸ Installing system packages..."
sudo apt-get update -y
sudo apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    chromium-browser \
    chromium-chromedriver \
    fonts-liberation \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcups2 \
    libdrm2 \
    libgbm1 \
    libnss3 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    xdg-utils

echo "  ✓ System packages installed"

# Verify Chromium is available
if command -v chromium-browser &>/dev/null; then
    echo "  ✓ chromium-browser: $(chromium-browser --version 2>/dev/null || echo 'installed')"
elif command -v chromium &>/dev/null; then
    echo "  ✓ chromium: $(chromium --version 2>/dev/null || echo 'installed')"
else
    echo "  ✗ Chromium not found — Selenium will not work"
    exit 1
fi

if command -v chromedriver &>/dev/null; then
    echo "  ✓ chromedriver: $(chromedriver --version 2>/dev/null || echo 'installed')"
else
    echo "  ⚠ chromedriver not in PATH — will try /usr/bin/chromedriver"
fi

# ─── App directory ──────────────────────────────────────────────────────
echo ""
echo "▸ Setting up app directory..."
mkdir -p "$APP_DIR" "$LOG_DIR"

# Copy project files if running from a different location
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ "$SCRIPT_DIR" != "$APP_DIR" ]; then
    echo "  Copying project files from $SCRIPT_DIR to $APP_DIR..."
    for f in requirements.txt models.py auth.py google_classroom_scraper.py brightspace_scraper.py main.py .env .gitignore README.md; do
        if [ -f "$SCRIPT_DIR/$f" ]; then
            cp "$SCRIPT_DIR/$f" "$APP_DIR/"
        fi
    done
fi
echo "  ✓ App directory ready: $APP_DIR"

# ─── Python venv & dependencies ────────────────────────────────────────
echo ""
echo "▸ Creating Python virtual environment..."
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"

echo "▸ Installing Python dependencies..."
pip install --upgrade pip
pip install -r "$APP_DIR/requirements.txt"
echo "  ✓ Python dependencies installed"

# ─── Verify Selenium can find Chromium ─────────────────────────────────
echo ""
echo "▸ Verifying Selenium + Chromium..."
python3 -c "
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
import os

opts = Options()
opts.add_argument('--headless=new')
opts.add_argument('--no-sandbox')
opts.add_argument('--disable-dev-shm-usage')

# Try default first, then system Chromium
try:
    d = webdriver.Chrome(options=opts)
    print('  ✓ Selenium launched Chrome via default path')
    d.quit()
except Exception:
    for binary in ['/usr/bin/chromium-browser', '/usr/bin/chromium']:
        if os.path.exists(binary):
            opts.binary_location = binary
            svc = Service('/usr/bin/chromedriver')
            d = webdriver.Chrome(service=svc, options=opts)
            print(f'  ✓ Selenium launched {binary}')
            d.quit()
            break
    else:
        print('  ✗ Selenium could not launch Chromium')
        exit(1)
" && echo "  ✓ Selenium verification passed" || {
    echo "  ✗ Selenium verification failed"
    echo "  Try: sudo apt-get install -y chromium-browser chromium-chromedriver"
    exit 1
}

# ─── Create daily runner script ────────────────────────────────────────
echo ""
echo "▸ Creating daily runner script..."
cat > "$RUNNER_SCRIPT" << 'RUNNER_EOF'
#!/usr/bin/env bash
# Daily runner for Classroom Aggregator (called by cron)
set -euo pipefail

APP_DIR="$HOME/classroom-aggregator"
VENV_DIR="$APP_DIR/venv"
LOG_DIR="$APP_DIR/logs"
LOG_FILE="$LOG_DIR/run_$(date +%Y%m%d_%H%M%S).log"

mkdir -p "$LOG_DIR"

# Activate venv
source "$VENV_DIR/bin/activate"

# Run the aggregator in headless mode
cd "$APP_DIR"
python3 main.py --headless 2>&1 | tee "$LOG_FILE"

# Keep only last 14 days of logs
find "$LOG_DIR" -name "run_*.log" -mtime +14 -delete 2>/dev/null || true

deactivate
RUNNER_EOF

chmod +x "$RUNNER_SCRIPT"
echo "  ✓ Runner script: $RUNNER_SCRIPT"

# ─── Cron job ──────────────────────────────────────────────────────────
echo ""
echo "▸ Setting up cron job..."
CRON_CMD="$CRON_SCHEDULE $RUNNER_SCRIPT"

# Remove any existing aggregator cron entries
(crontab -l 2>/dev/null | grep -v "classroom-aggregator" || true) | \
    { cat; echo "$CRON_CMD"; } | crontab -

echo "  ✓ Cron job installed: $CRON_SCHEDULE"
echo "    (runs Mon-Fri at 7:00 AM)"

# ─── Final summary ─────────────────────────────────────────────────────
echo ""
echo "╔════════════════════════════════════════════════════╗"
echo "║  Setup Complete!                                    ║"
echo "╠════════════════════════════════════════════════════╣"
echo "║  App dir:    $APP_DIR"
echo "║  Venv:       $VENV_DIR"
echo "║  Runner:     $RUNNER_SCRIPT"
echo "║  Logs:       $LOG_DIR"
echo "║  Cron:       $CRON_SCHEDULE"
echo "╠════════════════════════════════════════════════════╣"
echo "║  Test manually:                                     ║"
echo "║    cd $APP_DIR"
echo "║    source venv/bin/activate"
echo "║    python3 main.py --headless"
echo "╚════════════════════════════════════════════════════╝"
