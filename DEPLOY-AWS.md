# Deploying to AWS: the architecture

The dashboard deploys to **one EC2 instance in AWS, with Caddy in front of it**.
This document is the design and the runbook. [`DEPLOY.md`](DEPLOY.md) describes an
earlier Render + Vercel plan; it is kept for reference only.

**Live (November 2026):**

| | URL | Host |
|---|---|---|
| Dashboard | <https://f1-dash-backend.vercel.app> | Vercel (static page, CDN) |
| API, and the same dashboard served directly | <https://3-19-133-243.sslip.io> | AWS EC2 behind Caddy, `us-east-2` |

It is a read-only public demo (section 0.4). It runs about $2.25/day, so it is stopped
again after use (section 0.8).

## 0. Architecture

### 0.1 The shape of it

```
   visitor's browser
        │  https://<host>/            (pages, JSON)
        │  wss://<host>/sessions/...  (live push)
        ▼
   Elastic IP  ──  security group f1-dash-web: 80 + 443 in, nothing else (no SSH)
        │
 ┌──────┼────────────── EC2 · Amazon Linux 2023 · 2 vCPU / 4 GiB · IMDSv2 only ──────────────┐
 │      ▼                                                                                     │
 │  ┌─────────────┐  docker network "f1"   ┌───────────────────────────────────────────────┐  │
 │  │ caddy:2     │ ─────────────────────▶ │ f1-api  (FastAPI, uvicorn --workers 1)        │  │
 │  │ :80  :443   │   reverse_proxy :8000  │  · serves the dashboard and the API           │  │
 │  │ TLS (ACME)  │                        │  · read-only middleware in public mode        │  │
 │  └──────┬──────┘                        │  · director, live loops, job pool, broadcaster│  │
 │         │ /var/cache/caddy (certs)      └──────────────┬────────────────────────────────┘  │
 │         ▼                                              ▼ /var/cache/f1                     │
 │   EBS volume (gp3, encrypted): SQLite DB · model artifacts · replays · telemetry windows    │
 │                                · FastF1 cache · Caddy certificates                         │
 └───────────────────────────────┬────────────────────────────────────────────────────────────┘
                                 │ instance role f1-dash-ec2 (no access keys anywhere)
            ┌────────────────────┼─────────────────────────────┐
            ▼                    ▼                             ▼
   S3 bucket (private)    SSM Parameter Store /f1-dash/*   SSM Run Command / Session Manager
   prod/       app blobs  settings + SecureString secrets  deploys and shell access
   bootstrap/  src + seed one env var per parameter        (no port 22)
   archive/    F1 timing backup (the app role cannot read it)
```

Everything is one process on one host. That is a constraint of the app, not a
shortcut. The live loops, the training job queue and the WebSocket broadcaster
all live in memory inside the API process. A second worker or a second replica
would run a second copy of each: OpenF1 would be polled twice, and the
WebSocket clients would be split between two processes that don't know about
each other. So the unit of deployment is "exactly one container", and the
architecture is built around keeping it that way.

### 0.1a The page on Vercel, the API on AWS

The dashboard is static HTML, CSS and ES modules with no build step, so the
public front door is **Vercel**: `vercel.json` runs `scripts/build-static.js`,
which copies `backend/app/static` to `public/` and writes `config.js` with
`window.API_BASE = "https://<host>"` from the `API_BASE` environment variable.
Vercel serves that from its CDN and rebuilds on every push to `main`.

Everything dynamic is on AWS. The page calls the API across origins, so:

- **The API must be HTTPS.** An HTTPS page can't call an HTTP API (mixed
  content), which is the reason Caddy and the certificate exist.
- **CORS:** `F1_CORS_ORIGINS` lists the Vercel origin and the API's own host.
  The API answers the browser's preflight and sends `Access-Control-Allow-Origin` for them only.
- **WebSockets** go straight from the page to `wss://<host>`. Origins don't
  apply to them in the same way, and Caddy passes the upgrade through.
- **Two front doors, one backend.** The API still serves the same dashboard at
  `/`, so the AWS URL works on its own if Vercel is unavailable.

```
 browser ──▶ Vercel CDN: index.html, css, js, config.js (API_BASE)
    │
    └──── fetch https / wss ──▶ Caddy on EC2 ──▶ f1-api ──▶ EBS · S3 · SSM
```

### 0.2 The pieces and why each is there

| Piece | What it does | Why this, not something else |
|---|---|---|
| **EC2, one instance** | Runs both containers | The API is a long-lived process. It holds WebSockets open, runs background threads, and needs about 1.5 GB of ML wheels. **Lambda** can't hold a process or a socket. **App Runner** scales by adding instances and (as of late 2025) doesn't do WebSockets. **ECS Fargate + ALB** works, and is the scaling path (section 3), but it costs about twice as much and needs RDS, because a task has no durable disk. |
| **Caddy** | TLS termination, HTTP→HTTPS redirect, reverse proxy, WebSocket pass-through, gzip | Five lines of config. It gets and renews a Let's Encrypt certificate on its own, with no certbot cron, and it passes the WebSocket `Upgrade` through without extra rules. **nginx** needs certbot plus explicit upgrade headers. **An ALB + ACM** gives managed certificates, but costs about $16/month before any traffic. |
| **Hostname** | Names the instance for the certificate | Let's Encrypt won't issue for a bare IP. `<ip-with-dashes>.sslip.io` resolves to the IP with no DNS setup, and sslip.io is on the Public Suffix List, so it has its own issuance limits. A real domain via Route 53 is a one-line change to `F1_PUBLIC_HOST`. |
| **Docker network `f1`** | Private link between Caddy and the API | With a public host set, the API publishes only `127.0.0.1:8000`. The only way in from outside is through Caddy. |
| **EBS volume** | SQLite, models, replays, caches, certificates | The app's state is small (tens of MB of rows, about 100 MB of artifacts) and written by one process. That is SQLite's sweet spot. Moving to **RDS Postgres** is a connection-string change (`F1_DATABASE_URL`). It earns its keep once there is more than one writer, or managed backups are wanted. |
| **S3 bucket** | `prod/` is the app's blob store (models, replay bundles); `bootstrap/` holds the shipped source and the seed data; `archive/` holds the raw F1 timing backup | Blobs outlive the instance. The EBS copy is a cache in front of S3 (`F1_BLOB_BACKEND=s3`). |
| **SSM Parameter Store** | Every `/f1-dash/NAME` parameter becomes `NAME=value` in the container's env file on each deploy | Configuration and secrets live outside the image and outside git. Secrets are SecureString (KMS `aws/ssm`). Changing a setting is a `put-parameter` and a redeploy, with no rebuild of the instance. |
| **IAM role `f1-dash-ec2`** | The instance's only credentials | Least privilege. S3 Get/Put/Delete on `prod/*` and `bootstrap/*` only (it can't read `archive/`), `ssm:GetParameter*` on `/f1-dash/*`, `kms:Decrypt` only via SSM, plus the AWS-managed SSM core policy. No access keys exist on the box. |
| **SSM Run Command / Session Manager** | Deploys, and shell access when needed | No port 22, no SSH keys to manage or leak. Every command is authenticated by IAM and logged. |
| **Security group `f1-dash-web`** | The firewall | Inbound 80 and 443 only. Port 80 exists for the ACME challenge and the redirect to HTTPS. |
| **Elastic IP** | A fixed address | The hostname, the certificate and any DNS record stay valid across stop/start. |

