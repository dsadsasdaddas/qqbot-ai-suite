import asyncio
import base64
import copy
import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Tuple

import astrbot.api.star as star
import astrbot.api.event.filter as filter
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.event.filter import EventMessageType

CONFIG_PATH = Path("data/config/group_style_learner.json")

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "group_only": True,
    "style_dir": "data/group_style",
    "provider_id": "glm5_default",
    "summarize_every_messages": 80,
    "max_samples": 160,
    "max_sample_chars": 220,
    "admin_only_manage": True,
    "exclude_prefixes": [
        "/群风格", "/风格", "/生图", "/画图", "/绘图", "/image",
        "/群记忆", "/记忆", "/群脑", "/help", "/plugin", "/sid"
    ],
}

STYLE_SYSTEM_PROMPT = """
你是 QQ 群语言风格分析器。根据群聊样本，提炼整个群的表达风格，供机器人“一个蛋”自然融入群聊。
要求：
- 学习整体风格，不要模仿某个具体个人，不要输出隐私。
- 优先提炼：句子长短、口语程度、吐槽方式、常见口癖、标点/emoji 习惯、禁忌风格。
- 不要保存 token、手机号、身份证、地址等敏感信息。
- 输出严格 JSON，不要 Markdown。
JSON schema:
{
  "tone": "80字内群语气总结",
  "style_prompt": "给机器人使用的风格指令，150字内",
  "catchphrases": ["常见口癖/梗，最多12个"],
  "emoji_style": "emoji/标点习惯，60字内",
  "avg_length": "短|中|长",
  "avoid": ["应该避免的表达方式，最多8条"]
}
""".strip()


def load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"group_style_learner JSON 读取失败 {path}: {exc}")
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


def parse_json_object(content: str) -> Dict[str, Any]:
    text = (content or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
    try:
        obj = json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, flags=re.S)
        if not m:
            raise ValueError("没有找到 JSON")
        obj = json.loads(m.group(0))
    if not isinstance(obj, dict):
        raise ValueError("JSON 不是对象")
    return obj


def normalize_list(value: Any, limit: int) -> List[str]:
    if not isinstance(value, list):
        return []
    out: List[str] = []
    for item in value:
        s = str(item).strip()
        if s and s not in out:
            out.append(clip(s, 80))
        if len(out) >= limit:
            break
    return out


