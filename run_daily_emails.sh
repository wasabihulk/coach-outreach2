#!/bin/bash
# Daily Email Generator Script
# Runs at 12:05 AM Pacific (after API limit resets at midnight)
#
# To set up as cron job:
#   crontab -e
#   Add: 5 0 * * * /Users/keelanunderwood/Desktop/coach-outreach-project/run_daily_emails.sh >> ~/.coach_outreach/daily.log 2>&1

cd /Users/keelanunderwood/Desktop/coach-outreach-project

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

# Run the generator
python3 generate_emails.py

echo ""
echo "Completed at $(date)"
