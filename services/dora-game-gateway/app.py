#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from aiohttp import ClientSession, WSMsgType, web

HOST = os.environ.get("BIND_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8890"))
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8890").rstrip("/")
GATEWAY_TOKEN = os.environ.get("DORA_GATEWAY_TOKEN", "") or os.environ.get("GATEWAY_TOKEN", "")
GATEWAY_TOKEN_FILE = os.environ.get("DORA_GATEWAY_TOKEN_FILE", "") or os.environ.get("GATEWAY_TOKEN_FILE", "")
if not GATEWAY_TOKEN and GATEWAY_TOKEN_FILE:
    try:
        GATEWAY_TOKEN = Path(GATEWAY_TOKEN_FILE).read_text(encoding="utf-8").strip()
    except Exception as exc:
        print(f"WARN: failed to read gateway token file {GATEWAY_TOKEN_FILE}: {exc}", flush=True)
RUNTIME_IMAGE = os.environ.get("DORA_RUNTIME_IMAGE", "qqbot/dora-runtime:latest")
DOCKER_NETWORK = os.environ.get("DORA_DOCKER_NETWORK", "qqbot_dora_games")
HOST_PROJECTS_DIR = Path(os.environ.get("HOST_PROJECTS_DIR", "/home/wzu/qqbot/dora/projects"))
SESSIONS_DIR = Path(os.environ.get("SESSIONS_DIR", "/data"))
SESSIONS_FILE = SESSIONS_DIR / "sessions.json"
CONTAINER_PREFIX = os.environ.get("CONTAINER_PREFIX", "dora-game-")
DEFAULT_TTL = int(os.environ.get("DEFAULT_TTL_SECONDS", "1800"))
MAX_TTL = int(os.environ.get("MAX_TTL_SECONDS", "7200"))
MAX_FILES = int(os.environ.get("MAX_FILES", "32"))
MAX_FILE_BYTES = int(os.environ.get("MAX_FILE_BYTES", "262144"))
MAX_TOTAL_BYTES = int(os.environ.get("MAX_TOTAL_BYTES", "1048576"))
MEMORY = os.environ.get("RUNTIME_MEMORY", "768m")
CPUS = os.environ.get("RUNTIME_CPUS", "1.0")
PIDS_LIMIT = os.environ.get("RUNTIME_PIDS", "256")
WIDTH = os.environ.get("RUNTIME_WIDTH", "1280")
HEIGHT = os.environ.get("RUNTIME_HEIGHT", "720")
RUNTIME_READ_ONLY = os.environ.get("RUNTIME_READ_ONLY", "false").lower() in {"1", "true", "yes"}
AUTO_CLEAN_INTERVAL = int(os.environ.get("AUTO_CLEAN_INTERVAL", "30"))

SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{6,48}$")
SAFE_PATH_RE = re.compile(r"^[a-zA-Z0-9_./\\-]+$")
DANGEROUS_PATTERNS = [
    re.compile(p, re.I)
    for p in [
        r"\bos\.execute\b",
        r"\bio\.popen\b",
        r"\bloadfile\b",
        r"\bdofile\b",
        r"\bpackage\.loadlib\b",
        r"require\s*\(?\s*[\"']socket",
        r"require\s*\(?\s*[\"']ssl",
        r"require\s*\(?\s*[\"']http",
        r"\bHttpClient\b",
        r"\bGit\b",
        r"/var/run/docker\.sock",
        r"\.\./",
    ]
]


def now() -> int:
    return int(time.time())


def json_response(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda x: json.dumps(x, ensure_ascii=False))


def require_api_auth(request: web.Request) -> Optional[web.Response]:
    if not GATEWAY_TOKEN:
        return None
    auth = request.headers.get("Authorization", "")
    token = request.headers.get("X-Dora-Gateway-Token", "")
    if auth == f"Bearer {GATEWAY_TOKEN}" or token == GATEWAY_TOKEN:
        return None
    return json_response({"success": False, "message": "unauthorized"}, 401)


def safe_join(base: Path, rel: str) -> Path:
    rel = rel.replace("\\", "/")
    if rel.startswith("/") or ".." in Path(rel).parts or not SAFE_PATH_RE.match(rel):
        raise ValueError(f"unsafe path: {rel}")
    path = (base / rel).resolve()
    if not str(path).startswith(str(base.resolve()) + os.sep):
        raise ValueError(f"path escapes project: {rel}")
    return path


