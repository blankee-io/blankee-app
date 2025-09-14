#!/bin/bash

# Define directories
backup_dir="/media/fs-01/Budget/AppBackup/$(date +%Y-%m-%d_%H-%M-%S)"
latest_database_dir="/media/fs-01/Budget/LatestDatabase"
latest_release_dir="/media/fs-01/Budget/LatestRelease"
budget_dir="/var/www/html/budget"
static_dir="$budget_dir/static"
uploads_dir="$static_dir/uploads"
log_file="/var/log/budget_backup.log"

# Define database credentials (use .my.cnf for security)
db_name="budget"  # Replace with your database name

# Create the backup directory
mkdir -p "$backup_dir"

# Log start time
echo "$(date): Backup and update process started." | tee -a "$log_file"

# Check write permissions on NFS backup directory
if ! touch "$backup_dir/.test" 2>/dev/null; then
    echo "Error: Cannot write to backup directory $backup_dir. Check NFS permissions." | tee -a "$log_file"
    exit 1
else
    rm -f "$backup_dir/.test"
fi

# Perform the file backup excluding 'uploads' and all hidden files
rsync -av --inplace --temp-dir=/tmp \
    --no-owner --no-group --no-perms \
    --exclude="static/uploads" --exclude='.*' --exclude='/**/.*' \
    "$budget_dir/" "$backup_dir/" | tee -a "$log_file"

# Backup the database structure
echo "Backing up the database..." | tee -a "$log_file"
mysqldump --defaults-extra-file=~/.my.cnf --no-data "$db_name" > "$backup_dir/dblatest.sql" 2>> "$log_file"
if [ $? -eq 0 ]; then
    echo "Database structure backup successful." | tee -a "$log_file"
else
    echo "Database structure backup failed." | tee -a "$log_file"
    exit 1
fi

# Backup uploads directory
echo "Checking uploads directory size before backup..." | tee -a "$log_file"
du -sh "$uploads_dir" | tee -a "$log_file"

echo "Copying the uploads directory to a temporary location..." | tee -a "$log_file"
rsync -av --inplace --temp-dir=/tmp \
    --no-owner --no-group --no-perms \
    --exclude='.*' --exclude='/**/.*' \
    "$uploads_dir/" "$backup_dir/uploads_backup/" | tee -a "$log_file"
if [ $? -eq 0 ]; then
    echo "Uploads directory backup successful." | tee -a "$log_file"
    rm -rf "$uploads_dir"
else
    echo "Uploads directory backup failed." | tee -a "$log_file"
    exit 1
fi

# Clear the budget directory
echo "Clearing the budget directory..." | tee -a "$log_file"
rm -rf "$budget_dir/"* | tee -a "$log_file"

# Ensure the static directory exists before restoring uploads
mkdir -p "$static_dir"

# Restore the uploads directory
echo "Restoring the uploads directory..." | tee -a "$log_file"
rsync -av --inplace --temp-dir=/tmp \
    --no-owner --no-group --no-perms \
    --exclude='.*' --exclude='/**/.*' \
    "$backup_dir/uploads_backup/" "$uploads_dir/" | tee -a "$log_file"
if [ $? -eq 0 ]; then
    echo "Uploads directory restored successfully." | tee -a "$log_file"
    rm -rf "$backup_dir/uploads_backup"
else
    echo "Uploads directory restoration failed." | tee -a "$log_file"
    exit 1
fi

# Copy files from latest release
if [ -d "$latest_release_dir" ]; then
    rsync -av --inplace --temp-dir=/tmp \
        --no-owner --no-group --no-perms \
        --exclude="static/uploads" --exclude='.*' --exclude='/**/.*' \
        "$latest_release_dir/" "$budget_dir/" | tee -a "$log_file"

    sudo chown -R www-data:www-data "$budget_dir" | tee -a "$log_file"
    sudo chmod -R 755 "$budget_dir" | tee -a "$log_file"

    echo "Files copied successfully." | tee -a "$log_file"
else
    echo "Error: LatestRelease directory not found." | tee -a "$log_file"
    exit 1
fi

# Log end time
echo "$(date): Backup, update, and file synchronization process completed." | tee -a "$log_file"
