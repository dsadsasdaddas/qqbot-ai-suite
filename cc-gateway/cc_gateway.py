#!/usr/bin/env python3
import base64
import hashlib
import json
import os
import random
import re
import socket
import struct
import threading
import time
import traceback
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

BIND_HOST = os.environ.get("CC_GATEWAY_HOST", "0.0.0.0")
BIND_PORT = int(os.environ.get("CC_GATEWAY_PORT", "8791"))
ASTRBOT_WS_URL = os.environ.get("ASTRBOT_WS_URL", "ws://astrbot:6199/ws")
HOOKD_URL = os.environ.get("HOOKD_URL", "http://qqbot-hookd:8788").rstrip("/")
TOKEN_CONFIG_PATH = Path(os.environ.get("TOKEN_CONFIG_PATH", "/config/claude_glm_runner.json"))
STATE_PATH = Path(os.environ.get("CC_GATEWAY_STATE", "/data/.agent_state/cc_gateway_state.json"))
ALLOWED_CONFIG_PATH = Path(os.environ.get("MOBILE_CONFIG_PATH", "/config/claude_glm_mobile.json"))
DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("CC_GATEWAY_TIMEOUT_SECONDS", "1200"))
POLL_INTERVAL = int(os.environ.get("CC_GATEWAY_POLL_INTERVAL", "5"))
CONTROL_PREFIX = os.environ.get("CC_GATEWAY_CONTROL_PREFIX", ",")
MAX_REPLY_CHARS = int(os.environ.get("CC_GATEWAY_MAX_REPLY_CHARS", "7000"))

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
STATE_LOCK = threading.Lock()
SEND_LOCK = threading.Lock()
CURRENT_NAPCAT: Optional["WSConn"] = None
WATCHING = set()


def log(msg: str):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def clip(s: str, n: int = MAX_REPLY_CHARS) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + f"\n...[省略 {len(s)-n} 字]"


def load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"load_json failed {path}: {e}")
    return default