def scan_files(files: Dict[str, str]) -> None:
    if not isinstance(files, dict) or not files:
        raise ValueError("files must be a non-empty object")
    if len(files) > MAX_FILES:
        raise ValueError(f"too many files: {len(files)} > {MAX_FILES}")
    total = 0
    for rel, content in files.items():
        if not isinstance(rel, str) or not isinstance(content, str):
            raise ValueError("file path and content must be strings")
        encoded_len = len(content.encode("utf-8"))
        if encoded_len > MAX_FILE_BYTES:
            raise ValueError(f"file too large: {rel}")
        total += encoded_len
        if total > MAX_TOTAL_BYTES:
            raise ValueError("project too large")
        suffix = Path(rel).suffix.lower()
        if suffix in {".lua", ".tl", ".yue", ".ts", ".tsx", ".xml", ".wasm"}:
            for pattern in DANGEROUS_PATTERNS:
                if pattern.search(content):
                    raise ValueError(f"dangerous code pattern in {rel}: {pattern.pattern}")


def write_project(game_id: str, files: Dict[str, str]) -> Path:
    project_dir = (HOST_PROJECTS_DIR / game_id).resolve()
    if project_dir.exists():
        shutil.rmtree(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        target = safe_join(project_dir, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    if not (project_dir / "init.lua").exists() and not (project_dir / "init.ts").exists() and not (project_dir / "init.yue").exists():
        raise ValueError("project must contain init.lua/init.ts/init.yue")
    return project_dir


def load_sessions() -> Dict[str, Dict[str, Any]]:
    try:
        if SESSIONS_FILE.exists():
            data = json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def save_sessions(sessions: Dict[str, Dict[str, Any]]) -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SESSIONS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(sessions, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SESSIONS_FILE)


def docker_cmd(args: Iterable[str], timeout: float = 60) -> subprocess.CompletedProcess[str]:
    cmd = ["docker", *args]
    return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def docker_ok(args: Iterable[str], timeout: float = 60) -> subprocess.CompletedProcess[str]:
    cp = docker_cmd(args, timeout=timeout)
    if cp.returncode != 0:
        raise RuntimeError((cp.stderr or cp.stdout or f"docker exited {cp.returncode}").strip())
    return cp


def remove_container(name: str) -> None:
    docker_cmd(["rm", "-f", name], timeout=30)


def start_container(game_id: str, project_dir: Path) -> str:
    container = CONTAINER_PREFIX + game_id
    remove_container(container)
    args = [
        "run", "-d",
        "--name", container,
        "--network", DOCKER_NETWORK,
        "--label", "qqbot.dora_game=1",
        "--label", f"qqbot.dora_game_id={game_id}",
        "--memory", MEMORY,
        "--cpus", CPUS,
        "--pids-limit", PIDS_LIMIT,
        "--security-opt", "no-new-privileges",
        "--cap-drop", "ALL",
        "--shm-size", "128m",
    ]
    if RUNTIME_READ_ONLY:
        args += [
            "--read-only",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m",
            "--tmpfs", "/workspace/runtime:rw,nosuid,nodev,size=256m",
            "--tmpfs", "/home/dora:rw,nosuid,nodev,size=128m",
        ]
    args += [
        "-e", "PROJECT_DIR=/workspace/project",
        "-e", f"WIDTH={WIDTH}",
        "-e", f"HEIGHT={HEIGHT}",
        "-v", f"{project_dir}:/workspace/project:ro",
        RUNTIME_IMAGE,
    ]
    cp = docker_ok(args, timeout=120)
    return (cp.stdout or "").strip()[:64]


def make_play_url(game_id: str, token: str) -> str:
    return f"{PUBLIC_BASE_URL}/play/{game_id}?token={token}"


def public_session(session: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(session)
    data.pop("token", None)
    return data


async def cleanup_expired(app: web.Application) -> None:
    while True:
        try:
            sessions = load_sessions()
            changed = False
            t = now()
            for game_id, session in list(sessions.items()):
                if int(session.get("expires_at", 0)) <= t:
                    remove_container(str(session.get("container") or CONTAINER_PREFIX + game_id))
                    session["status"] = "expired"
                    session["stopped_at"] = t
                    changed = True
            if changed:
                save_sessions(sessions)
        except Exception as exc:
            print(f"[cleanup] {type(exc).__name__}: {exc}", flush=True)
        await asyncio.sleep(AUTO_CLEAN_INTERVAL)


async def on_startup(app: web.Application) -> None:
    HOST_PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    app["http"] = ClientSession()
    app["cleanup_task"] = asyncio.create_task(cleanup_expired(app))


async def on_cleanup(app: web.Application) -> None:
    task = app.get("cleanup_task")
    if task:
        task.cancel()
    http = app.get("http")
    if http:
        await http.close()


async def health(request: web.Request) -> web.Response:
    docker_available = shutil.which("docker") is not None
    return json_response({
        "success": True,
        "service": "dora-game-gateway",
        "docker": docker_available,
        "runtime_image": RUNTIME_IMAGE,
        "network": DOCKER_NETWORK,
        "projects_dir": str(HOST_PROJECTS_DIR),
    })


async def create_game(request: web.Request) -> web.Response:
    auth = require_api_auth(request)
    if auth is not None:
        return auth
    try:
        body = await request.json()
    except Exception:
        return json_response({"success": False, "message": "invalid json"}, 400)
    try:
        game_id = str(body.get("game_id") or ("g" + secrets.token_hex(6)))
        if not SESSION_ID_RE.match(game_id):
            raise ValueError("invalid game_id")
        ttl = max(60, min(MAX_TTL, int(body.get("ttl_seconds") or DEFAULT_TTL)))
        title = str(body.get("title") or game_id)[:120]
        files = body.get("files")
        scan_files(files)
        project_dir = write_project(game_id, files)
        container_id = start_container(game_id, project_dir)
        token = secrets.token_urlsafe(32)
        t = now()
        session = {
            "id": game_id,
            "title": title,
            "status": "running",
            "created_at": t,
            "expires_at": t + ttl,
            "ttl_seconds": ttl,
            "project_dir": str(project_dir),
            "container": CONTAINER_PREFIX + game_id,
            "container_id": container_id,
            "url": make_play_url(game_id, token),
            "token": token,
        }
        sessions = load_sessions()
        sessions[game_id] = session
        save_sessions(sessions)
        return json_response({"success": True, "game": public_session(session), "url": session["url"]})
    except Exception as exc:
        return json_response({"success": False, "message": f"{type(exc).__name__}: {exc}"}, 400)


async def get_game(request: web.Request) -> web.Response:
    auth = require_api_auth(request)
    if auth is not None:
        return auth
    game_id = request.match_info["game_id"]
    session = load_sessions().get(game_id)
    if not session:
        return json_response({"success": False, "message": "not found"}, 404)
    # refresh runtime status from docker if possible
    container = str(session.get("container") or CONTAINER_PREFIX + game_id)
    cp = docker_cmd(["inspect", "-f", "{{.State.Status}}", container], timeout=10)
    if cp.returncode == 0:
        runtime_status = cp.stdout.strip()
        session["runtime_status"] = runtime_status
        if runtime_status == "running":
            session["status"] = "running"
        elif runtime_status in {"exited", "dead"}:
            session["status"] = runtime_status
    return json_response({"success": True, "game": public_session(session)})


async def list_games(request: web.Request) -> web.Response:
    auth = require_api_auth(request)
    if auth is not None:
        return auth
    sessions = load_sessions()
    return json_response({"success": True, "games": [public_session(v) for v in sessions.values()]})


async def delete_game(request: web.Request) -> web.Response:
    auth = require_api_auth(request)
    if auth is not None:
        return auth
    game_id = request.match_info["game_id"]
    sessions = load_sessions()
    session = sessions.get(game_id)
    if not session:
        return json_response({"success": False, "message": "not found"}, 404)
    remove_container(str(session.get("container") or CONTAINER_PREFIX + game_id))
    session["status"] = "stopped"
    session["stopped_at"] = now()
    save_sessions(sessions)
    return json_response({"success": True, "game": public_session(session)})


async def game_logs(request: web.Request) -> web.Response:
    auth = require_api_auth(request)
    if auth is not None:
        return auth
    game_id = request.match_info["game_id"]
    session = load_sessions().get(game_id)
    if not session:
        return json_response({"success": False, "message": "not found"}, 404)
    tail = str(max(1, min(1000, int(request.query.get("tail", "200")))))
    container = str(session.get("container") or CONTAINER_PREFIX + game_id)
    cp = docker_cmd(["logs", "--tail", tail, container], timeout=20)
    return json_response({"success": cp.returncode == 0, "logs": (cp.stdout or "") + (cp.stderr or "")})


def validate_play_token(request: web.Request, session: Dict[str, Any], game_id: str) -> bool:
    token = request.query.get("token") or request.cookies.get(f"dora_play_{game_id}") or ""
    if not token or token != session.get("token"):
        return False
    if int(session.get("expires_at", 0)) <= now():
        return False
    return True


async def play_entry(request: web.Request) -> web.StreamResponse:
    game_id = request.match_info["game_id"]
    sessions = load_sessions()
    session = sessions.get(game_id)
    if not session:
        return web.Response(status=404, text="game not found")
    if not validate_play_token(request, session, game_id):
        return web.Response(status=403, text="forbidden")
    # noVNC page, with websockify path routed back through this gateway.
    location = f"/play/{game_id}/vnc.html?autoconnect=1&resize=scale&path=play/{game_id}/websockify"
    resp = web.HTTPFound(location)
    resp.set_cookie(f"dora_play_{game_id}", str(session.get("token")), max_age=max(60, int(session.get("expires_at", 0)) - now()), httponly=True, samesite="Lax")
    return resp


async def proxy_http(request: web.Request, session: Dict[str, Any], rest: str) -> web.StreamResponse:
    container = str(session.get("container"))
    if not rest:
        rest = "vnc.html"
    target = f"http://{container}:6080/{rest}"
    if request.query_string:
        # Do not forward our auth token to the backend; it does not need it.
        query = [(k, v) for k, v in request.query.items() if k != "token"]
    else:
        query = []
    http: ClientSession = request.app["http"]
    headers = {k: v for k, v in request.headers.items() if k.lower() not in {"host", "connection", "upgrade", "content-length"}}
    data = await request.read() if request.can_read_body else None
    async with http.request(request.method, target, params=query, headers=headers, data=data, timeout=30) as backend:
        body = await backend.read()
        response_headers = {k: v for k, v in backend.headers.items() if k.lower() not in {"transfer-encoding", "connection", "content-encoding", "content-length"}}
        return web.Response(status=backend.status, body=body, headers=response_headers)


async def proxy_ws(request: web.Request, session: Dict[str, Any], rest: str) -> web.StreamResponse:
    container = str(session.get("container"))
    ws_server = web.WebSocketResponse(heartbeat=30)
    await ws_server.prepare(request)
    scheme = "ws"
    target = f"{scheme}://{container}:6080/{rest or 'websockify'}"
    if request.query_string:
        query = [(k, v) for k, v in request.query.items() if k != "token"]
    else:
        query = []
    http: ClientSession = request.app["http"]
    async with http.ws_connect(target, params=query, heartbeat=30, max_msg_size=16 * 1024 * 1024) as ws_client:
        async def client_to_backend() -> None:
            async for msg in ws_server:
                if msg.type == WSMsgType.TEXT:
                    await ws_client.send_str(msg.data)
                elif msg.type == WSMsgType.BINARY:
                    await ws_client.send_bytes(msg.data)
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.ERROR):
                    await ws_client.close()
                    break

        async def backend_to_client() -> None:
            async for msg in ws_client:
                if msg.type == WSMsgType.TEXT:
                    await ws_server.send_str(msg.data)
                elif msg.type == WSMsgType.BINARY:
                    await ws_server.send_bytes(msg.data)
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.ERROR):
                    await ws_server.close()
                    break

        await asyncio.gather(client_to_backend(), backend_to_client(), return_exceptions=True)
    return ws_server


async def play_proxy(request: web.Request) -> web.StreamResponse:
    game_id = request.match_info["game_id"]
    rest = request.match_info.get("rest", "")
    sessions = load_sessions()
    session = sessions.get(game_id)
    if not session:
        return web.Response(status=404, text="game not found")
    if not validate_play_token(request, session, game_id):
        return web.Response(status=403, text="forbidden")
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await proxy_ws(request, session, rest)
    return await proxy_http(request, session, rest)


def create_app() -> web.Application:
    app = web.Application(client_max_size=MAX_TOTAL_BYTES + 1024 * 64)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/health", health)
    app.router.add_post("/api/games", create_game)
    app.router.add_get("/api/games", list_games)
    app.router.add_get("/api/games/{game_id}", get_game)
    app.router.add_delete("/api/games/{game_id}", delete_game)
    app.router.add_get("/api/games/{game_id}/logs", game_logs)
    app.router.add_get("/play/{game_id}", play_entry)
    app.router.add_route("*", "/play/{game_id}/{rest:.*}", play_proxy)
    return app


if __name__ == "__main__":
    if not GATEWAY_TOKEN:
        print("WARN: DORA_GATEWAY_TOKEN is empty; API is unauthenticated. Set it in production.", flush=True)
    web.run_app(create_app(), host=HOST, port=PORT)
