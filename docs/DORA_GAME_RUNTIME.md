# Dora SSR game runtime

This stack lets the QQ bot generate a Dora SSR mini-game and expose a safe web
preview of the **native Dora runtime**.

It does not turn Dora into a browser canvas game. Instead it streams the native
runtime window:

```text
Dora SSR Runtime -> Xvfb -> x11vnc -> noVNC/websockify -> dora-game-gateway -> HTTP/HTTPS
```

## Current runtime design

- The bot writes a generated Dora Lua project to `dora/projects/<game_id>/`.
- `dora-game-gateway` static-scans the generated files, then starts one Docker
  runtime container per game.
- `dora-runtime` creates a temporary asset root inside the container:
  - generated project files are symlinked into `/tmp/dora-assets`;
  - built-in Dora `Font/` and `Image/` folders are also linked in;
  - Dora is launched directly with `dora-ssr --asset /tmp/dora-assets`.
- The native Dora window is rendered in `Xvfb` and streamed by `x11vnc` +
  `websockify`/`noVNC`.

## Security model

- Dora runs inside one short-lived Docker container per game.
- Runtime containers are on an internal Docker network (`qqbot_dora_games`).
- VNC `5900` and noVNC `6080` are not published; only the gateway is exposed.
- Public access goes only through `dora-game-gateway` and short-lived tokens.
- Generated project files are mounted read-only into the runtime container.
- Gateway static-scans obvious dangerous APIs before starting a container.
- Runtime containers drop Linux capabilities and use CPU/memory/PID limits.

## Build runtime image

```bash
cd /home/wzu/qqbot
docker compose --profile dora-build build dora-runtime-image
```

If the Dora package name/source changes, override `DORA_INSTALL_COMMAND` while
building `services/dora-runtime`.

## Start gateway

Create a shared secret:

```bash
mkdir -p data/secrets dora/projects dora/sessions
openssl rand -hex 32 > data/secrets/dora_game_gateway.token
```

Start the gateway:

```bash
docker compose --profile dora up -d dora-game-gateway
```

Health check:

```bash
curl http://127.0.0.1:8890/health
```

## AstrBot plugin config

`data/config/dora_game_builder.json`:

```json
{
  "enabled": true,
  "gateway_url": "http://dora-game-gateway:8890",
  "gateway_token_file": "/AstrBot/data/secrets/dora_game_gateway.token",
  "glm_provider_id": "glm5_default",
  "ttl_seconds": 1800,
  "admin_only_create": true,
  "allowed_user_ids": ["1939455790"]
}
```

Make sure the same token file is available to AstrBot, or put the token in
`gateway_token` directly.

## Commands

```text
/做游戏 做一个躲避弹幕小游戏，玩家是猫，30秒计分
/游戏 状态 <id>
/游戏 日志 <id>
/游戏 停止 <id>
/游戏 列表
```

## Public preview URL

For private Tailnet testing, the gateway can bind to `100.70.188.115:8890` and
set:

```text
PUBLIC_BASE_URL=http://100.70.188.115:8890
```

For normal QQ group users, put Nginx/Caddy in front of the gateway and expose
only HTTPS:

```nginx
location / {
  proxy_pass http://127.0.0.1:8890;
  proxy_http_version 1.1;
  proxy_set_header Host $host;
  proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header Upgrade $http_upgrade;
  proxy_set_header Connection "upgrade";
}
```

Then set `PUBLIC_BASE_URL=https://game.example.com` on the gateway.
