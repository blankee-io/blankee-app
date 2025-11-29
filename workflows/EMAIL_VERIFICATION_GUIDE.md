# Email Verification System Documentation

## Overview

The email verification system ensures that users confirm their email addresses before accessing the Blankee application. This prevents spam accounts and ensures users can receive important notifications.

---

## Features

✅ **Secure Token Generation** - Uses cryptographically secure random tokens  
✅ **Token Expiration** - Verification links expire after 24 hours  
✅ **Resend Capability** - Users can request new verification emails  
✅ **Account Initialization** - Sets up default categories upon verification  
✅ **Branded Emails** - Professional HTML emails with Blankee branding  
✅ **Security** - Prevents unverified users from logging in  

---

## Setup Instructions

### 1. Run the Database Migration

Apply the migration to add email verification columns to the users table:

```bash
mysql -u your_username -p budget < /srv/blankee/migrations/add_email_verification.sql
```

Or manually execute:

```sql
ALTER TABLE users 
ADD COLUMN email_verified TINYINT(1) DEFAULT 0 AFTER email,
ADD COLUMN verification_token VARCHAR(100) DEFAULT NULL AFTER email_verified,
ADD COLUMN verification_token_expires DATETIME DEFAULT NULL AFTER verification_token;

CREATE INDEX idx_verification_token ON users(verification_token);
```

### 2. Configure Email Settings

#### Option A: Using Environment Variables (Recommended)

Copy the example environment file:

```bash
cp .env.example .env
```

Edit `.env` with your email provider settings:

```bash
# For Gmail (most common)
SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your-email@gmail.com
SMTP_PASSWORD=your-app-password
FROM_EMAIL=noreply@blankee.io
APP_URL=https://your-domain.com
```

#### Option B: Direct Configuration

Edit `/srv/blankee/email_utils.py` and set the values directly:

```python
SMTP_SERVER = 'smtp.gmail.com'
SMTP_PORT = 587
SMTP_USERNAME = 'your-email@gmail.com'
SMTP_PASSWORD = 'your-app-password'
FROM_EMAIL = 'noreply@blankee.io'
APP_URL = 'https://your-domain.com'
```

### 3. Gmail Setup (If Using Gmail)

If you're using Gmail as your SMTP server:

1. **Enable 2-Factor Authentication**
   - Go to https://myaccount.google.com/security
   - Enable 2-Step Verification

2. **Generate App Password**
   - Go to https://myaccount.google.com/apppasswords
   - Select "Mail" and your device
   - Copy the 16-character password
   - Use this as your `SMTP_PASSWORD`

3. **Update Settings**
   ```bash
   SMTP_USERNAME=youremail@gmail.com
   SMTP_PASSWORD=xxxx-xxxx-xxxx-xxxx  # Your app password
   ```

### 4. Other Email Providers

#### Outlook/Hotmail
```bash
SMTP_SERVER=smtp-mail.outlook.com
SMTP_PORT=587
SMTP_USERNAME=your-email@outlook.com
SMTP_PASSWORD=your-password
```

#### Yahoo
```bash
SMTP_SERVER=smtp.mail.yahoo.com
SMTP_PORT=587
SMTP_USERNAME=your-email@yahoo.com
SMTP_PASSWORD=your-app-password  # Generate at account.yahoo.com
```

#### SendGrid (Recommended for Production)
```bash
SMTP_SERVER=smtp.sendgrid.net
SMTP_PORT=587
SMTP_USERNAME=apikey
SMTP_PASSWORD=your-sendgrid-api-key
```

#### Mailgun
```bash
SMTP_SERVER=smtp.mailgun.org
SMTP_PORT=587
SMTP_USERNAME=postmaster@your-domain.com
SMTP_PASSWORD=your-mailgun-password
```

### 5. Load Environment Variables

#### Python with python-dotenv (Recommended)

Install python-dotenv:
```bash
pip install python-dotenv
```

Add to the top of `app.py`:
```python
from dotenv import load_dotenv
load_dotenv()  # Load .env file
```

#### Manual Export (Development)
```bash
export SMTP_SERVER=smtp.gmail.com
export SMTP_PORT=587
export SMTP_USERNAME=your-email@gmail.com
export SMTP_PASSWORD=your-app-password
export FROM_EMAIL=noreply@blankee.io
export APP_URL=http://localhost:5000
```

#### Systemd Service File (Production)
Add to your systemd service file:
```ini
[Service]
Environment="SMTP_SERVER=smtp.gmail.com"
Environment="SMTP_PORT=587"
Environment="SMTP_USERNAME=your-email@gmail.com"
Environment="SMTP_PASSWORD=your-app-password"
Environment="FROM_EMAIL=noreply@blankee.io"
Environment="APP_URL=https://blankee.io"
```

