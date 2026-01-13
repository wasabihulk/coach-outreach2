#!/bin/bash
# Daily Email Generator Script
# Runs at 8:05 AM via launchd, or on wake/login to catch up missed days
#
# Features:
# - Prevents running twice in same day
# - Catches up if Mac was off/asleep during scheduled time

cd /Users/keelanunderwood/coach-outreach-project

LAST_RUN_FILE="$HOME/.coach_outreach/last_run_date"
TODAY=$(date +%Y-%m-%d)

# Check if already ran today
if [ -f "$LAST_RUN_FILE" ]; then
    LAST_RUN=$(cat "$LAST_RUN_FILE")
    if [ "$LAST_RUN" = "$TODAY" ]; then
        echo "Already ran today ($TODAY), skipping."
        exit 0
    fi
fi

# Log start
echo ""
echo "=========================================="
echo "Daily Email Generation - $(date)"
echo "=========================================="

# Start Ollama if not running
if ! pgrep -x "ollama" > /dev/null; then
    echo "Starting Ollama..."
    ollama serve > /dev/null 2>&1 &
    sleep 5
fi

# Run the generator using venv python
/Users/keelanunderwood/coach-outreach-project/venv/bin/python3 generate_emails.py

# Record successful run
echo "$TODAY" > "$LAST_RUN_FILE"

echo ""
echo "Completed at $(date)"
