"""
Email utility functions for password resets and notifications.
"""
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
import secrets
from log_config import get_logger, log_info, log_error

logger = get_logger(__name__)

# SMTP configuration is NOT read here any more. It is resolved per send by
# instance_settings.get_smtp_config(), which reads the instance_settings table
# and nothing else - there is no environment fallback, so mail is configured in
# the app or not at all. Resolving per send is also what lets a settings change
# take effect without restarting the process.
#
# APP_URL stays here: it is a link base rather than mail transport, and it is
# not a secret.
APP_URL = os.getenv('APP_URL', 'http://localhost:5000')

def send_email(to_email, subject, html_content, text_content=None):
    """
    Send an email using SMTP.
    
    Args:
        to_email (str): Recipient email address
        subject (str): Email subject
        html_content (str): HTML content of the email
        text_content (str, optional): Plain text fallback content
        
    Returns:
        bool: True if email sent successfully, False otherwise
    """
    from instance_settings import get_smtp_config
    cfg = get_smtp_config()

    if not cfg['configured']:
        log_error(logger, 'EMAIL',
                  'Email is not configured, so nothing was sent. Add the mail server, '
                  'address, username and password under Email Delivery on the settings page.')
        return False

    if not to_email:
        log_error(logger, 'EMAIL', 'No recipient address; nothing sent.')
        return False

    try:
        # Create message
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = cfg['from_email']
        msg['To'] = to_email
        
        # Add plain text version if provided, otherwise strip HTML
        if text_content:
            part1 = MIMEText(text_content, 'plain')
            msg.attach(part1)
        
        # Add HTML version
        part2 = MIMEText(html_content, 'html')
        msg.attach(part2)
        
        # Send email
        with smtplib.SMTP(cfg['server'], cfg['port']) as server:
            if cfg['use_tls']:
                server.starttls()
            server.login(cfg['username'], cfg['password'])
            server.send_message(msg)

        # No source to report any more - there is only one.
        log_info(logger, 'EMAIL',
                 f"Email sent successfully to {to_email} via {cfg['server']}:{cfg['port']}")
        return True
        
    except Exception as e:
        log_error(logger, 'EMAIL', f'Failed to send email to {to_email}: {str(e)}')
        return False


def generate_password_reset_token():
    """
    Generate a secure random token for password reset.
    
    Returns:
        str: A secure random token
    """
    return secrets.token_urlsafe(32)


def get_password_reset_token_expiry():
    """
    Get the expiration datetime for a password reset token (1 hour from now).
    
    Returns:
        datetime: Expiration datetime
    """
    return datetime.now() + timedelta(hours=1)