### 6. Restart the Application

```bash
# If using systemd
sudo systemctl restart blankee

# If running manually
# Stop the current process and restart
python app.py
```

---

## User Flow

### Registration Flow

1. **User registers** → Enters email and password
2. **Email sent** → Verification email sent to user's inbox
3. **User clicks link** → Verification token validated
4. **Account activated** → User redirected to profile setup
5. **Ready to use** → User can now log in normally

### Verification Email

Users receive an email like this:

```
Subject: Verify Your Blankee Account

Welcome to Blankee!

Hi user@example.com,

Thank you for registering with Blankee! To complete your 
registration, please verify your email address:

[Verify Email Address Button]

This link expires in 24 hours.
```

### What Happens on Verification

When a user clicks the verification link:

1. ✅ Token is validated
2. ✅ `email_verified` set to `1` in database
3. ✅ Verification token is cleared
4. ✅ Default income/expense categories created
5. ✅ User automatically logged in
6. ✅ Redirected to profile setup page

---

## Database Schema

### New Columns in `users` Table

| Column | Type | Default | Description |
|--------|------|---------|-------------|
| `email_verified` | TINYINT(1) | 0 | Whether email is verified (0=no, 1=yes) |
| `verification_token` | VARCHAR(100) | NULL | Unique token for email verification |
| `verification_token_expires` | DATETIME | NULL | When the token expires |

### Index
- `idx_verification_token` on `verification_token` for fast lookups

---

## API Endpoints

### POST `/register`

Registers a new user and sends verification email.

**Form Data:**
- `username` - Email address
- `password` - User password
- `member_since` - Start date

**Response:**
- Redirects to `/login` with flash message
- Sends verification email in background

---

### GET `/verify-email?token=<token>`

Verifies user's email address.

**Query Parameters:**
- `token` - Verification token from email

**Responses:**
- **Success**: Email verified, user logged in, redirect to setup_profile
- **Already Verified**: Flash message, redirect to login
- **Expired**: Flash message with instructions, redirect to login
- **Invalid**: Flash message, redirect to login

---

### POST `/resend-verification`

Resends verification email to user.

**Form Data:**
- `email` - User's email address

**Response:**
- Flash message confirming email sent (or error)
- Redirects to `/login`

**Security Note:** Doesn't reveal whether email exists in system

---

### POST `/login`

Modified to check email verification status.

**Behavior:**
- Checks `email_verified` field before allowing login
- If not verified, shows flash message and rejects login
- If verified, proceeds with normal login flow

---

## Email Templates

### Verification Email

