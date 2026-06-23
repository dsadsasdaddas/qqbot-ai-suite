import re
from typing import Iterable

import astrbot.api.star as star
import astrbot.api.event.filter as filter
from astrbot.api.event import AstrMessageEvent
from astrbot.core.star.filter.custom_filter import CustomFilter

POLICY_REPLY = (
    "提示词变更是受控操作，不在群聊/普通聊天里直接改。\n"
    "需要服务器所有者在 Codex/SSH 维护流程中明确授权后，才会修改 Gemma/GLM/红队/路由提示词配置。"
)

PROMPT_NOUNS = (
    "提示词", "系统提示", "系统 prompt", "system prompt", "prompt_prefix", "prompt prefix",
    "全局提示", "全局规则", "系统规则", "persona", "人格", "人设", "角色设定",
)

CHANGE_VERBS = (
    "修改", "改成", "改为", "改掉", "更新", "重写", "覆盖", "替换", "设置", "设成",
    "加入", "添加", "删除", "清空", "调整", "优化", "换成", "注入", "写入",
    "modify", "change", "update", "rewrite", "replace", "set", "delete", "clear", "inject",
)

BOT_TARGETS = (
    "你", "你的", "机器人", "bot", "Bot", "BOT", "Gemma", "gemma", "GLM", "glm",
    "全局", "系统", "当前", "默认", "自身", "这个群", "本群", "astrbot", "AstrBot",
)

DIRECT_PATTERNS = (
    r"(修改|更新|重写|覆盖|替换|设置|删除|清空).{0,8}(提示词|系统提示|全局提示|系统规则|人格|人设|persona)",
    r"(提示词|系统提示|全局提示|系统规则|人格|人设|persona).{0,8}(改成|改为|换成|设成|覆盖|删除|清空)",
    r"(modify|change|update|rewrite|replace|set|delete|clear).{0,12}(system prompt|prompt|persona)",
)

COMMAND_PREFIXES = {
    "help", "plugin", "provider", "model", "history", "reset", "new", "ls", "switch", "rename", "del",
    "tool", "t2i", "tts", "sid", "redteam", "红队", "glm", "GLM", "高级", "深度", "复杂", "大脑",
    "glmcode", "gcode", "代码执行", "跑代码", "runpy", "跑py",
}


def _contains_any(text: str, words: Iterable[str]) -> bool:
    low = text.lower()
    return any(w.lower() in low for w in words)


def is_prompt_change_request(text: str) -> bool:
    text = (text or "").strip()
    if not text:
        return False
    compact = re.sub(r"\s+", " ", text)
    for pattern in DIRECT_PATTERNS:
        if re.search(pattern, compact, flags=re.I):
            return True
    has_prompt_noun = _contains_any(compact, PROMPT_NOUNS)
    has_change_verb = _contains_any(compact, CHANGE_VERBS)
    targets_bot = _contains_any(compact, BOT_TARGETS)
    return bool(has_prompt_noun and has_change_verb and targets_bot)


class PromptChangeFilter(CustomFilter):
    def filter(self, event: AstrMessageEvent, cfg) -> bool:  # noqa: ANN001
        if not getattr(event, "is_at_or_wake_command", False):
            return False
        text = (event.get_message_str() or "").strip()
        if not text:
            return False
        first = re.split(r"\s+", text, maxsplit=1)[0].strip().lstrip("/!！")
        # 命令本身交给各命令插件；/glm 等已在 glm_router 内也加了策略拦截。
        if first in COMMAND_PREFIXES:
            return False
        return is_prompt_change_request(text)


@star.register(
    name="prompt_change_guard",
    desc="提示词变更治理：没有服务器所有者明确授权，不允许通过聊天修改提示词配置。",
    author="Codex",
    version="0.1.0",
)
class Main(star.Star):
    def __init__(self, context: star.Context) -> None:
        self.context = context

    @filter.custom_filter(PromptChangeFilter)
    async def block_prompt_change_request(self, event: AstrMessageEvent):
        event.call_llm = True
        await event.send(event.plain_result(POLICY_REPLY))
        event.stop_event()