def save_json(path: Path, obj: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def token() -> str:
    obj = load_json(TOKEN_CONFIG_PATH, {})
    return str(obj.get("token") or "")


def allowed_user_ids() -> set:
    obj = load_json(ALLOWED_CONFIG_PATH, {})
    vals = obj.get("allowed_user_ids") or ["1939455790"]
    return {str(x) for x in vals if str(x).strip()}


def allowed(user_id: Any) -> bool:
    ids = allowed_user_ids()
    return not ids or str(user_id) in ids


def session_key(ev: Dict[str, Any]) -> str:
    if ev.get("message_type") == "group":
        return f"group:{ev.get('group_id')}"
    return f"private:{ev.get('user_id')}"


def load_state() -> Dict[str, Any]:
    with STATE_LOCK:
        obj = load_json(STATE_PATH, {"sessions": {}})
        obj.setdefault("sessions", {})
        return obj


def save_state(obj: Dict[str, Any]):
    with STATE_LOCK:
        obj.setdefault("sessions", {})
        save_json(STATE_PATH, obj)


def get_session(obj: Dict[str, Any], key: str) -> Dict[str, Any]:
    sessions = obj.setdefault("sessions", {})
    s = sessions.setdefault(key, {})
    s.setdefault("enabled", False)
    s.setdefault("disabled", False)
    s.setdefault("active_task", "default")
    s.setdefault("tasks", {"default": {"name": "default"}})
    return s


def state_enabled(key: str) -> bool:
    obj = load_state()
    return bool((obj.get("sessions") or {}).get(key, {}).get("enabled"))


def state_disabled(key: str) -> bool:
    obj = load_state()
    return bool((obj.get("sessions") or {}).get(key, {}).get("disabled"))


def set_session_disabled(key: str, disabled: bool):
    st = load_state()
    sess = get_session(st, key)
    sess["disabled"] = bool(disabled)
    if not disabled:
        sess["enabled"] = True
    save_state(st)


class WSConn:
    def __init__(self, sock: socket.socket, masked_send: bool, name: str, headers: Optional[Dict[str, str]] = None):
        self.sock = sock
        self.masked_send = masked_send
        self.name = name
        self.headers = headers or {}
        self.alive = True
        self.lock = threading.Lock()

    def close(self):
        self.alive = False
        try:
            self.sock.close()
        except Exception:
            pass

    def send_text(self, text: str):
        self.send_frame(0x1, text.encode("utf-8"))

    def send_pong(self, payload: bytes = b""):
        self.send_frame(0xA, payload)

    def send_ping(self, payload: bytes = b""):
        self.send_frame(0x9, payload)

    def send_close(self):
        self.send_frame(0x8, b"")
        self.close()

    def send_frame(self, opcode: int, payload: bytes):
        if not self.alive:
            return
        fin_opcode = 0x80 | (opcode & 0x0F)
        ln = len(payload)
        header = bytearray([fin_opcode])
        mask_bit = 0x80 if self.masked_send else 0
        if ln < 126:
            header.append(mask_bit | ln)
        elif ln < 65536:
            header.append(mask_bit | 126)
            header.extend(struct.pack("!H", ln))
        else:
            header.append(mask_bit | 127)
            header.extend(struct.pack("!Q", ln))
        if self.masked_send:
            mask = random.randbytes(4) if hasattr(random, "randbytes") else os.urandom(4)
            header.extend(mask)
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self.lock:
            self.sock.sendall(header + payload)

    def recv_frame(self) -> Optional[Tuple[int, bytes]]:
        try:
            h = recvn(self.sock, 2)
            if not h:
                return None
            b1, b2 = h[0], h[1]
            opcode = b1 & 0x0F
            masked = bool(b2 & 0x80)
            ln = b2 & 0x7F
            if ln == 126:
                ln = struct.unpack("!H", recvn(self.sock, 2))[0]
            elif ln == 127:
                ln = struct.unpack("!Q", recvn(self.sock, 8))[0]
            mask = recvn(self.sock, 4) if masked else b""
            payload = recvn(self.sock, ln) if ln else b""
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                self.alive = False
                return None
            if opcode == 0x9:
                self.send_pong(payload)
                return self.recv_frame()
            if opcode == 0xA:
                return self.recv_frame()
            return opcode, payload
        except Exception as e:
            self.alive = False
            log(f"{self.name} recv_frame error: {e}")
            return None


def recvn(sock: socket.socket, n: int) -> bytes:
    data = bytearray()
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise EOFError("socket closed")
        data.extend(chunk)
    return bytes(data)


def read_http_headers(sock: socket.socket) -> str:
    data = bytearray()
    while b"\r\n\r\n" not in data and len(data) < 65536:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data.extend(chunk)
    return data.decode("utf-8", "replace")


def parse_headers(raw: str) -> Tuple[str, Dict[str, str]]:
    lines = raw.split("\r\n")
    first = lines[0] if lines else ""
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return first, headers


def accept_napcat(sock: socket.socket) -> WSConn:
    raw = read_http_headers(sock)
    first, headers = parse_headers(raw)
    key = headers.get("sec-websocket-key", "")
    accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    resp = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
        "\r\n"
    )
    sock.sendall(resp.encode())
    sock.settimeout(None)
    log(f"NapCat connected: {first}")
    return WSConn(sock, masked_send=False, name="napcat", headers=headers)


def parse_ws_url(url: str) -> Tuple[str, int, str]:
    u = url.strip().removeprefix("ws://")
    hostport, _, path = u.partition("/")
    path = "/" + path if path else "/"
    host, sep, port = hostport.partition(":")
    return host, int(port or 80), path


