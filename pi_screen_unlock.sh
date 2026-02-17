#!/bin/bash
# ============================================================
# Disable screen lock, screen blanking, and auto-suspend
# Run once on Ubuntu Desktop LTS (Raspberry Pi 4)
# ============================================================

echo "Disabling screen lock..."
gsettings set org.gnome.desktop.screensaver lock-enabled false

echo "Disabling screen timeout (idle delay = never)..."
gsettings set org.gnome.desktop.session idle-delay 0

echo "Disabling display power-off and dimming..."
gsettings set org.gnome.settings-daemon.plugins.power idle-dim false
gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-timeout 0
gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-battery-timeout 0

echo "Disabling automatic suspend..."
gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing'

echo ""
echo "✓ Screen lock disabled"
echo "✓ Screen timeout disabled"
echo "✓ Auto-suspend disabled"
echo ""
echo "Desktop will stay unlocked and on indefinitely."
