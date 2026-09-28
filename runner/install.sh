#!/usr/bin/env bash
# VISION runner installer for a Linux VPS (Ubuntu/Debian) with a STATIC public IP.
# Usage:  curl -fsSL https://raw.githubusercontent.com/16VITAWS/market-scanner/main/runner/install.sh | bash
set -euo pipefail
sudo apt-get update -y && sudo apt-get install -y python3 python3-venv curl
mkdir -p ~/vision && cd ~/vision
curl -fsSL -o vision_runner.py https://raw.githubusercontent.com/16VITAWS/market-scanner/main/runner/vision_runner.py
python3 -m venv venv && ./venv/bin/pip install -q growwapi pyotp requests
if [ ! -f ~/vision/runner.env ]; then
cat > ~/vision/runner.env <<'ENV'
# Fill these in, then: sudo systemctl restart vision-runner
GROWW_TOTP_TOKEN=
GROWW_TOTP_SECRET=
RUNNER_DRY_RUN=1
NTFY_TOPIC=vision-ai-16vitaws-k7q2m9x4
VISION_SITE=https://16vitaws.github.io/market-scanner
RUNNER_GITHUB_TOKEN=
ENV
chmod 600 ~/vision/runner.env
fi
sudo tee /etc/systemd/system/vision-runner.service >/dev/null <<UNIT
[Unit]
Description=VISION AI live order runner
After=network-online.target
[Service]
User=$USER
WorkingDirectory=$HOME/vision
EnvironmentFile=$HOME/vision/runner.env
Environment=TZ=Asia/Kolkata
ExecStart=$HOME/vision/venv/bin/python $HOME/vision/vision_runner.py
Restart=always
RestartSec=30
[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload && sudo systemctl enable vision-runner
echo "Installed. Your public IP (whitelist this in Groww):"; curl -s https://api.ipify.org; echo
echo "Next: nano ~/vision/runner.env  (fill keys; keep RUNNER_DRY_RUN=1 for the first week), then: sudo systemctl restart vision-runner"
echo "Logs: journalctl -u vision-runner -f      Audit: ~/vision_runner_audit.jsonl      Stop now: sudo systemctl stop vision-runner"