def connect_astrbot(napcat_headers: Optional[Dict[str, str]] = None) -> Optional[WSConn]:
    host, port, path = parse_ws_url(ASTRBOT_WS_URL)
    try:
        sock = socket.create_connection((host, port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        # aiocqhttp(AstrBot OneBot) requires these reverse WS headers.
        # NapCat normally sends them; preserve the real self_id when present.
        hdrs = napcat_headers or {}
        self_id = hdrs.get("x-self-id") or hdrs.get("x-self_id") or "1441327498"
        role = hdrs.get("x-client-role") or "Universal"
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"X-Self-ID: {self_id}\r\n"
            f"X-Client-Role: {role}\r\n"
            "\r\n"
        )
        sock.sendall(req.encode())
        raw = read_http_headers(sock)
        if "101" not in raw.split("\r\n", 1)[0]:
            log(f"AstrBot WS handshake failed: {raw[:200]}")
            sock.close()
            return None
        sock.settimeout(None)
        log("connected to AstrBot WS")
        return WSConn(sock, masked_send=True, name="astrbot")
    except Exception as e:
        log(f"connect AstrBot failed: {e}")
        return None


def text_from_event(ev: Dict[str, Any]) -> str:
    raw = ev.get("raw_message")
    if isinstance(raw, str):
        return raw.strip()
    msg = ev.get("message")
    if isinstance(msg, list):
        parts = []
        for seg in msg:
            if isinstance(seg, dict) and seg.get("type") == "text":
                data = seg.get("data") or {}
                parts.append(str(data.get("text") or ""))
        return "".join(parts).strip()
    return str(msg or "").strip()


def bot_self_id(ev: Dict[str, Any]) -> str:
    return str(ev.get("self_id") or "1441327498")


def strip_bot_mention(ev: Dict[str, Any], fallback_text: str = "") -> Tuple[bool, str]:
    """Return (mentioned, text_without_bot_at) for group events."""
    self_id = bot_self_id(ev)
    msg = ev.get("message")
    if isinstance(msg, list):
        mentioned = False
        parts = []
        for seg in msg:
            if not isinstance(seg, dict):
                continue
            typ = seg.get("type")
            data = seg.get("data") or {}
            if typ == "at":
                qq = str(data.get("qq") or "")
                if qq in {self_id, "all"}:
                    mentioned = True
                    continue
            if typ == "text":
                parts.append(str(data.get("text") or ""))
        return mentioned, "".join(parts).strip()

    raw = str(ev.get("raw_message") or fallback_text or "")
    patterns = [
        rf"\[CQ:at,qq={re.escape(self_id)}\]",
        r"\[CQ:at,qq=all\]",
    ]
    mentioned = any(re.search(p, raw) for p in patterns)
    clean = raw
    for p in patterns:
        clean = re.sub(p, "", clean)
    return mentioned, clean.strip()


def extract_group_cc_task(ev: Dict[str, Any], text: str) -> Optional[str]:
    if ev.get("message_type") != "group":
        return None
    mentioned, clean = strip_bot_mention(ev, text)
    if not mentioned:
        return None
    m = re.match(r"^(?:cc|code|代码|编程)\b[\s:：,，-]*(.*)$", clean, re.IGNORECASE)
    if not m:
        return None
    return m.group(1).strip()


def is_plain_chat_escape(text: str) -> bool:
    raw = (text or "").strip()
    low = raw.lower()
    return raw.startswith(("普通 ", "聊天 ", "闲聊 ")) or low.startswith(("gemma ", "chat "))


def strip_plain_chat_prefix(text: str) -> str:
    raw = (text or "").strip()
    for p in ("普通 ", "聊天 ", "闲聊 ", "gemma ", "Gemma ", "chat ", "Chat "):
        if raw.startswith(p):
            return raw[len(p):].strip()
    return raw


def escaped_plain_chat_payload(ev: Dict[str, Any], text: str) -> Optional[str]:
    if ev.get("message_type") != "private" or not allowed(ev.get("user_id")):
        return None
    if not is_plain_chat_escape(text):
        return None
    clean = strip_plain_chat_prefix(text)
    obj = json.loads(json.dumps(ev, ensure_ascii=False))
    obj["raw_message"] = clean
    msg = obj.get("message")
    if isinstance(msg, str):
        obj["message"] = clean
    elif isinstance(msg, list):
        obj["message"] = [{"type": "text", "data": {"text": clean}}]
    return json.dumps(obj, ensure_ascii=False)


def onebot_send(ev: Dict[str, Any], message: str):
    global CURRENT_NAPCAT
    conn = CURRENT_NAPCAT
    if not conn or not conn.alive:
        return
    message = clip(message)
    echo = "ccg_" + str(int(time.time() * 1000)) + "_" + str(random.randint(1000, 9999))
    if ev.get("message_type") == "group" and ev.get("group_id"):
        action = {"action": "send_group_msg", "params": {"group_id": int(ev.get("group_id")), "message": message}, "echo": echo}
    else:
        action = {"action": "send_private_msg", "params": {"user_id": int(ev.get("user_id")), "message": message}, "echo": echo}
    try:
        with SEND_LOCK:
            conn.send_text(json.dumps(action, ensure_ascii=False))
    except Exception as e:
        log(f"send onebot failed: {e}")


def http_json(method: str, path: str, payload: Optional[Dict[str, Any]] = None, timeout: int = 60) -> Dict[str, Any]:
    url = HOOKD_URL + path
    headers = {"Content-Type": "application/json"}
    t = token()
    if t:
        headers["Authorization"] = "Bearer " + t
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            obj = json.loads(raw)
        except Exception:
            obj = {"ok": False, "error": raw}
        obj.setdefault("ok", False)
        obj.setdefault("http_status", e.code)
        return obj
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def decorate_task(ev: Dict[str, Any], task: str) -> str:
    st = load_state()
    sess = get_session(st, session_key(ev))
    active = str(sess.get("active_task") or "default")
    if active and active != "default":
        return f"【CC任务槽】{active}\n本消息属于该任务槽；请围绕这个任务继续，不要串到其它任务槽。\n\n{task}"
    return task


def submit_job(ev: Dict[str, Any], task: str):
    task = (task or "").strip()
    if not task:
        onebot_send(ev, help_text())
        return
    payload = {
        "source": "napcat-direct",
        "user_id": str(ev.get("user_id") or ""),
        "group_id": str(ev.get("group_id") or ""),
        "command": "cc-gateway",
        "task": decorate_task(ev, task),
        "engine": "claude_glm",
        "continue_session": True,
        "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
    }
    res = http_json("POST", "/jobs", payload, timeout=60)
    if not res.get("ok"):
        onebot_send(ev, "创建 Claude job 失败：" + str(res.get("error") or res))
        return
    jid = str(res.get("job_id") or "")
    onebot_send(ev, f"收到，后台执行：{jid}\n完成后自动发结果。查进度：进度；取消：停止")
    start_watch(ev, jid)


def start_watch(ev: Dict[str, Any], jid: str):
    if not jid or jid in WATCHING:
        return
    WATCHING.add(jid)
    snapshot = dict(ev)
    threading.Thread(target=watch_job, args=(snapshot, jid), daemon=True).start()


def watch_job(ev: Dict[str, Any], jid: str):
    try:
        waited = 0
        while waited < DEFAULT_TIMEOUT_SECONDS + 900:
            time.sleep(POLL_INTERVAL)
            waited += POLL_INTERVAL
            res = http_json("GET", f"/jobs/{jid}", None, timeout=30)
            job = res.get("job") or {}
            status = str(job.get("status") or "")
            if status in {"done", "failed", "cancelled"}:
                onebot_send(ev, format_job_log(jid, prefix=f"任务 {jid} 已{status_cn(status)}。\n"))
                return
    finally:
        WATCHING.discard(jid)


def status_cn(st: str) -> str:
    return {"done": "完成", "failed": "失败", "cancelled": "取消"}.get(st, st)


def format_job_log(jid: str, prefix: str = "") -> str:
    res = http_json("GET", f"/jobs/{jid}/log?tail=120", None, timeout=60)
    if not res.get("ok"):
        return "查询 job 失败：" + str(res.get("error") or res)
    job = res.get("job") or {}
    result = str(res.get("result") or "")
    stderr = str(res.get("stderr") or "")
    stdout = str(res.get("stdout") or "")
    lines = []
    if prefix.strip():
        lines.append(prefix.rstrip())
    lines.append(f"job: {jid}")
    lines.append(f"status: {job.get('status')} | {job.get('summary')}")
    if result:
        lines.append("\n结果：\n" + result)
    elif stdout or stderr:
        lines.append("\n日志：\n" + (stdout or stderr))
    else:
        lines.append("\n暂无结果，可能还在运行。")
    if stderr and result:
        lines.append("\nstderr tail:\n" + stderr[-1200:])
    return clip("\n".join(lines))


def list_jobs() -> str:
    res = http_json("GET", "/jobs?limit=10", None, timeout=30)
    if not res.get("ok"):
        return "查询 jobs 失败：" + str(res.get("error") or res)
    jobs = res.get("jobs") or []
    if not jobs:
        return "暂无 Claude job。"
    rows = ["最近 Claude jobs："]
    for j in jobs:
        rows.append(f"{j.get('job_id')} | {j.get('status')} | {j.get('summary')} | {j.get('task_preview')}")
    return clip("\n".join(rows))


def resolve_job_id(raw: str) -> str:
    raw = (raw or "").strip()
    res = http_json("GET", "/jobs?limit=50", None, timeout=30)
    jobs = res.get("jobs") or []
    if not raw:
        return str(jobs[0].get("job_id") or "") if jobs else ""
    for j in jobs:
        jid = str(j.get("job_id") or "")
        if jid == raw or jid.startswith(raw):
            return jid
    return raw


def normalize_control_text(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return raw

    if raw.lower() == "/exit":
        return CONTROL_PREFIX + "exit"
    if raw.startswith("/") or raw.startswith("!/") or raw.startswith("！/"):
        # 除 /exit 之外，所有 slash 命令都交给 Claude Code 原生处理。
        return raw

    # 兼容中文逗号。
    if raw.startswith("，"):
        return CONTROL_PREFIX + raw[1:].strip()

    for prefix, cmd in (
        ("新任务 ", ",task new "),
        ("创建任务 ", ",task new "),
        ("切任务 ", ",task use "),
        ("切换任务 ", ",task use "),
        ("用任务 ", ",task use "),
    ):
        if raw.startswith(prefix):
            return cmd + raw[len(prefix):].strip()

    compact = "".join(raw.lower().split())
    aliases = {
        "开工": ",cc on",
        "开始干活": ",cc on",
        "进入cc": ",cc on",
        "开启cc": ",cc on",
        "打开cc": ",cc on",
        "code": ",cc on",
        "cc开": ",cc on",
        "ccon": ",cc on",
        "收工": ",exit",
        "退出cc": ",exit",
        "关闭cc": ",exit",
        "关掉cc": ",exit",
        "cc关": ",exit",
        "ccoff": ",cc off",
        "cc状态": ",cc status",
        "ccstatus": ",cc status",
        "状态": ",cc status",
        "进度": ",log",
        "查进度": ",log",
        "查询进度": ",log",
        "当前进度": ",log",
        "看进度": ",log",
        "结果": ",log",
        "最新结果": ",log",
        "看结果": ",log",
        "日志": ",log",
        "任务列表": ",jobs",
        "任务": ",jobs",
        "jobs": ",jobs",
        "停止": ",stop",
        "取消": ",stop",
        "停": ",stop",
        "帮助": ",help",
        "用法": ",help",
        "cc帮助": ",help",
        "当前任务": ",task current",
        "任务状态": ",task current",
        "任务槽": ",task list",
        "cc": ",cc on",
    }
    if compact in aliases:
        return aliases[compact]

    # 不想输逗号时：cc 帮我改代码 / cc帮我改代码 都是一次性任务。
    if raw.lower().startswith("cc "):
        return CONTROL_PREFIX + raw
    if raw.lower().startswith("code "):
        return CONTROL_PREFIX + "cc " + raw[5:].strip()
    if compact.startswith("cc") and len(compact) > 2:
        rest = raw[2:].strip()
        if rest:
            return CONTROL_PREFIX + "cc " + rest
    return raw


def is_control_text(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    if raw.startswith(CONTROL_PREFIX) or raw.startswith("，"):
        return True
    if raw.lower() == "/exit":
        return True
    return normalize_control_text(raw) != raw


def help_text() -> str:
    return (
        "手机 Claude Code 直连说明：\n"
        "私聊管理员：直接打字就是 Claude Code。\n"
        "/compact /init /help 等 slash 命令原样给 Claude；只有 /exit 是退出直连。\n"
        "退出后发 code 可恢复直连。\n"
        "控制词：状态 / 结果 / 进度 / 停止 / 任务列表。\n"
        "任务槽：新任务 名称 / 切任务 名称 / 当前任务。\n"
        "想走普通聊天：普通 你好。\n"
        "群里：@机器人 code 任务内容 或 @机器人 cc 任务内容。"
    )


def handle_cc_mode_command(ev: Dict[str, Any], rest: str) -> bool:
    action, _, _arg = rest.strip().partition(" ")
    a = action.lower()
    st = load_state(); sess = get_session(st, session_key(ev))
    if a in {"on", "enable", "开", "开启"}:
        sess["enabled"] = True; sess["disabled"] = False; save_state(st)
        onebot_send(ev, "已进入 Claude Code 直连。现在直接打字即可；/compact 等原样转发。退出发：/exit")
        return True
    if a in {"off", "disable", "关", "关闭"}:
        sess["enabled"] = False; sess["disabled"] = True; save_state(st)
        onebot_send(ev, "已退出 Claude Code 直连。恢复发：code")
        return True
    if a in {"status", "状态", ""}:
        direct = ev.get("message_type") == "private" and not bool(sess.get("disabled"))
        onebot_send(ev, f"direct={direct}\ndisabled={bool(sess.get('disabled'))}\nactive_task={sess.get('active_task') or 'default'}")
        return True
    return False


def handle_control(ev: Dict[str, Any], text: str) -> bool:
    t = normalize_control_text(text)
    if not t.startswith(CONTROL_PREFIX):
        return False
    body = t[len(CONTROL_PREFIX):].strip()
    if not body:
        return False
    parts = body.split(maxsplit=1)
    cmd = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""

    if cmd in {"help", "h", "?"}:
        onebot_send(ev, help_text()); return True
    if cmd in {"exit", "quit"}:
        st = load_state(); sess = get_session(st, session_key(ev))
        sess["enabled"] = False; sess["disabled"] = True; save_state(st)
        onebot_send(ev, "已退出 Claude Code 直连。恢复发：code")
        return True
    if cmd in {"cc", "ccmode"}:
        if not handle_cc_mode_command(ev, rest):
            submit_job(ev, rest)
        return True
    if cmd in {"task", "cctask"}:
        return handle_task_cmd(ev, rest)
    if cmd in {"jobs", "job"}:
        onebot_send(ev, list_jobs()); return True
    if cmd in {"log", "last", "result"}:
        jid = resolve_job_id(rest)
        onebot_send(ev, format_job_log(jid) if jid else "暂无任务。")
        return True
    if cmd in {"stop", "cancel"}:
        jid = resolve_job_id(rest)
        if not jid:
            onebot_send(ev, "暂无任务。")
        else:
            res = http_json("POST", f"/jobs/{jid}/cancel", {}, timeout=30)
            onebot_send(ev, f"已发送取消：{jid}\n" + json.dumps(res.get("job") or res, ensure_ascii=False)[:1000])
        return True
    if cmd == "status":
        res = http_json("POST", "/status", {}, timeout=30)
        onebot_send(ev, clip(json.dumps({k: res.get(k) for k in ["ok", "service", "queue_len", "total_jobs", "counts", "current_jobs"]}, ensure_ascii=False, indent=2)))
        return True
    return False


def handle_task_cmd(ev: Dict[str, Any], rest: str) -> bool:
    st = load_state(); sess = get_session(st, session_key(ev)); tasks = sess.setdefault("tasks", {"default": {"name":"default"}})
    action, _, name = rest.strip().partition(" ")
    a = action.lower()
    name = name.strip()
    if a in {"new", "add", "创建", "新增"}:
        if not name:
            onebot_send(ev, "需要任务槽名称：任务新建 知识产权系统")
        else:
            tasks[name] = {"name": name}; sess["active_task"] = name; save_state(st); onebot_send(ev, f"已创建并切换任务槽：{name}")
    elif a in {"use", "switch", "切换", "选"}:
        if not name:
            onebot_send(ev, "需要任务槽名称：任务切换 知识产权系统")
        else:
            tasks.setdefault(name, {"name": name}); sess["active_task"] = name; save_state(st); onebot_send(ev, f"已切换任务槽：{name}")
    elif a in {"list", "ls", "列表"}:
        active = sess.get("active_task") or "default"
        onebot_send(ev, "CC 任务槽：\n" + "\n".join(("* " if k == active else "- ") + k for k in sorted(tasks.keys())))
    elif a in {"current", "status", "当前", "状态", ""}:
        onebot_send(ev, f"当前任务槽：{sess.get('active_task') or 'default'}")
    else:
        onebot_send(ev, "用法：任务新建/任务切换/任务列表/当前任务 <name>")
    return True


def should_intercept_event(ev: Dict[str, Any]) -> Tuple[bool, str]:
    if ev.get("post_type") != "message":
        return False, ""
    if ev.get("message_type") not in {"private", "group"}:
        return False, ""
    if not allowed(ev.get("user_id")):
        return False, ""
    text = text_from_event(ev)
    if not text:
        return False, ""

    if ev.get("message_type") == "private":
        # 私聊管理员默认就是 Claude Code 手机终端。/exit 退出；code 恢复。
        if is_control_text(text):
            return True, text
        if is_plain_chat_escape(text):
            return False, text
        if not state_disabled(session_key(ev)):
            return True, text
        return False, text

    # 群里不默认接管：需要 @机器人 + code/cc，或显式控制命令。
    mentioned, clean = strip_bot_mention(ev, text)
    if mentioned:
        if is_control_text(clean):
            return True, clean
        if re.match(r"^(?:cc|code|代码|编程)\b", clean, re.IGNORECASE):
            return True, clean
    if text.strip().startswith(CONTROL_PREFIX) or text.strip().startswith("，"):
        return True, text
    return False, text


def handle_napcat_text(text: str, astr: Optional[WSConn]):
    try:
        obj = json.loads(text)
    except Exception:
        if astr and astr.alive:
            astr.send_text(text)
        return

    # Drop replies to our own OneBot actions.
    echo = str(obj.get("echo") or "")
    if echo.startswith("ccg_"):
        return

    msg_text0 = text_from_event(obj)
    escaped = escaped_plain_chat_payload(obj, msg_text0)
    if escaped is not None:
        if astr and astr.alive:
            astr.send_text(escaped)
        return

    intercept, msg_text = should_intercept_event(obj)
    if intercept:
        try:
            if handle_control(obj, msg_text):
                return
            submit_job(obj, msg_text)
            return
        except Exception:
            log("intercept error:\n" + traceback.format_exc())
            onebot_send(obj, "cc-gateway 出错：" + traceback.format_exc()[-1500:])
            return

    if astr and astr.alive:
        astr.send_text(text)


def pump_astr_to_nap(astr: WSConn, nap: WSConn):
    while astr.alive and nap.alive:
        frame = astr.recv_frame()
        if frame is None:
            break
        opcode, payload = frame
        if opcode == 0x1:
            nap.send_text(payload.decode("utf-8", "replace"))
        elif opcode == 0x2:
            nap.send_frame(0x2, payload)
    log("AstrBot -> NapCat pump ended")
    astr.close()


def handle_connection(sock: socket.socket):
    global CURRENT_NAPCAT
    nap = accept_napcat(sock)
    CURRENT_NAPCAT = nap
    astr = connect_astrbot(nap.headers)
    if astr:
        threading.Thread(target=pump_astr_to_nap, args=(astr, nap), daemon=True).start()
    try:
        while nap.alive:
            frame = nap.recv_frame()
            if frame is None:
                break
            opcode, payload = frame
            if opcode == 0x1:
                if not astr or not astr.alive:
                    astr = connect_astrbot(nap.headers)
                    if astr:
                        threading.Thread(target=pump_astr_to_nap, args=(astr, nap), daemon=True).start()
                handle_napcat_text(payload.decode("utf-8", "replace"), astr)
            elif opcode == 0x2 and astr and astr.alive:
                astr.send_frame(0x2, payload)
    finally:
        log("NapCat connection ended")
        nap.close()
        if astr:
            astr.close()
        if CURRENT_NAPCAT is nap:
            CURRENT_NAPCAT = None


def main():
    log(f"cc-gateway listening {BIND_HOST}:{BIND_PORT}; astrbot={ASTRBOT_WS_URL}; hookd={HOOKD_URL}")
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((BIND_HOST, BIND_PORT))
    srv.listen(16)
    while True:
        sock, addr = srv.accept()
        log(f"tcp accepted {addr}")
        threading.Thread(target=handle_connection, args=(sock,), daemon=True).start()


if __name__ == "__main__":
    main()
