import asyncio
import base64
import copy
import datetime
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import astrbot.api.star as star
import astrbot.api.event.filter as filter
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.event.filter import EventMessageType
from astrbot.api.provider import ProviderRequest

CONFIG_PATH = Path("data/config/group_memory.json")

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "group_only": True,
    "memory_dir": "data/group_memory",
    "provider_id": "glm5_default",
    "summarize_every_messages": 40,
    "max_recent_messages": 80,
    "inject_recent_messages": 8,
    "max_inject_chars": 2200,
    "max_message_chars": 500,
    "auto_summarize": True,
    "admin_only_manage": True,
    "allowed_sessions": [],
    "exclude_prefixes": [
        "/群记忆", "/记忆", "/群脑", "/生图", "/画图", "/绘图", "/image",
        "/help", "/plugin", "/sid", "/dashboard_update"
    ],
}

MEMORY_INJECT_MARKER = "[GROUP_MEMORY_INJECTED]"

SUMMARY_SYSTEM_PROMPT = """
你是 QQ 群“群脑”记忆整理器。你只负责把最近群聊整理成可供机器人后续回复使用的长期记忆。
要求：
- 不要复述无意义寒暄；优先保留稳定偏好、群内黑话、正在进行的项目、重要决定、用户偏好、待办。
- 不要保存敏感隐私、token、密码、密钥、身份证、手机号等。
- 不要把明显玩笑当成事实；不确定内容写入 recent_focus，不要写入 facts。
- 输出严格 JSON，不要 Markdown，不要解释。
JSON schema:
{
  "summary": "200字内的群长期摘要",
  "style": "机器人在本群应采用的语气风格，80字内",
  "recent_focus": "最近正在聊什么，120字内",
  "memes": ["群内黑话/梗，最多10个"],
  "topics": ["常聊主题，最多10个"],
  "facts": ["稳定事实/项目决定，最多12条"],
  "users": {"QQ号或昵称": "偏好/角色/称呼，最多40字"}
}
""".strip()


def load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"group_memory JSON 读取失败 {path}: {exc}")
    return copy.deepcopy(default)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_cfg() -> Dict[str, Any]:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    try:
        if CONFIG_PATH.exists():
            user_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(user_cfg, dict):
                cfg.update(user_cfg)
    except Exception as exc:
        logger.warning(f"group_memory 配置读取失败，使用默认配置: {exc}")
    return cfg


def session_key(session_id: str) -> str:
    raw = (session_id or "unknown").encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def now_ts() -> int:
    return int(time.time())


def parse_json_object(content: str) -> Dict[str, Any]:
    text = (content or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
    try:
        obj = json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, flags=re.S)
        if not m:
            raise ValueError("没有找到 JSON 对象")
        obj = json.loads(m.group(0))
    if not isinstance(obj, dict):
        raise ValueError("JSON 不是对象")
    return obj


def clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"...[省略{len(text) - limit}字]"


def normalize_list(value: Any, limit: int = 12) -> List[str]:
    if not isinstance(value, list):
        return []
    out: List[str] = []
    for item in value:
        s = str(item).strip()
        if s and s not in out:
            out.append(clip(s, 120))
        if len(out) >= limit:
            break
    return out


