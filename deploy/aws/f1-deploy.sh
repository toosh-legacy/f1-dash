#!/bin/bash
# Build and (re)start f1-dash on the EC2 instance from s3://$BUCKET/bootstrap/src.tar.gz.
#
# Installed to /usr/local/bin/f1-deploy and run by scripts/deploy-aws.sh over
# SSM Run Command; every deploy installs the copy from the code it ships, so
# this file in the repo is the one that runs.
#
#   /etc/f1-dash/deploy.conf   REGION, BUCKET            (written by user-data)
#   /etc/f1-dash/app.env       static container settings  (written by user-data)
#   SSM /f1-dash/NAME          one env var per parameter, secrets as SecureString
#
# With F1_PUBLIC_HOST set (an SSM parameter), Caddy fronts the API on 80/443
# and fetches a Let's Encrypt certificate for that name; without it the API
# answers plain HTTP on port 80.
set -euo pipefail
exec > >(tee -a /var/log/f1-deploy.log) 2>&1
echo "=== f1-deploy $(date -Is)"
source /etc/f1-dash/deploy.conf
umask 077

# 1. Env file: static settings + every /f1-dash/NAME SSM parameter as NAME=value.
tmp=/etc/f1-dash/env.tmp
cp /etc/f1-dash/app.env "$tmp"
aws ssm get-parameters-by-path --region "$REGION" --path /f1-dash/ --with-decryption \
    --query 'Parameters[].[Name,Value]' --output text \
  | awk -F'\t' '{ n = $1; sub(".*/", "", n); print n "=" $2 }' >> "$tmp"
mv "$tmp" /etc/f1-dash/env
chmod 600 /etc/f1-dash/env
echo "env: $(wc -l < /etc/f1-dash/env) lines (values not shown)"
PUBLIC_HOST="$(grep '^F1_PUBLIC_HOST=' /etc/f1-dash/env | tail -n1 | cut -d= -f2- || true)"

# 2. Source + image.
umask 022
rm -rf /opt/f1-dash/src && mkdir -p /opt/f1-dash/src
aws s3 cp --region "$REGION" --only-show-errors "s3://$BUCKET/bootstrap/src.tar.gz" /opt/f1-dash/src.tar.gz
tar -xzf /opt/f1-dash/src.tar.gz -C /opt/f1-dash/src
docker build -t f1-dash:latest /opt/f1-dash/src/backend

# 3. First run only: restore the laptop's data onto the volume. The DB goes
#    last, so its presence means the restore finished.
if [ ! -f /var/cache/f1/f1_dashboard.db ]; then
  echo "restoring bootstrap data"
  aws s3 sync --region "$REGION" --only-show-errors "s3://$BUCKET/bootstrap/data/model_artifacts" /var/cache/f1/model_artifacts
  aws s3 sync --region "$REGION" --only-show-errors "s3://$BUCKET/bootstrap/data/replays" /var/cache/f1/replays
  aws s3 cp --region "$REGION" --only-show-errors "s3://$BUCKET/bootstrap/data/f1_dashboard.db" /var/cache/f1/f1_dashboard.db.part
  mv /var/cache/f1/f1_dashboard.db.part /var/cache/f1/f1_dashboard.db
fi

# 4. Replace the API container. Old one goes first: never two copies of the
#    live loops. It joins a private network; only Caddy is on the host ports.
docker network create f1 >/dev/null 2>&1 || true
docker rm -f f1-api 2>/dev/null || true
if [ -n "$PUBLIC_HOST" ]; then PORTS=(-p 127.0.0.1:8000:8000); else PORTS=(-p 80:8000); fi
docker run -d --name f1-api --network f1 --restart unless-stopped \
  "${PORTS[@]}" \
  --env-file /etc/f1-dash/env \
  -v /var/cache/f1:/var/cache/f1 \
  --log-opt max-size=20m --log-opt max-file=5 \
  f1-dash:latest

# 5. HTTPS in front, when there is a name to serve.
docker rm -f caddy 2>/dev/null || true
if [ -n "$PUBLIC_HOST" ]; then
  mkdir -p /var/cache/caddy
  cat > /etc/f1-dash/Caddyfile <<EOF
$PUBLIC_HOST {
	encode gzip
	# WebSockets (/sessions/{id}/live) pass straight through.
	reverse_proxy f1-api:8000
}
EOF
  # Certificates live in /var/cache/caddy, so a redeploy never asks again.
  docker run -d --name caddy --network f1 --restart unless-stopped \
    -p 80:80 -p 443:443 \
    -v /etc/f1-dash/Caddyfile:/etc/caddy/Caddyfile:ro \
    -v /var/cache/caddy:/data \
    --log-opt max-size=10m --log-opt max-file=3 \
    caddy:2
  echo "caddy: serving https://$PUBLIC_HOST"
fi
docker image prune -f >/dev/null || true

if [ -n "$PUBLIC_HOST" ]; then HEALTH=http://127.0.0.1:8000/health; else HEALTH=http://127.0.0.1/health; fi
for i in $(seq 1 30); do
  if curl -fsS "$HEALTH" >/dev/null 2>&1; then
    echo "healthy: $(curl -fsS "$HEALTH")"; exit 0
  fi
  sleep 5
done
echo "container did not become healthy; last logs:"
docker logs --tail 50 f1-api
exit 1
