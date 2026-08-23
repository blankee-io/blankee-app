# CloudWatch Integration for Pool Monitor

## Overview

This guide shows how to integrate the pool monitor with AWS CloudWatch for advanced monitoring, alerting, and visualization.

## Cost Considerations

### CloudWatch Pricing (as of 2025)

**CloudWatch Logs:**
- Ingestion: $0.50 per GB
- Storage: $0.03 per GB/month
- First 5GB ingestion free

**Typical Monitor Costs:**
- Low-traffic: ~1MB/day = $0.015/month ($0.18/year)
- High-traffic: ~10MB/day = $0.15/month ($1.80/year)

**CloudWatch Alarms:**
- $0.10 per alarm/month
- 10 free alarms per month

**Expected Monthly Cost:**
- Logs: $0.02 - $0.20
- Alarms (2-3): $0 (within free tier)
- **Total: ~$0.20/month or less**

### Cost Optimization Tips

1. **Reduce log verbosity in production**
2. **Use metric filters instead of storing all logs**
3. **Set log retention to 7-30 days**
4. **Only alert on critical metrics**

## Setup Methods

### Method 1: CloudWatch Agent (Recommended)

The CloudWatch Agent automatically ships logs to CloudWatch.

#### Step 1: Install CloudWatch Agent

```bash
# On EC2 instance
wget https://s3.amazonaws.com/amazoncloudwatch-agent/ubuntu/amd64/latest/amazon-cloudwatch-agent.deb
sudo dpkg -i -E ./amazon-cloudwatch-agent.deb
```

#### Step 2: Configure CloudWatch Agent

Create `/opt/aws/amazon-cloudwatch-agent/etc/config.json`:

```json
{
  "logs": {
    "logs_collected": {
      "files": {
        "collect_list": [
          {
            "file_path": "/var/log/blankee/pool_monitor.log",
            "log_group_name": "/blankee/pool-monitor",
            "log_stream_name": "{instance_id}",
            "timezone": "UTC",
            "timestamp_format": "%Y-%m-%d %H:%M:%S"
          }
        ]
      }
    },
    "log_stream_name": "{instance_id}"
  }
}
```

#### Step 3: Start CloudWatch Agent

```bash
sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
    -a fetch-config \
    -m ec2 \
    -s \
    -c file:/opt/aws/amazon-cloudwatch-agent/etc/config.json
```

#### Step 4: Verify Logs Are Flowing

1. Go to AWS Console → CloudWatch → Log groups
2. Find `/blankee/pool-monitor`
3. Check for recent log streams

### Method 2: Manual Integration (No Agent)

Send logs directly to CloudWatch using boto3.

#### Step 1: Install boto3

```bash
pip3 install boto3
```

#### Step 2: Update Monitor Script

Add to `monitor_pool_internal.py`:

```python
import boto3
from botocore.exceptions import ClientError

# CloudWatch configuration
CLOUDWATCH_ENABLED = True
CLOUDWATCH_LOG_GROUP = '/blankee/pool-monitor'
CLOUDWATCH_LOG_STREAM = 'production-instance'

class CloudWatchHandler(logging.Handler):
    """Custom handler to send logs to CloudWatch"""
    
    def __init__(self, log_group, log_stream):
        super().__init__()
        self.log_group = log_group
        self.log_stream = log_stream
        self.client = boto3.client('logs', region_name='us-east-1')
        self.sequence_token = None
        self._ensure_log_stream()
    
    def _ensure_log_stream(self):
        """Create log group and stream if they don't exist"""
        try:
            self.client.create_log_group(logGroupName=self.log_group)
        except ClientError as e:
            if e.response['Error']['Code'] != 'ResourceAlreadyExistsException':
                raise
        
        try:
            self.client.create_log_stream(
                logGroupName=self.log_group,
                logStreamName=self.log_stream
            )
        except ClientError as e:
            if e.response['Error']['Code'] != 'ResourceAlreadyExistsException':
                raise
    
    def emit(self, record):
        """Send log record to CloudWatch"""
        try:
            log_event = {
                'timestamp': int(record.created * 1000),
                'message': self.format(record)
            }
            
            kwargs = {
                'logGroupName': self.log_group,
                'logStreamName': self.log_stream,
                'logEvents': [log_event]
            }
            
            if self.sequence_token:
                kwargs['sequenceToken'] = self.sequence_token
            
            response = self.client.put_log_events(**kwargs)
            self.sequence_token = response.get('nextSequenceToken')
            
        except Exception as e:
            # Don't let CloudWatch errors crash the monitor
            print(f"Failed to send to CloudWatch: {e}")

# Add to setup_logging() function:
if CLOUDWATCH_ENABLED:
    cw_handler = CloudWatchHandler(CLOUDWATCH_LOG_GROUP, CLOUDWATCH_LOG_STREAM)
    cw_handler.setFormatter(formatter)
    logger.addHandler(cw_handler)
```

## Creating CloudWatch Alarms

### Alarm 1: Critical - No Available Connections

```bash
aws cloudwatch put-metric-alarm \
  --alarm-name blankee-pool-critical-no-connections \
  --alarm-description "Critical: Database pool exhausted" \
  --metric-name IncomingLogEvents \
  --namespace AWS/Logs \
  --statistic Sum \
  --period 60 \
  --threshold 1 \
  --comparison-operator GreaterThanThreshold \
  --evaluation-periods 1 \
  --treat-missing-data notBreaching
```

### Alarm 2: Warning - High Pool Usage

Create a metric filter first:

