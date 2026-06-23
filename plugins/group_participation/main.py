import base64
import copy
import datetime
import json
import random
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import astrbot.api.star as star
import astrbot.api.event.filter as filter
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.event.filter import EventMessageType

CONFIG_PATH = Path("data/config/group_participation.json")

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "group_only": True,
    "provider_id": "ollama_gemma4_uncensored",
    "state_dir": "data/group_participation",
    "memory_dir": "data/group_memory",
    "style_dir": "data/group_style",
    "bot_names": ["一个蛋", "蛋蛋", "小蛋", "蛋", "bot", "机器人"],
    "mode": "normal",
    "modes": {
        "quiet": {"direct_threshold": 0.78, "proactive_threshold": 1.1, "cooldown_seconds": 600, "max_replies_per_20_messages": 1, "proactive_probability": 0.0},
        "normal": {"direct_threshold": 0.62, "proactive_threshold": 0.88, "cooldown_seconds": 180, "max_replies_per_20_messages": 2, "proactive_probability": 0.25},
        "active": {"direct_threshold": 0.55, "proactive_threshold": 0.78, "cooldown_seconds": 90, "max_replies_per_20_messages": 4, "proactive_probability": 0.45},
        "chatty": {"direct_threshold": 0.48, "proactive_threshold": 0.70, "cooldown_seconds": 45, "max_replies_per_20_messages": 6, "proactive_probability": 0.65}
    },
    "admin_only_manage": True,
    "max_context_messages": 14,
    "max_reply_chars": 800,
    "exclude_prefixes": [
        "/", "！", "!"
    ],
    "ability_keywords": ["生图", "画图", "代码", "跑代码", "执行", "部署", "docker", "github", "模型", "llm", "bot", "机器人", "架构", "报错", "怎么做", "怎么搞"],
}

REPLY_SYSTEM = """
你叫“一个蛋”，正在 QQ 群里以群友身份发言。
你不是客服，也不是公告机器人。你要像真人群友一样自然接话。

回复要求：
- 默认 1-3 句，短、自然、口语。
- 可以有一点吐槽和熟人感，但别恶意攻击。
- 直接回答当前语境，不要解释内部打分/策略/记忆。
- 如果是主动插嘴，必须更短，不要抢话，不要长篇大论。
- 如果是技术/代码/部署问题，可以认真一点，但仍然别废话。
""".strip()


def load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"group_participation JSON 读取失败 {path}: {exc}")
    return copy.deepcopy(default)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_cfg() -> Dict[str, Any]:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        user_cfg = load_json(CONFIG_PATH, {})
        if isinstance(user_cfg, dict):
            cfg.update(user_cfg)
    return cfg


def session_key(session_id: str) -> str:
    return base64.urlsafe_b64encode((session_id or "unknown").encode("utf-8")).decode("ascii").rstrip("=")


def now_ts() -> int:
    return int(time.time())


def clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"...[省略{len(text)-limit}字]"


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


