# Forgot Password Implementation Guide

## Overview
A complete forgot password / password reset system has been implemented for the Blankee application.

## Components Added

### 1. Database Migration
**File:** `/srv/blankee/migrations/add_password_reset.sql`

Creates the `password_resets` table to store secure password reset tokens with the following features:
- Unique tokens for each reset request
- 1-hour expiration time
- Tracks if token has been used
- Cascading delete when user is deleted
- Indexed for fast lookups

**To apply the migration:**
```bash
mysql -u your_user -p budget < /srv/blankee/migrations/add_password_reset.sql
```

### 2. Backend Routes (app.py)
Added two new routes:

#### `/forgot-password` (GET, POST)
- Shows email input form
- Generates secure reset token
- Sends password reset email
- Always shows success message (security best practice)

#### `/reset-password` (GET, POST)
- Validates reset token
- Checks token expiration and usage
- Allows user to set new password
- Hashes and updates password in database
- Marks token as used

### 3. Email Utilities (email_utils.py)
Added new functions:
- `generate_password_reset_token()` - Creates secure token
- `get_password_reset_token_expiry()` - Returns 1-hour expiration
- `send_password_reset_email()` - Sends formatted email (already existed)

### 4. Templates

#### `templates/forgot_password.html`
- Clean email input form
- Success/error modal messages
- Link back to login page

#### `templates/reset_password.html`
- Password input with confirmation
- Client-side password matching validation
- Token validation display
- Automatic token expiration handling
- Error state for invalid/expired tokens

#### `templates/login.html` (updated)
- Added "Forgot Password?" link above login button
- Styled consistently with existing design

## User Flow

1. **User clicks "Forgot Password?" on login page**
   → Redirected to `/forgot-password`

2. **User enters email address**
   → System generates reset token
   → Email sent with reset link
   → Success message displayed (regardless of email existence)

3. **User clicks link in email**
   → Redirected to `/reset-password?token=...`
   → Token validated (expiry, usage)

4. **User enters new password**
   → Password validated (min 8 chars, matching)
   → Password hashed and stored
   → Token marked as used
   → Redirected to login with success message

## Security Features

✅ **Secure tokens** - Uses `secrets.token_urlsafe(32)` for cryptographically secure tokens
✅ **Short expiration** - Tokens expire after 1 hour
✅ **One-time use** - Tokens marked as used and cannot be reused
✅ **No email disclosure** - Always shows success message to prevent email enumeration
✅ **Password hashing** - Uses bcrypt for secure password storage
✅ **Token validation** - Multiple checks before allowing password reset

## Configuration Requirements

The following environment variables should be set for email sending:
```bash
SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your-email@example.com
SMTP_PASSWORD=your-app-password
FROM_EMAIL=noreply@blankee.io
APP_URL=https://your-domain.com
```

## Testing Checklist

- [ ] Apply database migration
- [ ] Restart Flask application
- [ ] Test forgot password request with valid email
- [ ] Test forgot password request with invalid email (should still show success)
- [ ] Verify email is received with reset link
- [ ] Click reset link and set new password
- [ ] Verify can login with new password
- [ ] Test expired token (wait 1 hour or manually update database)
- [ ] Test reusing same token (should fail)
- [ ] Test invalid token
- [ ] Test password validation (minimum length, matching)

## Database Schema

```sql
CREATE TABLE `password_resets` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `token` varchar(255) NOT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `expires_at` datetime NOT NULL,
  `used` tinyint(1) DEFAULT '0',
  PRIMARY KEY (`id`),
  UNIQUE KEY `token` (`token`),
  KEY `user_id` (`user_id`),
  KEY `idx_token_expiry` (`token`, `expires_at`, `used`),
  CONSTRAINT `password_resets_ibfk_1` FOREIGN KEY (`user_id`) 
    REFERENCES `users` (`id`) ON DELETE CASCADE
);
```

## Maintenance

### Cleanup Old Tokens
Consider adding a periodic cleanup job to remove expired tokens:

```sql
DELETE FROM password_resets 
WHERE expires_at < NOW() OR used = 1;
```

This can be run daily via cron or as part of your application's maintenance routines.

## Future Enhancements

Consider adding:
- Rate limiting on password reset requests
- Email notification when password is changed
- Password strength meter on reset form
- Account lockout after multiple failed reset attempts
- Admin view of password reset activity
- Two-factor authentication for password resets

## Files Modified/Created

### New Files
- `/srv/blankee/migrations/add_password_reset.sql`
- `/srv/blankee/templates/forgot_password.html`
- `/srv/blankee/templates/reset_password.html`

### Modified Files
- `/srv/blankee/app.py` - Added routes and imports
- `/srv/blankee/email_utils.py` - Added helper functions
- `/srv/blankee/templates/login.html` - Added forgot password link

## Support

The implementation follows security best practices and integrates seamlessly with the existing Blankee authentication system. All templates use the existing CSS styling for consistency.