@star.register(
    name="group_style_learner",
    desc="学习群友说话风格，提炼群口癖、语气、句长和表达禁忌。",
    author="Codex",
    version="0.1.0",
)
class Main(star.Star):
    def __init__(self, context: star.Context) -> None:
        self.context = context
        cfg = self._ensure_config()
        self.tasks: Dict[str, asyncio.Task] = {}
        self.lock = asyncio.Lock()
        Path(str(cfg.get("style_dir") or "data/group_style")).mkdir(parents=True, exist_ok=True)

    def _ensure_config(self) -> Dict[str, Any]:
        cfg = load_cfg()
        if not CONFIG_PATH.exists():
            write_json(CONFIG_PATH, cfg)
        return cfg

    def _style_path(self, session_id: str) -> Path:
        cfg = load_cfg()
        return Path(str(cfg.get("style_dir") or "data/group_style")) / f"{session_key(session_id)}.json"

    def _default_style(self, session_id: str) -> Dict[str, Any]:
        return {
            "schema_version": 1,
            "session_id": session_id,
            "enabled": True,
            "created_at": now_ts(),
            "updated_at": now_ts(),
            "message_count": 0,
            "unsummarized_count": 0,
            "samples": [],
            "tone": "",
            "style_prompt": "",
            "catchphrases": [],
            "emoji_style": "",
            "avg_length": "短",
            "avoid": [],
            "heuristics": {},
            "last_summary_at": 0,
        }

    def _load_style(self, session_id: str) -> Dict[str, Any]:
        data = load_json(self._style_path(session_id), self._default_style(session_id))
        if not isinstance(data, dict):
            data = self._default_style(session_id)
        data.setdefault("samples", [])
        data.setdefault("enabled", True)
        return data

    def _save_style(self, session_id: str, data: Dict[str, Any]) -> None:
        data["updated_at"] = now_ts()
        write_json(self._style_path(session_id), data)

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            admins = [str(x) for x in self.context.get_config().get("admins_id", [])]
            return str(event.get_sender_id()) in admins
        except Exception:
            return False

    def _excluded(self, text: str, cfg: Dict[str, Any]) -> bool:
        t = (text or "").strip()
        if not t:
            return True
        for p in cfg.get("exclude_prefixes", []):
            if str(p) and t.startswith(str(p)):
                return True
        return False

    def _update_heuristics(self, style: Dict[str, Any]) -> None:
        samples = style.get("samples") or []
        texts = [str(x.get("text", "")) for x in samples if isinstance(x, dict)]
        if not texts:
            style["heuristics"] = {}
            return
        lens = [len(t) for t in texts]
        punct = Counter()
        short_tokens = Counter()
        for t in texts:
            for ch in "！？?!。~～…哈哈哈草绷艹w":
                if ch in t:
                    punct[ch] += t.count(ch)
            for token in re.findall(r"[\u4e00-\u9fffA-Za-z0-9_]{2,8}", t):
                if token not in {"这个", "那个", "就是", "然后", "不是", "可以", "什么"}:
                    short_tokens[token] += 1
        avg = sum(lens) / len(lens)
        style["heuristics"] = {
            "avg_chars": round(avg, 1),
            "short_ratio": round(sum(1 for x in lens if x <= 25) / len(lens), 2),
            "common_marks": [x for x, _ in punct.most_common(8)],
            "common_tokens": [x for x, c in short_tokens.most_common(12) if c >= 2],
        }
        style["avg_length"] = "短" if avg <= 35 else "中" if avg <= 90 else "长"

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def collect_style(self, event: AstrMessageEvent):
        cfg = load_cfg()
        if not cfg.get("enabled", True):
            return
        if cfg.get("group_only", True) and event.is_private_chat():
            return
        text = re.sub(r"\s+", " ", (event.get_message_str() or "").strip())
        if self._excluded(text, cfg):
            return
        session_id = event.unified_msg_origin
        async with self.lock:
            style = self._load_style(session_id)
            if not style.get("enabled", True):
                return
            sample = {
                "ts": now_ts(),
                "sender_id": str(event.get_sender_id() or ""),
                "sender_name": str(event.get_sender_name() or ""),
                "text": clip(text, int(cfg.get("max_sample_chars", 220) or 220)),
            }
            samples = style.get("samples") or []
            samples.append(sample)
            style["samples"] = samples[-int(cfg.get("max_samples", 160) or 160):]
            style["message_count"] = int(style.get("message_count", 0) or 0) + 1
            style["unsummarized_count"] = int(style.get("unsummarized_count", 0) or 0) + 1
            self._update_heuristics(style)
            self._save_style(session_id, style)
        threshold = int(cfg.get("summarize_every_messages", 80) or 80)
        if int(style.get("unsummarized_count", 0) or 0) >= threshold:
            self._schedule_summary(session_id)

    def _schedule_summary(self, session_id: str) -> None:
        task = self.tasks.get(session_id)
        if task and not task.done():
            return
        self.tasks[session_id] = asyncio.create_task(self._summarize(session_id, manual=False))

    async def _summarize(self, session_id: str, manual: bool = False) -> Tuple[bool, str]:
        cfg = load_cfg()
        style = self._load_style(session_id)
        samples = style.get("samples") or []
        if not samples:
            return False, "还没有可学习的群风格样本。"
        provider_id = str(cfg.get("provider_id") or "").strip()
        provider = self.context.get_provider_by_id(provider_id) if provider_id else self.context.get_using_provider()
        if provider is None:
            return False, f"风格总结 provider 不可用：{provider_id or 'current'}"
        payload = {
            "old_style": {k: style.get(k) for k in ["tone", "style_prompt", "catchphrases", "emoji_style", "avg_length", "avoid", "heuristics"]},
            "samples": samples[-int(cfg.get("max_samples", 160) or 160):],
        }
        try:
            logger.info(f"group_style_learner summarizing session={session_id}, samples={len(samples)}")
            resp = await provider.text_chat(
                prompt=json.dumps(payload, ensure_ascii=False),
                session_id=f"group_style:{session_id}",
                contexts=[],
                system_prompt=STYLE_SYSTEM_PROMPT,
                func_tool=None,
                image_urls=[],
                conversation=None,
            )
            obj = parse_json_object(resp.completion_text or "")
            style = self._load_style(session_id)
            style["tone"] = clip(str(obj.get("tone") or style.get("tone") or ""), 300)
            style["style_prompt"] = clip(str(obj.get("style_prompt") or style.get("style_prompt") or ""), 500)
            style["catchphrases"] = normalize_list(obj.get("catchphrases"), 12) or normalize_list(style.get("catchphrases"), 12)
            style["emoji_style"] = clip(str(obj.get("emoji_style") or style.get("emoji_style") or ""), 200)
            style["avg_length"] = str(obj.get("avg_length") or style.get("avg_length") or "短")[:10]
            style["avoid"] = normalize_list(obj.get("avoid"), 8) or normalize_list(style.get("avoid"), 8)
            style["unsummarized_count"] = 0
            style["last_summary_at"] = now_ts()
            self._update_heuristics(style)
            self._save_style(session_id, style)
            return True, "群风格学习完成。"
        except Exception as exc:
            logger.warning(f"group_style_learner 总结失败: {type(exc).__name__}: {exc}")
            return False, f"群风格总结失败：{type(exc).__name__}: {exc}" if manual else "群风格总结失败。"

    def _format_style(self, style: Dict[str, Any]) -> str:
        h = style.get("heuristics") if isinstance(style.get("heuristics"), dict) else {}
        parts = [
            f"群风格：{'开启' if style.get('enabled', True) else '关闭'}",
            f"消息数：{style.get('message_count', 0)}，待总结：{style.get('unsummarized_count', 0)}",
            f"语气：{style.get('tone') or '暂无'}",
            f"风格指令：{style.get('style_prompt') or '暂无'}",
            f"平均句长：{style.get('avg_length') or '未知'} / {h.get('avg_chars', 'n/a')} 字",
            "口癖/梗：" + ("；".join(normalize_list(style.get("catchphrases"), 12)) or "暂无"),
            "标点/emoji：" + (style.get("emoji_style") or "；".join(h.get("common_marks", [])) or "暂无"),
            "避免：" + ("；".join(normalize_list(style.get("avoid"), 8)) or "暂无"),
        ]
        return clip("\n".join(parts), 3000)

    def _parse_command(self, event: AstrMessageEvent) -> str:
        text = re.sub(r"^[/!！\s]*(群风格|风格)\b", "", (event.get_message_str() or "").strip(), flags=re.I).strip()
        return text.split(maxsplit=1)[0].lower() if text else "查看"

    @filter.command("群风格", alias={"风格"})
    async def style_command(self, event: AstrMessageEvent):
        event.call_llm = True
        cfg = load_cfg()
        if cfg.get("group_only", True) and event.is_private_chat():
            yield event.plain_result("群风格学习当前只在群聊里使用。")
            return
        action = self._parse_command(event)
        aliases = {"view": "查看", "show": "查看", "status": "查看", "summary": "总结", "summarize": "总结", "clear": "清空", "reset": "清空", "on": "开启", "off": "关闭", "help": "帮助", "?": "帮助", "？": "帮助"}
        action = aliases.get(action, action)
        session_id = event.unified_msg_origin
        style = self._load_style(session_id)
        if action in {"帮助"}:
            yield event.plain_result("用法：/群风格 查看 | 总结 | 开启 | 关闭 | 清空")
            return
        if action in {"查看"}:
            yield event.plain_result(self._format_style(style))
            return
        if cfg.get("admin_only_manage", True) and not self._is_admin(event):
            yield event.plain_result("只有管理员可以管理群风格学习。")
            return
        if action == "总结":
            ok, msg = await self._summarize(session_id, manual=True)
            yield event.plain_result(msg + ("\n" + self._format_style(self._load_style(session_id)) if ok else ""))
            return
        if action == "开启":
            style["enabled"] = True
            self._save_style(session_id, style)
            yield event.plain_result("已开启当前群风格学习。")
            return
        if action == "关闭":
            style["enabled"] = False
            self._save_style(session_id, style)
            yield event.plain_result("已关闭当前群风格学习。")
            return
        if action == "清空":
            self._save_style(session_id, self._default_style(session_id))
            yield event.plain_result("已清空当前群风格。")
            return
        yield event.plain_result("用法：/群风格 查看 | 总结 | 开启 | 关闭 | 清空")
