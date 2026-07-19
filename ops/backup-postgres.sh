#!/usr/bin/env bash
# Nightly logical Postgres backup, shipped off-box.
# Runs on the HOST (not in a container) via the systemd timer in ops/openbrainstore-backup.*.
# Requires: docker compose (for pg_dump inside the postgres container), aws-cli v2
# (works against any S3-compatible endpoint, including R2, via --endpoint-url).
#
# Required env (set in /opt/openbrainstore/.env):
#   OBS_BACKUP_S3_BUCKET     e.g. openbrainstore-backups
#   OBS_BACKUP_S3_ENDPOINT   e.g. https://<account>.r2.cloudflarestorage.com
#   AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY   R2 API token
# Optional:
#   OBS_BACKUP_RETAIN_DAILY   default 14
#   OBS_BACKUP_RETAIN_WEEKLY  default 4

set -euo pipefail

COMPOSE_DIR="/opt/openbrainstore"
ENV_FILE="$COMPOSE_DIR/.env"
RETAIN_DAILY="${OBS_BACKUP_RETAIN_DAILY:-14}"
RETAIN_WEEKLY="${OBS_BACKUP_RETAIN_WEEKLY:-4}"

cd "$COMPOSE_DIR"
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

: "${OBS_BACKUP_S3_BUCKET:?set OBS_BACKUP_S3_BUCKET in .env}"
: "${OBS_BACKUP_S3_ENDPOINT:?set OBS_BACKUP_S3_ENDPOINT in .env}"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DOW="$(date -u +%u)"  # 1 = Monday
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

echo "[backup] dumping openbrainstore at $STAMP"
docker compose exec -T postgres pg_dump -U obs -d openbrainstore --format=custom \
  | gzip -9 > "$TMP"

DAILY_KEY="daily/openbrainstore_${STAMP}.dump.gz"
aws s3 cp "$TMP" "s3://${OBS_BACKUP_S3_BUCKET}/${DAILY_KEY}" \
  --endpoint-url "$OBS_BACKUP_S3_ENDPOINT"
echo "[backup] uploaded $DAILY_KEY"

if [ "$DOW" = "1" ]; then
  WEEKLY_KEY="weekly/openbrainstore_${STAMP}.dump.gz"
  aws s3 cp "$TMP" "s3://${OBS_BACKUP_S3_BUCKET}/${WEEKLY_KEY}" \
    --endpoint-url "$OBS_BACKUP_S3_ENDPOINT"
  echo "[backup] uploaded $WEEKLY_KEY (Monday weekly copy)"
fi

prune() {
  local prefix="$1" keep="$2"
  # `Contents || []` defaults to an empty array when the prefix is empty, so
  # sort_by() doesn't error on null (which, under pipefail, would kill the
  # whole script AFTER a successful upload). Command-substitution + `|| true`
  # keeps a failed list from tripping set -e; head -n -N tolerates short input.
  local keys old
  keys="$(aws s3api list-objects-v2 --bucket "$OBS_BACKUP_S3_BUCKET" --prefix "$prefix" \
    --endpoint-url "$OBS_BACKUP_S3_ENDPOINT" \
    --query 'sort_by(Contents || `[]`, &LastModified)[].Key' --output text 2>/dev/null || true)"
  old="$(printf '%s\n' "$keys" | tr '\t ' '\n\n' | grep -vE '^(None)?$' | head -n "-$keep" || true)"
  [ -z "$old" ] && return 0
  while IFS= read -r key; do
    [ -n "$key" ] || continue
    echo "[backup] pruning old backup: $key"
    aws s3 rm "s3://${OBS_BACKUP_S3_BUCKET}/${key}" --endpoint-url "$OBS_BACKUP_S3_ENDPOINT"
  done <<< "$old"
}
prune "daily/" "$RETAIN_DAILY"
prune "weekly/" "$RETAIN_WEEKLY"

echo "[backup] done"