```bash
# Create metric filter for high usage warnings
aws logs put-metric-filter \
  --log-group-name /blankee/pool-monitor \
  --filter-name high-pool-usage \
  --filter-pattern "[timestamp, dash, component, dash, level=WARNING*, msg=*High usage*]" \
  --metric-transformations \
    metricName=HighPoolUsage,\
    metricNamespace=Blankee/Pool,\
    metricValue=1,\
    defaultValue=0
```

Then create the alarm:

```bash
aws cloudwatch put-metric-alarm \
  --alarm-name blankee-pool-high-usage \
  --alarm-description "Warning: Database pool usage above 80%" \
  --metric-name HighPoolUsage \
  --namespace Blankee/Pool \
  --statistic Sum \
  --period 300 \
  --threshold 5 \
  --comparison-operator GreaterThanThreshold \
  --evaluation-periods 1 \
  --treat-missing-data notBreaching
```

### Alarm 3: Monitor is Down

```bash
aws cloudwatch put-metric-alarm \
  --alarm-name blankee-pool-monitor-down \
  --alarm-description "Pool monitor stopped logging" \
  --metric-name IncomingLogEvents \
  --namespace AWS/Logs \
  --dimensions Name=LogGroupName,Value=/blankee/pool-monitor \
  --statistic Sum \
  --period 300 \
  --threshold 1 \
  --comparison-operator LessThanThreshold \
  --evaluation-periods 2 \
  --treat-missing-data breaching
```

## SNS Notifications (Email/SMS)

### Step 1: Create SNS Topic

```bash
aws sns create-topic --name blankee-pool-alerts
```

### Step 2: Subscribe to Topic

```bash
# Email notification
aws sns subscribe \
  --topic-arn arn:aws:sns:us-east-1:ACCOUNT_ID:blankee-pool-alerts \
  --protocol email \
  --notification-endpoint your-email@example.com

# SMS notification (charges apply)
aws sns subscribe \
  --topic-arn arn:aws:sns:us-east-1:ACCOUNT_ID:blankee-pool-alerts \
  --protocol sms \
  --notification-endpoint +1234567890
```

### Step 3: Link Alarms to SNS

Add to your alarm commands:

```bash
--alarm-actions arn:aws:sns:us-east-1:ACCOUNT_ID:blankee-pool-alerts
```

## CloudWatch Dashboards

Create a custom dashboard to visualize pool metrics:

```json
{
  "widgets": [
    {
      "type": "metric",
      "properties": {
        "metrics": [
          [ "Blankee/Pool", "HighPoolUsage", { "stat": "Sum", "label": "High Usage Warnings" } ],
          [ ".", "CriticalNoConnections", { "stat": "Sum", "label": "Critical Alerts" } ]
        ],
        "period": 300,
        "stat": "Sum",
        "region": "us-east-1",
        "title": "Pool Health",
        "yAxis": {
          "left": {
            "min": 0
          }
        }
      }
    },
    {
      "type": "log",
      "properties": {
        "query": "SOURCE '/blankee/pool-monitor'\n| fields @timestamp, @message\n| filter @message like /WARNING|CRITICAL/\n| sort @timestamp desc\n| limit 20",
        "region": "us-east-1",
        "title": "Recent Alerts",
        "stacked": false
      }
    }
  ]
}
```

Save this as `dashboard.json` and create:

```bash
aws cloudwatch put-dashboard \
  --dashboard-name BlankeePoolMonitor \
  --dashboard-body file://dashboard.json
```

## Useful CloudWatch Insights Queries

### Query 1: Alert Frequency

```
fields @timestamp, @message
| filter @message like /WARNING|CRITICAL/
| stats count() by bin(5m) as alert_count
| sort @timestamp desc
```

### Query 2: Pool Usage Over Time

```
fields @timestamp, @message
| parse @message "* in use, * available (*% usage)" as in_use, available, usage_pct
| filter usage_pct > 0
| stats avg(usage_pct) by bin(5m)
| sort @timestamp desc
```

### Query 3: Connection Availability Trends

```
fields @timestamp, @message
| parse @message "* in use, * available" as in_use, available
| filter available > 0
| stats min(available), avg(available), max(available) by bin(1h)
| sort @timestamp desc
```

## Log Retention Policy

Set retention to reduce costs:

```bash
# Retain logs for 30 days
aws logs put-retention-policy \
  --log-group-name /blankee/pool-monitor \
  --retention-in-days 30
```

Options: 1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1827, 3653 days

## Estimated Costs Summary

### Minimal Setup (Logs Only)
- CloudWatch Logs: $0.05/month
- Total: **~$0.05/month**

### Basic Monitoring (Logs + 2 Alarms)
- CloudWatch Logs: $0.10/month
- Alarms: $0 (free tier)
- Total: **~$0.10/month**

### Full Monitoring (Logs + Alarms + Dashboard + SNS)
- CloudWatch Logs: $0.20/month
- Alarms (3): $0 (free tier)
- Dashboard: Free
- SNS Email: Free
- SNS SMS: $0.00645 per message (only when alerts fire)
- Total: **~$0.20/month + SMS costs**

## Disable CloudWatch Integration

To disable CloudWatch:

1. Stop CloudWatch Agent:
```bash
sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a stop
```

2. Or in monitor script, set:
```python
CLOUDWATCH_ENABLED = False
```

## IAM Permissions Required

Your EC2 instance needs these IAM permissions:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "logs:DescribeLogStreams"
      ],
      "Resource": "arn:aws:logs:*:*:log-group:/blankee/*"
    }
  ]
}
```

Attach this policy to your EC2 instance role.
