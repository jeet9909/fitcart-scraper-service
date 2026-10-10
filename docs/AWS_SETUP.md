# Moving the MyDripCheck API to AWS (console guide)

The API runs on **ECS Fargate** in **Mumbai (ap-south-1)** behind an **Application Load Balancer** at
`https://api.mydripcheck.com`. Supabase, Gemini, Vertex, Bright Data and Razorpay stay as they are. The website
stays on GitHub Pages and only learns the new API address.

Render keeps running the whole time. You switch the website over only after the AWS address passes the smoke test,
and you can switch back in two minutes (part 9).

Time needed: about 1.5 to 2 hours the first time. Keep this page open next to the AWS console. In every AWS
screen, check the region at the top right says **Asia Pacific (Mumbai) ap-south-1**.

Write these down as you go: your **AWS account ID** (12 digits, top right menu), the **secret ARN**, the **role
ARN** and the **load balancer DNS name**.

---

## Part 0 — Make the account safe (10 min)

1. **Root MFA:** top right menu → *Security credentials* → *Assign MFA device* → authenticator app.
2. **Your daily login:** IAM → *Users* → *Create user* → name `jeet-admin` → tick *Provide user access to the AWS
   Management Console* → *I want to create an IAM user* → custom password → *Next* → *Attach policies directly* →
   `AdministratorAccess` → *Create user*. Sign out of root and sign in as this user (add MFA to it too). Use root
   only for billing.
3. **Budget alarm:** *Billing and Cost Management* → *Budgets* → *Create budget* → *Use a template* →
   *Monthly cost budget* → amount `100` (USD) → your email → *Create*. Add a second one at `50` if you like.

## Part 1 — A home for the server image (ECR) (3 min)

1. *Elastic Container Registry* → *Create repository* → Private → name `mydripcheck-api` → *Create*.
2. Open it → *Lifecycle policy* → *Create rule* → priority `1`, *Image count more than* `10`, expire → *Save*.
   (Keeps the last 10 builds, deletes older ones so storage stays free.)

## Part 2 — Let GitHub deploy without storing any AWS keys (10 min)

GitHub signs in to AWS with a short-lived token (OIDC), so no AWS password or key is ever saved in GitHub.

1. IAM → *Identity providers* → *Add provider* → **OpenID Connect**
   - Provider URL: `https://token.actions.githubusercontent.com`
   - Audience: `sts.amazonaws.com` → *Add provider*.
2. IAM → *Roles* → *Create role* → **Web identity**
   - Identity provider: `token.actions.githubusercontent.com`, Audience: `sts.amazonaws.com`
   - GitHub organization: `jeet9909`, GitHub repository: `fitcart-scraper-service`, GitHub branch: `main`
   - *Next* → skip permissions → *Next* → name `github-deploy-mydripcheck` → *Create role*.