def send_password_reset_email(to_email, username, reset_token):
    """
    Send a password reset link to a user.
    
    Args:
        to_email (str): User's email address
        username (str): User's username
        reset_token (str): Unique reset token
        
    Returns:
        bool: True if email sent successfully, False otherwise
    """
    reset_url = f"{APP_URL}/reset-password?token={reset_token}"
    
    subject = "Reset Your Blankee Password"
    
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{
                font-family: 'Nunito', Arial, sans-serif;
                line-height: 1.6;
                color: #333;
                max-width: 600px;
                margin: 0 auto;
                padding: 20px;
            }}
            .header {{
                background-color: #2aaaa8;
                color: white;
                padding: 30px;
                text-align: center;
                border-radius: 10px 10px 0 0;
            }}
            .content {{
                background-color: #f5fffe;
                padding: 30px;
                border-radius: 0 0 10px 10px;
            }}
            .button {{
                display: inline-block;
                background-color: #2aaaa8;
                color: white;
                padding: 15px 30px;
                text-decoration: none;
                border-radius: 5px;
                margin: 20px 0;
                font-weight: bold;
            }}
            .footer {{
                text-align: center;
                margin-top: 20px;
                color: #666;
                font-size: 12px;
            }}
            .warning {{
                background-color: #fff3cd;
                border-left: 4px solid #ffc107;
                padding: 15px;
                margin: 20px 0;
            }}
        </style>
    </head>
    <body>
        <div class="header">
            <h1>Password Reset Request</h1>
        </div>
        <div class="content">
            <p>Hi {username},</p>
            
            <p>We received a request to reset your Blankee account password.</p>
            
            <p>To reset your password, click the button below:</p>
            
            <center>
                <a href="{reset_url}" class="button">Reset Password</a>
            </center>
            
            <p>Or copy and paste this link into your browser:</p>
            <p style="word-break: break-all; color: #2aaaa8;">{reset_url}</p>
            
            <div class="warning">
                <strong>⏰ Important:</strong> This password reset link will expire in 1 hour.
            </div>
            
            <p>If you didn't request a password reset, you can safely ignore this email. Your password will remain unchanged.</p>
            
            <p>Best regards,<br>The Blankee Team</p>
        </div>
        <div class="footer">
            <p>This is an automated email. Please do not reply to this message.</p>
        </div>
    </body>
    </html>
    """
    
    text_content = f"""
    Password Reset Request
    
    Hi {username},
    
    We received a request to reset your Blankee account password.
    
    To reset your password, visit this link:
    
    {reset_url}
    
    This password reset link will expire in 1 hour.
    
    If you didn't request a password reset, you can safely ignore this email.
    
    Best regards,
    The Blankee Team
    """
    
    return send_email(to_email, subject, html_content, text_content)

def send_notification_email(to_email, user_name, message, notification_date):
    """
    Send a notification email to the user.
    
    Args:
        to_email (str): Recipient email address
        user_name (str): User's first name
        message (str): The notification message
        notification_date (datetime): When the notification was created
        
    Returns:
        bool: True if email sent successfully, False otherwise
    """
    subject = "Blankee Notification"
    
    formatted_date = notification_date.strftime('%B %d, %Y at %I:%M %p')
    
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
                line-height: 1.6;
                color: #333;
                max-width: 600px;
                margin: 0 auto;
                padding: 20px;
            }}
            .container {{
                background-color: #ffffff;
                border-radius: 10px;
                padding: 30px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            }}
            .header {{
                text-align: center;
                margin-bottom: 30px;
            }}
            .header h1 {{
                color: #2aaaa8;
                margin: 0;
                font-size: 28px;
            }}
            .notification-box {{
                background-color: #f8f9fa;
                border-left: 4px solid #2aaaa8;
                padding: 20px;
                margin: 20px 0;
                border-radius: 4px;
            }}
            .notification-date {{
                color: #666;
                font-size: 13px;
                margin-bottom: 10px;
            }}
            .notification-message {{
                color: #333;
                font-size: 15px;
                line-height: 1.6;
            }}
            .footer {{
                margin-top: 30px;
                padding-top: 20px;
                border-top: 1px solid #eee;
                text-align: center;
                color: #666;
                font-size: 12px;
            }}
            .button {{
                display: inline-block;
                padding: 12px 30px;
                background-color: #2aaaa8;
                color: white !important;
                text-decoration: none;
                border-radius: 5px;
                margin: 20px 0;
                font-weight: bold;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>🔔 Blankee Notification</h1>
            </div>
            
            <p>Hi {user_name},</p>
            
            <p>You have a new notification from your Blankee budget tracker:</p>
            
            <div class="notification-box">
                <div class="notification-date">{formatted_date}</div>
                <div class="notification-message">{message}</div>
            </div>
            
            <center>
                <a href="{APP_URL}/notifications" class="button">View All Notifications</a>
            </center>
            
            <p>Best regards,<br>The Blankee Team</p>
        </div>
        <div class="footer">
            <p>You're receiving this email because you have email notifications enabled.</p>
            <p>To manage your notification preferences, visit Settings in your Blankee account.</p>
            <p>This is an automated email. Please do not reply to this message.</p>
        </div>
    </body>
    </html>
    """
    
    text_content = f"""
    Blankee Notification
    
    Hi {user_name},
    
    You have a new notification from your Blankee budget tracker:
    
    Date: {formatted_date}
    
    {message}
    
    View all notifications: {APP_URL}/notifications
    
    Best regards,
    The Blankee Team
    
    ---
    You're receiving this email because you have email notifications enabled.
    To manage your notification preferences, visit Settings in your Blankee account.
    """
    
    return send_email(to_email, subject, html_content, text_content)


