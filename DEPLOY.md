# Deploying

The dashboard splits across two hosts, and the split is forced by what the
backend actually does rather than by taste:

| Piece | Host | Why there |
|---|---|---|
| `dashboard.html` | **Vercel** | One static file, no build step. A CDN is exactly right. |
| FastAPI API | **Render** (Docker) | Holds WebSockets open, trains on background threads, needs ~1 GB of ML wheels. |
| Rows | **Render Postgres** | The container is replaceable; the data is not. |
| Model artifacts, replay gzips | **AWS S3** | Too large for a row, too durable for a container's disk. |

Vercel cannot host the API: its functions do not accept WebSocket upgrades,
have no persistent disk, and cap unzipped size at 250 MB — xgboost, pandas,
scikit-learn and fastf1 together are past that on their own.

---

## 1. S3 bucket

```bash
aws s3 mb s3://f1-dash-artifacts --region us-west-2
```

Nothing in the bucket is public; the API reads it with credentials. Create an
IAM user limited to this bucket:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"],
    "Resource": [
      "arn:aws:s3:::f1-dash-artifacts",
      "arn:aws:s3:::f1-dash-artifacts/*"
    ]
  }]
}
```

Keep its access key for step 2. Any S3-compatible service works instead —
point `F1_S3_ENDPOINT_URL` at Cloudflare R2 or MinIO and the rest is unchanged.

## 2. Render

Blueprint deploy: <https://render.com/deploy?repo=https://github.com/toosh-legacy/f1-dash>

Render reads `render.yaml`, creates the Postgres instance and the Docker web
service, and wires `F1_DATABASE_URL` between them. Four values are marked
`sync: false` and must be filled in the dashboard, because they are secrets or
depend on the Vercel URL you do not have yet:

| Variable | Value |
|---|---|
| `F1_S3_BUCKET` | `f1-dash-artifacts` |
| `AWS_DEFAULT_REGION` | `us-west-2` |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | the IAM user from step 1 |
| `F1_CORS_ORIGINS` | your Vercel origin — fill this after step 3 |

The `starter` plan is deliberate. Free instances sleep after inactivity, and a
sleeping live loop is not a live loop.

## 3. Vercel

New project: <https://vercel.com/new/clone?repository-url=https://github.com/toosh-legacy/f1-dash>

`vercel.json` already sets the build (`scripts/build-static.js`) and output
directory (`public/`). The build copies the page, the stylesheet and the `js/`
module directory, so nothing needs bundling -- the browser resolves the module
graph itself. Set one environment variable:

```
API_BASE = https://f1-dash-api.onrender.com
```

The build writes it into `public/config.js`, and the page reads it at load.
Then go back to Render and set `F1_CORS_ORIGINS` to the Vercel origin, e.g.
`https://f1-dash.vercel.app`.

Why an absolute URL rather than a Vercel rewrite to `/api`: rewrites do not
carry a WebSocket upgrade, and the live feed is a WebSocket. Pointing the page
straight at Render keeps `wss://` working, which is what `F1_CORS_ORIGINS` is
for.

## 4. Data

The database and the artifacts are gitignored, so a fresh deploy starts empty.
Two ways to fill it.

**Push what you have.** From this machine, with the deployed Postgres URL and
the bucket credentials in the environment:

```bash
export F1_DATABASE_URL="postgresql://...render.com/f1_dash"
export F1_BLOB_BACKEND=s3 F1_S3_BUCKET=f1-dash-artifacts F1_S3_PREFIX=prod
export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_DEFAULT_REGION=us-west-2

python -m app.cli seed                 # circuits, calendar, teams, drivers
python scripts/push_artifacts.py       # .joblib files and replay gzips into S3
```

`push_artifacts.py` walks the registry rows and publishes each artifact under
the key the deployed API looks for, so the first prediction request after a
deploy finds a model rather than a missing path.

**Or rebuild there.** In Render's shell:

```bash
python -m app.cli seed
python -m app.cli backfill --year 2025
```

Slower, but it needs nothing from your laptop.

## How storage works

`app/storage/blobs.py` is a mirror, not a filesystem abstraction. Callers still
write and read local paths; they bracket those with `publish()` after a write
and `materialize()` before a read. With `F1_BLOB_BACKEND=local` both are no-ops
and the code path is the one that has always run on a laptop. With `s3` the
local disk becomes a cache in front of the bucket and a cold container refills
it on first use — which is why the Render disk at `/var/cache/f1` is sized for
convenience and can be lost without losing anything.

Catalogue listings ask "is this replay built?" once per row, so that question
goes to `exists()`, answered from a bucket listing cached for 30 seconds,
rather than to `materialize()`, which would download every bundle to answer it.

## Local development is unchanged

```bash
npm start                                       # dashboard on :3000
python -m uvicorn app.main:app --reload --port 8000   # API on :8000, from backend/
```

Defaults are SQLite and local disk. Nothing above is needed to run it.