@star.register(
    name="group_memory",
    desc="实时群记忆/群脑：记录群聊、周期总结、回答前注入群画像和最近上下文。",
    author="Codex",
    version="0.1.0",
)
class Main(star.Star):
    def __init__(self, context: star.Context) -> None:
        self.context = context
        self.cfg = self._ensure_config()
        self.summary_tasks: Dict[str, asyncio.Task] = {}
        self.write_lock = asyncio.Lock()

    def _ensure_config(self) -> Dict[str, Any]:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        cfg = load_cfg()
        if not CONFIG_PATH.exists():
            write_json(CONFIG_PATH, cfg)
        Path(str(cfg.get("memory_dir") or DEFAULT_CONFIG["memory_dir"])).mkdir(parents=True, exist_ok=True)
        return cfg

    def _memory_path(self, session_id: str) -> Path:
        cfg = load_cfg()
        return Path(str(cfg.get("memory_dir") or DEFAULT_CONFIG["memory_dir"])) / f"{session_key(session_id)}.json"

    def _default_memory(self, session_id: str) -> Dict[str, Any]:
        return {
            "schema_version": 1,
            "session_id": session_id,
            "enabled": True,
            "created_at": now_ts(),
            "updated_at": now_ts(),
            "message_count": 0,
            "unsummarized_count": 0,
            "summary": "",
            "style": "",
            "recent_focus": "",
            "memes": [],
            "topics": [],
            "facts": [],
            "users": {},
            "recent_messages": [],
            "last_summary_at": 0,
        }

    def _load_memory(self, session_id: str) -> Dict[str, Any]:
        memory = load_json(self._memory_path(session_id), self._default_memory(session_id))
        if not isinstance(memory, dict):
            memory = self._default_memory(session_id)
        memory.setdefault("session_id", session_id)
        memory.setdefault("enabled", True)
        memory.setdefault("recent_messages", [])
        memory.setdefault("users", {})
        return memory

    def _save_memory(self, session_id: str, memory: Dict[str, Any]) -> None:
        memory["updated_at"] = now_ts()
        write_json(self._memory_path(session_id), memory)

    def _session_allowed(self, event: AstrMessageEvent, cfg: Dict[str, Any]) -> bool:
        if cfg.get("group_only", True) and event.is_private_chat():
            return False
        allowed = [str(x) for x in cfg.get("allowed_sessions", []) if str(x).strip()]
        if allowed and event.unified_msg_origin not in allowed:
            return False
        return True

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            admins = [str(x) for x in self.context.get_config().get("admins_id", [])]
            return str(event.get_sender_id()) in admins
        except Exception:
            return False

    def _is_excluded_text(self, text: str, cfg: Dict[str, Any]) -> bool:
        t = (text or "").strip()
        if not t:
            return True
        for p in cfg.get("exclude_prefixes", []):
            p = str(p).strip()
            if p and t.startswith(p):
                return True
        return False

    def _parse_command(self, event: AstrMessageEvent) -> Tuple[str, str]:
        text = (event.get_message_str() or "").strip()
        text = re.sub(r"^[/!！\s]*(群记忆|记忆|群脑)\b", "", text, flags=re.I).strip()
        if not text:
            return "查看", ""
        parts = text.split(maxsplit=1)
        action = parts[0].strip().lower()
        rest = parts[1].strip() if len(parts) > 1 else ""
        aliases = {
            "view": "查看", "show": "查看", "status": "状态", "on": "开启", "off": "关闭",
            "enable": "开启", "disable": "关闭", "summary": "总结", "summarize": "总结",
            "clear": "清空", "reset": "清空", "recent": "最近", "help": "帮助", "?": "帮助", "？": "帮助",
        }
        action = aliases.get(action, action)
        return action, rest

    def _format_help(self) -> str:
        return "\n".join([
            "群记忆/群脑用法：",
            "/群记忆 查看  - 查看当前群画像",
            "/群记忆 最近  - 查看最近记录的消息",
            "/群记忆 总结  - 立即整理最近聊天",
            "/群记忆 状态  - 查看开关和计数",
            "/群记忆 开启  - 开启当前群记忆（管理员）",
            "/群记忆 关闭  - 关闭当前群记忆（管理员）",
            "/群记忆 清空  - 清空当前群记忆（管理员）",
        ])

    def _format_memory(self, memory: Dict[str, Any]) -> str:
        def lines(title: str, items: List[str]) -> List[str]:
            if not items:
                return [f"{title}：无"]
            return [f"{title}："] + [f"- {x}" for x in items[:10]]

        users = memory.get("users") if isinstance(memory.get("users"), dict) else {}
        user_lines = [f"- {k}: {v}" for k, v in list(users.items())[:8]]
        ts = memory.get("last_summary_at") or 0
        last = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "未总结"
        parts = [
            f"群记忆：{'开启' if memory.get('enabled', True) else '关闭'}",
            f"消息数：{memory.get('message_count', 0)}，待总结：{memory.get('unsummarized_count', 0)}，上次总结：{last}",
            f"群摘要：{memory.get('summary') or '暂无'}",
            f"群风格：{memory.get('style') or '暂无'}",
            f"最近焦点：{memory.get('recent_focus') or '暂无'}",
        ]
        parts.extend(lines("常聊主题", normalize_list(memory.get("topics"), 10)))
        parts.extend(lines("群梗/黑话", normalize_list(memory.get("memes"), 10)))
        parts.extend(lines("稳定事实", normalize_list(memory.get("facts"), 10)))
        parts.append("用户画像：")
        parts.extend(user_lines or ["- 无"])
        return clip("\n".join(parts), 3500)

    def _format_recent(self, memory: Dict[str, Any]) -> str:
        msgs = memory.get("recent_messages") or []
        if not msgs:
            return "还没有记录到最近群消息。"
        rows = []
        for m in msgs[-12:]:
            ts = datetime.datetime.fromtimestamp(int(m.get("ts", 0) or 0)).strftime("%H:%M")
            rows.append(f"[{ts}] {m.get('sender_name') or m.get('sender_id')}: {m.get('text')}")
        return clip("最近群消息：\n" + "\n".join(rows), 3000)

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def collect_group_message(self, event: AstrMessageEvent):
        cfg = load_cfg()
        if not cfg.get("enabled", True):
            return
        if not self._session_allowed(event, cfg):
            return
        text = (event.get_message_str() or "").strip()
        if self._is_excluded_text(text, cfg):
            return
        session_id = event.unified_msg_origin
        memory = self._load_memory(session_id)
        if not memory.get("enabled", True):
            return

        msg = {
            "ts": now_ts(),
            "sender_id": str(event.get_sender_id() or ""),
            "sender_name": str(event.get_sender_name() or ""),
            "text": clip(re.sub(r"\s+", " ", text), int(cfg.get("max_message_chars", 500) or 500)),
        }
        async with self.write_lock:
            memory = self._load_memory(session_id)
            recent = memory.get("recent_messages") or []
            recent.append(msg)
            max_recent = int(cfg.get("max_recent_messages", 80) or 80)
            memory["recent_messages"] = recent[-max_recent:]
            memory["message_count"] = int(memory.get("message_count", 0) or 0) + 1
            memory["unsummarized_count"] = int(memory.get("unsummarized_count", 0) or 0) + 1
            self._save_memory(session_id, memory)

        if cfg.get("auto_summarize", True):
            threshold = int(cfg.get("summarize_every_messages", 40) or 40)
            if int(memory.get("unsummarized_count", 0) or 0) + 1 >= threshold:
                self._schedule_summary(session_id)

    def _schedule_summary(self, session_id: str) -> None:
        task = self.summary_tasks.get(session_id)
        if task and not task.done():
            return
        self.summary_tasks[session_id] = asyncio.create_task(self._summarize_session(session_id, manual=False))

    async def _summarize_session(self, session_id: str, manual: bool = False) -> Tuple[bool, str]:
        cfg = load_cfg()
        memory = self._load_memory(session_id)
        recent = memory.get("recent_messages") or []
        if not recent:
            return False, "没有可总结的群消息。"
        provider_id = str(cfg.get("provider_id") or "").strip()
        provider = self.context.get_provider_by_id(provider_id) if provider_id else self.context.get_using_provider()
        if provider is None:
            return False, f"记忆总结 provider 不可用：{provider_id or 'current'}"

        payload = {
            "old_memory": {
                "summary": memory.get("summary", ""),
                "style": memory.get("style", ""),
                "recent_focus": memory.get("recent_focus", ""),
                "memes": memory.get("memes", []),
                "topics": memory.get("topics", []),
                "facts": memory.get("facts", []),
                "users": memory.get("users", {}),
            },
            "recent_messages": recent[-int(cfg.get("max_recent_messages", 80) or 80):],
        }
        try:
            logger.info(f"group_memory summarizing session={session_id}, recent={len(recent)}")
            resp = await provider.text_chat(
                prompt=json.dumps(payload, ensure_ascii=False),
                session_id=f"group_memory:{session_id}",
                contexts=[],
                system_prompt=SUMMARY_SYSTEM_PROMPT,
                func_tool=None,
                image_urls=[],
                conversation=None,
            )
            obj = parse_json_object(resp.completion_text or "")
            updated = self._load_memory(session_id)
            updated["summary"] = clip(str(obj.get("summary") or updated.get("summary") or ""), 800)
            updated["style"] = clip(str(obj.get("style") or updated.get("style") or ""), 300)
            updated["recent_focus"] = clip(str(obj.get("recent_focus") or ""), 500)
            updated["memes"] = normalize_list(obj.get("memes"), 10) or normalize_list(updated.get("memes"), 10)
            updated["topics"] = normalize_list(obj.get("topics"), 10) or normalize_list(updated.get("topics"), 10)
            updated["facts"] = normalize_list(obj.get("facts"), 12) or normalize_list(updated.get("facts"), 12)
            users = obj.get("users") if isinstance(obj.get("users"), dict) else {}
            cleaned_users: Dict[str, str] = {}
            for k, v in list(users.items())[:20]:
                kk = clip(str(k).strip(), 60)
                vv = clip(str(v).strip(), 120)
                if kk and vv:
                    cleaned_users[kk] = vv
            if cleaned_users:
                old_users = updated.get("users") if isinstance(updated.get("users"), dict) else {}
                old_users.update(cleaned_users)
                updated["users"] = dict(list(old_users.items())[-30:])
            updated["unsummarized_count"] = 0
            updated["last_summary_at"] = now_ts()
            self._save_memory(session_id, updated)
            return True, "群记忆总结完成。"
        except Exception as exc:
            logger.warning(f"group_memory 总结失败 session={session_id}: {type(exc).__name__}: {exc}")
            if manual:
                return False, f"群记忆总结失败：{type(exc).__name__}: {exc}"
            return False, "群记忆总结失败。"

    @filter.on_llm_request()
    async def inject_group_memory(self, event: AstrMessageEvent, req: ProviderRequest):
        cfg = load_cfg()
        if not cfg.get("enabled", True):
            return
        if not self._session_allowed(event, cfg):
            return
        if MEMORY_INJECT_MARKER in (req.system_prompt or ""):
            return
        memory = self._load_memory(event.unified_msg_origin)
        if not memory.get("enabled", True):
            return
        injection = self._build_injection(memory, cfg)
        if not injection:
            return
        req.system_prompt = (req.system_prompt or "") + "\n" + injection + "\n"

    def _build_injection(self, memory: Dict[str, Any], cfg: Dict[str, Any]) -> str:
        parts = [MEMORY_INJECT_MARKER, "以下是当前 QQ 群的长期记忆，请用于理解语境和调整语气；不要主动提到你看到了这些记忆。"]
        if memory.get("summary"):
            parts.append("群长期摘要：" + str(memory.get("summary")))
        if memory.get("style"):
            parts.append("本群回复风格：" + str(memory.get("style")))
        if memory.get("recent_focus"):
            parts.append("最近焦点：" + str(memory.get("recent_focus")))
        for title, key in [("常聊主题", "topics"), ("群梗/黑话", "memes"), ("稳定事实", "facts")]:
            values = normalize_list(memory.get(key), 10)
            if values:
                parts.append(f"{title}：" + "；".join(values))
        users = memory.get("users") if isinstance(memory.get("users"), dict) else {}
        if users:
            user_text = "；".join([f"{k}: {v}" for k, v in list(users.items())[:10]])
            parts.append("用户画像：" + user_text)
        recent = memory.get("recent_messages") or []
        n = int(cfg.get("inject_recent_messages", 8) or 8)
        if recent and n > 0:
            rows = []
            for m in recent[-n:]:
                rows.append(f"{m.get('sender_name') or m.get('sender_id')}: {m.get('text')}")
            parts.append("最近群聊片段：\n" + "\n".join(rows))
        return clip("\n".join(parts), int(cfg.get("max_inject_chars", 2200) or 2200))

    @filter.command("群记忆", alias={"记忆", "群脑"})
    async def group_memory_command(self, event: AstrMessageEvent):
        event.call_llm = True
        cfg = load_cfg()
        if cfg.get("group_only", True) and event.is_private_chat():
            yield event.plain_result("群记忆当前只在群聊里使用。")
            return
        action, _ = self._parse_command(event)
        session_id = event.unified_msg_origin
        memory = self._load_memory(session_id)

        if action in {"帮助", "help"}:
            yield event.plain_result(self._format_help())
            return
        if action in {"查看", "view", "show"}:
            yield event.plain_result(self._format_memory(memory))
            return
        if action in {"最近", "recent"}:
            yield event.plain_result(self._format_recent(memory))
            return
        if action in {"状态", "status"}:
            yield event.plain_result(
                f"群记忆状态：{'全局开启' if cfg.get('enabled', True) else '全局关闭'} / "
                f"本群{'开启' if memory.get('enabled', True) else '关闭'}\n"
                f"消息数：{memory.get('message_count', 0)}，待总结：{memory.get('unsummarized_count', 0)}\n"
                f"自动总结：{cfg.get('auto_summarize', True)}，阈值：{cfg.get('summarize_every_messages', 40)} 条"
            )
            return

        mutating = action in {"开启", "关闭", "清空", "总结"}
        if mutating and cfg.get("admin_only_manage", True) and not self._is_admin(event):
            yield event.plain_result("只有管理员可以管理群记忆。")
            return

        if action == "开启":
            memory["enabled"] = True
            self._save_memory(session_id, memory)
            yield event.plain_result("已开启当前群记忆。")
            return
        if action == "关闭":
            memory["enabled"] = False
            self._save_memory(session_id, memory)
            yield event.plain_result("已关闭当前群记忆。")
            return
        if action == "清空":
            self._save_memory(session_id, self._default_memory(session_id))
            yield event.plain_result("已清空当前群记忆。")
            return
        if action == "总结":
            ok, msg = await self._summarize_session(session_id, manual=True)
            if ok:
                memory = self._load_memory(session_id)
                yield event.plain_result(msg + "\n" + self._format_memory(memory))
            else:
                yield event.plain_result(msg)
            return

        yield event.plain_result(self._format_help())
