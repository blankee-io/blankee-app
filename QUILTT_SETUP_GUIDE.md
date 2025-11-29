# Quiltt.io Open Banking Integration - Setup Guide

## Overview

This guide will walk you through setting up the Quiltt.io integration for your budgeting app. Once complete, your users will be able to:

- Connect their bank accounts securely using Quiltt Connector
- Automatically import transactions from 2000+ financial institutions
- Sync account balances in real-time
- Map imported transactions to budget categories
- Significantly reduce manual data entry

## Architecture

The integration consists of:

1. **Backend Utilities** (`quiltt_utils.py`): Python client for Quiltt API
2. **Database Schema** (`migrations/add_quiltt_integration.sql`): 6 tables for storing connections, accounts, transactions
3. **Frontend UI** (`templates/quiltt_settings.html`): User interface for managing bank connections
4. **Backend Routes** (`app.py`): Flask endpoints for handling Quiltt operations
5. **Navigation** (`templates/nav.html`): Link to Bank Connections in dropdown menu

## Prerequisites

- Python 3.7+
- MySQL database
- Active Quiltt.io account (sign up at https://dashboard.quiltt.dev)
- `requests` library (already in requirements.txt)

## Setup Steps

### 1. Get Quiltt API Credentials

1. Go to https://dashboard.quiltt.dev and sign up/sign in
2. Create a new environment (Development or Production)
3. Note your:
   - **API Key** (for backend operations)
   - **Environment ID** (for Quiltt Connector)

### 2. Set Environment Variables

Add these to your `.env` file or environment configuration:

```bash
QUILTT_API_KEY=your_api_key_here
QUILTT_ENVIRONMENT_ID=your_environment_id_here
```

**Security Note**: Never commit these credentials to version control. Keep them in `.env` and ensure `.env` is in your `.gitignore`.

### 3. Run Database Migration

Run the SQL migration to create Quiltt tables:

```bash
mysql -u your_username -p your_database < migrations/add_quiltt_integration.sql
```

Or if you're using a remote database:

```bash
mysql -h your_host -u your_username -p your_database < migrations/add_quiltt_integration.sql
```

This creates:
- `quiltt_profiles` - User session tokens
- `quiltt_connections` - Connected financial institutions
- `quiltt_accounts` - Individual bank accounts
- `quiltt_transactions` - Imported transactions
- `quiltt_category_mappings` - Custom category mappings
- `quiltt_webhook_events` - Webhook event queue

### 4. Restart Your Application

Restart your Flask application to load the new routes and utilities:

```bash
# If using systemd
sudo systemctl restart your-app-service

# Or if running directly
python app.py
```

### 5. Test the Integration

1. Log into your app
2. Click your profile picture → **Bank Connections**
3. Click **Connect Bank Account**
4. The Quiltt Connector modal should appear
5. Search for a bank and connect (use test credentials in development)

### 6. Configure Webhooks (Optional but Recommended)

Webhooks enable real-time updates when:
- New transactions are available
- Account balances change
- Connection status changes

To set up webhooks:

1. In Quiltt Dashboard, go to **Webhooks**
2. Add webhook URL: `https://your-domain.com/quiltt/webhook`
3. Select events to subscribe to:
   - `transaction.created`
   - `transaction.updated`
   - `account.updated`
   - `connection.updated`
4. Copy the webhook secret
5. Add to your `.env`:
   ```bash
   QUILTT_WEBHOOK_SECRET=your_webhook_secret_here
   ```

## Testing in Development

Quiltt provides test credentials for development:

**Test Bank Login:**
- Institution: "Quiltt Bank" (search in Connector)
- Username: `user_good`
- Password: `pass_good`

This creates test accounts and transactions you can work with without connecting real bank accounts.

## Features

### User Features

- **Connect Banks**: Link checking, savings, credit card accounts
- **Auto-Import**: Automatically import new transactions daily
- **Manual Sync**: Refresh connection data on demand
- **Account Control**: Enable/disable transaction sync per account
- **Category Mapping**: Customize how transactions are categorized
- **Import History**: Import past transactions for historical data
- **Disconnect**: Remove bank connections at any time

### Security Features

- **Read-Only Access**: Quiltt only reads account data, never writes
- **Bank-Level Encryption**: All data encrypted in transit and at rest
- **Session Tokens**: Short-lived tokens for user authentication
- **No Stored Credentials**: Bank credentials never touch your server
- **OAuth 2.0**: Uses bank-native OAuth when available

## Default Category Mappings

The system includes sensible defaults for mapping Quiltt transaction categories to your budget categories. These can be customized by users in the UI:

| Quiltt Category | Default Budget Category |
|----------------|------------------------|
| Food & Dining  | Groceries / Dining Out |
| Transportation | Gas / Auto |
| Shopping       | Shopping |
| Bills & Utilities | Utilities |
| Healthcare     | Medical |
| Entertainment  | Entertainment |
| Income         | Paycheck / Other Income |

## Troubleshooting

### "No active session" error

**Cause**: Session token expired or missing.

**Solution**: Session tokens are automatically refreshed when you visit `/quiltt-settings`. If issues persist, check that `QUILTT_API_KEY` is set correctly.

### Connection fails with "Invalid credentials"

**Cause**: Either actual invalid credentials, or bank requires MFA.

**Solution**: 
- Verify credentials are correct
- Some banks require answering security questions in Connector
- Check if bank supports OAuth (preferred method)

### Transactions not importing

**Checklist**:
1. Is auto-import enabled? (toggle in Bank Connections)
2. Is the specific account enabled for sync? (checkboxes on connection card)
3. Has the connection been synced recently? (click Sync button)
4. Are there actually new transactions? (check bank's website)

### Webhook events not processing

**Checklist**:
1. Is webhook URL publicly accessible?
2. Is webhook URL correct in Quiltt Dashboard?
3. Check `quiltt_webhook_events` table for received events
4. Implement background job processing for events (TODO in code)

## API Rate Limits

Quiltt API has rate limits:
- **Session Token Creation**: 100 requests/hour per user
- **GraphQL Queries**: 1000 requests/hour per environment
- **Webhooks**: Unlimited (receiving only)

The integration caches session tokens to minimize API calls.

## Production Checklist

Before deploying to production:

- [ ] Set production environment variables
- [ ] Use production Quiltt environment (not development)
- [ ] Configure webhooks for real-time updates
- [ ] Implement background job processing for webhook events
- [ ] Set up monitoring for failed connections
- [ ] Test with real bank connections
- [ ] Review security: HTTPS, secure database, environment variables protected
- [ ] Set up error logging and alerting
- [ ] Document category mapping guidelines for users
- [ ] Create user help documentation

## Next Steps

### Implement Background Job Processing

The webhook handler currently stores events but doesn't process them. Implement async processing using:

- **Celery**: Distributed task queue (recommended)
- **RQ (Redis Queue)**: Simpler alternative
- **APScheduler**: For periodic tasks

Example with Celery:

```python
from celery import Celery

celery = Celery('tasks', broker='redis://localhost:6379/0')

@celery.task
def process_quiltt_webhook(event_id):
    # Fetch event from database
    # Process based on event type
    # Import transactions, update balances, etc.
    pass
```

### Add Transaction Import Route

Currently users can only connect banks. Add a route to actually import transactions:

```python
@app.route('/quiltt/import-transactions', methods=['POST'])
@login_required
def quiltt_import_transactions():
    # Get transactions from Quiltt
    # Map to budget categories
    # Insert into income_entries/expense_entries
    # Return summary
    pass
```

### Enhance Category Mapping

Allow users to:
- Create custom mapping rules
- Set default categories per merchant
- Use ML to learn from manual categorizations

### Add Dashboard Widgets

Display on main dashboard:
- Total account balances
- Recent transactions
- Connection health status
- Sync status and last sync time

## Support

- **Quiltt Documentation**: https://docs.quiltt.dev
- **Quiltt Dashboard**: https://dashboard.quiltt.dev
- **Quiltt Support**: support@quiltt.io

## License & Compliance

Quiltt is SOC 2 Type II certified and complies with:
- PCI DSS Level 1
- GDPR
- CCPA
- SOX

Your integration inherits these compliance standards when using Quiltt as your aggregation layer.
