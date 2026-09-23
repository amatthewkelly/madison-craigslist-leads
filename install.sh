#!/bin/bash
# Installs the twice-daily launchd job. Re-run any time to change the times.
set -euo pipefail

LABEL="com.craigslistcash.jobs"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$HOME/.craigslistcash"

MORNING="${1:-8}"     # hour, 24h clock
EVENING="${2:-16}"
MIN="${3:-30}"

PYTHON="$(command -v python3 || true)"
[ -z "$PYTHON" ] && { echo "error: python3 not found" >&2; exit 1; }

# launchd gets a bare PATH, so pin the dirs holding python3 and claude.
CLAUDE="$(command -v claude || true)"
EXTRA_PATH="$(dirname "$PYTHON")"
[ -n "$CLAUDE" ] && EXTRA_PATH="$EXTRA_PATH:$(dirname "$CLAUDE")"

mkdir -p "$HOME/Library/LaunchAgents" "$LOGDIR"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$HERE/cljobs.py</string>
    </array>

    <key>WorkingDirectory</key><string>$HERE</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$EXTRA_PATH:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key><string>$HOME</string>
    </dict>

    <key>StartCalendarInterval</key>
    <array>
        <dict><key>Hour</key><integer>$MORNING</integer><key>Minute</key><integer>$MIN</integer></dict>
        <dict><key>Hour</key><integer>$EVENING</integer><key>Minute</key><integer>$MIN</integer></dict>
    </array>

    <key>RunAtLoad</key><false/>
    <key>StandardOutPath</key><string>$LOGDIR/run.log</string>
    <key>StandardErrorPath</key><string>$LOGDIR/run.log</string>
    <key>ProcessType</key><string>Background</string>
</dict>
</plist>
PLISTEOF

chmod +x "$HERE/cljobs.py"

# terminal-notifier lets a click on the notification open the leads file
# (plain osascript notifications can't set a click action). Homebrew needs full
# Xcode to build it, so fetch the prebuilt release and ad-hoc sign it.
TN_APP="$HOME/Applications/terminal-notifier.app"
TN_URL="https://github.com/julienXX/terminal-notifier/releases/download/2.0.0/terminal-notifier-2.0.0.zip"
if [ ! -x "$TN_APP/Contents/MacOS/terminal-notifier" ]; then
    TN_TMP="$(mktemp -d)"
    if curl -fsSL -o "$TN_TMP/tn.zip" "$TN_URL" && unzip -q "$TN_TMP/tn.zip" -d "$TN_TMP"; then
        mkdir -p "$HOME/Applications"
        rm -rf "$TN_APP"
        cp -R "$TN_TMP/terminal-notifier.app" "$TN_APP"
        codesign --force --deep -s - "$TN_APP"
        echo "installed terminal-notifier -> $TN_APP"
    else
        echo "warning: could not fetch terminal-notifier; notifications will not open the leads file" >&2
    fi
    rm -rf "$TN_TMP"
fi

# bootout is expected to fail when nothing is loaded yet
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"

printf '\nInstalled %s\n' "$LABEL"
printf '  runs daily at %02d:%02d and %02d:%02d\n' "$MORNING" "$MIN" "$EVENING" "$MIN"
printf '  log:    %s/run.log\n' "$LOGDIR"
printf '  output: %s\n\n' "$(python3 -c "import json,os;print(os.path.expanduser(json.load(open('$HERE/config.json'))['output_file']))")"
echo "Run it once right now with:  launchctl kickstart -p gui/$UID/$LABEL"