def send_notification_email_for_user(user, message, notification_date, kind=None):
    """
    Send one notification email, if this user has opted in and a destination
    exists. Returns True when a message was actually handed to SMTP.

    This exists because the opt-in check, the recipient decision and the send
    were previously duplicated in app.py and bucket_utils.py. Two copies of the
    same rule is how they drift, and the recipient rule in particular is the one
    most likely to change (see instance_settings.get_notification_recipient).

    Recipient is the instance mailbox, not user['email']: notifications are sent
    from and to the configured address. The per-user opt-in still gates it.

    kind names which sort of notification this is, so the per-type switches can
    narrow what gets sent - see notification_kinds. The gate belongs here rather
    than at each call site for the same reason the opt-in does: three senders,
    one rule. Omitting it sends, which keeps a caller that has not been given a
    kind yet working rather than silently muting it.
    """
    if not user or not user.get('email_notifications'):
        return False

    from notification_kinds import emails_enabled_for
    if not emails_enabled_for(user, kind):
        log_info(logger, 'EMAIL',
                 f"Notification of kind {kind!r} not emailed: the user has that "
                 f"type switched off")
        return False

    from instance_settings import get_notification_recipient, get_smtp_config
    recipient = get_notification_recipient()
    if not recipient:
        # Two different causes, and telling them apart in the log saves a real
        # debugging session: nothing filled in, versus filled in but never
        # proven to receive mail.
        if get_smtp_config()['configured']:
            log_error(logger, 'EMAIL',
                      'Notification not emailed: the mail settings have not been verified. '
                      'Save them under Email Delivery in Settings and enter the code.')
        else:
            log_error(logger, 'EMAIL',
                      'Notification not emailed: no mail settings are configured. '
                      'Add them under Email Delivery in Settings.')
        return False

    user_name = user.get('first_name') or 'User'
    return send_notification_email(recipient, user_name, message, notification_date)
def send_smtp_verification_email(to_email, code):
    """
    Email the verification code to the address being configured.

    Sent through the settings that were just saved, which is the whole design:
    arrival proves the configuration works, so there is no separate "test" step
    that could pass while real delivery fails.

    Note this does NOT go through get_notification_recipient() - that refuses
    until verification succeeds, which would make verification impossible. The
    address comes straight from the saved config instead.
    """
    subject = 'Your Blankee email verification code'

    # Same shell as send_notification_email - white card, teal heading, the grey
    # box with a teal left border, the same footer rule - so the mail a user gets
    # while setting delivery up looks like the mail delivery will actually send.
    # It was Arial with a purple heading, matching nothing else.
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
                line-height: 1.6;
                color: #333;
                max-width: 600px;
                margin: 0 auto;
                padding: 20px;
            }}
            .container {{
                background-color: #ffffff;
                border-radius: 10px;
                padding: 30px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            }}
            .header {{
                text-align: center;
                margin-bottom: 30px;
            }}
            .header h1 {{
                color: #2aaaa8;
                margin: 0;
                font-size: 28px;
            }}
            .code-box {{
                background-color: #f8f9fa;
                border-left: 4px solid #2aaaa8;
                padding: 20px;
                margin: 20px 0;
                border-radius: 4px;
                text-align: center;
            }}
            .code {{
                color: #2aaaa8;
                font-size: 32px;
                font-weight: bold;
                letter-spacing: 6px;
            }}
            .note {{
                color: #666;
                font-size: 13px;
            }}
            .footer {{
                margin-top: 30px;
                padding-top: 20px;
                border-top: 1px solid #eee;
                text-align: center;
                color: #666;
                font-size: 12px;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>Verify this address</h1>
            </div>

            <p>Enter this code in Blankee to confirm that notification emails
               reach this mailbox:</p>

            <div class="code-box">
                <div class="code">{code}</div>
            </div>

            <p class="note">The code is valid for about five minutes.</p>

            <p>If you did not just save email settings in Blankee, you can ignore
               this message - but you may want to check who has access to the
               instance.</p>
        </div>
        <div class="footer">
            <p>This message was sent through the mail settings that were just
               saved, which is how its arrival proves they work.</p>
            <p>This is an automated email. Please do not reply to this message.</p>
        </div>
    </body>
    </html>
    """
    text = (f'Your Blankee verification code is {code}\n\n'
            'Enter it in Blankee to confirm that notification emails reach this '
            'mailbox. It is valid for about five minutes.')
    return send_email(to_email, subject, html, text)

