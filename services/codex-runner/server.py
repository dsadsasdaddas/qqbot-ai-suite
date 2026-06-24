#!/usr/bin/env python3
import datetime as _dt
import http.server
import json
import os
import re
import subprocess
import tempfile
import traceback
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Dict, List, Tuple

TOKEN = os.environ.get("CODEX_RUNNER_TOKEN", "")
CODEX_BIN = os.environ.get("CODEX_BIN", "/home/wzu/.local/bin/codex")
DEFAULT_WORKSPACE = os.environ.get("CODEX_DEFAULT_WORKSPACE", "/home/wzu/qqbot")
STATE_DIR = Path(os.environ.get("AGENT_STATE_DIR", "/home/wzu/qqbot/.agent_state"))
DEFAULT_TIMEOUT = int(os.environ.get("CODEX_RUNNER_TIMEOUT", "900"))
MAX_REPLY = int(os.environ.get("CODEX_RUNNER_MAX_REPLY", "12000"))
GLM_DEFAULT_MODEL = os.environ.get("GLM_MODEL", "glm-5.2")
OWNER_UID = int(os.environ.get("AGENT_FILE_UID", "1000"))
OWNER_GID = int(os.environ.get("AGENT_FILE_GID", "1000"))


def now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S %z")


def clip(text: str, limit: int = MAX_REPLY) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[省略 {len(text)-limit} 字]"


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


def tail_text(path: Path, chars: int) -> str:
    try:
        s = path.read_text(encoding="utf-8", errors="replace")
        return s[-chars:] if len(s) > chars else s
    except Exception:
        return ""




