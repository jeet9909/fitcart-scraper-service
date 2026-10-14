# Email codes for sign-up and password reset

New accounts confirm their email with a 6-digit code, and a forgotten password can be reset with a code.
Supabase Auth sends and checks the codes; the API only passes them through. Everything is off until
`EMAIL_VERIFICATION_ENABLED=true` is set on the API, so do the steps below first.

## 1. Custom SMTP (required)

Supabase's built-in email sender only delivers to your own team's addresses (and about 2 emails an hour), so real
shoppers would never get a code.

1. Create an account with an email provider, for example Resend, Brevo, Amazon SES or SendGrid. Verify the domain
   `mydripcheck.com` there (it gives you DNS records to add).
2. Supabase → **Authentication → Emails → SMTP Settings** → turn on **Enable custom SMTP**. Fill in host, port, user and
   password from the provider; sender email `no-reply@mydripcheck.com`, sender name `MyDripCheck`. Save.
3. Supabase → **Authentication → Rate Limits** → raise **emails sent per hour** (for example 100).

## 2. Put the code in the emails

Supabase → **Authentication → Emails → Templates**:

- **Confirm signup**, subject `Your MyDripCheck code`, body for example:
  ```html
  <h2>Confirm your email</h2>
  <p>Your MyDripCheck code is:</p>
  <p style="font-size:28px;font-weight:700;letter-spacing:6px">{{ .Token }}</p>
  <p>It expires in 1 hour. If you didn't create an account, ignore this email.</p>
  ```
- **Reset Password**, subject `Reset your MyDripCheck password`, same body with "Use this code to set a new password".

## 3. Turn on email confirmation

Supabase → **Authentication → Sign In / Providers → Email**: turn on **Confirm email**. Leave **Email OTP Length** at 6
and **Email OTP Expiration** at 3600 seconds.

## 4. Switch it on

Render → API service → **Environment** → add `EMAIL_VERIFICATION_ENABLED` = `true` → **Save** (it redeploys).
Test: create an account with a new email, get the code, confirm; then try **Forgot password?**.

To switch it off again, set it to `false`: sign-up goes back to signing in straight away. Accounts that never entered
their code can then be confirmed from Supabase → Authentication → Users.
