#!/bin/bash
# Rotate Blankee app logs daily at 23:59 UTC
# Creates dated log files: blankee_app_yyyymmdd.log, quiltt_checker_yyyymmdd.log
# Works on both dev (budget_error.log) and prod (blankee_error.log)

LOG_DIR="/var/log/apache2"
DATE=$(date -u +"%Y%m%d")

# Function to rotate a log file
rotate_log() {
    local LOG_FILE="$1"
    local PREFIX="$2"
    local ROTATED_LOG="${LOG_DIR}/${PREFIX}_${DATE}.log"
    
    if [ -s "$LOG_FILE" ]; then
        cp "$LOG_FILE" "$ROTATED_LOG"
        truncate -s 0 "$LOG_FILE"
        chmod 640 "$ROTATED_LOG"
        chown root:adm "$ROTATED_LOG"
        echo "$(date -u): Rotated $LOG_FILE to $ROTATED_LOG (size: $(stat -c%s "$ROTATED_LOG") bytes)"
    else
        echo "$(date -u): No rotation needed for $LOG_FILE - empty or missing"
    fi
}

# Rotate main app log (check for both dev and prod names)
if [ -f "/var/log/apache2/blankee_error.log" ]; then
    rotate_log "/var/log/apache2/blankee_error.log" "blankee_app"
elif [ -f "/var/log/apache2/budget_error.log" ]; then
    rotate_log "/var/log/apache2/budget_error.log" "blankee_app"
else
    echo "$(date -u): No main error log found (checked blankee_error.log and budget_error.log)"
fi

# Rotate quiltt checker log
rotate_log "/var/log/apache2/quiltt_checker.log" "quiltt_checker"

# Clean up logs older than 180 days (6 months)
find "$LOG_DIR" -name "blankee_app_*.log" -type f -mtime +180 -delete
find "$LOG_DIR" -name "quiltt_checker_*.log" -type f -mtime +180 -delete
