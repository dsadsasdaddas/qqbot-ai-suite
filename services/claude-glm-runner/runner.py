#!/usr/bin/env python3
import datetime as dt
import http.server
import json
import os
import re
import subprocess
import time
import threading
import traceback
import uuid
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Dict, List, Tuple

TOKEN = os.environ.get("CLAUDE_GLM_RUNNER_TOKEN", "")
PORT = int(os.environ.get("CLAUDE_GLM_RUNNER_PORT", "8790"))
WORKSPACE = Path(os.environ.get("CLAUDE_GLM_WORKSPACE", "/home/wzu/qqbot"))
STATE_DIR = Path(os.environ.get("CLAUDE_GLM_STATE_DIR", str(WORKSPACE / ".agent_state")))
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/home/wzu/.local/bin/claude")
CLAUDE_CONFIG_DIR = Path(os.environ.get("ANTHROPIC_CONFIG_DIR", str(WORKSPACE / ".claude-glm")))
DEFAULT_MODEL = os.environ.get("CLAUDE_GLM_CLAUDE_MODEL", "claude-sonnet-4-5")
GLM_DEFAULT_MODEL = os.environ.get("GLM_MODEL", "glm-5.2")
MAX_REPLY = int(os.environ.get("CLAUDE_GLM_MAX_REPLY", "12000"))
RUN_LOCK = threading.Lock()
CURRENT_JOB = {"running": False, "task": "", "started_at": ""}
OWNER_UID = int(os.environ.get("AGENT_FILE_UID", "1000"))
OWNER_GID = int(os.environ.get("AGENT_FILE_GID", "1000"))


def clip(s: str, n: int = MAX_REPLY) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + f"\n...[省略 {len(s)-n} 字]"


def now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S %z")


def chown_relaxed(path: Path) -> None:
    try:
        os.chown(str(path), OWNER_UID, OWNER_GID)
    except Exception:
        pass


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    chown_relaxed(path)
    chown_relaxed(path.parent)


def append_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(text)
    chown_relaxed(path)
    chown_relaxed(path.parent)


