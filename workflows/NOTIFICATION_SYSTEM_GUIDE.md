# Notification System Guide

## Overview

This guide explains how to create notifications and update the notification badge in the Blankee application.

---

## Table of Contents

1. [System Architecture](#system-architecture)
2. [Creating Notifications](#creating-notifications)
3. [Updating the Notification Badge](#updating-the-notification-badge)
4. [Complete Integration Example](#complete-integration-example)
5. [API Reference](#api-reference)

---

## System Architecture

### Components

1. **Backend (Python/Flask)**
   - `add_notification()` - Creates notifications in MySQL
   - `check_negative_remainders()` - Auto-creates overdraft warnings
   - `/get-unread-notification-count` - Returns current unread count
   - Context processor - Injects unread count into all templates

2. **Frontend (JavaScript)**
   - `refreshNotificationBadge()` - Updates the red dot badge
   - AJAX handlers in notifications.html - Mark read/delete functionality

3. **Database (MySQL)**
   - `notifications` table with user_id, date, message, is_read fields

### APNs Push for iOS WebView

- Backend stores device tokens in `device_tokens` (see migrations/add_device_tokens.sql).
- Env vars required: `APNS_KEY_PATH`, `APNS_KEY_ID`, `APNS_TEAM_ID`, `APNS_TOPIC`, `APNS_USE_SANDBOX` (true for dev).
- Register token: `POST /api/notifications/register` with JSON `{ "deviceToken": "<token>", "platform": "ios", "deviceInfo": {...} }` while authenticated in the webview.
- Unregister token: `POST /api/notifications/unregister` with JSON `{ "deviceToken": "<token>" }`.
- Delivery: `add_notification()` now sends APNs pushes (badge = unread count) and prunes invalid tokens automatically.
- The lock-screen text uses the notification message and the title "Blankee".

---

## Creating Notifications

### Method 1: Server-Side (Python)

Use the `add_notification()` function in your Flask routes:

```python
from app import add_notification

# Basic notification
add_notification(user_id=current_user.id, message="Your action was successful!")

# Notification with specific date
from datetime import date
notification_date = date(2025, 11, 15)
add_notification(
    user_id=current_user.id,
    message="Reminder: Payment due soon",
    notification_date=notification_date
)

# Notification with HTML link (use Markup for safety)
from markupsafe import Markup
message = Markup('On <a href="/dashboard_d?year=2025&month=10&day=15">2025-11-15</a> you will overdraft.')
add_notification(user_id=current_user.id, message=message)
```

### Method 2: Automatic Overdraft Warnings

The system automatically creates overdraft notifications when:
- `check_negative_remainders(user_id)` is called
- It detects the first negative remainder in the next 90 days
- No duplicate notification exists for that date within 7 days

This function is automatically called in:
- `/save_totals_remainders_d` route (after saving daily totals)

```python
# Called automatically, but you can trigger manually:
check_negative_remainders(current_user.id)
```

---

## Updating the Notification Badge

### The Red Dot Badge

The notification badge (red dot) appears on the bell icon in the navigation bar when there are unread notifications.

### Method 1: Automatic Update (Preferred)

The badge updates automatically in these scenarios:

1. **After saving totals** - Badge refreshes when notifications are created
2. **After marking as read** - Badge disappears when last unread is read
3. **After deleting notifications** - Badge updates based on remaining unread count
4. **On page load** - Context processor injects current count

### Method 2: Manual Update (JavaScript)

Call the global `refreshNotificationBadge()` function from any JavaScript code:

```javascript
// After creating a notification or performing an action
if (typeof refreshNotificationBadge === 'function') {
    refreshNotificationBadge();
}
```

### How It Works

The `refreshNotificationBadge()` function:
1. Makes an AJAX call to `/get-unread-notification-count`
2. Receives the count of unread notifications
3. Adds the red dot if count > 0
4. Removes the red dot if count = 0

---

## Complete Integration Example

### Scenario: Adding a New Feature That Creates Notifications

Let's say you want to add a notification when a user creates a new budget category.

#### Step 1: Create the notification in your Flask route

```python
@app.route('/create_category', methods=['POST'])
@login_required
def create_category():
    data = request.get_json()
    category_name = data.get('name')
    
    # Your existing category creation logic here
    # ...
    
    # Add notification
    add_notification(
        user_id=current_user.id,
        message=f"New category '{category_name}' created successfully!"
    )
    
    return jsonify({'status': 'success'})
```

#### Step 2: Update the badge in your JavaScript success handler

```javascript
$.ajax({
    url: '/create_category',
    method: 'POST',
    contentType: 'application/json',
    data: JSON.stringify({ name: categoryName }),
    success: function(response) {
        if (response.status === 'success') {
            // Refresh the notification badge
            if (typeof refreshNotificationBadge === 'function') {
                refreshNotificationBadge();
            }
            
            // Continue with your success logic
            alert('Category created!');
            location.reload();
        }
    },
    error: function() {
        alert('Failed to create category');
    }
});
```

---

## API Reference

### Backend Functions

#### `add_notification(user_id, message, notification_date=None)`

Creates a new notification in the database.

**Parameters:**
- `user_id` (int, required) - The ID of the user receiving the notification
- `message` (str, required) - The notification message (can include HTML)
- `notification_date` (date, optional) - Specific date for the notification (defaults to current timestamp)

**Returns:**
- `int` - The ID of the created notification

**Example:**
```python
notification_id = add_notification(
    user_id=current_user.id,
    message="Your budget is ready!",
    notification_date=date.today()
)
```

---

#### `check_negative_remainders(user_id)`

Automatically checks for upcoming negative remainders and creates notifications.

**Parameters:**
- `user_id` (int, required) - The ID of the user to check

**Behavior:**
- Scans the next 90 days for negative remainders
- Creates notification for the FIRST negative occurrence only
- Prevents duplicates (checks for existing notifications within 7 days)
- Includes link to dashboard_d for the specific date

**Example:**
```python
check_negative_remainders(current_user.id)
```

---

### Frontend Functions

#### `refreshNotificationBadge()`

Updates the notification badge based on current unread count.

**Parameters:** None

**Returns:** None (updates DOM directly)

**Example:**
```javascript
// Call after any action that might create notifications
if (typeof refreshNotificationBadge === 'function') {
    refreshNotificationBadge();
}
```

---

### API Endpoints

#### `GET /get-unread-notification-count`

Returns the number of unread notifications for the current user.

**Authentication:** Required (login_required)

**Response:**
```json
{
    "count": 3
}
```

**Example:**
```javascript
$.ajax({
    url: '/get-unread-notification-count',
    method: 'GET',
    success: function(response) {
        console.log('Unread notifications:', response.count);
    }
});
```

---

#### `POST /mark-notification-read`

Marks a notification as read.

**Authentication:** Required (login_required)

**Request Body:**
```json
{
    "notification_id": 123
}
```

**Response:**
```json
{
    "success": true
}
```

---

#### `POST /delete-notification`

Deletes a specific notification.

**Authentication:** Required (login_required)

**Request Body:**
```json
{
    "notification_id": 123
}
```

**Response:**
```json
{
    "success": true
}
```

---

#### `POST /clear-read-notifications`

Deletes all read notifications for the current user.

**Authentication:** Required (login_required)

**Response:**
```json
{
    "success": true,
    "deleted_count": 5
}
```

---

## Database Schema

### `notifications` Table

| Column | Type | Description |
|--------|------|-------------|
| `id` | INT AUTO_INCREMENT | Primary key |
| `user_id` | INT | Foreign key to users table |
| `date` | DATETIME | Notification timestamp |
| `message` | TEXT | Notification content (can include HTML) |
| `is_read` | TINYINT(1) | Read status (0=unread, 1=read) |
| `created_at` | DATETIME | Record creation timestamp |

**Indexes:**
- `user_id` - For efficient user queries
- `(user_id, date)` - Composite index for date-based queries

---

## Best Practices

### 1. Always Update the Badge

When creating notifications from AJAX calls, always refresh the badge:

```javascript
success: function(response) {
    if (response.status === 'success') {
        // Refresh badge immediately
        if (typeof refreshNotificationBadge === 'function') {
            refreshNotificationBadge();
        }
        // ... rest of success handling
    }
}
```

### 2. Use HTML Links Safely

When including links in notifications, use `Markup` to ensure safety:

```python
from markupsafe import Markup

message = Markup(f'Check <a href="{url}">this page</a>.')
add_notification(user_id, message)
```

### 3. Avoid Duplicate Notifications

Before creating notifications, consider if one already exists:

```python
# Check for existing notification
with get_db_pool().get_connection() as conn:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id FROM notifications
        WHERE user_id = %s 
        AND message LIKE %s
        AND date > DATE_SUB(NOW(), INTERVAL 7 DAY)
    """, (user_id, f"%{key_phrase}%"))
    
    if not cursor.fetchone():
        add_notification(user_id, message)
    cursor.close()
```

### 4. Test Badge Updates

After implementing notification creation, test:
1. Notification appears in `/notifications` page
2. Badge appears on bell icon
3. Badge disappears when notification is marked as read
4. Badge updates without page reload

---

## Troubleshooting

### Badge Not Appearing

**Problem:** Notification created but badge doesn't show

**Solutions:**
1. Check if `refreshNotificationBadge()` is being called
2. Verify `/get-unread-notification-count` returns count > 0
3. Check browser console for JavaScript errors
4. Ensure `script.js` is loaded before the page script

### Badge Not Disappearing

**Problem:** Badge remains after marking all as read

**Solutions:**
1. Check if `updateNotificationBadge()` is called in mark-as-read handler
2. Verify `is_read` field is actually updated in database
3. Clear browser cache and reload

### Duplicate Notifications

**Problem:** Same notification appears multiple times

**Solutions:**
1. Add duplicate checking before creating notifications
2. Use the pattern in `check_negative_remainders()` as a template
3. Check for notifications within a time window (e.g., 7 days)

---

## Files Modified in This System

### Backend
- `/srv/blankee/app.py`
  - `add_notification()` function
  - `check_negative_remainders()` function
  - `inject_unread_notifications()` context processor
  - `/notifications` route
  - `/mark-notification-read` route
  - `/delete-notification` route
  - `/clear-read-notifications` route
  - `/get-unread-notification-count` route

### Frontend Templates
- `/srv/blankee/templates/nav.html` - Badge display
- `/srv/blankee/templates/notifications.html` - Notification management UI
- `/srv/blankee/templates/dashboard_3m.html` - Badge refresh
- `/srv/blankee/templates/dashboard_m.html` - Badge refresh
- `/srv/blankee/templates/dashboard_d.html` - Badge refresh
- `/srv/blankee/templates/dashboard_y.html` - Badge refresh
- `/srv/blankee/templates/dashboard.html` - Badge refresh
- `/srv/blankee/templates/buds.html` - Badge refresh
- `/srv/blankee/templates/recurring_i.html` - Badge refresh
- `/srv/blankee/templates/recurring_e.html` - Badge refresh
- `/srv/blankee/templates/settings.html` - Badge refresh
- `/srv/blankee/templates/setup_profile.html` - Badge refresh

### JavaScript
- `/srv/blankee/static/js/script.js` - `refreshNotificationBadge()` function

### CSS
- `/srv/blankee/static/css/style.css` - Badge and notification styling

### Database
- `/srv/blankee/migrations/schema.sql` - `notifications` table definition

---

## Future Enhancements

Potential improvements to the notification system:

1. **Real-time Updates** - WebSocket integration for instant badge updates
2. **Notification Preferences** - User settings for notification types
3. **Notification Categories** - Group notifications by type (info, warning, error)
4. **Push Notifications** - Browser push notifications for important alerts
5. **Notification History** - Archive instead of delete
6. **Batch Notifications** - Combine similar notifications
7. **Notification Sound** - Optional audio alerts
8. **Notification Actions** - Quick action buttons in notifications

---

## Support

For questions or issues with the notification system, please refer to:
- Main project documentation
- Database schema: `/srv/blankee/migrations/schema.sql`
- Redis keys documentation: `/srv/blankee/migrations/redis_keys.sql`
