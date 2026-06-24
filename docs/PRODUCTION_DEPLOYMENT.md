# Production deployment notes

This repository now contains the production code shape used by the QQ bot, but not production state.

## What is committed

- AstrBot plugins under `plugins/`
- Dora image/game services under `services/`
- GLM/Codex runner service templates:
  - `services/claude-glm-runner/`
  - `services/codex-runner/`
  - `services/rust-hook/`
  - `services/glm-code-runner/`
- Redacted production compose template: `docker-compose.prod.example.yml`
- Config examples under `config/examples/`

## What must stay private

Do not commit these directories/files from the production server:

- `.env`, `.env.*` except examples
- `secrets/`, `data/secrets/`
- `data/config/` real configs
- `data/data_v3.db`
- `data/group_memory/`, `data/group_style/`, `data/group_participation/`
- `data/temp/`, generated images, Dora sessions/projects
- `ntqq/`, `napcat/config/`
- logs, pid files, model archives, local Ollama model data

## Runner security note

The public template intentionally does not include any setuid-root helper. If a private production deployment uses host-level privileged maintenance, keep that code out of the public repository and gate it behind explicit operator approval.

## Basic production bootstrap

1. Copy examples:

```bash
cp .env.prod.example .env
mkdir -p data/config data/secrets secrets dora/projects dora/sessions
cp config/examples/claude_glm_mobile.example.json data/config/claude_glm_mobile.json
cp config/examples/claude_glm_runner.example.json data/config/claude_glm_runner.json
cp config/examples/dora_game_builder.example.json data/config/dora_game_builder.json
cp config/examples/dora_imagegen.example.json data/config/dora_imagegen.json
```

2. Fill real tokens only on the server:

```bash
printf '%s' 'real-dora-token' > data/secrets/dora_game_gateway.token
# Put Google service account JSON at secrets/gcp-service-account.json if Vertex proxy is used.
```

3. Start selected profiles:

```bash
docker compose -f docker-compose.prod.example.yml up -d astrbot napcat dora-vertex-proxy
COMPOSE_PROFILES=dora docker compose -f docker-compose.prod.example.yml up -d dora-game-gateway
```

4. For Dora game runtime image rebuild:

```bash
docker compose -f docker-compose.prod.example.yml --profile dora-build build dora-runtime-image
```