### 0.3 A request, end to end

1. The browser resolves `<host>` to the Elastic IP and opens TLS to Caddy on 443.
2. Caddy terminates TLS and proxies to `f1-api:8000` on the private Docker network.
3. The read-only middleware (`backend/app/main.py`) lets `GET`/`HEAD`/`OPTIONS` through.
4. `/` redirects to `/static/dashboard.html`. The page asks `/health`, then loads the replay list. (`/app-config` reports `{"public": true}` for any client that wants to know; the page itself learns it from the API's refusal messages.)
5. A replay bundle is served from the EBS cache, falling back to S3, gzipped. The car card asks `/replays/{key}/telemetry/{car}?t=`, which comes from windows saved to disk when the replay was built (no upstream call).
6. A live session, when one is being followed, arrives over `wss://<host>/sessions/{id}/live`. Caddy passes the upgrade straight through.

### 0.4 Public, but read-only

A link anyone can open must not let anyone change anything. The API has no user
accounts, so the rule sits at the edge of the app instead of in each endpoint:

- `F1_PUBLIC_MODE=true` turns on one HTTP middleware that refuses every
  non-read request (`POST`, `PUT`, `PATCH`, `DELETE`) with
  `403 {"detail": {"reason": "read_only", ...}}`. A new write route is closed to
  the public the day it is written, because the guard isn't something each
  endpoint has to remember.
- The owner still writes, by sending `X-Admin-Token: <F1_ADMIN_TOKEN>`. The
  comparison is constant-time (`hmac.compare_digest`). With no token configured,
  nobody writes at all. That header is how replays get built on the server.
- `F1_LIVE_AUTOSTART=false` keeps the director from following sessions and
  queuing builds on its own.
- **No F1 TV token on a public server.** F1 TV data is licensed for personal
  use, so the public deployment serves replays built from OpenF1's free
  historical data. Live F1 TV timing stays on the owner's laptop.
- The page reads the API's own refusal messages, so a visitor who clicks an
  unbuilt race sees "This is a read-only public demo…" rather than a status code.

### 0.5 How a deploy works

```
 laptop: scripts/deploy-aws.sh
   1. git archive HEAD  ──▶  s3://<bucket>/bootstrap/src.tar.gz   (committed code only; refuses if a .env is tracked)
   2. ssm send-command  ──▶  instance:
        install deploy/aws/f1-deploy.sh from that tarball as /usr/local/bin/f1-deploy, then run it:
          a. SSM /f1-dash/*  ──▶  /etc/f1-dash/env   (chmod 600, values never logged)
          b. docker build f1-dash:latest from backend/
          c. first boot only: restore bootstrap/data (DB last, so its presence means "restore finished")
          d. docker rm -f f1-api, THEN docker run f1-api   (never two copies of the live loops)
          e. with F1_PUBLIC_HOST: write the Caddyfile, (re)start caddy on 80/443
          f. wait for /health, or print the container's last logs and fail
   3. poll the command, print its output, curl https://<host>/health
```

- **The image is built on the instance, not pushed to a registry.** One host, one
  image, so ECR adds a moving part without buying anything. With more than one
  host, build once in CI and push to ECR instead (section 2.6 shows how).
- **The server-side script ships with the code.** Every deploy installs the
  `deploy/aws/f1-deploy.sh` from the tarball it is deploying, so what runs on the
  server is always what is in git at that commit.
- **The deploy has a brief gap.** The old container is removed before the new
  one starts. That's a few seconds of 502s from Caddy, accepted on purpose:
  two overlapping containers would double-poll OpenF1 and split live clients.
  Zero-downtime would need the live loops moved out of the process first (see 0.7).

### 0.6 Security, in one list

- **Ingress:** 443 (and 80 for ACME and the redirect) only. No SSH; shell access
  is SSM Session Manager, authenticated and audited by IAM.
- **Instance metadata:** IMDSv2 required, so a server-side request forgery can't
  read the role's credentials with a simple GET.
- **No long-lived credentials:** the instance uses its role. The laptop uses
  `aws login` (temporary, auto-refreshed).
- **Secrets:** SSM SecureString. They land only in a root-only env file on the
  instance, never in the image, git, user-data or logs.
- **Least-privilege role:** two bucket prefixes, one parameter path, decrypt only via SSM.
- **Storage:** EBS and S3 encrypted at rest. The bucket blocks all public access.
- **App:** read-only middleware, admin token compared in constant time, CORS limited to the site's own origin.
- **Data licensing:** no F1 TV token on any public host.

### 0.7 Trade-offs, limits, and the way to scale

| Limit | Why it is accepted | What changes it |
|---|---|---|
| One instance is a single point of failure | A demo and a personal tool. `--restart unless-stopped` covers crashes; the Elastic IP and the S3 blobs survive the instance. | Fargate + ALB, after the next row |
| Live loops are in-process, so there's exactly one replica | It's what keeps the code simple, and live sessions are a few hours a week | Move live state into Redis pub/sub (ElastiCache) and run the loops as one dedicated worker. API replicas then scale freely behind an ALB |
| SQLite on one disk | One writer, small data | `F1_DATABASE_URL` to RDS Postgres; the code avoids SQLite-only features on purpose |
| No CDN | The static files are small and gzipped | CloudFront in front of Caddy |
| Deploys have a seconds-long gap | See 0.5 | Comes free with the "one live worker" change |
| No metrics or alarms | Logs go to `docker logs` with rotation | The CloudWatch agent, or `--log-driver awslogs` (section 4) |

### 0.8 Running it, and what it costs

| State | What bills | About |
|---|---|---|
| **Running** (c7i-flex.large, 30 GB gp3, Elastic IP) | compute + disk + public IPv4 | **$68/month** |
| **Stopped** (current) | disk + Elastic IP | **$6/month** |
| **Torn down** (section 4, "Teardown") | the S3 archive only | **cents** |

These are on-demand estimates. The account's Free plan refused `t3.medium`, which is why the instance type is `c7i-flex.large`.

**To bring the public demo up** (from Git Bash or PowerShell, signed in with `aws login --region us-east-2`):

1. Start the instance: `aws ec2 start-instances --instance-ids <id> --region us-east-2`.
2. One-time switch to public:
   - Open the firewall: 80 and 443 from `0.0.0.0/0` on `f1-dash-web`, and remove the old single-IP rule.
   - Settings: `F1_PUBLIC_MODE=true`, `F1_LIVE_AUTOSTART=false`, `F1_PUBLIC_HOST=<ip-with-dashes>.sslip.io` and `F1_CORS_ORIGINS=https://<that host>,https://<your-app>.vercel.app`.
   - Secrets: `F1_ADMIN_TOKEN` as a SecureString, and delete `F1TV_LOGIN_SESSION`.
3. `scripts/deploy-aws.sh`. It installs Caddy, which fetches the certificate on first start.
4. Build the season's races on the server: `POST /replays/{key}/build` with the `X-Admin-Token` header.
5. Check `https://<host>/health`, then open the page.

**To take it down again:** `aws ec2 stop-instances --instance-ids <id> --region us-east-2`.

---

The rest of this document is the manual walk-through the architecture above was built from: installing tools, each resource by hand, the Fargate outline, running it, and troubleshooting.

## 1. Software you need to install (Windows 11)

Run these in **PowerShell**. You don't need an admin prompt for most of them;
winget asks for elevation when an installer needs it.

| Tool | Why | Install |
|---|---|---|
| **AWS CLI v2** | Every `aws ...` command in this guide | `winget install -e --id Amazon.AWSCLI` |
| **Docker Desktop** | Build the image locally and push it to ECR | `wsl --install` first if WSL2 isn't set up (then reboot), then `winget install -e --id Docker.DockerDesktop` |
| **Git** | You already have it (Git for Windows includes Git Bash) | `winget install -e --id Git.Git` |
| **Session Manager plugin** (optional) | `aws ssm start-session` opens a shell on the instance from your terminal without SSH. Without it, use the EC2 console's **Connect → Session Manager** button instead. | `winget install -e --id Amazon.SessionManagerPlugin`. If that ID isn't found, run `winget search SessionManager` or use the installer at `https://s3.amazonaws.com/session-manager-downloads/plugin/latest/windows/SessionManagerPluginSetup.exe` |
| **Python 3.13** (optional) | Only needed to push artifacts you already trained on this laptop (step 2.10, option B) | `winget install -e --id Python.Python.3.13` |

Open a **new** PowerShell window after installing, so `PATH` picks up the new
tools, then check them:

```powershell
aws --version          # aws-cli/2.x
docker version         # Docker Desktop must be running; shows both Client and Server
```

### Sign the CLI in to your AWS account

Use one of these:

- **IAM Identity Center (SSO), recommended:** `aws configure sso`, then
  `aws sso login --profile <name>` each day. Set `$env:AWS_PROFILE = "<name>"`
  in each PowerShell window, or add `--profile` to every command.
- **Console sign-in:** newer CLI builds (2.32 and later, I believe) have
  `aws login`, which opens a browser and uses your console credentials. If your
  CLI says the command doesn't exist, use one of the other options.
- **Access keys** (the simplest, but the least safe): `aws configure`, then paste
  an IAM user's access key and secret. Use these on the laptop only. The server
  never needs keys, because it gets credentials from its IAM role.

Check that it worked:

```powershell
aws sts get-caller-identity
```

### If you want Claude to run the steps

Claude Code has an AWS MCP connector that can call AWS APIs for you. If it shows
as disconnected or unauthenticated, run `/mcp` in Claude Code and authenticate
it again. **Don't paste access keys or passwords into the chat.** Use the
connector's own sign-in, or let Claude run the `aws` CLI with your signed-in
profile.

### Shell conventions in this guide

- **Laptop** commands are **PowerShell** (Windows PowerShell 5.1 works).
  Multi-line commands use the backtick (`` ` ``) for line continuation.
  Policy documents are written to files first, because 5.1 mangles inline JSON
  passed to native programs.
- **Instance** commands are **bash**. Run them on the EC2 instance through
  Session Manager.
- If you'd rather use **Git Bash** on the laptop, run
  `export MSYS_NO_PATHCONV=1` first. Without it, Git Bash rewrites arguments
  like `/f1-dash/F1_OPENF1_PASSWORD` into Windows paths.

---

## 2. Recommended path: one EC2 instance

### 2.0 Variables (laptop, PowerShell)

Set these once per PowerShell window. Every later laptop step uses them.

```powershell
$REGION  = "us-east-2"        # this account's console project is pinned to us-east-2
$env:AWS_DEFAULT_REGION = $REGION
$ACCOUNT = aws sts get-caller-identity --query Account --output text
$BUCKET  = "f1-dash-artifacts-$ACCOUNT"   # bucket names are global; the account id keeps it unique
$REPO    = "f1-dash-api"
$ECR     = "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"
$DOMAIN  = "f1.example.com"   # your hostname, or leave as-is if you have no domain yet (see 2.7)

mkdir $HOME\f1-dash-aws -Force | Out-Null; cd $HOME\f1-dash-aws   # scratch folder for policy files
```

### 2.1 S3 bucket for artifacts

```powershell
aws s3 mb "s3://$BUCKET" --region $REGION
aws s3api put-public-access-block --bucket $BUCKET `
  --public-access-block-configuration "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
```

New buckets block public access by default. The second command just makes that
explicit. The API reads and writes objects under `prod/` (`F1_S3_PREFIX=prod`).
The `bootstrap/` and `backups/` prefixes are used for moving and backing up the
SQLite file.

### 2.2 Secrets in SSM Parameter Store

Store the OpenF1 sponsor login as `SecureString` parameters. Standard parameters
are free. The values never go into git, the image, or your shell history:

```powershell
$u = Read-Host "OpenF1 username"
$p = Read-Host "OpenF1 password" -AsSecureString
$pPlain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($p))

aws ssm put-parameter --name /f1-dash/F1_OPENF1_USERNAME --type SecureString --value $u      --overwrite
aws ssm put-parameter --name /f1-dash/F1_OPENF1_PASSWORD --type SecureString --value $pPlain --overwrite
Remove-Variable u, p, pPlain
```

Every parameter under `/f1-dash/` becomes an environment variable in the
container, named after the last part of the path. If you choose RDS (step 2.4),
you'll add `/f1-dash/F1_DATABASE_URL` there.

> The default `aws/ssm` KMS key decrypts for any principal in the account that is
> allowed `ssm:GetParameter*`. Because of that, the role policy below has no
> `kms:Decrypt` statement. If you use a customer-managed KMS key, add one.

### 2.3 IAM role for the instance (least privilege)

```powershell
@"
{
  "Version": "2012-10-17",
  "Statement": [{ "Effect": "Allow", "Principal": { "Service": "ec2.amazonaws.com" }, "Action": "sts:AssumeRole" }]
}
"@ | Set-Content -Encoding ascii ec2-trust.json

@"
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "ListBucket", "Effect": "Allow", "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::$BUCKET" },
    { "Sid": "Objects", "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": ["arn:aws:s3:::$BUCKET/prod/*", "arn:aws:s3:::$BUCKET/bootstrap/*", "arn:aws:s3:::$BUCKET/backups/*"] },
    { "Sid": "AppParameters", "Effect": "Allow",
      "Action": ["ssm:GetParametersByPath", "ssm:GetParameters", "ssm:GetParameter"],
      "Resource": ["arn:aws:ssm:${REGION}:${ACCOUNT}:parameter/f1-dash", "arn:aws:ssm:${REGION}:${ACCOUNT}:parameter/f1-dash/*"] },
    { "Sid": "OptionalCloudWatchLogs", "Effect": "Allow",
      "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "arn:aws:logs:${REGION}:${ACCOUNT}:log-group:/f1-dash/*" }
  ]
}
"@ | Set-Content -Encoding ascii f1-app-policy.json

aws iam create-role --role-name f1-dash-ec2 --assume-role-policy-document file://ec2-trust.json
aws iam put-role-policy --role-name f1-dash-ec2 --policy-name f1-dash-app --policy-document file://f1-app-policy.json
aws iam attach-role-policy --role-name f1-dash-ec2 --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam attach-role-policy --role-name f1-dash-ec2 --policy-arn arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly
aws iam create-instance-profile --instance-profile-name f1-dash-ec2
aws iam add-role-to-instance-profile --instance-profile-name f1-dash-ec2 --role-name f1-dash-ec2
```

What each permission is for:

- **`AmazonSSMManagedInstanceCore`** lets you open a Session Manager shell, so
  the instance needs no SSH port.
- **`AmazonEC2ContainerRegistryReadOnly`** lets the instance pull the image from
  ECR.
- **The inline policy** covers this one bucket and the `/f1-dash/*` parameters.

### 2.4 Database: choose one

**Option A: SQLite on the instance's EBS volume (simplest, about $0 extra).**
The database file sits at `/var/cache/f1/f1_dashboard.db` on the instance's
root volume. There's nothing to create now. In step 2.8 you'll set
`F1_DATABASE_URL=sqlite:////var/cache/f1/f1_dashboard.db` (four slashes means an
absolute path). Backups are your job; see 2.12. This is fine because there's
only ever one process.

**Option B: RDS Postgres db.t4g.micro (managed backups, about $15/month).**
Choose this if you want automated daily backups and point-in-time restore.
Run it after 2.5, because it needs the web security group:

```powershell
# a password with only letters and digits, so it needs no URL-encoding
$DBPASS = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 32 | ForEach-Object { [char]$_ })

$VPC  = aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query "Vpcs[0].VpcId" --output text
$DBSG = aws ec2 create-security-group --group-name f1-dash-db --description "Postgres from f1-dash web" --vpc-id $VPC --query GroupId --output text
aws ec2 authorize-security-group-ingress --group-id $DBSG --protocol tcp --port 5432 --source-group $SG

aws rds create-db-instance `
  --db-instance-identifier f1-dash-db `
  --engine postgres `
  --db-instance-class db.t4g.micro `
  --allocated-storage 20 --storage-type gp3 `
  --db-name f1_dash `
  --master-username f1admin --master-user-password $DBPASS `
  --vpc-security-group-ids $DBSG `
  --no-publicly-accessible `
  --backup-retention-period 7 `
  --storage-encrypted
aws rds wait db-instance-available --db-instance-identifier f1-dash-db   # takes 5 to 10 minutes

$DBHOST = aws rds describe-db-instances --db-instance-identifier f1-dash-db --query "DBInstances[0].Endpoint.Address" --output text
aws ssm put-parameter --name /f1-dash/F1_DATABASE_URL --type SecureString `
  --value "postgresql://f1admin:$DBPASS@${DBHOST}:5432/f1_dash" --overwrite
Remove-Variable DBPASS
```

Notes on this option:

- Leaving out `--engine-version` gives you RDS's current default Postgres
  version. The code uses psycopg 3, and `app/config.py` rewrites
  `postgresql://` to `postgresql+psycopg://` for you.
- RDS Postgres 15 and later require SSL by default. psycopg's default
  `sslmode=prefer` negotiates it. I believe this works without changes; if you
  see SSL errors, add `?sslmode=require` to the URL.

### 2.5 Security group: 80 and 443 only, no SSH

```powershell
$VPC = aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query "Vpcs[0].VpcId" --output text
$SG  = aws ec2 create-security-group --group-name f1-dash-web --description "f1-dash HTTP/HTTPS" --vpc-id $VPC --query GroupId --output text
aws ec2 authorize-security-group-ingress --group-id $SG --protocol tcp --port 80  --cidr 0.0.0.0/0
aws ec2 authorize-security-group-ingress --group-id $SG --protocol tcp --port 443 --cidr 0.0.0.0/0
```

Port 22 stays closed. Shell access goes through Session Manager, which only needs
outbound HTTPS from the instance. The default security-group egress rule already
allows that.

### 2.6 Build the image and push it to ECR (laptop)

Run this from the repo root with Docker Desktop running:

```powershell
cd C:\Users\tusha\Documents\Project\f1-dash
aws ecr create-repository --repository-name $REPO --image-scanning-configuration scanOnPush=true

aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $ECR

$TAG = git rev-parse --short HEAD
docker build --platform linux/amd64 -t "${ECR}/${REPO}:$TAG" -t "${ECR}/${REPO}:latest" backend
docker push "${ECR}/${REPO}:$TAG"
docker push "${ECR}/${REPO}:latest"
```

The build context is `backend/`, the same as `render.yaml`'s `dockerContext`.
`--platform linux/amd64` matches the t3 (Intel/AMD) instance types. If you choose
a Graviton type (t4g), build `linux/arm64` instead. The first push uploads about
1.5 GB. Later pushes only send the layers that changed.

### 2.7 Launch the instance and give it a fixed IP

**t3.medium (4 GB RAM) is recommended.** pandas, xgboost and FastF1 session loads
take a lot of memory during `backfill` and retraining. t3.small (2 GB) can work
with the swap file added in 2.8, but training will be slow and could run out of
memory. You can resize later (see section 5).

```powershell
cd $HOME\f1-dash-aws
$INSTANCE_ID = aws ec2 run-instances `
  --image-id resolve:ssm:/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 `
  --instance-type t3.medium `
  --iam-instance-profile Name=f1-dash-ec2 `
  --security-group-ids $SG `
  --metadata-options "HttpTokens=required,HttpPutResponseHopLimit=2,HttpEndpoint=enabled" `
  --block-device-mappings "DeviceName=/dev/xvda,Ebs={VolumeSize=30,VolumeType=gp3,Encrypted=true}" `
  --credit-specification CpuCredits=standard `
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=f1-dash}]" `
  --query "Instances[0].InstanceId" --output text
aws ec2 wait instance-running --instance-ids $INSTANCE_ID

$ALLOC = aws ec2 allocate-address --domain vpc --query AllocationId --output text
aws ec2 associate-address --instance-id $INSTANCE_ID --allocation-id $ALLOC
$EIP = aws ec2 describe-addresses --allocation-ids $ALLOC --query "Addresses[0].PublicIp" --output text
"Instance $INSTANCE_ID at $EIP"
```

About these flags:

- **`HttpPutResponseHopLimit=2` is required.** With a hop limit of 1, containers
  can't reach the instance metadata service, so boto3 inside the container finds
  no credentials and S3 fails.
- **`CpuCredits=standard`** stops a long training run from adding surprise
  "unlimited" CPU-credit charges. The trade-off: once the credits run out, the
  CPU is throttled.
- **If `run-instances` says the instance profile is invalid,** IAM hasn't
  propagated yet. Wait about 10 seconds and run it again.

**Domain (optional, but needed for HTTPS).** Point an `A` record for `$DOMAIN`
at `$EIP`.

If the domain's hosted zone is in Route 53:

```powershell
$ZONE = aws route53 list-hosted-zones-by-name --dns-name "example.com." --query "HostedZones[0].Id" --output text
@"
{ "Changes": [{ "Action": "UPSERT", "ResourceRecordSet": {
    "Name": "$DOMAIN", "Type": "A", "TTL": 300, "ResourceRecords": [{ "Value": "$EIP" }] } }] }
"@ | Set-Content -Encoding ascii dns.json
aws route53 change-resource-record-sets --hosted-zone-id $ZONE --change-batch file://dns.json
```

If the domain is with another registrar, add the same `A` record in its DNS
panel.

If you have **no domain**, you can run HTTP-only on the bare IP for now (see the
Caddyfile variant in 2.9). Let's Encrypt won't issue a certificate for the
`ec2-…amazonaws.com` hostname, as far as I know, so a real domain is the
practical way to get HTTPS. A cheap domain or a free dynamic-DNS subdomain both
work.

### 2.8 Prepare the instance (instance, bash)

Open a shell. From the laptop, run
`aws ssm start-session --target $INSTANCE_ID --region $REGION` (this needs the
plugin). Or use the EC2 console: **Instances → f1-dash → Connect → Session
Manager**. If the instance isn't listed yet, wait 2 to 3 minutes. Then:

```bash
sudo -i                                   # Session Manager logs in as ssm-user; everything below runs as root

# Docker (Amazon Linux 2023 ships it in its own repo; the AWS CLI v2 is preinstalled)
dnf install -y docker
systemctl enable --now docker

# 2 GB swap: a safety net for training spikes (essential on t3.small, harmless on t3.medium)
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile swap swap defaults 0 0' >> /etc/fstab

mkdir -p /etc/f1-dash /var/cache/f1
docker network create f1
```

Now write the non-secret settings. **Edit the first four lines** before pasting:

```bash
REGION=us-east-2
BUCKET=f1-dash-artifacts-123456789012        # the $BUCKET from step 2.0
DOMAIN=f1.example.com                        # or the Elastic IP if you have no domain
DB=sqlite                                    # "sqlite" (option A) or "rds" (option B)

cat > /etc/f1-dash/app.env <<EOF
F1_ENV=production
F1_LOG_LEVEL=INFO
F1_SEASON=2026
F1_REGS_REGIME=2026
F1_BLOB_BACKEND=s3
F1_S3_BUCKET=${BUCKET}
F1_S3_PREFIX=prod
AWS_DEFAULT_REGION=${REGION}
F1_CORS_ORIGINS=https://${DOMAIN}
EOF
# SQLite only. With RDS, F1_DATABASE_URL comes from SSM instead; don't define it twice.
[ "$DB" = sqlite ] && echo 'F1_DATABASE_URL=sqlite:////var/cache/f1/f1_dashboard.db' >> /etc/f1-dash/app.env
echo "REGION=${REGION}" > /etc/f1-dash/deploy.conf
chmod 600 /etc/f1-dash/*.env
```

Two notes on these settings:

- **`F1_CORS_ORIGINS`.** The dashboard and the API share one origin, so CORS
  barely matters here. Set it to the public origin anyway, because it's checked
  if you ever add a separate frontend (for example on CloudFront). With no
  domain, use `http://<elastic-ip>`.
- **Cache paths.** `F1_MODEL_DIR`, `F1_REPLAY_DIR` and `F1_FASTF1_CACHE` already
  point under `/var/cache/f1` in the Dockerfile, so mounting that directory keeps
  them on the EBS volume.

### 2.9 Deploy script, API container and Caddy (instance, bash)

The deploy script below does the full deploy in one command, and it's also how
you redeploy later. It:

1. turns SSM parameters into an env file,
2. pulls the image,
3. replaces the container.

It removes the old container **before** starting the new one, so two copies of
the live loops never run at the same time.

```bash
cat > /usr/local/bin/f1-deploy <<'EOF'
#!/bin/bash
# Usage: f1-deploy [image-tag]   (default: latest)
set -euo pipefail
source /etc/f1-dash/deploy.conf
TAG="${1:-latest}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGISTRY="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"
IMAGE="${REGISTRY}/f1-dash-api:${TAG}"

# Secrets: every /f1-dash/NAME parameter becomes NAME=value. Read fresh on every deploy.
umask 077
aws ssm get-parameters-by-path --region "$REGION" --path /f1-dash/ --with-decryption \
    --query 'Parameters[].[Name,Value]' --output text \
  | awk -F'\t' '{ n = $1; sub(".*/", "", n); print n "=" $2 }' > /etc/f1-dash/secrets.env

aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY"
docker pull "$IMAGE"

docker rm -f f1-api 2>/dev/null || true
docker run -d --name f1-api --network f1 --restart unless-stopped \
  --env-file /etc/f1-dash/app.env --env-file /etc/f1-dash/secrets.env \
  -v /var/cache/f1:/var/cache/f1 \
  -p 127.0.0.1:8000:8000 \
  --log-opt max-size=20m --log-opt max-file=5 \
  "$IMAGE"
docker image prune -f >/dev/null
echo "started $IMAGE"
EOF
chmod +x /usr/local/bin/f1-deploy

f1-deploy
sleep 40; curl -s localhost:8000/health; echo
```

`/health` should return JSON with `"status":"ok"` and `"database":"sqlite"` (or
`postgresql+psycopg`). The container's own Docker `HEALTHCHECK` also polls it.

**Caddy** gets HTTPS certificates automatically from Let's Encrypt and proxies
WebSockets with no extra configuration. It runs as a second container on the
same Docker network:

```bash
DOMAIN=f1.example.com        # same as above

cat > /etc/f1-dash/Caddyfile <<EOF
${DOMAIN} {
	encode gzip

	# WebSockets (/sessions/{id}/live) need nothing extra; Caddy passes the Upgrade through.
	reverse_proxy f1-api:8000
}
EOF

docker run -d --name caddy --network f1 --restart unless-stopped \
  -p 80:80 -p 443:443 \
  -v /etc/f1-dash/Caddyfile:/etc/caddy/Caddyfile:ro \
  -v caddy_data:/data -v caddy_config:/config \
  caddy:2
docker logs -f caddy        # watch for "certificate obtained successfully", then Ctrl+C
```

Caddy needs the DNS record from 2.7 to resolve to the Elastic IP, and port 80 to
be open, before it can get a certificate. If DNS hasn't propagated yet, Caddy
keeps retrying on its own.

**No domain yet?** Replace the first line `${DOMAIN} {` with `:80 {`. Then Caddy
serves plain HTTP on the IP, and you set `F1_CORS_ORIGINS=http://<elastic-ip>`.

After changing the Caddyfile, reload it with:

```bash
docker exec caddy caddy reload --config /etc/caddy/Caddyfile
```

> **Optional: lock down write endpoints.** The API has no authentication.
> Anyone who finds the URL can `POST` to retrain models, start or stop live
> loops, or promote a model. To put a password on everything except reads, run
> `docker exec caddy caddy hash-password` to get a hash, then add this inside the
> site block, above `reverse_proxy`:
>
> ```
> @writes not method GET HEAD OPTIONS
> basic_auth @writes {
> 	admin <paste-hash-here>
> }
> ```
>
> The browser will prompt for the password the first time the dashboard makes a
> write request. Older Caddy versions spell the directive `basicauth`.

### 2.10 First data load

The database and the artifacts start empty. Choose one way to fill them.

**A. Build everything on the server** (needs nothing from the laptop; slow):

```bash
docker exec -it f1-api python -m app.cli seed --events 5    # circuits, calendar, teams, drivers (first 5 events; drop --events for all)
docker exec -it f1-api python -m app.cli backfill --year 2025   # retrain over a season in order (long, memory-heavy)
docker exec -it f1-api python -m app.cli status             # data and registry state
docker exec -it f1-api python -m app.cli promote --model-id <id>   # activate a version (F1_AUTO_PROMOTE is false)
```

`seed` also accepts `--year`, and `backfill --year` defaults to the configured
season. If the Session Manager connection drops during a long backfill, the
command dies with it. Run it in the background instead and watch it:

```bash
docker exec -d f1-api sh -c 'python -m app.cli backfill --year 2025 > /var/cache/f1/backfill.log 2>&1'
tail -f /var/cache/f1/backfill.log
```

**B. Ship what you already trained on the laptop** (SQLite option only).
On the laptop, in PowerShell from `backend\`, with your AWS profile signed in:

```powershell
cd C:\Users\tusha\Documents\Project\f1-dash\backend
$env:F1_BLOB_BACKEND = "s3"; $env:F1_S3_BUCKET = $BUCKET; $env:F1_S3_PREFIX = "prod"; $env:AWS_DEFAULT_REGION = $REGION
python ..\scripts\push_artifacts.py                 # .joblib files and replay gzips into s3://$BUCKET/prod/
aws s3 cp .\f1_dashboard.db "s3://$BUCKET/bootstrap/f1_dashboard.db"
Remove-Item Env:F1_BLOB_BACKEND, Env:F1_S3_BUCKET, Env:F1_S3_PREFIX   # so local dev goes back to local disk
```

Then on the instance:

```bash
BUCKET=f1-dash-artifacts-123456789012
docker stop f1-api
aws s3 cp "s3://${BUCKET}/bootstrap/f1_dashboard.db" /var/cache/f1/f1_dashboard.db
docker start f1-api
```

`push_artifacts.py` publishes each model under the key the deployed API looks
up. This is the same flow `DEPLOY.md` describes, so the Windows paths stored in
the copied rows shouldn't matter. Check by opening a prediction after the
restart.

With RDS, use option A. The database isn't reachable from the laptop because it
isn't publicly accessible.

### 2.11 Check it works

- `https://f1.example.com/health` returns JSON.
- `https://f1.example.com/` redirects to the dashboard and shows it with styles.
- Open a replay. Then, during a live session, open the live view. In the
  browser's DevTools, under **Network → WS**, `wss://f1.example.com/sessions/<id>/live`
  should show status **101**, followed by a `connected` message.
- `https://f1.example.com/docs` shows the API's OpenAPI page.

### 2.12 Backups

- **S3 artifacts** are durable already. If you want undo, you can turn on bucket
  versioning:
  `aws s3api put-bucket-versioning --bucket $BUCKET --versioning-configuration Status=Enabled`.
  This makes teardown slightly more work.
- **RDS** takes automated daily backups, kept for 7 days.
- **SQLite** has no automatic backup. Take a consistent copy and upload it.
  Python's sqlite3 module is in the image, so you don't need extra tools:

  ```bash
  BUCKET=f1-dash-artifacts-123456789012
  docker exec f1-api python -c "import sqlite3; s=sqlite3.connect('/var/cache/f1/f1_dashboard.db'); d=sqlite3.connect('/var/cache/f1/backup.db'); s.backup(d); d.close()"
  aws s3 cp /var/cache/f1/backup.db "s3://${BUCKET}/backups/f1_dashboard-$(date +%F).db"
  ```

  For a hands-off option, an EBS snapshot policy in **EC2 → Lifecycle Manager**
  can snapshot the root volume every day.

---

## 3. Alternative scalable path: ECS Fargate + ALB (outline)

Choose this when you want AWS to replace a failed host for you, and you're fine
paying roughly $40 to $50 more per month for the load balancer and Fargate.
Fargate has no persistent disk, so **RDS is required** here; SQLite isn't an
option.

1. **Reuse** the S3 bucket (2.1), the ECR image (2.6) and RDS (2.4 option B).
   Put the OpenF1 login and the DB URL in SSM (as above) or in Secrets Manager
   (about $0.40 per secret per month, with rotation if you want it).
2. **IAM:**
   - **Task execution role** (used to start the task): attach
     `AmazonECSTaskExecutionRolePolicy`, plus `ssm:GetParameters` on
     `parameter/f1-dash/*` (or `secretsmanager:GetSecretValue` on the secrets).
   - **Task role** (used by the app itself): the S3 statements from 2.3. boto3
     picks it up through the container credentials endpoint; no keys needed.
3. **Cluster and logs:**
   `aws ecs create-cluster --cluster-name f1-dash` and
   `aws logs create-log-group --log-group-name /f1-dash/api`.
4. **Task definition** (Fargate, 1 vCPU / 4 GB, or 2 vCPU / 8 GB if training is
   slow). The key parts:

   ```json
   {
     "family": "f1-dash-api", "requiresCompatibilities": ["FARGATE"], "networkMode": "awsvpc",
     "cpu": "1024", "memory": "4096",
     "executionRoleArn": "arn:aws:iam::<acct>:role/f1-dash-exec",
     "taskRoleArn": "arn:aws:iam::<acct>:role/f1-dash-task",
     "ephemeralStorage": { "sizeInGiB": 30 },
     "containerDefinitions": [{
       "name": "api", "image": "<acct>.dkr.ecr.<region>.amazonaws.com/f1-dash-api:<tag>", "essential": true,
       "portMappings": [{ "containerPort": 8000 }],
       "environment": [
         { "name": "F1_BLOB_BACKEND", "value": "s3" }, { "name": "F1_S3_BUCKET", "value": "<bucket>" },
         { "name": "F1_S3_PREFIX", "value": "prod" },   { "name": "AWS_DEFAULT_REGION", "value": "<region>" },
         { "name": "F1_CORS_ORIGINS", "value": "https://f1.example.com" }
       ],
       "secrets": [
         { "name": "F1_DATABASE_URL",    "valueFrom": "arn:aws:ssm:<region>:<acct>:parameter/f1-dash/F1_DATABASE_URL" },
         { "name": "F1_OPENF1_USERNAME", "valueFrom": "arn:aws:ssm:<region>:<acct>:parameter/f1-dash/F1_OPENF1_USERNAME" },
         { "name": "F1_OPENF1_PASSWORD", "valueFrom": "arn:aws:ssm:<region>:<acct>:parameter/f1-dash/F1_OPENF1_PASSWORD" }
       ],
       "logConfiguration": { "logDriver": "awslogs", "options": {
         "awslogs-group": "/f1-dash/api", "awslogs-region": "<region>", "awslogs-stream-prefix": "api" } }
     }]
   }
   ```

5. **ALB.** ALBs support WebSockets natively.
   - Create an internet-facing ALB in two public subnets.
   - Create a target group with `--target-type ip --protocol HTTP --port 8000`
     and health-check path `/health`.
   - Request an ACM certificate for the domain (DNS validation).
   - Add an HTTPS :443 listener that forwards to the target group, and an
     HTTP :80 listener that redirects to HTTPS.
   - **Raise the idle timeout** so quiet sockets between laps aren't cut at the
     default 60 s:

     ```bash
     aws elbv2 modify-load-balancer-attributes --load-balancer-arn <alb-arn> \
       --attributes Key=idle_timeout.timeout_seconds,Value=3600
     ```

   - Security groups: the ALB SG allows 80/443 from anywhere; the task SG allows
     8000 from the ALB SG only; the RDS SG allows 5432 from the task SG.
6. **Service.** It must stay at exactly one copy, including during deploys:

   ```bash
   aws ecs create-service --cluster f1-dash --service-name api --task-definition f1-dash-api \
     --desired-count 1 --launch-type FARGATE \
     --deployment-configuration "minimumHealthyPercent=0,maximumPercent=100,deploymentCircuitBreaker={enable=true,rollback=true}" \
     --health-check-grace-period-seconds 120 \
     --network-configuration "awsvpcConfiguration={subnets=[<subnet-a>,<subnet-b>],securityGroups=[<task-sg>],assignPublicIp=ENABLED}" \
     --load-balancers "targetGroupArn=<tg-arn>,containerName=api,containerPort=8000"
   ```

   - **`min 0 / max 100`** means ECS stops the old task before starting the new
     one. You get a short outage on each deploy, but the live loops never run
     twice.
   - **`assignPublicIp=ENABLED`** in public subnets avoids paying for a NAT
     gateway (about $33/month).
   - **Never enable autoscaling** on this service.
7. **DNS:** an alias `A` record from the domain to the ALB.
8. **Dashboard at `/`.** Works as-is: the API redirects `/` to `/static/dashboard.html`.
9. **Redeploy:** push a new tag, register a new task-definition revision, then
   run `aws ecs update-service --cluster f1-dash --service api --task-definition f1-dash-api:<rev>`.
   If you reuse the same tag, add `--force-new-deployment` instead.

You can also serve the frontend separately from S3 + CloudFront: build it with
`node scripts/build-static.js` and `API_BASE=https://api.example.com`, then
upload `public/`. It's optional, because the API already serves the page.
CloudFront does pass WebSockets through, but the page connects to `API_BASE`
directly anyway.

---

## 4. Running it

### Update and redeploy (EC2 path)

On the laptop:

```powershell
cd C:\Users\tusha\Documents\Project\f1-dash
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $ECR
$TAG = git rev-parse --short HEAD
docker build --platform linux/amd64 -t "${ECR}/${REPO}:$TAG" -t "${ECR}/${REPO}:latest" backend
docker push "${ECR}/${REPO}:$TAG"; docker push "${ECR}/${REPO}:latest"
```

Then on the instance:

```bash
sudo f1-deploy            # or: sudo f1-deploy <git-sha>  to pin or roll back to a specific build
```

You can also run it from the laptop without opening a shell:

```powershell
aws ssm send-command --instance-ids $INSTANCE_ID --document-name AWS-RunShellScript `
  --parameters 'commands=["/usr/local/bin/f1-deploy"]' --query Command.CommandId --output text
```

Each deploy causes 30 to 60 seconds of downtime while the new container starts.
Avoid deploying during a live session: the session's loops restart, and the
director picks the session back up within `F1_LIVE_WATCH_INTERVAL`.

**Changing a secret or setting:**

- **SSM parameter:** run `aws ssm put-parameter ... --overwrite`, then
  `f1-deploy`. The container reads env vars only when it starts.
- **`app.env`:** edit `/etc/f1-dash/app.env`, then `f1-deploy`.

### Logs

```bash
docker logs -f --tail 200 f1-api       # API (rotated: 5 x 20 MB)
docker logs -f caddy                   # TLS / proxy
docker stats --no-stream               # memory and CPU per container
```

If you want logs in **CloudWatch**, add these flags to the `docker run` line in
`/usr/local/bin/f1-deploy`, replacing the two `--log-opt max-*` flags. The
instance role already allows `/f1-dash/*` log groups:

```
--log-driver awslogs --log-opt awslogs-region=us-east-2 --log-opt awslogs-group=/f1-dash/api --log-opt awslogs-create-group=true
```

After that, `docker logs` no longer works. Read the logs in the CloudWatch
console or with `aws logs tail /f1-dash/api --follow`. CloudWatch log ingestion
costs about $0.50/GB.

### Rough monthly cost (estimate only)

These are on-demand prices for us-east-1 and us-east-2, from my knowledge as of
2025. Check the AWS Pricing Calculator. New accounts may have free-tier credits
that cover some of this.

| Item | EC2 path | Fargate path |
|---|---|---|
| Compute | t3.medium ≈ $30 (t3.small ≈ $15) | 1 vCPU / 4 GB Fargate ≈ $42 |
| Disk | 30 GB gp3 ≈ $2.40 | ephemeral 30 GB ≈ $1 |
| Public IPv4 (Elastic IP or task IP; AWS charges for every public IPv4) | ≈ $3.60 | ≈ $3.60 |
| Load balancer | none | ALB ≈ $16 + LCUs ≈ $20–25 |
| Database | SQLite: $0 · RDS db.t4g.micro + 20 GB ≈ $14–15 | RDS ≈ $14–15 (required) |
| S3 (a few GB) + ECR (about 1.5 GB per kept image) | ≈ $0.50–1 | ≈ $0.50–1 |
| SSM standard parameters | free | free (Secrets Manager: $0.40/secret) |
| Route 53 hosted zone (+ domain ≈ $10–15/yr) | $0.50 | $0.50 |
| Data transfer out | first 100 GB/month free | same |
| **Total, roughly** | **≈ $37/mo SQLite, ≈ $52/mo with RDS** | **≈ $85–90/mo** |

To keep ECR storage down, delete old tags now and then, or add an ECR lifecycle
policy that keeps the last 5 images.

### Teardown (EC2 path; run in reverse order of creation)

```powershell
aws ec2 terminate-instances --instance-ids $INSTANCE_ID
aws ec2 wait instance-terminated --instance-ids $INSTANCE_ID
aws ec2 release-address --allocation-id $ALLOC

# RDS, if you created it (drop --skip-final-snapshot to keep a final snapshot instead)
aws rds delete-db-instance --db-instance-identifier f1-dash-db --skip-final-snapshot --delete-automated-backups
aws rds wait db-instance-deleted --db-instance-identifier f1-dash-db
aws ec2 delete-security-group --group-id $DBSG

aws ec2 delete-security-group --group-id $SG
aws s3 rb "s3://$BUCKET" --force                 # deletes every object, then the bucket (with versioning on, empty the versions first in the console)
aws ecr delete-repository --repository-name $REPO --force
aws ssm delete-parameters --names /f1-dash/F1_OPENF1_USERNAME /f1-dash/F1_OPENF1_PASSWORD /f1-dash/F1_DATABASE_URL
aws logs delete-log-group --log-group-name /f1-dash/api     # only if you turned on CloudWatch logs

aws iam remove-role-from-instance-profile --instance-profile-name f1-dash-ec2 --role-name f1-dash-ec2
aws iam delete-instance-profile --instance-profile-name f1-dash-ec2
aws iam detach-role-policy --role-name f1-dash-ec2 --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam detach-role-policy --role-name f1-dash-ec2 --policy-arn arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly
aws iam delete-role-policy --role-name f1-dash-ec2 --policy-name f1-dash-app
aws iam delete-role --role-name f1-dash-ec2
```

Also remove the DNS `A` record. If you're done with the domain, delete the
hosted zone (it costs $0.50/month while it exists).

If you open a new PowerShell window for teardown, the variables are gone. Look
up the IDs again with:

```powershell
aws ec2 describe-instances --filters Name=tag:Name,Values=f1-dash --query "Reservations[].Instances[].InstanceId"
aws ec2 describe-addresses
aws ec2 describe-security-groups --filters Name=group-name,Values=f1-dash-web,f1-dash-db
```

---

## 5. Troubleshooting

**The live view never updates, or the WebSocket closes right away.**

- In DevTools **Network → WS**, the socket should be
  `wss://<host>/sessions/<id>/live` with status 101. A page served over `https`
  must use `wss`. The page derives this itself from the API base.
- Caddy proxies `Upgrade` automatically and doesn't time out idle sockets.
- If you swapped in **nginx**, it needs these lines; without them the socket
  either fails or drops after 60 s:

  ```nginx
  location / {
      proxy_pass http://127.0.0.1:8000;
      proxy_http_version 1.1;
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection "upgrade";
      proxy_set_header Host $host;
      proxy_read_timeout 3600s;
  }
  ```

- On the **ALB**, raise `idle_timeout.timeout_seconds` (section 3).
- A **Cloudflare** proxy in front also supports WebSockets, but it has its own
  idle limit of about 100 s.
- To bypass the proxy, test from the instance:
  `curl -i -N -H "Connection: Upgrade" -H "Upgrade: websocket" -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" localhost:8000/sessions/1/live`
  should answer `101 Switching Protocols`.

**OpenF1 returns 401 during a session.** While a session is running, OpenF1
refuses every request that isn't authenticated, including requests for past
sessions. Check that the container has the credentials:

```bash
docker exec f1-api python -c "from app.config import settings; print('authenticated:', settings.openf1_authenticated, 'rpm:', settings.OPENF1_MAX_RPM)"
grep -c '^F1_OPENF1_' /etc/f1-dash/secrets.env     # expect 2 (prints the count only, not the values)
```

- `authenticated: False` (or `rpm: 28`) means the parameters are missing,
  misnamed (the names must be exactly `/f1-dash/F1_OPENF1_USERNAME` and
  `/f1-dash/F1_OPENF1_PASSWORD`), or were added after the container started.
  Run `f1-deploy` again.
- `True` but still 401 means OpenF1 rejected the login. Look for token errors
  with `docker logs f1-api 2>&1 | grep -i token`, then fix the password with
  `aws ssm put-parameter --overwrite` and run `f1-deploy`.
- `AccessDeniedException` from `get-parameters-by-path` in `f1-deploy` means
  the instance role's SSM statement is missing the path or the region.

**Out of memory during training or backfill.** The container gets killed and
restarted (`--restart unless-stopped`), and the job is lost. Confirm it:

```bash
docker inspect f1-api --format 'OOMKilled={{.State.OOMKilled}} restarts={{.RestartCount}}'
dmesg -T | grep -i -E 'out of memory|oom-kill' | tail
free -h
```

Fixes, cheapest first:

1. Make sure the swap file from 2.8 is active (`swapon --show`).
2. Train fewer events at a time.
3. Resize the instance. The Elastic IP and the EBS data survive this:

   ```powershell
   aws ec2 stop-instances --instance-ids $INSTANCE_ID; aws ec2 wait instance-stopped --instance-ids $INSTANCE_ID
   aws ec2 modify-instance-attribute --instance-id $INSTANCE_ID --instance-type "{\"Value\": \"t3.large\"}"
   aws ec2 start-instances --instance-ids $INSTANCE_ID
   ```

   Docker and both containers come back on their own. If PowerShell 5.1 strips
   the quotes in the JSON, use `--instance-type Value=t3.large` instead.

**Other problems:**

| Symptom | Cause / fix |
|---|---|
| `NoCredentialsError` / `Unable to locate credentials` in the API logs | The container can't reach the instance role. Set the metadata hop limit to 2: `aws ec2 modify-instance-metadata-options --instance-id $INSTANCE_ID --http-put-response-hop-limit 2 --http-tokens required`, then `f1-deploy`. |
| `could not list s3://…: AccessDenied` | The bucket name in `app.env` doesn't match the policy, or `F1_S3_PREFIX` isn't `prod` (the policy only allows `prod/*`, `bootstrap/*` and `backups/*`). |
| Dashboard loads without styles at `/` | The image predates the `/` redirect. Rebuild and redeploy, or open `/static/dashboard.html`. |
| Instance doesn't appear in Session Manager | Wait 2 to 3 minutes after launch, and check the instance profile is attached (`aws ec2 describe-iam-instance-profile-associations`). Outbound 443 must be allowed (it is by default). |
| Caddy logs `challenge failed` / no certificate | DNS doesn't point at the Elastic IP yet, or port 80 isn't open in `f1-dash-web`. Let's Encrypt rate-limits repeated failures, so fix DNS first and then `docker restart caddy`. |
| `exec format error` when the container starts | The image architecture doesn't match the instance. Build with `--platform linux/amd64` for t3, or `linux/arm64` for t4g. |
| `/health` says `"status":"degraded"` | A live loop is stale (no poll for 120 s) or has failed 3 or more times in a row. The `live` array in the same JSON has the details; check `docker logs f1-api`. |
| Docker Desktop won't start | It needs WSL2: run `wsl --install`, reboot, and turn on virtualization in the BIOS if `wsl` complains about it. |
| `docker push` fails with `no basic auth credentials` / 403 | The ECR login token expires after 12 hours. Run the `aws ecr get-login-password … | docker login …` line again. |

---

## What is deployed (current setup)

Brought up on 2026-11-04 from the architecture in section 0:

| Piece | Value |
| --- | --- |
| Dashboard | Vercel project `f1-dash-backend`, production branch `main`, `API_BASE=https://3-19-133-243.sslip.io` |
| API host | `https://3-19-133-243.sslip.io`, a Let's Encrypt certificate obtained by Caddy |
| Instance | `f1-dash`, `c7i-flex.large`, AL2023, 30 GB gp3 encrypted, IMDSv2, 2 GB swap |
| Security group | `f1-dash-web`: 80 and 443 from anywhere; no other inbound |
| SSM settings | `F1_PUBLIC_MODE=true`, `F1_LIVE_AUTOSTART=false`, `F1_PUBLIC_HOST`, `F1_CORS_ORIGINS` (the Vercel and API origins), `F1_ADMIN_TOKEN` (SecureString). No F1 TV token on this host |
| IAM | role and instance profile `f1-dash-ec2`, as in 0.2 |
| Bucket | `f1-dash-archive-<account-id>`: `prod/`, `bootstrap/`, `archive/` |
| Replays | the season's races and sprints, built on the server with the admin token |

Verified from outside:

- `https://<host>/health` 200 with a valid certificate; `http://` redirects to `https://`; `/` → the dashboard.
- `POST` without the token → 403 `read_only`.
- Telemetry 200; the CORS preflight answered, with `Access-Control-Allow-Origin` for the Vercel origin.
- The WebSocket upgrade through Caddy → `101`.
- Every Vercel asset 200, with `config.js` pointing at the API.
