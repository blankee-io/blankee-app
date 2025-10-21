# Database Connection Pool Monitoring

This directory contains production-ready monitoring tools for the Blankee database connection pool.

## 📁 Files

### Core Monitoring
- **`monitor_pool_internal.py`** - Standalone Python monitoring script
  - Direct pool access (zero HTTP overhead)
  - Configurable alert thresholds
  - Structured logging
  - Signal handlers for graceful shutdown
  - Systemd-ready

### Deployment Configuration
- **`blankee-pool-monitor.service`** - Systemd service configuration
  - Auto-restart on failure
  - Resource limits (100M memory, 10% CPU)
  - Security hardening
  - Journal logging integration

### Documentation
- **`POOL_MONITOR_DEPLOYMENT.md`** - Deployment guide
  - Quick start for development
  - Production deployment steps
  - Service management commands
  - Troubleshooting tips

- **`CLOUDWATCH_INTEGRATION.md`** - AWS CloudWatch integration
  - Cost analysis (~$0.20/month)
  - Setup instructions (CloudWatch Agent + Manual)
  - Pre-configured alarms
  - SNS notification setup
  - Custom dashboards
  - IAM permissions

## 🚀 Quick Start

### Development Testing
```bash
# From project root
cd /var/www/html/budget/
python3 optimization/db-connection-pooling/pool-monitoring/monitor_pool_internal.py
```

Press `Ctrl+C` to stop.

### Production Deployment
```bash
# 1. Copy files to server
scp monitor_pool_internal.py ubuntu@your-server:/tmp/
scp blankee-pool-monitor.service ubuntu@your-server:/tmp/

# 2. SSH to server and install
ssh ubuntu@your-server
sudo mv /tmp/monitor_pool_internal.py /var/www/html/blankee/
sudo mv /tmp/blankee-pool-monitor.service /etc/systemd/system/
sudo chmod +x /var/www/html/blankee/monitor_pool_internal.py

# 3. Create log directory
sudo mkdir -p /var/log/blankee
sudo chown www-data:www-data /var/log/blankee

# 4. Start service
sudo systemctl daemon-reload
sudo systemctl enable blankee-pool-monitor
sudo systemctl start blankee-pool-monitor
sudo systemctl status blankee-pool-monitor
```

## ⚙️ Configuration

Edit `monitor_pool_internal.py` to adjust:

```python
# Monitoring intervals
CHECK_INTERVAL = 5.0  # seconds between checks

# Alert thresholds
ALERT_HIGH_USAGE = 0.8  # Alert at 80% pool usage
ALERT_LOW_AVAILABLE = 2  # Alert when ≤2 connections available
ALERT_CRITICAL_AVAILABLE = 0  # Critical when pool exhausted

# Logging
LOG_FILE = '/var/log/blankee/pool_monitor.log'
LOG_TO_CONSOLE = True  # Set to False in production with systemd
```

## 📊 Monitoring Features

### Alert Levels
- **INFO**: Normal operation (logged periodically)
- **WARNING**: High usage (>80%) or low availability (≤2 connections)
- **CRITICAL**: Pool exhausted (0 available connections)

### Log Output
```
2025-10-19 14:23:45 - POOL_MONITOR - INFO - Pool status: 5 total, 2 in use, 3 available (40.0% usage)
2025-10-19 14:24:15 - POOL_MONITOR - WARNING - High usage: 5 total, 4 in use, 1 available (80.0% usage)
2025-10-19 14:24:20 - POOL_MONITOR - CRITICAL - No available connections! 5 total, 5 in use, 0 available
```

## 📈 CloudWatch Integration (Optional)

For advanced monitoring on AWS:

1. **Low Cost**: ~$0.20/month total
2. **Features**: 
   - Real-time dashboards
   - Email/SMS alerts
   - Historical metrics
   - Query interface
3. **Setup**: See `CLOUDWATCH_INTEGRATION.md`

## 🔧 Service Management

```bash
# Start monitor
sudo systemctl start blankee-pool-monitor

# Stop monitor
sudo systemctl stop blankee-pool-monitor

# Restart monitor
sudo systemctl restart blankee-pool-monitor

# Check status
sudo systemctl status blankee-pool-monitor

# View logs (real-time)
sudo journalctl -u blankee-pool-monitor -f

# View last 100 log entries
sudo journalctl -u blankee-pool-monitor -n 100

# View log file
sudo tail -f /var/log/blankee/pool_monitor.log
```

## 🐛 Troubleshooting

### Monitor won't start
```bash
# Check for Python errors
sudo journalctl -u blankee-pool-monitor -n 50

# Verify db_connections.py is accessible
cd /var/www/html/blankee
python3 -c "from db_connections import get_db_pool; print('OK')"

# Check permissions
ls -la /var/www/html/blankee/monitor_pool_internal.py
ls -la /var/log/blankee
```

### High memory usage
```bash
# Check current memory usage
sudo systemctl status blankee-pool-monitor | grep Memory

# Adjust limits in service file if needed
sudo nano /etc/systemd/system/blankee-pool-monitor.service
# Change: MemoryLimit=100M to MemoryLimit=50M
sudo systemctl daemon-reload
sudo systemctl restart blankee-pool-monitor
```

## 📝 Related Documentation

- **Project Overview**: `../migration-summary.md`
- **Connection Pool Proposal**: `../context-manager-connection-pools-proposal.md`
- **Database Schema**: `/migrations/schema.sql`

## 📅 Version History

- **October 19, 2025**: Initial release
  - Internal monitoring with direct pool access
  - Systemd service configuration
  - CloudWatch integration guide
  - Renamed from 'budget' to 'blankee'

---

**Status**: ✅ Production Ready  
**Tested**: Development + Production environments  
**Cost**: Free (internal) or ~$0.20/month (with CloudWatch)
