# Continue with Google

The sign-in and create-account screens show **Continue with Google** / **Sign up with Google** once
`GOOGLE_SIGN_IN_ENABLED=true` is set on the API. Supabase Auth handles Google; the API sends the browser to
Supabase's Google sign-in and trades the token it brings back for a MyDripCheck session (the same
`/v1/auth/confirmed` step as the email link). Someone who already made an account with the same Gmail address
signs in to that same account: Supabase links the two by email.

## 1. Google Cloud (about 10 minutes)

1. console.cloud.google.com → project picker → **New project** → name `MyDripCheck` → Create.
2. **APIs & Services → OAuth consent screen** (or **Google Auth Platform → Branding**):
   - User type **External**; app name `MyDripCheck`; support email: yours; logo optional.
   - **Authorized domains**: `mydripcheck.com` and `supabase.co`.
   - Developer contact: your email. Save.
   - **Data access / Scopes**: add `openid`, `.../auth/userinfo.email`, `.../auth/userinfo.profile`.
   - **Audience / Publishing status**: **Publish app** (otherwise only test users can sign in).
3. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Application type **Web application**, name `MyDripCheck web`.
   - **Authorized JavaScript origins**: `https://mydripcheck.com` and `https://www.mydripcheck.com`.
   - **Authorized redirect URIs**: the **Callback URL** Supabase shows in step 2 below
     (it looks like `https://<your-project-id>.supabase.co/auth/v1/callback`).
   - Create, then copy the **Client ID** and **Client secret**.

## 2. Supabase

**Authentication → Sign In / Providers → Google** → turn on → paste the Client ID and Client secret → Save.
(The Callback URL for step 1.3 is shown on this page.)

URL Configuration is the same as for email links: Site URL `https://mydripcheck.com`, Redirect URLs
`https://mydripcheck.com/**` and `https://www.mydripcheck.com/**`.

## 3. Switch it on

Render → API service → **Environment** → `GOOGLE_SIGN_IN_ENABLED` = `true` → **Save**. The button appears on the site
within a minute. To hide it again, set it to `false`.

Never put the Client secret in the website code or in chat; it only goes into Supabase.
