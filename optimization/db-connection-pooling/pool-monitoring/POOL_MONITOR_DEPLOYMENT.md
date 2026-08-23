# Database Pool Monitor - Deployment Guide

## Overview

The internal pool monitor (`monitor_pool_internal.py`) provides real-time monitoring of your database connection pool with zero HTTP overhead. It runs as a standalone process on the same server as your Flask application.

## Quick Start (Development)

### Test Run on Home Server

```bash
# Navigate to your app directory
cd /Volumes/html

# Run the monitor directly
python3 monitor_pool_internal.py
```

Press `Ctrl+C` to stop.

## Production Deployment (EC2)

### Step 1: Upload Files to EC2

```bash
# From your local machine
scp monitor_pool_internal.py blankee-pool-monitor.service ubuntu@your-ec2-ip:/tmp/

# SSH to EC2
ssh ubuntu@your-ec2-ip

# Move files to correct locations
sudo mv /tmp/monitor_pool_internal.py /var/www/html/blankee/
sudo mv /tmp/blankee-pool-monitor.service /etc/systemd/system/

# Set permissions
sudo chmod +x /var/www/html/blankee/monitor_pool_internal.py
sudo chown www-data:www-data /var/www/html/blankee/monitor_pool_internal.py

# Create log directory
sudo mkdir -p /var/log/blankee
sudo chown www-data:www-data /var/log/blankee
```

### Step 2: Configure the Monitor

Edit `/var/www/html/blankee/monitor_pool_internal.py` and update:

```python
# Monitoring intervals
CHECK_INTERVAL = 5.0  # Adjust as needed

# Alert thresholds
ALERT_HIGH_USAGE = 0.8  # 80%
ALERT_LOW_AVAILABLE = 2
ALERT_CRITICAL_AVAILABLE = 0

# Logging
LOG_FILE = '/var/log/blankee/pool_monitor.log'
LOG_TO_CONSOLE = False  # Set to False for systemd
```

### Step 3: Enable and Start Service

```bash
# Reload systemd to recognize new service
sudo systemctl daemon-reload

# Enable service to start on boot
sudo systemctl enable blankee-pool-monitor

# Start the service
sudo systemctl start blankee-pool-monitor

# Check status
sudo systemctl status blankee-pool-monitor
```

### Step 4: Verify It's Working

```bash
# View real-time logs
sudo journalctl -u blankee-pool-monitor -f

# View last 100 lines
sudo journalctl -u blankee-pool-monitor -n 100

# Check log file
sudo tail -f /var/log/blankee/pool_monitor.log
```

## Service Management Commands

```bash
# Start the monitor
sudo systemctl start blankee-pool-monitor

# Stop the monitor
sudo systemctl stop blankee-pool-monitor

# Restart the monitor
sudo systemctl restart blankee-pool-monitor

# Check status
sudo systemctl status blankee-pool-monitor

# View logs
sudo journalctl -u blankee-pool-monitor -f

# Disable (won't start on boot)
sudo systemctl disable blankee-pool-monitor
```

## Monitoring the Monitor

The service will automatically restart if it crashes (up to 5 times in 5 minutes).

Check if it's running:
```bash
ps aux | grep monitor_pool_internal
```

## Troubleshooting

### Service Won't Start

1. Check permissions:
```bash
ls -la /var/www/html/blankee/monitor_pool_internal.py
```

2. Check logs:
```bash
sudo journalctl -u blankee-pool-monitor -n 50
```

3. Test manually:
```bash
sudo -u www-data python3 /var/www/html/blankee/monitor_pool_internal.py
```

### Pool Not Initialized Error

Make sure your Flask app is running first. The monitor needs the database pool to be initialized.

### Permission Denied on Log File

```bash
sudo chown www-data:www-data /var/log/blankee
sudo chmod 755 /var/log/blankee
```

## Configuration Reference

### Check Intervals

- `CHECK_INTERVAL = 5.0` - How often to check pool status (seconds)
- `LOG_NORMAL_EVERY_N = 12` - Log normal status every N checks (12 × 5s = 60s)

### Alert Thresholds

- `ALERT_HIGH_USAGE = 0.8` - Alert when 80% of pool is in use
- `ALERT_LOW_AVAILABLE = 2` - Alert when ≤2 connections available
- `ALERT_CRITICAL_AVAILABLE = 0` - Critical alert when exhausted

### Tuning Recommendations

**Low-traffic sites:**
- CHECK_INTERVAL = 10.0 (check every 10 seconds)
- LOG_NORMAL_EVERY_N = 6 (log every minute)

**High-traffic sites:**
- CHECK_INTERVAL = 2.0 (check every 2 seconds)
- LOG_NORMAL_EVERY_N = 30 (log every minute)

**Development:**
- CHECK_INTERVAL = 1.0 (check every second)
- LOG_NORMAL_EVERY_N = 10 (log every 10 seconds)
- LOG_TO_CONSOLE = True

## Uninstalling

```bash
# Stop and disable service
sudo systemctl stop blankee-pool-monitor
sudo systemctl disable blankee-pool-monitor

# Remove files
sudo rm /etc/systemd/system/blankee-pool-monitor.service
sudo rm /var/www/html/blankee/monitor_pool_internal.py
sudo rm -rf /var/log/blankee/pool_monitor.log

# Reload systemd
sudo systemctl daemon-reload
```

## Next Steps

See `CLOUDWATCH_INTEGRATION.md` for information on integrating with AWS CloudWatch for advanced monitoring and alerting.
