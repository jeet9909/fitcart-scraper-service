# Email confirmation and password reset (links)

New accounts confirm their email with a link, and a forgotten password is reset with a link. Supabase Auth sends
and checks the links; the site trades the link for a MyDripCheck session. Everything is off until
`EMAIL_VERIFICATION_ENABLED=true` is set on the API, so do the steps below first.

## 1. Custom SMTP (required)

Supabase's built-in email sender only delivers to your own team's addresses (and about 2 emails an hour), so real
shoppers would never get the email.

1. Create an account with an email provider, for example Resend, Brevo, Amazon SES or SendGrid. Verify the domain
   `mydripcheck.com` there (it gives you DNS records to add).
2. Supabase → **Authentication → Emails → SMTP Settings** → turn on **Enable custom SMTP**. Fill in host, port, user and
   password from the provider; sender email `no-reply@mydripcheck.com`, sender name `MyDripCheck`. Save.
3. Supabase → **Authentication → Rate Limits** → raise **emails sent per hour** (for example 100).

## 2. Links back to the site

New accounts confirm their email with a **link** (Supabase's default "Confirm signup" email). The link signs the
user in on mydripcheck.com and brings them back to their try-on with their look and photo.

Supabase → **Authentication → URL Configuration**:

- **Site URL**: `https://mydripcheck.com`
- **Redirect URLs**: add `https://mydripcheck.com/**` and `https://www.mydripcheck.com/**`

Keep the default templates (they use `{{ .ConfirmationURL }}`). Optional: reword the subject and text in
**Authentication → Emails → Templates** ("Confirm your MyDripCheck account", "Reset your MyDripCheck password").
The site also still accepts 6-digit codes through the API if a template ever uses `{{ .Token }}` instead.

## 3. Turn on email confirmation

Supabase → **Authentication → Sign In / Providers → Email**: turn on **Confirm email**. Leave **Email OTP Expiration** at 3600 seconds
(how long a link works).

## 4. Switch it on

Render → API service → **Environment** → add `EMAIL_VERIFICATION_ENABLED` = `true` → **Save** (it redeploys).
Test: build a look, tap **Create free account to try on**, create an account with a new email, open the link in the
email, and check you land back on your look signed in. Then try **Forgot password?**.

To switch it off again, set it to `false`: sign-up goes back to signing in straight away. Accounts that never opened
their link can then be confirmed from Supabase → Authentication → Users.