def run(args: List[str], cwd: str = None, env: Dict[str, str] = None, timeout: int = 60, input_text: str = None) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(args, cwd=cwd, env=env, input=input_text, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired as e:
        return 124, e.stdout or "", (e.stderr or "") + f"\nTIMEOUT after {timeout}s"
    except Exception as e:
        return 126, "", f"{type(e).__name__}: {e}"


def state_paths() -> Dict[str, Path]:
    return {
        "readme": STATE_DIR / "README.md",
        "tasks": STATE_DIR / "TASKS.md",
        "memory": STATE_DIR / "MEMORY.md",
        "handoff": STATE_DIR / "HANDOFF.md",
        "log": STATE_DIR / "LOG.md",
        "last": STATE_DIR / "LAST_RESULT.md",
    }


def ensure_state() -> None:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CLAUDE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    for p in [WORKSPACE, STATE_DIR, CLAUDE_CONFIG_DIR]:
        chown_relaxed(p)
    defaults = {
        "readme": "# Claude GLM Agent State\n\n这是 Claude Code CLI + GLM 离线注入长程任务状态目录。\n\n- MEMORY.md：长期事实/环境/决策\n- TASKS.md：任务队列\n- HANDOFF.md：交接给下次任务\n- LOG.md：执行日志\n- LAST_RESULT.md：最近一次结果\n",
        "tasks": "# TASKS\n\n- [ ] 维护 QQBot 的 Claude Code + GLM 长程工作区。\n",
        "memory": "# MEMORY\n\n- 长期工作区：/home/wzu/qqbot\n- Claude Code 通过本地 Anthropic 兼容代理离线注入 GLM，不使用 Claude Auth。\n- 默认 YOLO：--dangerously-skip-permissions + permission-mode bypassPermissions。\n- 宿主机根目录在 runner 内映射为 /host；Docker socket 可用。\n",
        "handoff": "# HANDOFF\n\n下一次任务开始前读取 MEMORY.md、TASKS.md、LOG.md 尾部。\n",
        "log": "# LOG\n\n",
        "last": "# LAST RESULT\n\n暂无。\n",
    }
    p = state_paths()
    for k, text in defaults.items():
        if not p[k].exists():
            write_text(p[k], text)
    claude_md = WORKSPACE / "CLAUDE.md"
    if not claude_md.exists():
        write_text(claude_md, """# CLAUDE.md

这是 QQBot 的 Claude Code 离线注入工作区说明。

## 运行方式
- 你通过 Claude Code CLI 运行，但模型请求会被本地 Anthropic 兼容代理转发给 GLM。
- 不依赖 Claude Auth，不调用 Anthropic 官方账号。
- 默认 YOLO 权限：`--dangerously-skip-permissions` 和 `--permission-mode bypassPermissions`。
- 长期工作区：`/home/wzu/qqbot`。
- 长期状态目录：`/home/wzu/qqbot/.agent_state`。

## 长程任务规则
- 每次开始先读取 `.agent_state/MEMORY.md`、`TASKS.md`、`HANDOFF.md`、`LOG.md` 尾部。
- 重要事实、决策、待办、交接必须写回 `.agent_state`。
- 不要依赖 QQ 聊天上下文保存长期状态。
- 修改机器人提示词/人格/全局规则之前必须得到用户明确授权；普通代码和部署配置可按任务修改。

## 权限与执行
- runner 里可用 `/host` 访问宿主机根目录。
- runner 挂载 Docker socket，可操作 Docker。
- 需要执行命令时优先使用 Bash 工具。
""")


def tail(path: Path, chars: int = 5000) -> str:
    try:
        s = path.read_text(encoding="utf-8", errors="replace")
        return s[-chars:] if len(s) > chars else s
    except Exception:
        return ""


def state_context() -> str:
    ensure_state()
    p = state_paths()
    return f"""
【长期状态目录】{STATE_DIR}

--- MEMORY.md ---
{tail(p['memory'], 5000)}

--- TASKS.md ---
{tail(p['tasks'], 5000)}

--- HANDOFF.md ---
{tail(p['handoff'], 5000)}

--- LOG tail ---
{tail(p['log'], 7000)}
""".strip()


def log_result(agent: str, task: str, result: str) -> None:
    ensure_state()
    p = state_paths()
    entry = f"\n## {now()} [{agent}]\n\nworkspace: `{WORKSPACE}`\n\n任务：\n{clip(task, 2000)}\n\n结果：\n{clip(result, 5000)}\n\n"
    append_text(p["log"], entry)
    write_text(p["last"], f"# LAST RESULT\n\n时间：{now()}\n\nagent: {agent}\n\n任务：\n{task}\n\n结果：\n{clip(result, 8000)}\n")
    try:
        subprocess.run(["chown", "-R", f"{OWNER_UID}:{OWNER_GID}", str(STATE_DIR), str(WORKSPACE / "CLAUDE.md"), str(CLAUDE_CONFIG_DIR)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
    except Exception:
        pass


def normalize_key(v) -> str:
    if isinstance(v, list):
        for x in v:
            x = str(x or "").strip()
            if x:
                return x
        return ""
    return str(v or "").strip()


def load_glm_provider() -> Dict[str, Any]:
    for p in [Path("/home/wzu/qqbot/data/cmd_config.json"), Path("/host/home/wzu/qqbot/data/cmd_config.json")]:
        if not p.exists():
            continue
        data = json.loads(p.read_text(encoding="utf-8-sig", errors="replace"))
        chosen = None
        for item in data.get("provider") or []:
            if item.get("id") == "glm5_default":
                chosen = item; break
        if chosen is None:
            for item in data.get("provider") or []:
                if "zhipu" in str(item.get("type", "")).lower() or "bigmodel" in str(item.get("api_base", "")).lower():
                    chosen = item; break
        if chosen:
            mc = chosen.get("model_config") or {}
            return {"key": normalize_key(chosen.get("key")), "api_base": chosen.get("api_base") or "https://open.bigmodel.cn/api/paas/v4/", "model": mc.get("model") or GLM_DEFAULT_MODEL, "temperature": mc.get("temperature", 0.7)}
    return {"key": "", "api_base": "https://open.bigmodel.cn/api/paas/v4/", "model": GLM_DEFAULT_MODEL, "temperature": 0.7}


def glm_chat(messages: List[Dict[str, str]], timeout: int = 240, temperature: float = None) -> Dict[str, Any]:
    cfg = load_glm_provider()
    key = os.environ.get("GLM_API_KEY") or cfg.get("key")
    if not key:
        return {"ok": False, "error": "GLM key not found"}
    url = (cfg.get("api_base") or "https://open.bigmodel.cn/api/paas/v4/").rstrip("/") + "/chat/completions"
    body = {"model": cfg.get("model") or GLM_DEFAULT_MODEL, "messages": messages, "temperature": cfg.get("temperature", 0.7) if temperature is None else temperature}
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode(), headers={"Content-Type": "application/json", "Authorization": "Bearer " + key}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
        data = json.loads(raw)
        content = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        return {"ok": True, "content": content, "raw": data, "model": body["model"]}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}: " + e.read().decode("utf-8", "replace")[:1200]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def repair_glm_json_like(s: str) -> str:
    # GLM sometimes emits near-JSON like {"type":"text","hello"}
    # Claude expects {"type":"text","text":"hello"}.
    s = re.sub(r'(\{\s*"type"\s*:\s*"text"\s*),\s*"([^"{}\[\]]*)"', r'\1, "text": "\2"', s)
    # Also repair content list text block with Chinese punctuation/newlines escaped poorly in simple cases.
    return s

def extract_json(text: str) -> Dict[str, Any]:
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    candidates = [s, repair_glm_json_like(s)]
    m = re.search(r"\{.*\}", s, re.S)
    if m:
        candidates.append(m.group(0))
        candidates.append(repair_glm_json_like(m.group(0)))
    last = None
    for c in candidates:
        try:
            return json.loads(c)
        except Exception as e:
            last = e
    raise last or ValueError("no JSON object")


def block_to_text(block: Any) -> str:
    if isinstance(block, str):
        return block
    if not isinstance(block, dict):
        return str(block)
    typ = block.get("type")
    if typ == "text":
        return block.get("text", "")
    if typ == "tool_use":
        return "[assistant tool_use] " + json.dumps({"id": block.get("id"), "name": block.get("name"), "input": block.get("input")}, ensure_ascii=False)
    if typ == "tool_result":
        c = block.get("content", "")
        if isinstance(c, list):
            c = "\n".join(block_to_text(x) for x in c)
        return "[tool_result id=%s is_error=%s]\n%s" % (block.get("tool_use_id"), block.get("is_error"), c)
    return "[block] " + json.dumps(block, ensure_ascii=False)[:2000]


def anthropic_messages_to_prompt(body: Dict[str, Any]) -> str:
    parts = []
    sys = body.get("system")
    if isinstance(sys, str):
        parts.append("<system>\n" + sys + "\n</system>")
    elif isinstance(sys, list):
        parts.append("<system>\n" + "\n".join(block_to_text(x) for x in sys) + "\n</system>")
    for m in body.get("messages") or []:
        role = m.get("role", "user")
        c = m.get("content", "")
        if isinstance(c, list):
            text = "\n".join(block_to_text(x) for x in c)
        else:
            text = str(c)
        parts.append(f"<{role}>\n{text}\n</{role}>")
    return "\n\n".join(parts)


def tool_names(body: Dict[str, Any]) -> List[str]:
    return [str(t.get("name")) for t in (body.get("tools") or []) if isinstance(t, dict) and t.get("name")]


def anthropic_response_from_glm(body: Dict[str, Any]) -> Dict[str, Any]:
    names = tool_names(body)
    prompt = anthropic_messages_to_prompt(body)
    has_tool_result = "[tool_result" in prompt
    system = f"""
你正在扮演 Claude Code CLI 背后的模型，但实际模型是 GLM。Claude Code 会执行你返回的 tool_use。
你必须只输出 JSON，不要 Markdown。

可用工具：{', '.join(names[:30])}
最常用工具是 Bash，input schema 近似：{{"command":"shell 命令","description":"一句话描述","timeout":毫秒}}。

返回 JSON schema：
{{
  "content": [
    {{"type":"text","text":"给用户看的简短文字"}},
    {{"type":"tool_use","name":"Bash","input":{{"command":"...","description":"...","timeout":120000}}}}
  ],
  "stop_reason": "tool_use 或 end_turn"
}}

规则：
- 如果需要查看文件/改代码/执行任务，返回 1 个 Bash tool_use；Claude Code 会在 YOLO 模式执行。
- 修改文件可用 python/sed/cat heredoc 等 Bash 命令完成。
- 命令默认在 Claude Code 当前工作区执行，即 /home/wzu/qqbot。
- 需要宿主机根目录时用 /host/...。
- Claude 进程不是 root；需要宿主机 root 权限时应通过人工审核后的独立运维流程执行。
- 每次长期任务结束前，重要交接写入 .agent_state/HANDOFF.md，事实写 MEMORY.md，待办写 TASKS.md。
- 如果已经看到 tool_result 且任务完成，返回 end_turn，不要继续调用工具。
- 输出必须是 JSON 对象。
""".strip()
    user = state_context() + "\n\n【Claude Code 请求】\n" + prompt
    res = glm_chat([{"role": "system", "content": system}, {"role": "user", "content": user}], timeout=240, temperature=0.2)
    if not res.get("ok"):
        txt = "GLM proxy error: " + str(res.get("error"))
        content = [{"type": "text", "text": txt}]
        stop = "end_turn"
    else:
        raw = res.get("content") or ""
        try:
            obj = extract_json(raw)
            content = obj.get("content") or [{"type": "text", "text": raw}]
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            stop = obj.get("stop_reason") or ("tool_use" if any(isinstance(x, dict) and x.get("type") == "tool_use" for x in content) else "end_turn")
        except Exception:
            content = [{"type": "text", "text": raw}]
            stop = "end_turn"
    fixed = []
    for b in content:
        if not isinstance(b, dict):
            fixed.append({"type": "text", "text": str(b)}); continue
        if b.get("type") == "tool_use":
            name = b.get("name") or "Bash"
            inp = b.get("input") or {}
            if name == "Bash":
                if isinstance(inp, str):
                    inp = {"command": inp}
                inp.setdefault("description", "Run shell command")
                inp.setdefault("timeout", 120000)
            fixed.append({"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:24], "name": name, "input": inp})
        else:
            fixed.append({"type": "text", "text": str(b.get("text", ""))})
    if any(b.get("type") == "tool_use" for b in fixed):
        stop = "tool_use"
    return {"id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant", "model": body.get("model") or DEFAULT_MODEL, "content": fixed, "stop_reason": stop, "stop_sequence": None, "usage": {"input_tokens": max(1, len(prompt)//4), "output_tokens": 128}}


def claude_env() -> Dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = "/home/wzu"
    env["PATH"] = "/home/wzu/.local/bin:/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")
    env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{PORT}"
    env["ANTHROPIC_API_KEY"] = "glm-offline"
    env["ANTHROPIC_CONFIG_DIR"] = str(CLAUDE_CONFIG_DIR)
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    env["DISABLE_TELEMETRY"] = "1"
    env["OTEL_METRICS_EXPORTER"] = "none"
    env["OTEL_LOGS_EXPORTER"] = "none"
    return env



def claude_exec_args(args: List[str]) -> List[str]:
    # Claude Code refuses yolo/bypass mode when the process itself is root.
    # The runner stays root, but Claude CLI runs as uid 1000 with docker group 998.
    # Privileged host edits are intentionally not exposed in the public template.
    if os.geteuid() == 0:
        return ["setpriv", "--reuid=1000", "--regid=1000", "--groups=1000,998"] + args
    return args


def clean_claude_output(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return s

    def from_obj(obj):
        if isinstance(obj, dict):
            if isinstance(obj.get("text"), str):
                return obj["text"]
            if isinstance(obj.get("final"), str):
                return obj["final"]
            if isinstance(obj.get("content"), list):
                parts = []
                tool_parts = []
                for b in obj["content"]:
                    if isinstance(b, dict):
                        if b.get("type") == "text":
                            parts.append(str(b.get("text") or ""))
                        elif b.get("type") == "tool_use":
                            tool_parts.append(str(b.get("name") or "tool"))
                    else:
                        parts.append(str(b))
                out = "\n".join(x for x in parts if x).strip()
                if out:
                    return out
                if tool_parts:
                    return "模型返回了工具调用但未形成最终文本：" + ", ".join(tool_parts)
        return None

    # Try exact JSON first; if the model printed a JSON envelope, expose only human text.
    for _ in range(3):
        try:
            try:
                obj = json.loads(s)
            except Exception:
                obj = json.loads(repair_glm_json_like(s))
            out = from_obj(obj)
            if out is None:
                return s
            if out == s:
                return out
            s = out.strip()
            continue
        except Exception:
            break
    # Try to extract a JSON object embedded in surrounding text.
    m = re.search(r'\{\s*"content"\s*:\s*\[.*\}\s*$', s, re.S)
    if m:
        try:
            try:
                obj = json.loads(m.group(0))
            except Exception:
                obj = json.loads(repair_glm_json_like(m.group(0)))
            out = from_obj(obj)
            if out:
                return out
        except Exception:
            pass
    return s

def run_claude_task(task: str, timeout: int = 1200, continue_session: bool = True) -> Dict[str, Any]:
    ensure_state()
    prompt = f"""
你在 QQBot 服务器的长期工作区中工作。

{state_context()}

【用户任务】
{task}

执行要求：
- 你是 Claude Code CLI，但模型由 GLM 离线注入提供。
- 使用工具完成任务；当前是 YOLO 模式，不要等待权限确认。
- Claude 进程以 uid=1000 运行；公开模板不提供 setuid root helper。
- Docker socket 可通过 docker 命令访问；如权限异常请调整容器用户/组或人工运维。
- 重要状态写回 .agent_state。
- 最终用中文简要汇报做了什么、如何验证、下一步。
""".strip()
    append_prompt = "读取并遵守 /home/wzu/qqbot/CLAUDE.md。当前长期工作区 /home/wzu/qqbot；状态目录 /home/wzu/qqbot/.agent_state；模型请求由本地代理转 GLM；不要要求 Claude Auth。你以 uid=1000 运行，公开模板不提供 root helper。"
    args = [CLAUDE_BIN, "--bare", "-p", prompt, "--model", DEFAULT_MODEL, "--dangerously-skip-permissions", "--permission-mode", "bypassPermissions", "--output-format", "text", "--append-system-prompt", append_prompt, "--add-dir", str(WORKSPACE)]
    # 不使用固定 Claude session-id，避免 “Session ID is already in use”。
    # 长程上下文由 .agent_state 文件持久化。
    args.append("--no-session-persistence")
    rc, out, err = run(claude_exec_args(args), cwd=str(WORKSPACE), env=claude_env(), timeout=timeout)
    text = clean_claude_output(out.strip() or err.strip() or f"claude rc={rc}")
    summary = f"Claude Code(GLM) 退出码：{rc}\n\n{text}"
    if err.strip():
        summary += "\n\nstderr:\n" + clip(err.strip(), 3000)
    log_result("ClaudeCode-GLM", task, summary)
    return {"ok": rc == 0, "rc": rc, "text": clip(summary), "stdout": clip(out, 5000), "stderr": clip(err, 5000)}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "claude-glm-runner/0.1"
    def log_message(self, fmt, *args):
        print(f"{self.client_address[0]} [{self.log_date_time_string()}] " + fmt % args, flush=True)
    def auth_ok(self) -> bool:
        if not TOKEN: return True
        # Anthropic API calls from local Claude use x-api-key glm-offline; runner API from AstrBot uses Bearer.
        if self.path.startswith("/v1/"):
            return True
        return self.headers.get("Authorization", "") == "Bearer " + TOKEN
    def read_json(self) -> Dict[str, Any]:
        n = int(self.headers.get("content-length", "0") or 0)
        if n <= 0: return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))
    def send_json(self, code: int, obj: Dict[str, Any]):
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
    def do_GET(self):
        if not self.auth_ok(): self.send_json(401, {"ok": False, "error": "unauthorized"}); return
        if self.path == "/health": self.send_json(200, {"ok": True}); return
        if self.path == "/status": self.handle_status({}); return
        if self.path == "/state": self.handle_state({}); return
        self.send_json(404, {"ok": False, "error": "not found"})
    def do_POST(self):
        if not self.auth_ok(): self.send_json(401, {"ok": False, "error": "unauthorized"}); return
        try:
            body = self.read_json()
            path = self.path.split("?", 1)[0]
            if path == "/v1/messages":
                self.send_json(200, anthropic_response_from_glm(body)); return
            if path == "/v1/messages/count_tokens":
                text = anthropic_messages_to_prompt(body)
                self.send_json(200, {"input_tokens": max(1, len(text)//4)}); return
            if path == "/run":
                task = str(body.get("task") or "").strip()
                if not task: self.send_json(400, {"ok": False, "error": "empty task"}); return
                if not RUN_LOCK.acquire(blocking=False):
                    self.send_json(409, {"ok": False, "busy": True, "error": "Claude(GLM) 当前已有任务在运行", "current_job": CURRENT_JOB}); return
                CURRENT_JOB.update({"running": True, "task": task[:500], "started_at": now()})
                try:
                    self.send_json(200, run_claude_task(task, int(body.get("timeout") or 1200), bool(body.get("continue_session", True)))); return
                finally:
                    CURRENT_JOB.update({"running": False, "task": "", "started_at": ""})
                    RUN_LOCK.release()
            if path == "/status": self.handle_status(body); return
            if path == "/state": self.handle_state(body); return
            self.send_json(404, {"ok": False, "error": "not found"})
        except Exception as e:
            self.send_json(500, {"ok": False, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-5000:]})
    def handle_status(self, body):
        ensure_state()
        rc_v, out_v, err_v = run([CLAUDE_BIN, "--version"], timeout=30)
        rc_id, out_id, err_id = run(["id"], timeout=10)
        rc_d, out_d, err_d = run(["sh", "-lc", "test -S /var/run/docker.sock && echo yes || echo no"], timeout=10)
        cfg = load_glm_provider()
        self.send_json(200, {"ok": True, "workspace": str(WORKSPACE), "state_dir": str(STATE_DIR), "claude": (out_v or err_v).strip(), "anthropic_base_url": f"http://127.0.0.1:{PORT}", "auth": "offline-glm-dummy-key", "yolo": "--dangerously-skip-permissions + permission-mode=bypassPermissions", "id": (out_id or err_id).strip(), "docker_sock": (out_d or err_d).strip(), "host_root_mount": Path("/host").exists(), "claude_exec_user": "uid=1000,gid=1000,groups=1000,998; no setuid root helper in public template", "context_mode": ".agent_state file persistence; no fixed Claude session-id", "current_job": CURRENT_JOB, "glm": {"model": cfg.get("model"), "api_base": cfg.get("api_base"), "key_loaded": bool(cfg.get("key"))}})
    def handle_state(self, body):
        ensure_state()
        limit = int(body.get("limit") or MAX_REPLY) if isinstance(body, dict) else MAX_REPLY
        self.send_json(200, {"ok": True, "workspace": str(WORKSPACE), "state_dir": str(STATE_DIR), "text": clip(state_context(), limit)})

if __name__ == "__main__":
    ensure_state()
    print(f"claude-glm-runner listening on 0.0.0.0:{PORT}; workspace={WORKSPACE}; state={STATE_DIR}", flush=True)
    http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