3. Open the role → *Add permissions* → *Create inline policy* → *JSON*, paste this (replace `ACCOUNT_ID` twice),
   name it `deploy` → *Create policy*:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*" },
    { "Effect": "Allow",
      "Action": ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
                 "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage"],
      "Resource": "arn:aws:ecr:ap-south-1:ACCOUNT_ID:repository/mydripcheck-api" },
    { "Effect": "Allow", "Action": ["ecs:UpdateService", "ecs:DescribeServices"],
      "Resource": "arn:aws:ecs:ap-south-1:ACCOUNT_ID:service/mydripcheck/mydripcheck-api" }
  ]
}
```

4. Copy the role's **ARN** (looks like `arn:aws:iam::ACCOUNT_ID:role/github-deploy-mydripcheck`).
5. GitHub → repo `fitcart-scraper-service` → *Settings* → *Secrets and variables* → *Actions* → **Variables** tab →
   *New repository variable*: `AWS_ROLE_ARN` = the ARN. (Not a secret: an ARN is only a name.)
6. GitHub → *Actions* → **Deploy API to AWS** → *Run workflow* (branch `main`). It builds the image and pushes it to
   ECR (the ECS step is skipped for now). Wait for the green tick, then check ECR → `mydripcheck-api` shows an image
   tagged `latest`. If it fails, send me the red step's log.

## Part 3 — Put the keys in a safe (Secrets Manager) (10 min)

Open Render → your API service → *Environment* in another tab: you will copy values from there.

1. *Secrets Manager* → *Store a new secret* → *Other type of secret* → **Key/value**. Add one row per secret below,
   with the same name and the exact value from Render (skip any you don't use):

   | Key | Notes |
   |---|---|
   | `GEMINI_API_KEY` | |
   | `SUPABASE_SERVICE_ROLE_KEY` | |
   | `ANONYMOUS_TOKEN_SECRET` | **Copy exactly**, or every shopper is signed out |
   | `ADMIN_API_TOKEN` | |
   | `BRIGHTDATA_API_TOKEN` | |
   | `RAZORPAY_KEY_SECRET` | |
   | `RAZORPAY_WEBHOOK_SECRET` | |
   | `GOOGLE_SERVICE_ACCOUNT_JSON` | Only if Vertex is set up; paste the whole JSON |
   | `OPENAI_API_KEY` | Only if set on Render |

2. *Next* → secret name `mydripcheck/api` → *Next* → no rotation → *Store*.
3. Open the secret and copy its **ARN** (ends in `mydripcheck/api-AbC123`).

Never paste these values in chat or email.

## Part 4 — Firewalls (security groups) (5 min)

*EC2* → *Security Groups* → *Create security group* (VPC: the **default** VPC) twice:

1. `mdc-alb-sg` — description "load balancer". Inbound: **HTTP 80** from `0.0.0.0/0`, **HTTPS 443** from
   `0.0.0.0/0`. → *Create*.
2. `mdc-app-sg` — description "API tasks". Inbound: **Custom TCP 8000**, source = the security group
   `mdc-alb-sg` (pick it from the list, not an IP). → *Create*.

Only the load balancer can reach the API; nothing else on the internet can.

## Part 5 — HTTPS certificate for api.mydripcheck.com (10 min, mostly waiting)

1. *Certificate Manager* (Mumbai) → *Request* → *Public certificate* → domain `api.mydripcheck.com` →
   **DNS validation** → *Request*.
2. Open it: it shows a **CNAME name** and **CNAME value**. At the company where you manage mydripcheck.com's DNS
   (where you set up GitHub Pages), add that CNAME record exactly.
3. Wait until the status says **Issued** (usually 5 to 30 minutes).

## Part 6 — The load balancer (15 min)

1. *EC2* → *Target Groups* → *Create target group*
   - Target type: **IP addresses**, name `mdc-api-tg`, protocol **HTTP**, port **8000**, VPC: default
   - Health check path: `/health` → *Next* → don't add targets → *Create*.
   - Open it → *Attributes* → *Edit* → **Deregistration delay: 120** seconds → *Save*. (A deploy lets running
     generations finish.)
2. *EC2* → *Load Balancers* → *Create* → **Application Load Balancer**
   - Name `mdc-api-alb`, **Internet-facing**, IPv4, default VPC, tick **all** availability zones
   - Security group: remove `default`, choose `mdc-alb-sg`
   - Listener **HTTPS 443** → forward to `mdc-api-tg`; certificate: from ACM, `api.mydripcheck.com`
   - *Create load balancer*.
3. Open the load balancer:
   - *Listeners* → *Add listener* → **HTTP 80** → action **Redirect to URL** → HTTPS, port 443, 301 → *Add*.
   - *Attributes* → *Edit* → **Connection idle timeout: 600** seconds → *Save*. (Looks, 4K and 360° views can take
     several minutes; the default 60 seconds would cut them off.)
   - Copy the **DNS name** (like `mdc-api-alb-123.ap-south-1.elb.amazonaws.com`).

## Part 7 — Run the API (ECS) (20 min)

1. *Elastic Container Service* → *Clusters* → *Create cluster* → name `mydripcheck` → **AWS Fargate** only →
   *Create*.
2. **Task definition:** *Task definitions* → *Create new task definition*
   - Family `mydripcheck-api`, launch type **AWS Fargate**, OS **Linux/X86_64**, **CPU 1 vCPU, Memory 2 GB**
   - Task execution role: **Create new role** (it becomes `ecsTaskExecutionRole`)
   - Container: name `api`, image URI `ACCOUNT_ID.dkr.ecr.ap-south-1.amazonaws.com/mydripcheck-api:latest`,
     essential **Yes**, container port **8000** TCP, app protocol HTTP
   - **Environment variables:**
     - Add every **non-secret** variable from Render as **Value** (for example `SUPABASE_URL`,
       `RAZORPAY_KEY_ID`, `RAZORPAY_ALLOW_LIVE`, `ADMIN_EMAILS`, `UNLIMITED_EMAILS`, `PAID_IMAGE_SIZE`,
       `BRIGHTDATA_ZONE`, `GEMINI_IMAGE_MODEL`, `GEMINI_TEXT_MODEL`, `LOOK_LIMITS_ENABLED`,
       `FREE_LOOKS_PER_MONTH`, any `COST_…`), plus `PORT` = `8000`.
     - For each secret from part 3 add a row with type **ValueFrom** and value
       `SECRET_ARN:KEY::` — for example
       `arn:aws:secretsmanager:ap-south-1:ACCOUNT_ID:secret:mydripcheck/api-AbC123:GEMINI_API_KEY::`
       (note the two colons at the end).
   - Logging: **Use log collection** (CloudWatch) — keep the defaults.
   - *Create*.
3. **Let the task read the secret:** IAM → *Roles* → `ecsTaskExecutionRole` → *Add permissions* → *Create inline
   policy* → JSON (replace `ACCOUNT_ID`), name `read-mydripcheck-secret`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:ap-south-1:ACCOUNT_ID:secret:mydripcheck/api-*" }
  ]
}
```