def fix_state_permissions() -> None:
    try:
        subprocess.run(["chown", "-R", f"{OWNER_UID}:{OWNER_GID}", str(STATE_DIR)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
    except Exception:
        pass


def base_env() -> Dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = env.get("HOME", "/home/wzu")
    env["CODEX_HOME"] = env.get("CODEX_HOME", "/home/wzu/.codex")
    env["GIT_CONFIG_GLOBAL"] = env.get("GIT_CONFIG_GLOBAL", "/home/wzu/.gitconfig")
    env["NPM_CONFIG_CACHE"] = env.get("NPM_CONFIG_CACHE", "/home/wzu/.npm-cache")
    env["AGENT_STATE_DIR"] = str(STATE_DIR)
    env["PATH"] = "/home/wzu/.local/bin:/usr/local/bin:/usr/bin:/bin:/opt/codex-runner:" + env.get("PATH", "")
    return env


def run(args, cwd=None, timeout=60, input_text=None) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(
            args,
            cwd=cwd,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=base_env(),
        )
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
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    chown_relaxed(STATE_DIR)
    p = state_paths()
    defaults = {
        "readme": "# Agent State\n\n这是 QQBot/Codex/GLM 的长期工作区状态目录。所有长程任务都应该读写这里。\n\n- TASKS.md：任务拆解和待办\n- MEMORY.md：长期事实、决策、环境信息\n- HANDOFF.md：给下一次任务的交接\n- LOG.md：每次执行摘要日志\n- LAST_RESULT.md：最近一次执行结果\n",
        "tasks": "# TASKS\n\n- [ ] 维护 QQBot / Codex / GLM 长程协作工作区。\n",
        "memory": "# MEMORY\n\n- 默认长期工作区：/home/wzu/qqbot\n- 宿主机根目录在 runner 内映射为 /host\n- Codex 与 GLM agent 都在 codex-mobile-runner 内以 root 运行。\n",
        "handoff": "# HANDOFF\n\n下一次任务开始前，先看本目录下 MEMORY.md、TASKS.md、LOG.md 的尾部。\n",
        "log": "# LOG\n\n",
        "last": "# LAST RESULT\n\n暂无。\n",
    }
    for k, text in defaults.items():
        if not p[k].exists():
            write_text(p[k], text)


def read_state_context() -> str:
    ensure_state()
    p = state_paths()
    return f"""
【长期工作区状态】
状态目录：{STATE_DIR}
你正在参与一个长程任务。每次开始前都要利用下面的持久状态；每次结束前，如果产生了新事实、待办、决策或交接信息，要更新对应文件。

--- HANDOFF.md ---
{tail_text(p['handoff'], 5000)}

--- MEMORY.md ---
{tail_text(p['memory'], 5000)}

--- TASKS.md ---
{tail_text(p['tasks'], 5000)}

--- LOG.md tail ---
{tail_text(p['log'], 7000)}

状态维护规则：
1. 新的长期事实写入 MEMORY.md。
2. 未完成/已完成任务同步 TASKS.md。
3. 给下次继续用的交接写入 HANDOFF.md。
4. 不要依赖聊天上下文，关键内容必须落盘。
""".strip()


def log_result(agent: str, workspace: str, task: str, result: str) -> None:
    ensure_state()
    p = state_paths()
    entry = f"\n## {now()} [{agent}]\n\nworkspace: `{workspace}`\n\n任务：\n{clip(task, 2000)}\n\n结果摘要：\n{clip(result, 4000)}\n\n"
    append_text(p["log"], entry)
    write_text(p["last"], f"# LAST RESULT\n\n时间：{now()}\n\nagent: {agent}\n\nworkspace: `{workspace}`\n\n任务：\n{task}\n\n结果：\n{clip(result, 8000)}\n")
    fix_state_permissions()


def normalize_key(value) -> str:
    if isinstance(value, list):
        for item in value:
            item = str(item or "").strip()
            if item:
                return item
        return ""
    return str(value or "").strip()


def resolve_workspace(path: str) -> Tuple[str, str]:
    path = (path or DEFAULT_WORKSPACE).strip() or DEFAULT_WORKSPACE
    if not path.startswith("/"):
        raise ValueError("workspace 必须是绝对路径")
    if path == "/":
        return "/host", "宿主机 / 已映射为 runner 内 /host"
    if path.startswith("/host"):
        return path, "宿主机路径"
    if path.startswith("/home/wzu/") or path == "/home/wzu":
        return path, "宿主机 /home/wzu 直通挂载"
    return "/host" + path, f"宿主机 {path} 映射为 runner 内 /host{path}"


def ensure_workspace(path: str) -> Tuple[str, str]:
    ensure_state()
    ws, note = resolve_workspace(path)
    p = Path(ws)
    p.mkdir(parents=True, exist_ok=True)
    chown_relaxed(p)
    # 只自动初始化专用工作区；/home/wzu/qqbot 等已有目录不强行 git init。
    if ws.startswith("/home/wzu/codex-mobile/") and not (p / ".git").exists():
        run(["git", "init"], cwd=ws, timeout=30)
        readme = p / "README.md"
        if not readme.exists():
            write_text(readme, "# Codex Mobile Workspace\n\n手机 QQ 触发 Codex 的专用工作区。\n")
        run(["git", "add", "README.md"], cwd=ws, timeout=30)
        run(["git", "config", "user.email", "codex-mobile@localhost"], cwd=ws, timeout=30)
        run(["git", "config", "user.name", "Codex Mobile"], cwd=ws, timeout=30)
        run(["git", "commit", "-m", "init codex mobile workspace"], cwd=ws, timeout=30)
    return ws, note


def git_text(ws: str) -> Dict[str, str]:
    rc, out, err = run(["git", "rev-parse", "--is-inside-work-tree"], cwd=ws, timeout=20)
    if rc != 0:
        return {"status": "非 Git 工作区", "stat": "", "diff": ""}
    rc_s, status, err_s = run(["git", "status", "--short"], cwd=ws, timeout=30)
    rc_stat, stat, err_stat = run(["git", "diff", "--stat"], cwd=ws, timeout=30)
    rc_diff, diff, err_diff = run(["git", "diff", "--", "."], cwd=ws, timeout=60)
    return {"status": (status or err_s).strip() or "干净", "stat": (stat or err_stat).strip(), "diff": (diff or err_diff).strip()}


def load_glm_provider() -> Dict[str, Any]:
    paths = [Path("/host/home/wzu/qqbot/data/cmd_config.json"), Path("/home/wzu/qqbot/data/cmd_config.json")]
    for p in paths:
        if not p.exists():
            continue
        data = json.loads(p.read_text(encoding="utf-8-sig", errors="replace"))
        providers = data.get("provider") or []
        chosen = None
        for item in providers:
            if item.get("id") == "glm5_default":
                chosen = item
                break
        if chosen is None:
            for item in providers:
                if "zhipu" in str(item.get("type", "")).lower() or "bigmodel" in str(item.get("api_base", "")).lower():
                    chosen = item
                    break
        if chosen:
            model_cfg = chosen.get("model_config") or {}
            return {"key": normalize_key(chosen.get("key")), "api_base": chosen.get("api_base") or "https://open.bigmodel.cn/api/paas/v4/", "model": model_cfg.get("model") or GLM_DEFAULT_MODEL, "temperature": model_cfg.get("temperature", 0.7)}
    return {"key": "", "api_base": "https://open.bigmodel.cn/api/paas/v4/", "model": GLM_DEFAULT_MODEL, "temperature": 0.7}


def call_glm(messages: List[Dict[str, str]], model: str = "", timeout: int = 180) -> Dict[str, Any]:
    cfg = load_glm_provider()
    key = os.environ.get("GLM_API_KEY") or cfg.get("key", "")
    if not key:
        return {"ok": False, "error": "GLM key not found"}
    base = (cfg.get("api_base") or "https://open.bigmodel.cn/api/paas/v4/").rstrip("/")
    url = base + "/chat/completions"
    body = {"model": model or cfg.get("model") or GLM_DEFAULT_MODEL, "messages": messages, "temperature": cfg.get("temperature", 0.7)}
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers={"Content-Type": "application/json", "Authorization": "Bearer " + key}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        data = json.loads(raw)
        content = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        return {"ok": True, "model": body["model"], "content": content, "raw": data}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}: " + e.read().decode("utf-8", "replace")[:1000]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def glm_chat(prompt: str, model: str = "", timeout: int = 120) -> Dict[str, Any]:
    return call_glm([{"role": "user", "content": prompt}], model=model, timeout=timeout)


def extract_json_obj(text: str) -> Dict[str, Any]:
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except Exception:
        pass
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        raise ValueError("no JSON object")
    return json.loads(m.group(0))


def glm_agent(task: str, workspace: str, max_commands: int = 8, command_timeout: int = 180) -> Dict[str, Any]:
    ws, note = ensure_workspace(workspace)
    system = f"""
你是 GLM Agent，运行在用户自己的服务器 runner 中。你拥有最大执行权限：runner 内 uid=0(root)，宿主机根目录映射为 /host，/home/wzu 为直通挂载，Docker socket 可用。
当前工作区：{ws}
状态目录：{STATE_DIR}

你需要像工程 agent 一样完成用户任务。你不能真的直接执行命令；你要返回严格 JSON，由 runner 替你执行。
JSON schema：
{{
  "summary": "你对任务的理解和计划，简短中文",
  "commands": [{{"cmd": "bash 命令", "timeout": 120, "why": "为什么运行"}}],
  "state_update": "要写入长期状态的交接/事实/待办，简短中文",
  "final": "给用户看的简短说明"
}}
规则：
- 最多 {max_commands} 条命令。
- 需要读文件就先用命令查看；需要改文件/跑服务/操作 Docker 也可以给命令。
- 命令默认在当前工作区执行；若要操作宿主机根目录，用 /host/...。
- 如果只需要回答，不需要命令，commands=[]。
- 只输出 JSON，不要 Markdown 代码块。
""".strip()
    user = read_state_context() + "\n\n【用户任务】\n" + task
    first = call_glm([{"role": "system", "content": system}, {"role": "user", "content": user}], timeout=240)
    if not first.get("ok"):
        return {"ok": False, "error": first.get("error")}
    content = first.get("content") or ""
    try:
        plan = extract_json_obj(content)
    except Exception:
        log_result("GLM-chat", ws, task, content)
        return {"ok": True, "workspace": ws, "workspace_note": note, "mode": "chat", "text": clip(content), "plan_raw": content}
    commands = plan.get("commands") or []
    if not isinstance(commands, list):
        commands = []
    commands = commands[:max_commands]
    results = []
    for i, item in enumerate(commands, 1):
        if isinstance(item, str):
            cmd, why, tout = item, "", command_timeout
        elif isinstance(item, dict):
            cmd = str(item.get("cmd") or "").strip()
            why = str(item.get("why") or "")
            tout = int(item.get("timeout") or command_timeout)
        else:
            continue
        if not cmd:
            continue
        tout = max(1, min(tout, 900))
        rc, out, err = run(["sh", "-lc", cmd], cwd=ws, timeout=tout)
        results.append({"index": i, "cmd": cmd, "why": why, "timeout": tout, "rc": rc, "stdout": clip(out, 3000), "stderr": clip(err, 3000)})
    fix_state_permissions()
    # 让 GLM 根据命令结果产出最终交接。
    final_prompt = "原任务：\n" + task + "\n\n计划：\n" + json.dumps(plan, ensure_ascii=False, indent=2) + "\n\n命令结果：\n" + json.dumps(results, ensure_ascii=False, indent=2) + "\n\n请返回严格 JSON：{\"final\":\"给用户看的结果\",\"state_update\":\"写入长期状态的交接/事实/待办\"}。"
    second = call_glm([{"role": "system", "content": "你是 GLM Agent 的总结器，只返回 JSON。"}, {"role": "user", "content": final_prompt}], timeout=180)
    final = str(plan.get("final") or "")
    state_update = str(plan.get("state_update") or "")
    if second.get("ok"):
        try:
            obj = extract_json_obj(second.get("content") or "")
            final = str(obj.get("final") or final or "GLM agent 执行完成。")
            state_update = str(obj.get("state_update") or state_update)
        except Exception:
            final = second.get("content") or final or "GLM agent 执行完成。"
    else:
        final = final or "GLM agent 执行完成，但总结调用失败：" + str(second.get("error"))
    if state_update:
        append_text(state_paths()["handoff"], f"\n## {now()} [GLM Agent]\n\n{state_update}\n")
    summary_text = "GLM Agent 执行完成\n\n" + final + "\n\n命令数：" + str(len(results))
    if results:
        summary_text += "\n\n命令结果：\n" + "\n".join([f"[{r['index']}] rc={r['rc']} {r['cmd']}\nstdout:\n{r['stdout']}\nstderr:\n{r['stderr']}" for r in results])
    log_result("GLM-agent", ws, task, summary_text)
    return {"ok": True, "workspace": ws, "workspace_note": note, "plan": plan, "results": results, "final": final, "state_update": state_update, "text": clip(summary_text)}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "codex-mobile-runner/0.2"

    def log_message(self, fmt, *args):
        print("%s - - [%s] %s" % (self.client_address[0], self.log_date_time_string(), fmt % args), flush=True)

    def _auth(self) -> bool:
        if not TOKEN:
            return True
        return self.headers.get("Authorization", "") == "Bearer " + TOKEN

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _send(self, code: int, obj: Dict[str, Any]):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._auth():
            self._send(401, {"ok": False, "error": "unauthorized"}); return
        if self.path == "/health":
            ensure_state(); self._send(200, {"ok": True}); return
        if self.path == "/status":
            self.handle_status({}); return
        if self.path == "/state":
            self.handle_state({}); return
        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if not self._auth():
            self._send(401, {"ok": False, "error": "unauthorized"}); return
        try:
            body = self._read_json()
            if self.path == "/status": self.handle_status(body)
            elif self.path == "/state": self.handle_state(body)
            elif self.path == "/ensure_workspace":
                ws, note = ensure_workspace(body.get("workspace") or DEFAULT_WORKSPACE)
                self._send(200, {"ok": True, "workspace": ws, "note": note, "state_dir": str(STATE_DIR)})
            elif self.path == "/diff": self.handle_diff(body)
            elif self.path == "/run": self.handle_run(body)
            elif self.path == "/glm":
                res = glm_chat(str(body.get("prompt") or ""), str(body.get("model") or ""), int(body.get("timeout") or 120))
                if res.get("ok"): log_result("GLM-chat", str(body.get("workspace") or DEFAULT_WORKSPACE), str(body.get("prompt") or ""), res.get("content") or "")
                self._send(200 if res.get("ok") else 500, res)
            elif self.path == "/glm_run":
                res = glm_agent(str(body.get("task") or body.get("prompt") or ""), str(body.get("workspace") or DEFAULT_WORKSPACE), int(body.get("max_commands") or 8), int(body.get("command_timeout") or 180))
                self._send(200 if res.get("ok") else 500, res)
            else: self._send(404, {"ok": False, "error": "not found"})
        except Exception as e:
            self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-4000:]})

    def handle_state(self, body: Dict[str, Any]):
        ensure_state()
        p = state_paths()
        limit = int(body.get("limit") or 12000) if isinstance(body, dict) else 12000
        self._send(200, {"ok": True, "state_dir": str(STATE_DIR), "files": {k: str(v) for k, v in p.items()}, "text": clip(read_state_context(), limit)})

    def handle_status(self, body: Dict[str, Any]):
        ws, note = ensure_workspace(body.get("workspace") or DEFAULT_WORKSPACE)
        rc_v, out_v, err_v = run([CODEX_BIN, "--version"], timeout=30)
        rc_l, out_l, err_l = run([CODEX_BIN, "login", "status"], timeout=60)
        rc_id, out_id, err_id = run(["id"], timeout=10)
        rc_d, out_d, err_d = run(["sh", "-lc", "test -S /var/run/docker.sock && echo yes || echo no"], timeout=10)
        g = git_text(ws)
        glm_cfg = load_glm_provider()
        self._send(200, {"ok": True, "workspace": ws, "workspace_note": note, "state_dir": str(STATE_DIR), "codex": (out_v or err_v).strip(), "login": (out_l or err_l).strip(), "id": (out_id or err_id).strip(), "host_root_mount": Path("/host").exists(), "docker_sock": (out_d or err_d).strip(), "git_status": g["status"], "glm": {"model": glm_cfg.get("model"), "api_base": glm_cfg.get("api_base"), "key_loaded": bool(glm_cfg.get("key"))}})

    def handle_diff(self, body: Dict[str, Any]):
        ws, note = ensure_workspace(body.get("workspace") or DEFAULT_WORKSPACE)
        g = git_text(ws)
        text = "git diff --stat:\n" + (g["stat"] or "无改动/非 Git 工作区")
        if g["diff"]:
            text += "\n\npatch:\n" + clip(g["diff"], MAX_REPLY - 300)
        self._send(200, {"ok": True, "workspace": ws, "workspace_note": note, "state_dir": str(STATE_DIR), "text": text, **g})

    def handle_run(self, body: Dict[str, Any]):
        raw_ws = body.get("workspace") or DEFAULT_WORKSPACE
        ws, note = ensure_workspace(raw_ws)
        task = str(body.get("task") or "").strip()
        if not task:
            self._send(400, {"ok": False, "error": "empty task"}); return
        sandbox = str(body.get("sandbox") or "danger-full-access")
        approval = str(body.get("approval_policy") or "never")
        timeout = int(body.get("timeout_seconds") or DEFAULT_TIMEOUT)
        extra = str(body.get("extra_instruction") or "").strip()
        model = str(body.get("model") or "").strip()
        final_fd, final_path = tempfile.mkstemp(prefix="codex-last-", suffix=".md")
        os.close(final_fd)
        try:
            prompt = (extra + "\n\n" if extra else "") + read_state_context() + "\n\n用户手机任务：\n" + task
            args = [CODEX_BIN, "exec", "--skip-git-repo-check", "--cd", ws, "--sandbox", sandbox, "-c", f"approval_policy=\"{approval}\"", "-o", final_path]
            if model:
                args.extend(["--model", model])
            args.append(prompt)
            rc, out, err = run(args, cwd=ws, timeout=timeout)
            final = Path(final_path).read_text(encoding="utf-8", errors="replace") if Path(final_path).exists() else ""
            g = git_text(ws)
            parts = [f"Codex 执行完成，退出码：{rc}", f"runner 工作区：{ws}", f"状态目录：{STATE_DIR}", f"权限：sandbox={sandbox}, approval={approval}", note]
            if final.strip(): parts.append("结果：\n" + final.strip())
            else: parts.append("输出：\n" + clip((out + "\n" + err).strip(), 4000))
            parts.append("git status:\n" + (g["status"] or "干净"))
            if g["stat"]: parts.append("diff stat:\n" + g["stat"])
            text = clip("\n\n".join(parts), MAX_REPLY)
            log_result("Codex", ws, task, text)
            self._send(200, {"ok": rc == 0, "rc": rc, "workspace": ws, "workspace_note": note, "state_dir": str(STATE_DIR), "text": text, "stdout": clip(out, 4000), "stderr": clip(err, 4000), "final": final, **g})
        finally:
            try: Path(final_path).unlink(missing_ok=True)
            except Exception: pass


if __name__ == "__main__":
    ensure_state()
    port = int(os.environ.get("CODEX_RUNNER_PORT", "8787"))
    print(f"codex-mobile-runner listening on 0.0.0.0:{port}; workspace={DEFAULT_WORKSPACE}; state={STATE_DIR}", flush=True)
    http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
