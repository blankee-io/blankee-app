"""
Email utility functions for sending verification emails and other notifications.
"""
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
import secrets

# Email configuration - these should be set in environment variables
SMTP_SERVER = os.getenv('SMTP_SERVER', 'smtp.gmail.com')
SMTP_PORT = int(os.getenv('SMTP_PORT', 587))
SMTP_USERNAME = os.getenv('SMTP_USERNAME', '')
SMTP_PASSWORD = os.getenv('SMTP_PASSWORD', '')
FROM_EMAIL = os.getenv('FROM_EMAIL', SMTP_USERNAME)
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
    if not SMTP_USERNAME or not SMTP_PASSWORD:
        print("ERROR: Email credentials not configured. Set SMTP_USERNAME and SMTP_PASSWORD environment variables.")
        return False
    
    try:
        # Create message
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = FROM_EMAIL
        msg['To'] = to_email
        
        # Add plain text version if provided, otherwise strip HTML
        if text_content:
            part1 = MIMEText(text_content, 'plain')
            msg.attach(part1)
        
        # Add HTML version
        part2 = MIMEText(html_content, 'html')
        msg.attach(part2)
        
        # Send email
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)
        
        print(f"Email sent successfully to {to_email}")
        return True
        
    except Exception as e:
        print(f"Failed to send email to {to_email}: {str(e)}")
        return False


def generate_verification_token():
    """
    Generate a secure random token for email verification.
    
    Returns:
        str: A secure random token
    """
    return secrets.token_urlsafe(32)


def get_verification_token_expiry():
    """
    Get the expiration datetime for a verification token (24 hours from now).
    
    Returns:
        datetime: Expiration datetime
    """
    return datetime.now() + timedelta(hours=24)


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


def send_verification_email(to_email, username, verification_token):
    """
    Send an email verification link to a new user.
    
    Args:
        to_email (str): User's email address
        username (str): User's username
        verification_token (str): Unique verification token
        
    Returns:
        bool: True if email sent successfully, False otherwise
    """
    verification_url = f"{APP_URL}/verify-email?token={verification_token}"
    
    subject = "Verify Your Blankee Account"
    
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
            <h1>Welcome to Blankee!</h1>
        </div>
        <div class="content">
            <p>Hi {username},</p>
            
            <p>Thank you for registering with Blankee! We're excited to help you manage your budget.</p>
            
            <p>To complete your registration and start using your account, please verify your email address by clicking the button below:</p>
            
            <center>
                <a href="{verification_url}" class="button">Verify Email Address</a>
            </center>
            
            <p>Or copy and paste this link into your browser:</p>
            <p style="word-break: break-all; color: #2aaaa8;">{verification_url}</p>
            
            <div class="warning">
                <strong>⏰ Important:</strong> This verification link will expire in 24 hours.
            </div>
            
            <p>If you didn't create an account with Blankee, you can safely ignore this email.</p>
            
            <p>Best regards,<br>The Blankee Team</p>
        </div>
        <div class="footer">
            <p>This is an automated email. Please do not reply to this message.</p>
        </div>
    </body>
    </html>
    """
    
    text_content = f"""
    Welcome to Blankee!
    
    Hi {username},
    
    Thank you for registering with Blankee! To complete your registration and start using your account, 
    please verify your email address by visiting this link:
    
    {verification_url}
    
    This verification link will expire in 24 hours.
    
    If you didn't create an account with Blankee, you can safely ignore this email.
    
    Best regards,
    The Blankee Team
    """
    
    return send_email(to_email, subject, html_content, text_content)


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
