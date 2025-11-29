# Quiltt Session Management Guide

## Problem: Session Token Expiration

Quiltt session tokens expire after a period of time (typically 30-60 minutes). If a user stays on the page longer than the token's lifetime, they cannot connect new banks because the token is stale.

## Solution: Just-In-Time Token Generation

Instead of embedding a session token in the page when it loads, we generate a **fresh token on-demand** right before opening the Quiltt Connector.

## Implementation

### 1. Backend: New API Endpoint

**File**: `app.py`

```python
@app.route('/quiltt/get-session-token', methods=['POST'])
@login_required
def get_quiltt_session_token():
    """Get a fresh Quiltt session token for the current user"""
    # Always generates a fresh token
    # Handles both new profiles and token refresh for existing profiles
    # Saves token and expiration to database
    # Returns: {'status': 'success', 'session_token': 'token...'}
```

**Key Features**:
- ✅ Always generates a fresh token (never uses cached/expired tokens)
- ✅ Automatically creates new profiles or refreshes existing ones
- ✅ Saves token and expiration time to database
- ✅ Protected by `@login_required` - only authenticated users

### 2. Frontend: Fetch Token Before Opening Connector

**File**: `templates/profile.html`

```javascript
// OLD WAY (❌ Token can expire)
const sessionToken = '{{ session_token }}'; // Embedded at page load
openQuilttConnector(sessionToken, connectorId);

// NEW WAY (✅ Always fresh token)
$.ajax({
    url: '/quiltt/get-session-token',
    method: 'POST',
    success: function(response) {
        const freshToken = response.session_token;
        openQuilttConnector(freshToken, connectorId);
    }
});
```

**Key Features**:
- ✅ Token is fetched only when user clicks "Connect Bank Account"
- ✅ Token is always fresh and valid
- ✅ Graceful error handling with user-friendly messages
- ✅ Console logging for debugging

## How It Works

### Flow Diagram

```
User clicks "Connect Bank Account"
    ↓
Frontend: POST /quiltt/get-session-token
    ↓
Backend: Check if user has Quiltt profile
    ├─ YES → Call quiltt_client.refresh_session_token()
    └─ NO  → Call quiltt_client.create_session_token()
    ↓
Backend: Save new token + expiration to database
    ↓
Backend: Return {'status': 'success', 'session_token': '...'}
    ↓
Frontend: Receive fresh token
    ↓
Frontend: Load Quiltt SDK (if needed)
    ↓
Frontend: Open Quiltt Connector with fresh token
    ↓
User connects their bank ✓
```

## Benefits

1. **No More Expired Tokens**: Users can stay on the page indefinitely
2. **Better Security**: Tokens are short-lived and regenerated frequently
3. **Improved UX**: Users don't see cryptic token expiration errors
4. **Database Tracking**: Token expiration times are recorded for monitoring

## Database Schema

The `quiltt_profiles` table tracks session tokens:

```sql
CREATE TABLE quiltt_profiles (
    id INT PRIMARY KEY AUTO_INCREMENT,
    user_id INT NOT NULL,
    profile_id VARCHAR(255) NOT NULL,        -- Quiltt's Profile ID
    session_token VARCHAR(512),               -- Current token (refreshed often)
    session_expires_at DATETIME,             -- When token expires
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_modified DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);
```

## Quiltt API Endpoints Used

### 1. Create Session Token (New Profile)
```http
POST https://auth.quiltt.io/v1/users/sessions
Authorization: Bearer {API_KEY}
Content-Type: application/json

{}  # Empty payload creates new profile
```

**Response**:
```json
{
    "token": "qt_...",
    "userId": "p_...",          # This is the Profile ID
    "expiresAt": "2025-11-28T15:30:00Z"
}
```

### 2. Refresh Session Token (Existing Profile)
```http
POST https://auth.quiltt.io/v1/users/sessions
Authorization: Bearer {API_KEY}
Content-Type: application/json

{
    "userId": "p_..."  # Include profile ID to refresh
}
```

**Response**: Same as above, but returns fresh token for same profile

## Best Practices

### ✅ DO

- **Always fetch fresh tokens** right before opening Connector
- **Handle errors gracefully** with user-friendly messages
- **Log token operations** for debugging
- **Store expiration times** in the database
- **Use separate API keys** for dev/staging/production

### ❌ DON'T

- **Don't embed tokens** in page HTML at load time
- **Don't reuse old tokens** from previous sessions
- **Don't ignore expiration times** in the database
- **Don't commit API keys** to version control
- **Don't use production keys** in development

## Error Handling

### Common Errors

**1. API Key Not Configured**
```
Error: QUILTT_API_KEY not set in environment variables
Solution: Add to .env or server environment
```

**2. Token Expired**
```
Error: Session token expired
Solution: (Now handled automatically - fetches fresh token)
```

**3. Profile Not Found**
```
Error: Profile p_xyz not found
Solution: Delete from quiltt_profiles table, will create new profile
```

## Monitoring

### Check Token Status

```sql
-- See all active session tokens
SELECT 
    user_id,
    profile_id,
    session_expires_at,
    TIMESTAMPDIFF(MINUTE, NOW(), session_expires_at) as minutes_until_expiry
FROM quiltt_profiles
WHERE session_expires_at > NOW();
```

### Check Recent Token Refreshes

```sql
-- See recently refreshed tokens
SELECT 
    user_id,
    profile_id,
    last_modified
FROM quiltt_profiles
WHERE last_modified > DATE_SUB(NOW(), INTERVAL 1 HOUR)
ORDER BY last_modified DESC;
```

## Testing

### Test Session Token Generation

1. Open browser console
2. Click "Connect Bank Account"
3. Check console logs:
   ```
   Connect Bank Account clicked
   Fetching fresh session token...
   Fresh session token received
   Loading Quiltt Connector SDK...
   Quiltt SDK loaded successfully
   ```

### Test With Expired Token

1. Manually set `session_expires_at` to past date:
   ```sql
   UPDATE quiltt_profiles 
   SET session_expires_at = '2020-01-01 00:00:00'
   WHERE user_id = X;
   ```
2. Click "Connect Bank Account"
3. Should generate fresh token automatically

## Migration from Old Implementation

If you were previously embedding tokens in the page:

1. ✅ Add the new `/quiltt/get-session-token` endpoint
2. ✅ Update frontend JavaScript to fetch token on-demand
3. ✅ Remove `session_token` from profile route's template context (optional - not breaking)
4. ✅ Test with existing users who have profiles
5. ✅ Test with new users who don't have profiles yet

## References

- [Quiltt Documentation](https://docs.quiltt.io/)
- [Quiltt Auth API](https://docs.quiltt.io/api/auth)
- [Quiltt Connector](https://docs.quiltt.io/connector)
- Project Quick Start: `QUILTT_QUICK_START.md`
- Setup Guide: `QUILTT_SETUP_GUIDE.md`

---

**Last Updated**: November 28, 2025  
**Implementation Status**: ✅ Complete
