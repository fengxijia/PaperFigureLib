#!/bin/bash
# Publish the public static site.
#
#   bash deploy/publish-site.sh cloudflare   # Pages (html + data) + R2 (figures)
#   bash deploy/publish-site.sh s3           # everything into one S3-compatible bucket via rclone
#
# Reads site.env (see site.env.example). Cloudflare mode needs an rclone remote named "r2":
#   rclone config create r2 s3 provider=Cloudflare access_key_id=$R2_ACCESS_KEY_ID \
#     secret_access_key=$R2_SECRET_ACCESS_KEY endpoint=https://$CLOUDFLARE_ACCOUNT_ID.r2.cloudflarestorage.com
set -euo pipefail
MODE=${1:-cloudflare}
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DATA=${FIGLIB_DATA:-$APP_DIR/data}
SITE=$DATA/site
ENV=${PAPERFIGURE_ENV:-$APP_DIR/site.env}
PY=${PYTHON:-python3}
RCLONE=${RCLONE:-rclone}

set -a; . "$ENV"; set +a
cd "$APP_DIR"

case "$MODE" in
  cloudflare)
    "$PY" -m figlib.cli site --data "$DATA" --out "$SITE" --img-base "${IMG_BASE:?}"
    # figures -> R2; only new or changed files move
    "$RCLONE" sync "$SITE/figs" "r2:${R2_BUCKET:?}/figs" --transfers 24 --checkers 32 --fast-list \
      --header-upload "Cache-Control: public, max-age=31536000, immutable" --stats 60s --stats-one-line
    # page + data -> Pages (Pages caps a deployment at 20,000 files, so the figures stay on R2)
    rm -rf "$SITE/figs"
    CLOUDFLARE_API_TOKEN="$CLOUDFLARE_API_TOKEN" CLOUDFLARE_ACCOUNT_ID="$CLOUDFLARE_ACCOUNT_ID" \
      npx --yes wrangler pages deploy "$SITE" --project-name "${CF_PAGES_PROJECT:?}" --commit-dirty=true
    echo "published: https://${SITE_DOMAIN:-$CF_PAGES_PROJECT.pages.dev}/"
    ;;
  s3)
    "$PY" -m figlib.cli site --data "$DATA" --out "$SITE" --img-base ""
    "$RCLONE" sync "$SITE" "${S3_REMOTE:?}" --transfers 16 --checkers 32 --fast-list --progress
    echo "published to $S3_REMOTE"
    ;;
  *) echo "usage: $0 cloudflare|s3"; exit 2 ;;
esac