@star.register(
    name="group_participation",
    desc="群友型参与策略：别人问一个蛋会答，平时按策略偶尔插嘴。",
    author="Codex",
    version="0.1.0",
)
class Main(star.Star):
    def __init__(self, context: star.Context) -> None:
        self.context = context
        cfg = self._ensure_config()
        Path(str(cfg.get("state_dir") or "data/group_participation")).mkdir(parents=True, exist_ok=True)

    def _ensure_config(self) -> Dict[str, Any]:
        cfg = load_cfg()
        if not CONFIG_PATH.exists():
            write_json(CONFIG_PATH, cfg)
        return cfg

    def _state_path(self, session_id: str) -> Path:
        cfg = load_cfg()
        return Path(str(cfg.get("state_dir") or "data/group_participation")) / f"{session_key(session_id)}.json"

    def _default_state(self, session_id: str) -> Dict[str, Any]:
        return {
            "schema_version": 1,
            "session_id": session_id,
            "mode": "",
            "recent_messages": [],
            "bot_replies": [],
            "last_reply_at": 0,
            "last_decision": {},
        }

    def _load_state(self, session_id: str) -> Dict[str, Any]:
        state = load_json(self._state_path(session_id), self._default_state(session_id))
        if not isinstance(state, dict):
            state = self._default_state(session_id)
        state.setdefault("recent_messages", [])
        state.setdefault("bot_replies", [])
        return state

    def _save_state(self, session_id: str, state: Dict[str, Any]) -> None:
        state["updated_at"] = now_ts()
        write_json(self._state_path(session_id), state)

    def _session_allowed(self, event: AstrMessageEvent, cfg: Dict[str, Any]) -> bool:
        if cfg.get("group_only", True) and event.is_private_chat():
            return False
        return True

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            admins = [str(x) for x in self.context.get_config().get("admins_id", [])]
            return str(event.get_sender_id()) in admins
        except Exception:
            return False

    def _get_mode(self, session_id: str, cfg: Dict[str, Any]) -> str:
        state = self._load_state(session_id)
        mode = str(state.get("mode") or cfg.get("mode") or "normal")
        return mode if mode in cfg.get("modes", {}) else "normal"

    def _mode_cfg(self, session_id: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
        return dict((cfg.get("modes") or {}).get(self._get_mode(session_id, cfg), (cfg.get("modes") or {}).get("normal", {})))

    def _excluded(self, text: str, cfg: Dict[str, Any]) -> bool:
        t = text.strip()
        if not t:
            return True
        for p in cfg.get("exclude_prefixes", []):
            if str(p) and t.startswith(str(p)):
                return True
        return False

    def _load_memory_style(self, session_id: str, cfg: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        key = session_key(session_id)
        mem = load_json(Path(str(cfg.get("memory_dir") or "data/group_memory")) / f"{key}.json", {})
        style = load_json(Path(str(cfg.get("style_dir") or "data/group_style")) / f"{key}.json", {})
        return (mem if isinstance(mem, dict) else {}, style if isinstance(style, dict) else {})

    def _append_recent(self, event: AstrMessageEvent, text: str, state: Dict[str, Any], bot: bool = False) -> None:
        item = {
            "ts": now_ts(),
            "sender_id": "BOT" if bot else str(event.get_sender_id() or ""),
            "sender_name": "一个蛋" if bot else str(event.get_sender_name() or ""),
            "text": clip(text, 400),
            "bot": bot,
        }
        recent = state.get("recent_messages") or []
        recent.append(item)
        state["recent_messages"] = recent[-60:]

    def _score(self, text: str, state: Dict[str, Any], memory: Dict[str, Any], cfg: Dict[str, Any], mode_cfg: Dict[str, Any]) -> Tuple[float, str, Dict[str, Any]]:
        lower = text.lower()
        bot_names = [str(x).lower() for x in cfg.get("bot_names", [])]
        name_hit = False
        for name in bot_names:
            if not name:
                continue
            # 单字“蛋”太容易误伤“蛋糕/坏蛋”，要求像称呼一样单独出现。
            if name == "蛋":
                if re.search(r"(^|[\s@])蛋([\s,，:：？?！!。~～]|$)", text):
                    name_hit = True
                    break
            elif name in lower:
                name_hit = True
                break
        question_hit = bool(re.search(r"(吗|么|嘛|咋|怎么|如何|为啥|为什么|哪|谁|啥|什么|多少|\?|？)", text))
        help_hit = bool(re.search(r"(帮我|帮忙|求|看看|解释|分析|算一下|跑一下|画一下|你怎么看|会不会|能不能|可不可以)", text))
        ability_hit = any(str(k).lower() in lower for k in cfg.get("ability_keywords", []))
        direct_address = name_hit or bool(re.search(r"(^|\s)(bot|机器人)(\s|$)", lower))
        recent = state.get("recent_messages") or []
        bot_recent_count = sum(1 for m in recent[-20:] if m.get("bot"))
        seconds_since = now_ts() - int(state.get("last_reply_at", 0) or 0)
        mode = self._get_mode(memory.get("session_id", "") or "", cfg)

        score = 0.0
        reasons: List[str] = []
        if direct_address:
            score += 0.45; reasons.append("提到一个蛋")
        if question_hit:
            score += 0.22; reasons.append("问题句")
        if help_hit:
            score += 0.20; reasons.append("求助/让机器人判断")
        if ability_hit:
            score += 0.18; reasons.append("命中能力范围")
        if memory.get("recent_focus") and any(w for w in re.findall(r"[\u4e00-\u9fffA-Za-z0-9_]{2,}", str(memory.get("recent_focus"))) if w in text):
            score += 0.08; reasons.append("和群记忆相关")

        proactive = False
        if not direct_address:
            # 插嘴只在“问题没人答/能力强相关/冷场后接梗”时考虑，分数更苛刻。
            if question_hit and ability_hit:
                score += 0.12; proactive = True; reasons.append("可帮忙回答")
            if len(recent) >= 3 and seconds_since > int(mode_cfg.get("cooldown_seconds", 180)):
                last_ts = int(recent[-2].get("ts", 0) or 0) if len(recent) >= 2 else 0
                if now_ts() - last_ts > 90 and (question_hit or ability_hit):
                    score += 0.08; proactive = True; reasons.append("轻微冷场")

        # 惩罚：刚说过、20条内说太多、群友连续对话中强插。
        cooldown = int(mode_cfg.get("cooldown_seconds", 180) or 180)
        if seconds_since < cooldown and not direct_address:
            score -= 0.35; reasons.append("冷却中")
        max_replies = int(mode_cfg.get("max_replies_per_20_messages", 2) or 2)
        if bot_recent_count >= max_replies and not direct_address:
            score -= 0.30; reasons.append("最近说太多")
        if len(recent) >= 3:
            last_senders = [m.get("sender_id") for m in recent[-3:] if not m.get("bot")]
            if len(set(last_senders)) == 2 and not direct_address and not ability_hit:
                score -= 0.12; reasons.append("避免打断对话")

        if direct_address or (question_hit and help_hit and name_hit):
            kind = "direct"
        elif proactive:
            kind = "proactive"
        else:
            kind = "silent"
        detail = {
            "name_hit": name_hit,
            "question_hit": question_hit,
            "help_hit": help_hit,
            "ability_hit": ability_hit,
            "bot_recent_count": bot_recent_count,
            "seconds_since_last_reply": seconds_since,
            "reasons": reasons,
        }
        return round(max(0.0, min(1.2, score)), 3), kind, detail

    def _should_reply(self, text: str, state: Dict[str, Any], memory: Dict[str, Any], cfg: Dict[str, Any], mode_cfg: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
        score, kind, detail = self._score(text, state, memory, cfg, mode_cfg)
        direct_th = float(mode_cfg.get("direct_threshold", 0.62) or 0.62)
        pro_th = float(mode_cfg.get("proactive_threshold", 0.88) or 0.88)
        prob = float(mode_cfg.get("proactive_probability", 0.25) or 0.25)
        detail["score"] = score
        detail["kind"] = kind
        if kind == "direct" and score >= direct_th:
            return True, "direct", detail
        if kind == "proactive" and score >= pro_th and random.random() <= prob:
            return True, "proactive", detail
        return False, "silent", detail

    def _build_context(self, event: AstrMessageEvent, text: str, kind: str, state: Dict[str, Any], memory: Dict[str, Any], style: Dict[str, Any], decision: Dict[str, Any]) -> str:
        recent_rows = []
        for m in (state.get("recent_messages") or [])[-int(load_cfg().get("max_context_messages", 14) or 14):]:
            ts = datetime.datetime.fromtimestamp(int(m.get("ts", 0) or 0)).strftime("%H:%M")
            recent_rows.append(f"[{ts}] {m.get('sender_name') or m.get('sender_id')}: {m.get('text')}")
        payload = {
            "发言类型": "别人问你/提到你，直接回答" if kind == "direct" else "你判断可以偶尔插一句，务必短，不抢话",
            "当前消息": text,
            "最近群聊": recent_rows,
            "群记忆": {k: memory.get(k) for k in ["summary", "style", "recent_focus", "memes", "topics", "facts", "users"]},
            "群风格": {k: style.get(k) for k in ["tone", "style_prompt", "catchphrases", "emoji_style", "avg_length", "avoid"]},
            "策略判断": decision,
        }
        return json.dumps(payload, ensure_ascii=False)

    async def _generate_reply(self, event: AstrMessageEvent, text: str, kind: str, state: Dict[str, Any], memory: Dict[str, Any], style: Dict[str, Any], decision: Dict[str, Any], cfg: Dict[str, Any]) -> str:
        provider_id = str(cfg.get("provider_id") or "").strip()
        provider = self.context.get_provider_by_id(provider_id) if provider_id else self.context.get_using_provider()
        if provider is None:
            raise RuntimeError(f"参与策略 provider 不可用：{provider_id or 'current'}")
        prompt = self._build_context(event, text, kind, state, memory, style, decision)
        resp = await provider.text_chat(
            prompt=prompt,
            session_id=f"group_participation:{event.unified_msg_origin}",
            contexts=[],
            system_prompt=REPLY_SYSTEM,
            func_tool=None,
            image_urls=[],
            conversation=None,
        )
        reply = (resp.completion_text or "").strip()
        reply = re.sub(r"^一个蛋[:：]\s*", "", reply)
        return clip(reply or "我看看。", int(cfg.get("max_reply_chars", 800) or 800))

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def participate(self, event: AstrMessageEvent):
        cfg = load_cfg()
        if not cfg.get("enabled", True) or not self._session_allowed(event, cfg):
            return
        # 已经被 @ / wake / 命令唤醒的消息交给 AstrBot 原流程，避免重复回复。
        if getattr(event, "is_at_or_wake_command", False):
            return
        text = clean_text(event.get_message_str() or "")
        if self._excluded(text, cfg):
            return
        session_id = event.unified_msg_origin
        state = self._load_state(session_id)
        self._append_recent(event, text, state, bot=False)
        memory, style = self._load_memory_style(session_id, cfg)
        mode_cfg = self._mode_cfg(session_id, cfg)
        should, kind, decision = self._should_reply(text, state, memory, cfg, mode_cfg)
        state["last_decision"] = decision
        self._save_state(session_id, state)
        if not should:
            return
        try:
            reply = await self._generate_reply(event, text, kind, state, memory, style, decision, cfg)
            if not reply:
                return
            await event.send(event.plain_result(reply))
            state = self._load_state(session_id)
            self._append_recent(event, reply, state, bot=True)
            state["last_reply_at"] = now_ts()
            bot_replies = state.get("bot_replies") or []
            bot_replies.append({"ts": now_ts(), "kind": kind, "text": reply, "decision": decision})
            state["bot_replies"] = bot_replies[-50:]
            self._save_state(session_id, state)
            event.stop_event()
        except Exception as exc:
            logger.warning(f"group_participation 生成回复失败: {type(exc).__name__}: {exc}")
            return

    def _parse_command(self, event: AstrMessageEvent) -> Tuple[str, str]:
        text = re.sub(r"^[/!！\s]*(参与策略|参与|插嘴)\b", "", (event.get_message_str() or "").strip(), flags=re.I).strip()
        if not text:
            return "查看", ""
        parts = text.split(maxsplit=1)
        action = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""
        aliases = {"view": "查看", "show": "查看", "status": "查看", "quiet": "安静", "normal": "正常", "active": "活跃", "chatty": "嘴欠", "help": "帮助", "?": "帮助", "？": "帮助"}
        return aliases.get(action, action), rest

    @filter.command("参与策略", alias={"参与", "插嘴"})
    async def policy_command(self, event: AstrMessageEvent):
        event.call_llm = True
        cfg = load_cfg()
        if cfg.get("group_only", True) and event.is_private_chat():
            yield event.plain_result("参与策略当前只在群聊里使用。")
            return
        action, _ = self._parse_command(event)
        session_id = event.unified_msg_origin
        state = self._load_state(session_id)
        if action in {"帮助"}:
            yield event.plain_result("用法：/参与策略 查看 | 安静 | 正常 | 活跃 | 嘴欠\n安静=只在明确问它时答；正常=默认；活跃=更常接话；嘴欠=更像群友但仍有冷却。")
            return
        if action in {"查看"}:
            mode = self._get_mode(session_id, cfg)
            last = state.get("last_decision") or {}
            yield event.plain_result(
                f"参与策略：{mode}\n"
                f"最近机器人发言：{len(state.get('bot_replies') or [])} 条记录\n"
                f"上次判断：{json.dumps(last, ensure_ascii=False)[:800]}"
            )
            return
        mode_map = {"安静": "quiet", "正常": "normal", "活跃": "active", "嘴欠": "chatty"}
        if action in mode_map:
            if cfg.get("admin_only_manage", True) and not self._is_admin(event):
                yield event.plain_result("只有管理员可以改参与策略。")
                return
            state["mode"] = mode_map[action]
            self._save_state(session_id, state)
            yield event.plain_result(f"已设置当前群参与策略：{action}")
            return
        yield event.plain_result("用法：/参与策略 查看 | 安静 | 正常 | 活跃 | 嘴欠")
