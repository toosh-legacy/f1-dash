#!/usr/bin/env bash
# Redeploy the f1-dash API to its EC2 instance (see DEPLOY-AWS.md, "What is deployed").
#
#   scripts/deploy-aws.sh            ship the committed HEAD
#
# Packs HEAD with `git archive` (committed code only: .env, the DB and caches are
# never included), uploads it to s3://$BUCKET/bootstrap/src.tar.gz, then runs
# /usr/local/bin/f1-deploy on the instance via SSM Run Command, which rebuilds
# the image and replaces the container. Uncommitted changes are NOT shipped.
#
# Needs: AWS CLI v2 signed in (`aws login --region us-east-2`). Works from Git Bash.
set -euo pipefail
export MSYS_NO_PATHCONV=1

REGION="${REGION:-us-east-2}"
NAME_TAG="${NAME_TAG:-f1-dash}"

if [ -z "${AWS:-}" ]; then
  if command -v aws >/dev/null 2>&1; then AWS=aws
  else AWS="/c/Program Files/Amazon/AWSCLIV2/aws.exe"; fi
fi
aws_() { "$AWS" --region "$REGION" "$@"; }

# The bucket is named after the account, so it is worked out from whoever is
# signed in rather than written into a public repo.
if [ -z "${BUCKET:-}" ]; then
  BUCKET="f1-dash-archive-$(aws_ sts get-caller-identity --query Account --output text | tr -d '[:cntrl:]')"
fi

cd "$(git rev-parse --show-toplevel)"
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "note: uncommitted changes are not deployed; shipping HEAD $(git rev-parse --short HEAD)" >&2
fi

INSTANCE_ID="${INSTANCE_ID:-$(aws_ ec2 describe-instances \
  --filters "Name=tag:Name,Values=$NAME_TAG" "Name=instance-state-name,Values=running" \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)}"
if [ -z "$INSTANCE_ID" ] || [ "$INSTANCE_ID" = "None" ]; then
  echo "no running instance tagged Name=$NAME_TAG (stopped? start it first)" >&2; exit 1
fi

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
tarball="$tmp/src.tar.gz"
git archive --format=tar.gz HEAD > "$tarball"   # redirect: no path for git.exe to mistranslate
if tar -tzf "$tarball" | grep -qE '(^|/)\.env$'; then
  echo "refusing: a .env file is tracked in git" >&2; exit 1
fi
# The Windows AWS CLI wants a Windows path.
src="$tarball"; command -v cygpath >/dev/null 2>&1 && src="$(cygpath -w "$tarball")"
aws_ s3 cp --only-show-errors "$src" "s3://$BUCKET/bootstrap/src.tar.gz"
echo "uploaded HEAD $(git rev-parse --short HEAD) to s3://$BUCKET/bootstrap/src.tar.gz"

CMD_ID="$(aws_ ssm send-command --instance-ids "$INSTANCE_ID" \
  --document-name AWS-RunShellScript --comment "f1-deploy $(git rev-parse --short HEAD)" \
  --timeout-seconds 1800 \
  --parameters 'commands=["set -e; source /etc/f1-dash/deploy.conf; aws s3 cp --region $REGION --only-show-errors s3://$BUCKET/bootstrap/src.tar.gz /tmp/f1-src.tar.gz; tar -xzOf /tmp/f1-src.tar.gz deploy/aws/f1-deploy.sh > /usr/local/bin/f1-deploy; chmod 755 /usr/local/bin/f1-deploy; /usr/local/bin/f1-deploy"],executionTimeout=["1800"]' \
  --query Command.CommandId --output text)"
echo "running f1-deploy on $INSTANCE_ID (command $CMD_ID)..."

status=Pending
for _ in $(seq 1 180); do
  status="$(aws_ ssm get-command-invocation --command-id "$CMD_ID" --instance-id "$INSTANCE_ID" \
    --query Status --output text 2>/dev/null || echo Pending)"
  case "$status" in Pending|InProgress|Delayed) sleep 10 ;; *) break ;; esac
done

aws_ ssm get-command-invocation --command-id "$CMD_ID" --instance-id "$INSTANCE_ID" \
  --query StandardOutputContent --output text | tail -n 15
echo "status: $status"

EIP="$(aws_ ec2 describe-instances --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)"
HOST="$(aws_ ssm get-parameter --name /f1-dash/F1_PUBLIC_HOST --query Parameter.Value --output text 2>/dev/null | tr -d '[:cntrl:]' || true)"
if [ -n "$HOST" ] && [ "$HOST" != "None" ]; then URL="https://$HOST"; else URL="http://$EIP"; fi
echo "health: $(curl -s -o /dev/null -w '%{http_code}' "$URL/health" || true)  ->  $URL/"
[ "$status" = Success ]
