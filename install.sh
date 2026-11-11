#!/bin/bash
# Install netmon. Detects the OS:
#   macOS                     -> launchd agent running as you:   ./install.sh
#   Raspberry Pi OS / Linux   -> systemd service as `netmon`:    sudo ./install.sh
set -euo pipefail
cd "$(dirname "$0")"

src_config() {  # your local netmon.ini if present, else the template
  if [[ -f netmon.ini ]]; then echo netmon.ini; else echo netmon.example.ini; fi
}

save_key() {  # $1 = destination file, $2 = line prefix
  [[ -t 0 ]] || { echo "Needs an interactive terminal to read the API key." >&2; exit 1; }
  read -rsp "Meraki API key: " key; echo
  [[ -n "$key" ]] || { echo "No key entered." >&2; exit 1; }
  ( umask 077; printf '%s%s\n' "$2" "$key" > "$1" )
}

install_macos() {
  [[ $EUID -ne 0 ]] || { echo "On macOS run without sudo; it installs for your user."; exit 1; }
  local app="$HOME/Library/Application Support/netmon"
  local conf="$HOME/.config/netmon/netmon.ini"
  local label="local.netmon"
  local plist="$HOME/Library/LaunchAgents/$label.plist"
  local py=/usr/bin/python3
  "$py" -c '' 2>/dev/null || py=$(command -v python3)

  mkdir -p "$app" "$(dirname "$conf")" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
  install -m 755 netmon.py "$app/netmon.py"
  [[ -f "$conf" ]] || install -m 644 "$(src_config)" "$conf"
  [[ -f "$HOME/.meraki_key" ]] || save_key "$HOME/.meraki_key" ""

  cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>$py</string><string>$app/netmon.py</string>
    <string>--config</string><string>$conf</string><string>run</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/netmon.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/netmon.log</string>
</dict>
</plist>
EOF
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$plist"

  mkdir -p "$HOME/.local/bin"
  printf '#!/bin/bash\nexec "%s" "%s" --config "%s" "$@"\n' "$py" "$app/netmon.py" "$conf" > "$HOME/.local/bin/netmon"
  chmod 755 "$HOME/.local/bin/netmon"

  echo
  echo "Installed (launchd agent $label). Speed tests use the built-in networkQuality."
  echo "Edit $conf (ntfy_url, email), then restart:"
  echo "  launchctl kickstart -k gui/$(id -u)/$label"
  echo "Logs:    tail -f ~/Library/Logs/netmon.log"
  echo "Command: ~/.local/bin/netmon report   (add ~/.local/bin to PATH to just type 'netmon')"
}

install_linux() {
  [[ $EUID -eq 0 ]] || { echo "On Linux run with sudo."; exit 1; }
  command -v systemctl >/dev/null || { echo "systemd is required."; exit 1; }

  id netmon &>/dev/null || useradd --system --home-dir /var/lib/netmon --shell /usr/sbin/nologin netmon
  install -d -o netmon -g netmon /var/lib/netmon
  install -D -m 755 netmon.py /opt/netmon/netmon.py
  install -d /etc/netmon
  [[ -f /etc/netmon/netmon.ini ]] || install -m 644 "$(src_config)" /etc/netmon/netmon.ini
  [[ -f /etc/netmon/env ]] || save_key /etc/netmon/env "MERAKI_DASHBOARD_API_KEY="
  chown root:netmon /etc/netmon/env && chmod 640 /etc/netmon/env

  if ! command -v speedtest >/dev/null && ! command -v speedtest-cli >/dev/null; then
    echo "Installing speedtest-cli for speed tests"
    apt-get install -y speedtest-cli || echo "Could not install speedtest-cli; speed tests will be skipped"
  fi

  install -m 644 netmon.service /etc/systemd/system/netmon.service
  systemctl daemon-reload
  systemctl enable netmon
  systemctl restart netmon

  cat > /usr/local/bin/netmon <<'EOF'
#!/bin/bash
exec sudo -u netmon bash -c 'set -a; . /etc/netmon/env; set +a; exec /usr/bin/python3 /opt/netmon/netmon.py --config /etc/netmon/netmon.ini "$@"' _ "$@"
EOF
  chmod 755 /usr/local/bin/netmon

  echo
  echo "Installed (systemd service netmon)."
  echo "Edit /etc/netmon/netmon.ini (ntfy_url, email), then: sudo systemctl restart netmon"
  echo "Logs:    journalctl -u netmon -f"
  echo "Command: netmon report"
}

case "$(uname -s)" in
  Darwin) install_macos ;;
  Linux)  install_linux ;;
  *)      echo "Unsupported OS: $(uname -s). netmon supports macOS and Linux (Raspberry Pi OS)."; exit 1 ;;
esac
