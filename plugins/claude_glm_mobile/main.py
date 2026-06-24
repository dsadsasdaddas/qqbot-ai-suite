import asyncio
import copy
import json
import re
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

import astrbot.api.star as star
import astrbot.api.event.filter as filter
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

CONFIG_PATH = Path("data/config/claude_glm_mobile.json")
RUNNER_CONFIG_PATH = Path("data/config/claude_glm_runner.json")

DEFAULT_CONFIG: Dict[str, Any] = {
    "runner_url": "http://qqbot-hookd:8788",
    "runner_token": "",
    "allowed_user_ids": ["1939455790"],
    "timeout_seconds": 1200,
    "max_reply_chars": 7000,
    "continue_session": True,
    "job_poll_interval_seconds": 8,
    "job_auto_push": True,
}

COMMAND_RE = re.compile(r"^[/!！\s]*(?:cc|claude|claudecode|克劳德)(?:\s+|[:：,，]\s*|$)(.*)$", re.I | re.S)
TERMINAL_STATUSES = {"done", "failed", "cancelled"}


def load_cfg() -> Dict[str, Any]:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    try:
        if RUNNER_CONFIG_PATH.exists():
            r = json.loads(RUNNER_CONFIG_PATH.read_text(encoding="utf-8"))
            cfg["runner_token"] = r.get("token") or cfg["runner_token"]
        if CONFIG_PATH.exists():
            u = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(u, dict):
                cfg.update(u)
    except Exception as exc:
        logger.warning(f"claude_glm_mobile 配置读取失败: {exc}")
    return cfg


