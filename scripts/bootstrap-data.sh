#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$ROOT/data/plugins" "$ROOT/data/config" "$ROOT/secrets" "$ROOT/napcat/config"
for p in dora_imagegen glm_router glm_code_space group_memory group_style_learner group_participation egg_persona gemma_chat_style help_guard prompt_change_guard redteam_mode; do
  rm -rf "$ROOT/data/plugins/$p"
  cp -R "$ROOT/plugins/$p" "$ROOT/data/plugins/$p"
done
for ex in "$ROOT"/config/examples/*.example.json; do
  name="$(basename "$ex" .example.json).json"
  if [[ ! -f "$ROOT/data/config/$name" ]]; then
    cp "$ex" "$ROOT/data/config/$name"
  fi
done
if [[ ! -f "$ROOT/.env" ]]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
fi
if [[ ! -f "$ROOT/secrets/glm_code_runner.token" ]]; then
  python3 - <<'PY' > "$ROOT/secrets/glm_code_runner.token"
import secrets
print(secrets.token_hex(32))
PY
fi
chmod 600 "$ROOT/secrets/glm_code_runner.token" || true
cat <<MSG
Bootstrap done.
Next:
1. edit .env
2. put Google service-account JSON at secrets/gcp-service-account.json
3. copy secrets/glm_code_runner.token into data/config/glm_code_space.json runner_token
4. set QQ admin/user IDs in config examples copied under data/config
5. docker compose -f docker-compose.example.yml up -d
MSG
