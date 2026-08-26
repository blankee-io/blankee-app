#!/bin/bash
# Rotate Blankee app logs daily at 23:59 UTC
# Creates dated log files for app logs and cron job logs
# Works on both dev (budget_error.log) and prod (blankee_error.log)

# Where the live logs are. An installed host has them in /var/log/blankee, which
# is what /admin/logs reads; dev and prod still keep theirs beside Apache's, so
# the older location is the fallback rather than a second code path.
if [ -n "$BLANKEE_LOG_DIR" ]; then
    LOG_DIR="$BLANKEE_LOG_DIR"
elif [ -f /var/log/blankee/blankee_error.log ]; then
    LOG_DIR="/var/log/blankee"
else
    LOG_DIR="/var/log/apache2"
fi

# Where the dated copies go. The same directory as the live log, so one place
# holds a day's file and the fortnight before it, and /admin/logs finds both.
ROTATED_DIR="${BLANKEE_ROTATED_LOG_DIR:-$LOG_DIR}"
mkdir -p "$ROTATED_DIR"

DATE=$(date -u +"%Y%m%d")

# Function to rotate a log file
rotate_log() {
    local LOG_FILE="$1"
    local PREFIX="$2"
    local ROTATED_LOG="${ROTATED_DIR}/${PREFIX}_${DATE}.log"
    
    if [ -s "$LOG_FILE" ]; then
        cp "$LOG_FILE" "$ROTATED_LOG"
        truncate -s 0 "$LOG_FILE"
        chmod 640 "$ROTATED_LOG"
        chown root:www-data "$ROTATED_LOG"
        echo "$(date -u): Rotated $LOG_FILE to $ROTATED_LOG (size: $(stat -c%s "$ROTATED_LOG") bytes)"
    else
        echo "$(date -u): No rotation needed for $LOG_FILE - empty or missing"
    fi
}

# Rotate main app error log (check for both dev and prod names)
if [ -f "$LOG_DIR/blankee_error.log" ]; then
    rotate_log "$LOG_DIR/blankee_error.log" "blankee_app"
elif [ -f "$LOG_DIR/budget_error.log" ]; then
    rotate_log "$LOG_DIR/budget_error.log" "blankee_app"
else
    echo "$(date -u): No main error log found (checked blankee_error.log and budget_error.log)"
fi

# Rotate cron job logs
# The bank-provider cron scripts (connection checker, auto-confirm, nightly
# sync) were removed along with the Quiltt integration, so these logs no longer
# get written. Left commented rather than deleted because the crontabs on
# on your hosts are not in this repo and must be cleaned separately -
# if any of them is still scheduled, re-enable the matching line.
# rotate_log "/var/log/apache2/quiltt_checker.log" "quiltt_checker"
# rotate_log "/var/log/apache2/auto_confirm.log" "auto_confirm"
# rotate_log "/var/log/apache2/nightly_sync.log" "nightly_sync"

# Clean up logs older than 180 days (6 months)
find "$ROTATED_DIR" -name "blankee_app_*.log" -type f -mtime +180 -delete
# Keep purging any already-rotated files from the removed cron scripts.
find "$ROTATED_DIR" -name "quiltt_checker_*.log" -type f -mtime +180 -delete
find "$ROTATED_DIR" -name "auto_confirm_*.log" -type f -mtime +180 -delete
find "$ROTATED_DIR" -name "nightly_sync_*.log" -type f -mtime +180 -delete
