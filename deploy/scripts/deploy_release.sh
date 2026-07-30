#!/usr/bin/env bash
set -Eeuo pipefail

environment="${1:-}"
if [[ "$environment" != "staging" && "$environment" != "production" ]]; then
  echo "usage: $0 <staging|production>" >&2
  exit 2
fi

: "${XIANYU_API_IMAGE:?XIANYU_API_IMAGE is required}"
: "${XIANYU_WORKER_IMAGE:?XIANYU_WORKER_IMAGE is required}"

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
env_file="${XIANYU_ENV_FILE:-$project_root/deploy/env/$environment.env}"
environment_override="$project_root/deploy/compose.$environment.yaml"
release_override="$project_root/deploy/compose.release.yaml"

if [[ ! -f "$env_file" ]]; then
  echo "deployment environment file not found: $env_file" >&2
  exit 2
fi

compose=(
  docker compose
  --env-file "$env_file"
  -f "$project_root/compose.yaml"
  -f "$environment_override"
  -f "$release_override"
)

export XIANYU_RELEASE="${XIANYU_RELEASE:-${XIANYU_API_IMAGE##*:}}"

"${compose[@]}" config >/dev/null
"${compose[@]}" pull postgres redis proxy migrate api worker
"${compose[@]}" up -d --remove-orphans
"${compose[@]}" ps

echo "deployed $environment release=$XIANYU_RELEASE"