- **Subject**: "Verify Your Blankee Account"
- **Content**: Welcome message with verification button
- **Styling**: Branded with Blankee colors (#2aaaa8)
- **Expiration Warning**: Clearly states 24-hour expiration

### Password Reset Email (Future Enhancement)

The `email_utils.py` module includes a `send_password_reset_email()` function ready for implementation.

---

## Security Considerations

### Token Generation

Tokens are generated using Python's `secrets` module:
```python
def generate_verification_token():
    return secrets.token_urlsafe(32)
```

This produces a 32-byte URL-safe random token (43 characters).

### Token Expiration

Tokens expire after 24 hours:
```python
def get_verification_token_expiry():
    return datetime.now() + timedelta(hours=24)
```

### Rate Limiting (Recommended)

Consider adding rate limiting to prevent abuse:
- Limit verification email resends to 3 per hour per email
- Implement CAPTCHA on resend form
- Track failed verification attempts

---

## Troubleshooting

### Email Not Sending

**Problem**: Registration succeeds but no email received

**Solutions:**

1. **Check SMTP credentials**
   ```python
   # Test in Python console
   from email_utils import send_email
   result = send_email(
       "test@example.com", 
       "Test", 
       "<p>Test email</p>"
   )
   print(result)  # Should print True
   ```

2. **Check spam folder** - Verification emails might be marked as spam

3. **Check SMTP server logs**
   ```bash
   # If error occurs, check app logs
   tail -f /var/log/blankee/app.log
   ```

4. **Verify environment variables are loaded**
   ```python
   import os
   print(os.getenv('SMTP_USERNAME'))  # Should print your email
   ```

5. **Test SMTP connection directly**
   ```python
   import smtplib
   server = smtplib.SMTP('smtp.gmail.com', 587)
   server.starttls()
   server.login('your-email@gmail.com', 'your-app-password')
   server.quit()  # Should not raise exception
   ```

### Token Expired

**Problem**: User clicks link but token has expired

**Solution:**
- Use the "Resend verification email" link on login page
- New token will be generated and sent

### Already Verified

**Problem**: User tries to verify again

**Solution:**
- System checks `email_verified` status and redirects to login
- User can log in normally

### Email Not Received After Resend

**Problem**: Resend doesn't work

**Solutions:**
1. Check if email exists in system
2. Verify email provider isn't blocking
3. Check application logs for errors
4. Try different email address

---

## Testing

### Manual Testing Checklist

- [ ] Register new user
- [ ] Receive verification email
- [ ] Click verification link
- [ ] Account activated
- [ ] Can log in
- [ ] Try logging in before verifying (should be blocked)
- [ ] Request resend verification
- [ ] Receive new email
- [ ] Verify with new link
- [ ] Try using expired token (wait 24h or manually set expiry)

### Automated Testing

```python
def test_email_verification():
    # Register user
    response = client.post('/register', data={
        'username': 'test@example.com',
        'password': 'password123',
        'member_since': '2025-01-01'
    })
    assert response.status_code == 302  # Redirect
    
    # Get verification token from database
    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT verification_token FROM users WHERE username = %s",
            ('test@example.com',)
        )
        token = cursor.fetchone()[0]
        cursor.close()
    
    # Verify email
    response = client.get(f'/verify-email?token={token}')
    assert response.status_code == 302
    
    # Check email_verified is now 1
    with get_db_pool().get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT email_verified FROM users WHERE username = %s",
            ('test@example.com',)
        )
        verified = cursor.fetchone()[0]
        cursor.close()
    
    assert verified == 1
```

---

## Migration Guide for Existing Users

If you have existing users who registered before email verification was implemented:

### Option 1: Auto-Verify Existing Users

```sql
-- Mark all existing users as verified
UPDATE users 
SET email_verified = 1 
WHERE verification_token IS NULL 
AND email_verified = 0;
```

### Option 2: Send Verification Emails to All

```python
# Script to send verification emails to all unverified users
from app import get_db_pool
from email_utils import send_verification_email, generate_verification_token, get_verification_token_expiry

with get_db_pool().get_connection() as conn:
    cursor = conn.cursor(pymysql.cursors.DictCursor)
    cursor.execute("""
        SELECT id, username, email 
        FROM users 
        WHERE email_verified = 0
    """)
    users = cursor.fetchall()
    
    for user in users:
        token = generate_verification_token()
        expiry = get_verification_token_expiry()
        
        cursor.execute("""
            UPDATE users 
            SET verification_token = %s, verification_token_expires = %s 
            WHERE id = %s
        """, (token, expiry, user['id']))
        
        send_verification_email(user['email'], user['username'], token)
        print(f"Sent verification email to {user['email']}")
    
    conn.commit()
    cursor.close()
```

---

## Future Enhancements

### Email Verification Improvements

1. **Magic Link Login** - Allow login via email link (passwordless)
2. **Email Change Verification** - Verify new email when user changes it
3. **Welcome Email Series** - Send onboarding emails after verification
4. **Email Preferences** - Allow users to opt in/out of notifications

### Additional Email Features

1. **Password Reset** - Use `send_password_reset_email()` function
2. **Budget Alerts** - Email when nearing budget thresholds
3. **Weekly/Monthly Reports** - Automated budget summaries
4. **Notification Digest** - Daily/weekly email of app notifications

### Security Enhancements

1. **Rate Limiting** - Prevent email spam/abuse
2. **CAPTCHA** - On registration and resend forms
3. **IP Tracking** - Log verification attempts
4. **Two-Factor Auth** - Already implemented, can tie to verified email

---

## Files Modified/Created

### New Files

- `/srv/blankee/email_utils.py` - Email sending functionality
- `/srv/blankee/migrations/add_email_verification.sql` - Database migration
- `/srv/blankee/.env.example` - Environment variable template
- `/srv/blankee/EMAIL_VERIFICATION_GUIDE.md` - This documentation

### Modified Files

- `/srv/blankee/app.py`
  - Added email verification imports
  - Modified `/register` route
  - Modified `/login` route to check verification
  - Added `/verify-email` route
  - Added `/resend-verification` route

- `/srv/blankee/templates/login.html`
  - Added "Resend verification email" section
  - Added JavaScript for show/hide resend form

### Database Schema

- `users` table:
  - Added `email_verified` column
  - Added `verification_token` column
  - Added `verification_token_expires` column
  - Added index on `verification_token`

---

## Support

For issues or questions:
1. Check troubleshooting section above
2. Review application logs
3. Test email sending manually with `email_utils.py`
4. Refer to main project documentation

---

## License

This email verification system is part of the Blankee budgeting application.
