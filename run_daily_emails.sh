#!/bin/bash
# Daily Email Generator Script
# Runs at 8:05 AM via launchd, or on wake/login to catch up missed days
#
# Features:
# - Waits for network connectivity before running
# - Prevents running twice in same day
# - Only marks complete on SUCCESS (retries on failure)
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

# Wait for network connectivity (up to 2 minutes)
echo "Checking network connectivity..."
MAX_WAIT=120
WAITED=0
while ! curl -s --connect-timeout 3 https://www.google.com > /dev/null 2>&1; do
    if [ $WAITED -ge $MAX_WAIT ]; then
        echo "ERROR: No network after ${MAX_WAIT}s - will retry next wake/login"
        exit 1
    fi
    echo "Waiting for network... (${WAITED}s)"
    sleep 5
    WAITED=$((WAITED + 5))
done
echo "Network connected!"

# Start Ollama if not running
if ! pgrep -x "ollama" > /dev/null; then
    echo "Starting Ollama..."
    ollama serve > /dev/null 2>&1 &
    sleep 5
fi

# Run the generator using venv python (daily_generate.py includes cloud sync)
/Users/keelanunderwood/coach-outreach-project/venv/bin/python3 daily_generate.py
EXIT_CODE=$?

# Only record successful run
if [ $EXIT_CODE -eq 0 ]; then
    echo "$TODAY" > "$LAST_RUN_FILE"
    echo ""
    echo "Generation completed successfully at $(date)"

    # =========================================================================
    # SEND EMAILS - Trigger auto-send after successful generation
    # =========================================================================
    echo ""
    echo "=========================================="
    echo "Sending Emails - $(date)"
    echo "=========================================="

    # Run the email sender script
    /Users/keelanunderwood/coach-outreach-project/venv/bin/python3 daily_send.py
    SEND_EXIT=$?

    if [ $SEND_EXIT -eq 0 ]; then
        echo "Emails sent successfully at $(date)"
    else
        echo "Email sending failed (exit code $SEND_EXIT) - emails can be sent manually"
    fi

    echo ""
    echo "All tasks completed at $(date)"
else
    echo ""
    echo "FAILED (exit code $EXIT_CODE) at $(date) - will retry next wake/login"
    exit $EXIT_CODE
fi
