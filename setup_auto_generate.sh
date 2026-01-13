#!/bin/bash
# Setup Auto-Generation on Mac Startup
# ============================================================================
# This script creates a LaunchAgent that runs daily_generate.py when you login
#
# Usage: ./setup_auto_generate.sh
# ============================================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_NAME="com.coachoutreach.dailygenerate"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"

echo "Setting up auto-generation on login..."

# Create LaunchAgents directory if needed
mkdir -p "$HOME/Library/LaunchAgents"

# Create the plist file
cat > "$PLIST_PATH" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_NAME}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>${SCRIPT_DIR}/daily_generate.py</string>
        <string>-n</string>
        <string>10</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>StartInterval</key>
    <integer>86400</integer>
    <key>StandardOutPath</key>
    <string>${HOME}/.coach_outreach/daily_generate_stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${HOME}/.coach_outreach/daily_generate_stderr.log</string>
    <key>WorkingDirectory</key>
    <string>${SCRIPT_DIR}</string>
</dict>
</plist>
EOF

# Load the LaunchAgent
launchctl unload "$PLIST_PATH" 2>/dev/null
launchctl load "$PLIST_PATH"

echo ""
echo "Done! Auto-generation is now set up."
echo ""
echo "What happens now:"
echo "  - When you login, it will generate 10 AI emails and sync to cloud"
echo "  - It also runs once every 24 hours while logged in"
echo "  - Logs are saved to ~/.coach_outreach/daily_generate_*.log"
echo ""
echo "To disable:"
echo "  launchctl unload $PLIST_PATH"
echo ""
echo "To run manually:"
echo "  python3 ${SCRIPT_DIR}/daily_generate.py"
echo ""