4. **Service:** cluster `mydripcheck` → *Services* → *Create*
   - Compute: **Launch type FARGATE**; task definition `mydripcheck-api` (latest revision)
   - Service name **`mydripcheck-api`**, desired tasks **1**
   - Deployment: minimum healthy **100%**, maximum **200%**, turn on **deployment circuit breaker with
     rollback**; health check grace period **60** seconds
   - Networking: default VPC, all subnets, security group **`mdc-app-sg`** (remove default), **Public IP: ON**
     (lets the task reach Google, Supabase and Bright Data without a paid NAT gateway)
   - Load balancing: **Application Load Balancer** → existing `mdc-api-alb` → listener **443** → existing target
     group `mdc-api-tg`
   - *Create*. After 2 to 4 minutes the task shows **Running** and the target group shows the target **healthy**.
5. GitHub → repo variables → add `ECS_SERVICE` = `mydripcheck-api`. From now on every merge to `main` deploys to
   AWS by itself (and Render keeps deploying as before, until you turn it off).

If the task keeps stopping: ECS → the service → *Tasks* → a stopped task → *Stopped reason*, and *Logs*. Send me
the text.

## Part 8 — Point api.mydripcheck.com at AWS and smoke-test (20 min)

1. At your DNS provider add **CNAME** `api` → the load balancer DNS name from part 6.
2. After a few minutes open `https://api.mydripcheck.com/health` → `{"status":"ok"}`.
3. Open `https://api.mydripcheck.com/admin/`, sign in → *Integrations*: every row should be **Healthy**.
4. Still on the AWS address, test with the admin console and a few API calls, or wait for part 9 and test on the
   site. Nothing visible has changed for shoppers yet: the site still talks to Render.

## Part 9 — Switch the website to AWS (5 min)

1. GitHub → repo *Settings* → *Variables* → edit **`FITCART_API_BASE`** → `https://api.mydripcheck.com`.
2. GitHub → *Actions* → **Deploy UI to GitHub Pages** → *Run workflow* → branch **`mydripcheck`**.
3. Razorpay dashboard → *Webhooks* → change the URL to `https://api.mydripcheck.com/v1/billing/webhook`
   (same secret).
4. Run the full smoke test on your phone: sign in, a look (Free and Pro), a 360° view, a pose, Myntra and Amazon
   links, the wardrobe, a help message, maintenance on/off, and a small real payment.
5. Your admin console is now `https://api.mydripcheck.com/admin/`.

**To go back to Render at any time:** set `FITCART_API_BASE` back to `https://fitcart-scraper-api.onrender.com`,
re-run the Pages deploy, and point the Razorpay webhook back. Two minutes.

## Part 10 — Alarms (10 min)

*CloudWatch* → *Alarms* → *Create alarm* (create an SNS topic with your email the first time, and confirm the email
AWS sends you):

- **API down:** metric *ApplicationELB → Per AppELB, per TG* → `UnHealthyHostCount` for `mdc-api-tg` → `> 0` for
  2 periods of 1 minute.
- **Errors:** *Per AppELB* → `HTTPCode_ELB_5XX_Count` for `mdc-api-alb` → `> 10` in 5 minutes.
- **Memory:** *ECS → ClusterName, ServiceName* → `MemoryUtilization` → `> 85` for 5 minutes. If it fires, raise the
  task to 3 or 4 GB (new task definition revision, then *Update service*).

Also: *CloudWatch* → *Log groups* → `/ecs/mydripcheck-api` → *Actions* → *Edit retention* → **30 days**.

## Part 11 — After a few calm days

- Render → suspend the API service (keep it a week as a spare, then delete it).
- If traffic grows: ECS service → *Auto scaling* → target tracking on CPU 60%, min 1, max 4 tasks.
- Apply for **AWS Activate** startup credits.

## Rough cost (Mumbai)

Fargate 1 vCPU / 2 GB always on, about $35 to 40 a month; load balancer about $20; Secrets Manager, ECR and
CloudWatch a few dollars. Roughly **$60 to 70 a month**. Check the AWS pricing calculator for exact figures.
