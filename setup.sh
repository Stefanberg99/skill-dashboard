#!/bin/bash
# setup.sh — install the Skill Sync Daemon on macOS
# Run once: bash ~/Claude/skill-dashboard/setup.sh

set -e

DASHBOARD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON_PATH="$DASHBOARD_DIR/sync-daemon.py"
CONFIG_DIR="$HOME/.config/claude"
CONFIG_PATH="$CONFIG_DIR/skill-sync.json"
PLIST_LABEL="com.stefan.skill-sync"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"

echo ""
echo "  Skill Sync Daemon — setup"
echo "  ─────────────────────────"
echo ""

# ── GitHub token ──────────────────────────────────────────────────────────────
GITHUB_TOKEN=""

# Try gh CLI first
if command -v gh &>/dev/null; then
  GITHUB_TOKEN=$(gh auth token --hostname github.com 2>/dev/null || true)
fi

if [ -z "$GITHUB_TOKEN" ]; then
  echo "  gh CLI token not found. Enter a GitHub Personal Access Token"
  echo "  (needs: repo scope — create at github.com/settings/tokens)"
  echo ""
  read -rsp "  Token: " GITHUB_TOKEN
  echo ""
fi

if [ -z "$GITHUB_TOKEN" ]; then
  echo "  Error: no token provided. Exiting."
  exit 1
fi

# ── GitHub repo ───────────────────────────────────────────────────────────────
GITHUB_OWNER=""
GITHUB_REPO=""

if command -v gh &>/dev/null; then
  GITHUB_OWNER=$(gh api user -q .login 2>/dev/null || true)
fi

if [ -z "$GITHUB_OWNER" ]; then
  read -rp "  GitHub username: " GITHUB_OWNER
fi

# Check if we're inside a git repo with a remote
if git -C "$DASHBOARD_DIR" remote get-url origin &>/dev/null 2>&1; then
  REMOTE_URL=$(git -C "$DASHBOARD_DIR" remote get-url origin)
  # Extract repo name from URL (handles both SSH and HTTPS)
  GITHUB_REPO=$(echo "$REMOTE_URL" | sed -E 's|.*[:/]([^/]+)\.git$|\1|;s|.*[:/]([^/]+)$|\1|')
fi

if [ -z "$GITHUB_REPO" ]; then
  read -rp "  GitHub repo name [skill-dashboard]: " GITHUB_REPO
  GITHUB_REPO="${GITHUB_REPO:-skill-dashboard}"
fi

echo ""
echo "  Config:"
echo "    Owner : $GITHUB_OWNER"
echo "    Repo  : $GITHUB_REPO"
echo "    Daemon: $DAEMON_PATH"
echo ""

# ── Write config ──────────────────────────────────────────────────────────────
mkdir -p "$CONFIG_DIR"
cat > "$CONFIG_PATH" <<CONFIG
{
  "github_token": "$GITHUB_TOKEN",
  "github_owner": "$GITHUB_OWNER",
  "github_repo":  "$GITHUB_REPO"
}
CONFIG
chmod 600 "$CONFIG_PATH"
echo "  Config written → $CONFIG_PATH"

# ── Install launchd plist ─────────────────────────────────────────────────────
# Unload existing if present
if launchctl list "$PLIST_LABEL" &>/dev/null 2>&1; then
  launchctl unload "$PLIST_PATH" 2>/dev/null || true
fi

cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>              <string>$PLIST_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>$DAEMON_PATH</string>
  </array>
  <key>RunAtLoad</key>          <true/>
  <key>KeepAlive</key>          <true/>
  <key>StandardOutPath</key>    <string>/tmp/skill-sync.log</string>
  <key>StandardErrorPath</key>  <string>/tmp/skill-sync-error.log</string>
  <key>ThrottleInterval</key>   <integer>10</integer>
</dict>
</plist>
PLIST

launchctl load "$PLIST_PATH"
echo "  Daemon installed → $PLIST_PATH"
echo "  Daemon started   (will run at login automatically)"
echo ""
echo "  Logs:  tail -f /tmp/skill-sync.log"
echo "  Stop:  launchctl unload $PLIST_PATH"
echo "  Start: launchctl load $PLIST_PATH"
echo ""
echo "  Initial sync running — skills.json will appear in GitHub within 60 s."
echo ""
