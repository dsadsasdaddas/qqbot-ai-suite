#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import BoundedSemaphore

HOST = os.environ.get("BIND_HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8879"))
TOKEN = os.environ.get("CODE_RUNNER_TOKEN", "")
TOKEN_FILE = os.environ.get("TOKEN_FILE", "")
SANDBOX_IMAGE = os.environ.get("SANDBOX_IMAGE", "soulter/astrbot:latest")
MAX_CODE_CHARS = int(os.environ.get("MAX_CODE_CHARS", "30000"))
MAX_OUTPUT_CHARS = int(os.environ.get("MAX_OUTPUT_CHARS", "12000"))
DEFAULT_TIMEOUT = float(os.environ.get("DEFAULT_TIMEOUT", "8"))
MAX_TIMEOUT = float(os.environ.get("MAX_TIMEOUT", "15"))
MAX_CONCURRENCY = int(os.environ.get("MAX_CONCURRENCY", "2"))
MEMORY = os.environ.get("SANDBOX_MEMORY", "256m")
CPUS = os.environ.get("SANDBOX_CPUS", "0.5")
PIDS_LIMIT = os.environ.get("SANDBOX_PIDS", "64")

if TOKEN_FILE and Path(TOKEN_FILE).exists():
    TOKEN = Path(TOKEN_FILE).read_text(encoding="utf-8").strip()
if not TOKEN:
    raise SystemExit("CODE_RUNNER_TOKEN or TOKEN_FILE is required")

sem = BoundedSemaphore(MAX_CONCURRENCY)


def clamp_timeout(value) -> float:
    try:
        t = float(value)
    except Exception:
        t = DEFAULT_TIMEOUT
    return max(1.0, min(MAX_TIMEOUT, t))


def truncate(text: str) -> str:
    text = text or ""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n...[truncated {len(text) - MAX_OUTPUT_CHARS} chars]"


def run_python(code: str, timeout: float) -> dict:
    if not isinstance(code, str) or not code.strip():
        return {"ok": False, "error": "empty code"}
    if len(code) > MAX_CODE_CHARS:
        return {"ok": False, "error": f"code too large: {len(code)} > {MAX_CODE_CHARS}"}
    if not shutil.which("docker"):
        return {"ok": False, "error": "docker CLI not found"}

    run_id = "glm-code-" + uuid.uuid4().hex[:16]
    start = time.time()
    tmpdir = tempfile.mkdtemp(prefix="glm_code_")
    main_py = Path(tmpdir) / "main.py"
    main_py.write_text(code, encoding="utf-8")
    os.chmod(tmpdir, 0o755)
    os.chmod(main_py, 0o444)

    cmd = [
        "docker", "run", "--rm", "--name", run_id,
        "--network", "none",
        "--read-only",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", str(PIDS_LIMIT),
        "--memory", str(MEMORY),
        "--cpus", str(CPUS),
        "--user", "65534:65534",
        "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=32m",
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-v", f"{tmpdir}:/workspace:ro",
        SANDBOX_IMAGE,
        "python3", "-B", "/workspace/main.py",
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        returncode = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        subprocess.run(["docker", "rm", "-f", run_id], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        stdout, stderr = proc.communicate(timeout=3)
        returncode = 124
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    elapsed_ms = int((time.time() - start) * 1000)
    return {
        "ok": (returncode == 0 and not timed_out),
        "timed_out": timed_out,
        "returncode": returncode,
        "stdout": truncate(stdout),
        "stderr": truncate(stderr),
        "elapsed_ms": elapsed_ms,
        "limits": {
            "timeout_seconds": timeout,
            "memory": MEMORY,
            "cpus": CPUS,
            "network": "none",
            "image": SANDBOX_IMAGE,
        },
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "GLMCodeRunner/0.1"

    def _json(self, status: int, obj: dict):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self) -> bool:
        auth = self.headers.get("Authorization", "")
        token = self.headers.get("X-Code-Runner-Token", "")
        return auth == f"Bearer {TOKEN}" or token == TOKEN

    def do_GET(self):
        if self.path != "/health":
            self._json(404, {"ok": False, "error": "not found"})
            return
        self._json(200, {"ok": True, "service": "glm-code-runner", "image": SANDBOX_IMAGE})

    def do_POST(self):
        if self.path != "/run":
            self._json(404, {"ok": False, "error": "not found"})
            return
        if not self._authorized():
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0 or length > MAX_CODE_CHARS + 4096:
            self._json(413, {"ok": False, "error": "request too large"})
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as exc:
            self._json(400, {"ok": False, "error": f"invalid json: {exc}"})
            return
        lang = str(body.get("language", "python")).lower().strip()
        if lang not in {"python", "py", "python3"}:
            self._json(400, {"ok": False, "error": "only python is supported"})
            return
        code = body.get("code", "")
        timeout = clamp_timeout(body.get("timeout", DEFAULT_TIMEOUT))
        acquired = sem.acquire(blocking=False)
        if not acquired:
            self._json(429, {"ok": False, "error": "runner busy"})
            return
        try:
            result = run_python(code, timeout)
        finally:
            sem.release()
        self._json(200, result)

    def log_message(self, fmt, *args):
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"GLM code runner listening on {HOST}:{PORT}, image={SANDBOX_IMAGE}", flush=True)
    httpd.serve_forever()
