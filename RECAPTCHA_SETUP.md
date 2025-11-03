# Google reCAPTCHA Setup Guide

## Step 1: Get Your reCAPTCHA Keys

1. Go to https://www.google.com/recaptcha/admin
2. Log in with your Google account
3. Click "+" to register a new site
4. Fill in the form:
   - **Label**: Give your site a name (e.g., "Blankee Registration")
   - **reCAPTCHA type**: Select "reCAPTCHA v2" → "I'm not a robot" checkbox
   - **Domains**: Add your domain (e.g., `blankee.io` or `localhost` for testing)
   - Accept the terms of service
5. Click "Submit"
6. You'll receive:
   - **Site Key** (public key - goes in your HTML)
   - **Secret Key** (private key - goes in your backend)

## Step 2: Update the Frontend

In `/srv/blankee/templates/register.html`, replace `YOUR_SITE_KEY_HERE` with your actual Site Key:

```html
<div class="g-recaptcha" data-sitekey="YOUR_ACTUAL_SITE_KEY" data-callback="recaptchaCallback"></div>
```

## Step 3: Update the Backend

You need to verify the reCAPTCHA response on your server. In your Flask route that handles registration (likely in `app.py`), add verification:

```python
import requests

@app.route('/register', methods=['POST'])
def register():
    # Get the reCAPTCHA response
    recaptcha_response = request.form.get('g-recaptcha-response')
    
    # Verify with Google
    secret_key = 'YOUR_SECRET_KEY_HERE'  # Store this securely, preferably in environment variables
    verify_url = 'https://www.google.com/recaptcha/api/siteverify'
    
    verify_data = {
        'secret': secret_key,
        'response': recaptcha_response
    }
    
    verify_response = requests.post(verify_url, data=verify_data)
    result = verify_response.json()
    
    if not result.get('success'):
        # reCAPTCHA verification failed
        return jsonify({'error': 'Please complete the CAPTCHA'}), 400
    
    # Continue with registration...
```

## Step 4: Security Best Practices

1. **Never expose your Secret Key** in client-side code
2. Store the Secret Key in environment variables:
   ```python
   import os
   secret_key = os.environ.get('RECAPTCHA_SECRET_KEY')
   ```
3. Always verify reCAPTCHA on the server-side (never trust client-side validation alone)

## Alternative: hCaptcha

If you prefer hCaptcha (privacy-focused alternative):

1. Sign up at https://www.hcaptcha.com/
2. Replace the script tag with:
   ```html
   <script src="https://js.hcaptcha.com/1/api.js" async defer></script>
   ```
3. Replace the div with:
   ```html
   <div class="h-captcha" data-sitekey="YOUR_HCAPTCHA_SITE_KEY" data-callback="recaptchaCallback"></div>
   ```
4. Verify at `https://hcaptcha.com/siteverify`

## Testing Locally

For local testing, you can add `localhost` or `127.0.0.1` to your reCAPTCHA domains list in the Google admin console.

## Current Status

✅ Frontend implemented with reCAPTCHA widget
⚠️ Backend verification needed - update your Flask registration route
🔑 Replace `YOUR_SITE_KEY_HERE` in register.html with your actual Site Key