def save_cfg(cfg: Dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    safe = {k: v for k, v in cfg.items() if k != "runner_token"}
    CONFIG_PATH.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")


def strip_command(text: str) -> str:
    m = COMMAND_RE.match(text or "")
    return (m.group(1) if m else text or "").strip()


def clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[省略 {len(text)-limit} 字]"


def short_job(job_id: str) -> str:
    return str(job_id or "")[:18]


@star.register(
    name="claude_glm_mobile",
    desc="Claude Code CLI YOLO + GLM 离线注入，通过 Rust hookd job 队列给 QQ /cc 调用。",
    author="Codex",
    version="0.2.0",
)
class Main(star.Star):
    def __init__(self, context: star.Context):
        self.context = context
        cfg = load_cfg()
        if not CONFIG_PATH.exists():
            save_cfg(cfg)
        self._watching = set()

    def _is_allowed(self, event: AstrMessageEvent, cfg: Dict[str, Any]) -> bool:
        allowed = [str(x) for x in cfg.get("allowed_user_ids", []) if str(x).strip()]
        if not allowed:
            return True
        return str(event.get_sender_id()) in allowed

    async def _request(self, cfg: Dict[str, Any], path: str, payload: Optional[Dict[str, Any]] = None, timeout: int = 60) -> Dict[str, Any]:
        url = str(cfg.get("runner_url") or DEFAULT_CONFIG["runner_url"]).rstrip("/") + path
        token = str(cfg.get("runner_token") or "")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token

        def do_req():
            data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
            method = "GET" if payload is None else "POST"
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

        return await asyncio.to_thread(do_req)

    @filter.command("cc", alias={"claude", "claudecode", "克劳德"})
    async def cc_command(self, event: AstrMessageEvent):
        event.call_llm = False
        cfg = load_cfg()
        if not self._is_allowed(event, cfg):
            yield event.plain_result("Claude(GLM) 长程 agent 当前只对白名单用户开放。")
            return
        arg = strip_command(event.get_message_str())
        action, rest = self._split(arg)
        try:
            if action in {"", "help", "帮助"}:
                yield event.plain_result(self._help())
                return
            if action in {"status", "状态"}:
                yield event.plain_result(await self._status(cfg))
                return
            if action in {"state", "memory", "上下文", "状态文件"}:
                yield event.plain_result(await self._state(cfg))
                return
            if action in {"jobs", "job", "任务", "队列"}:
                yield event.plain_result(await self._jobs(cfg))
                return
            if action in {"log", "logs", "result", "结果"}:
                job_id = await self._resolve_job_id(cfg, rest.strip())
                yield event.plain_result(await self._job_log(cfg, job_id))
                return
            if action in {"last", "最近"}:
                job_id = await self._resolve_job_id(cfg, "")
                yield event.plain_result(await self._job_log(cfg, job_id))
                return
            if action in {"stop", "cancel", "取消", "停止"}:
                job_id = await self._resolve_job_id(cfg, rest.strip())
                yield event.plain_result(await self._cancel(cfg, job_id))
                return
            if action in {"continue", "继续"}:
                task = rest or "继续上次任务，先读取 .agent_state，然后接着做。"
                yield event.plain_result(await self._submit_job(event, cfg, task, continue_session=True))
                return

            task = arg
            yield event.plain_result(await self._submit_job(event, cfg, task, continue_session=bool(cfg.get("continue_session", True))))
        except Exception as exc:
            logger.error(traceback.format_exc())
            yield event.plain_result(f"Claude(GLM) 出错：{type(exc).__name__}: {exc}")

    def _split(self, arg: str):
        arg = (arg or "").strip()
        if not arg:
            return "", ""
        p = arg.split(maxsplit=1)
        return p[0].lower(), (p[1] if len(p) > 1 else "")

    def _help(self) -> str:
        return (
            "Claude Code(GLM离线注入) / Rust hookd 命令：\n"
            "/cc status 查看 hookd/runner 状态\n"
            "/cc jobs 查看任务队列\n"
            "/cc 你的长程任务  提交后台 job\n"
            "/cc log <job_id> 查看结果/日志\n"
            "/cc last 查看最近任务结果\n"
            "/cc stop <job_id> 取消任务\n"
            "/cc continue 继续上次任务\n"
            "默认：YOLO/bypassPermissions，无 Claude Auth，模型走 GLM。"
        )

    async def _status(self, cfg: Dict[str, Any]) -> str:
        res = await self._request(cfg, "/status", {}, timeout=60)
        if not res.get("ok"):
            return "Claude hookd 状态异常：" + str(res.get("error") or res)
        runner = res.get("runner") or {}
        glm = runner.get("glm") or {}
        counts = res.get("counts") or {}
        current = res.get("current_jobs") or []
        lines = [
            "Claude Code(GLM) / Rust hookd 状态",
            f"hookd: {res.get('service')} {res.get('version')}",
            f"queue_len: {res.get('queue_len')}, total_jobs: {res.get('total_jobs')}, counts: {counts}",
            f"jobs_dir: {res.get('jobs_dir')}",
            f"runner_ok: {runner.get('ok')}",
            f"claude: {runner.get('claude')}",
            f"workspace: {runner.get('workspace')}",
            f"exec_user: {runner.get('claude_exec_user')}",
            f"glm: {glm.get('model')}, key_loaded={glm.get('key_loaded')}",
        ]
        if current:
            lines.append("running:")
            for j in current[:3]:
                lines.append(f"- {short_job(j.get('job_id'))} {j.get('summary')} {j.get('task_preview')}")
        return "\n".join(lines)

    async def _state(self, cfg: Dict[str, Any]) -> str:
        res = await self._request(cfg, "/state", {"limit": int(cfg.get("max_reply_chars", 7000))}, timeout=60)
        if not res.get("ok"):
            return "读取状态失败：" + str(res.get("error") or res)
        return clip(str(res.get("text") or ""), int(cfg.get("max_reply_chars", 7000)))

    async def _submit_job(self, event: AstrMessageEvent, cfg: Dict[str, Any], task: str, continue_session: bool = True) -> str:
        task = (task or "").strip()
        if not task:
            return self._help()
        payload = {
            "source": "qq",
            "user_id": str(event.get_sender_id() or ""),
            "group_id": self._group_id(event),
            "command": "cc",
            "task": task,
            "engine": "claude_glm",
            "continue_session": continue_session,
            "timeout_seconds": int(cfg.get("timeout_seconds", 1200)),
            "reply_to": {
                "platform": "astrbot",
                "session_id": str(getattr(event, "unified_msg_origin", "") or ""),
                "user_id": str(event.get_sender_id() or ""),
                "group_id": self._group_id(event),
            },
        }
        res = await self._request(cfg, "/jobs", payload, timeout=60)
        if not res.get("ok"):
            return "创建 Claude job 失败：" + str(res.get("error") or res)
        job_id = str(res.get("job_id") or "")
        if cfg.get("job_auto_push", True) and job_id:
            self._start_watch(event, cfg, job_id)
        return (
            f"已创建 Claude(GLM) 后台任务：{job_id}\n"
            f"查进度：/cc log {job_id}\n"
            f"任务列表：/cc jobs\n"
            f"取消：/cc stop {job_id}"
        )

    def _start_watch(self, event: AstrMessageEvent, cfg: Dict[str, Any], job_id: str) -> None:
        if job_id in self._watching:
            return
        self._watching.add(job_id)
        asyncio.create_task(self._watch_job(event, copy.deepcopy(cfg), job_id))

    async def _watch_job(self, event: AstrMessageEvent, cfg: Dict[str, Any], job_id: str) -> None:
        try:
            interval = max(3, int(cfg.get("job_poll_interval_seconds", 8)))
            max_wait = int(cfg.get("timeout_seconds", 1200)) + 600
            elapsed = 0
            while elapsed <= max_wait:
                await asyncio.sleep(interval)
                elapsed += interval
                res = await self._request(cfg, f"/jobs/{job_id}", None, timeout=30)
                job = res.get("job") or {}
                status = str(job.get("status") or "")
                if status in TERMINAL_STATUSES:
                    text = await self._job_log(cfg, job_id, prefix=f"任务 {job_id} 已{self._status_cn(status)}。\n")
                    try:
                        await event.send(event.plain_result(text))
                    except Exception as exc:
                        logger.warning(f"Claude job {job_id} 自动回推失败: {exc}")
                    return
        except Exception as exc:
            logger.warning(f"Claude job {job_id} watch 失败: {exc}")
        finally:
            self._watching.discard(job_id)

    def _status_cn(self, status: str) -> str:
        return {"done": "完成", "failed": "失败", "cancelled": "取消"}.get(status, status)

    async def _jobs(self, cfg: Dict[str, Any]) -> str:
        res = await self._request(cfg, "/jobs?limit=10", None, timeout=30)
        if not res.get("ok"):
            return "查询 jobs 失败：" + str(res.get("error") or res)
        jobs = res.get("jobs") or []
        if not jobs:
            return "暂无 Claude job。"
        lines = ["最近 Claude jobs："]
        for j in jobs:
            jid = str(j.get("job_id") or "")
            lines.append(f"{jid} | {j.get('status')} | {j.get('summary')} | {j.get('task_preview')}")
        return clip("\n".join(lines), int(cfg.get("max_reply_chars", 7000)))

    async def _resolve_job_id(self, cfg: Dict[str, Any], raw: str) -> str:
        raw = (raw or "").strip()
        res = await self._request(cfg, "/jobs?limit=50", None, timeout=30)
        jobs = res.get("jobs") or []
        if not raw:
            if not jobs:
                raise RuntimeError("暂无任务")
            return str(jobs[0].get("job_id") or "")
        for j in jobs:
            jid = str(j.get("job_id") or "")
            if jid == raw or jid.startswith(raw):
                return jid
        return raw

    async def _job_log(self, cfg: Dict[str, Any], job_id: str, prefix: str = "") -> str:
        if not job_id:
            return "需要 job_id。例：/cc log 19ef..."
        res = await self._request(cfg, f"/jobs/{job_id}/log?tail=120", None, timeout=60)
        if not res.get("ok"):
            return "查询 job 失败：" + str(res.get("error") or res)
        job = res.get("job") or {}
        result = str(res.get("result") or "")
        stderr = str(res.get("stderr") or "")
        stdout = str(res.get("stdout") or "")
        lines = [prefix.rstrip(), f"job: {job_id}", f"status: {job.get('status')} | {job.get('summary')}"]
        if result:
            lines.append("\n结果：\n" + result)
        elif stdout or stderr:
            lines.append("\n日志：\n" + (stdout or stderr))
        else:
            lines.append("\n暂无结果，可能还在运行。")
        if stderr and result:
            lines.append("\nstderr tail:\n" + stderr[-1200:])
        return clip("\n".join([x for x in lines if x]), int(cfg.get("max_reply_chars", 7000)))

    async def _cancel(self, cfg: Dict[str, Any], job_id: str) -> str:
        if not job_id:
            return "需要 job_id。例：/cc stop 19ef..."
        res = await self._request(cfg, f"/jobs/{job_id}/cancel", {}, timeout=30)
        if not res.get("ok"):
            return "取消失败：" + str(res.get("error") or res)
        job = res.get("job") or {}
        return f"已发送取消：{job_id}\nstatus: {job.get('status')}\nsummary: {job.get('summary')}"

    def _group_id(self, event: AstrMessageEvent) -> str:
        try:
            if event.is_private_chat():
                return ""
        except Exception:
            pass
        obj = getattr(event, "message_obj", None)
        for name in ("group_id", "group", "room_id"):
            val = getattr(obj, name, None)
            if val:
                return str(val)
        return str(getattr(event, "unified_msg_origin", "") or "")
