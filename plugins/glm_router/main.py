import copy
import datetime
import json
import re
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import astrbot.api.star as star
import astrbot.api.event.filter as filter
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Image, Reply
from astrbot.core.star.filter.custom_filter import CustomFilter


CONFIG_PATH = Path("data/config/glm_router.json")

DEFAULT_CONFIG: Dict[str, Any] = {
    "glm_provider_id": "glm5_default",
    "router_provider_id": "ollama_gemma4_uncensored",
    "auto_route": True,
    "force_prefixes": ["glm", "GLM", "高级", "深度", "复杂", "大脑"],
    "reply_prefix": "",
    "glm_threshold": 4,
    "router_timeout_seconds": 8,
    "router_max_tokens": 128,
    "router_temperature": 0,
    "router_context_messages": 4,
    "glm_context_messages": 20,
    "save_history": True,
    "pass_images_to_glm": False,
    "advanced_code_execution": True,
    "advanced_code_exec_admin_only": True,
    "advanced_code_exec_allowed_user_ids": [],
    "code_runner_url": "http://glm-code-runner:8879/run",
    "code_runner_token": "",
    "code_exec_timeout_seconds": 8,
    "code_exec_max_reply_chars": 5000,
    "code_exec_show_code": False,
}

COMMAND_PREFIXES = {
    "help", "plugin", "provider", "model", "history", "reset", "new", "ls",
    "switch", "rename", "del", "tool", "t2i", "tts", "sid", "op", "deop",
    "wl", "dwl", "persona", "dashboard_update", "set", "unset", "alter_cmd",
    "redteam", "红队", "glmcode", "gcode", "代码执行", "跑代码", "runpy", "跑py",
    "画图", "生图", "绘图", "image",
}

ROUTER_SYSTEM_PROMPT = """
你是 QQ 机器人的模型路由器，只判断任务应该交给哪个模型，不回答用户问题。

可选 route：
- gemma：普通聊天、短问答、闲聊、角色扮演、简单翻译/改写、简单事实问答、低复杂度任务。
- glm：需要更强智能的任务，例如复杂推理、代码排错/实现/重构、系统架构、部署方案、长文写作或总结、严谨分析、多步骤规划、数学证明、需要高可靠性的技术判断。

请综合判断用户真实意图和难度，不要用关键词机械判断。
输出必须是单行 JSON，不要 Markdown，不要解释，不要多余文字。
JSON schema：{"route":"gemma|glm","difficulty":1-5,"reason":"不超过20字"}
当 difficulty >= 4 时通常选择 glm；否则选择 gemma。
""".strip()

ADVANCED_CODE_EXEC_SYSTEM = """
你是 QQ 机器人 /高级 命令的代码执行决策器。你只决定是否需要调用 Python 沙箱，不要直接执行。

可用工具：一次性 Python 3 Docker 沙箱。
沙箱限制：无网络、无宿主目录、短超时、适合计算/验证/模拟/文本处理/小型数据分析。

决策规则：
- 用户明确说“执行/运行/跑/跑一下/计算/算一下/验证/模拟/求结果/统计/评估表达式/跑代码”时，通常选择 python。
- 用户只是要求“写代码/给代码/解释代码/设计方案/分析问题”，不要执行，选择 answer。
- 任务需要访问网络、外部文件、真实服务器、数据库、系统命令时，不要执行，选择 answer。
- 若选择 python，code 必须是完整自包含 Python 3，所有结果必须 print 输出。
- 不要读写宿主路径，不要访问网络，不要死循环，不要调用 shell。

输出必须是严格 JSON，不要 Markdown，不要代码围栏，不要多余文字。
JSON schema：
{"mode":"answer|python","reason":"一句话原因","code":"当 mode=python 时填写完整 Python；否则为空"}
""".strip()

CODE_EXEC_SUMMARY_SYSTEM = """
你是代码沙箱执行结果解释器。根据用户任务、Python 代码、stdout/stderr，用中文给出简洁结论。
要求：
- 先直接给最终结果或结论。
- 如执行失败，说明错误原因和下一步建议。
- 不要编造 stdout 中没有的结果。
- 默认不要贴完整代码，除非用户要求。
""".strip()

PROMPT_POLICY_REPLY = (
    "提示词变更是受控操作，不在群聊/普通聊天里直接改。\n"
    "需要服务器所有者在 Codex/SSH 维护流程中明确授权后，才会修改 Gemma/GLM/红队/路由提示词配置。"
)


def is_prompt_change_request(text: str) -> bool:
    text = (text or "").strip()
    if not text:
        return False
    compact = re.sub(r"\s+", " ", text)
    prompt_nouns = (
        "提示词", "系统提示", "系统 prompt", "system prompt", "prompt_prefix", "prompt prefix",
        "全局提示", "全局规则", "系统规则", "persona", "人格", "人设", "角色设定",
    )
    change_verbs = (
        "修改", "改成", "改为", "改掉", "更新", "重写", "覆盖", "替换", "设置", "设成",
        "加入", "添加", "删除", "清空", "调整", "优化", "换成", "注入", "写入",
        "modify", "change", "update", "rewrite", "replace", "set", "delete", "clear", "inject",
    )
    bot_targets = (
        "你", "你的", "机器人", "bot", "gemma", "glm", "全局", "系统", "当前", "默认",
        "自身", "这个群", "本群", "astrbot",
    )
    direct_patterns = (
        r"(修改|更新|重写|覆盖|替换|设置|删除|清空).{0,8}(提示词|系统提示|全局提示|系统规则|人格|人设|persona)",
        r"(提示词|系统提示|全局提示|系统规则|人格|人设|persona).{0,8}(改成|改为|换成|设成|覆盖|删除|清空)",
        r"(modify|change|update|rewrite|replace|set|delete|clear).{0,12}(system prompt|prompt|persona)",
    )
    for pattern in direct_patterns:
        if re.search(pattern, compact, flags=re.I):
            return True
    low = compact.lower()
    return (
        any(w.lower() in low for w in prompt_nouns)
        and any(w.lower() in low for w in change_verbs)
        and any(w.lower() in low for w in bot_targets)
    )



def clip_text(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[省略 {len(text) - limit} 字]"


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


def load_cfg() -> Dict[str, Any]:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    try:
        if CONFIG_PATH.exists():
            user_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(user_cfg, dict):
                cfg.update(user_cfg)
    except Exception as exc:
        logger.warning(f"glm_router 配置读取失败，使用默认配置: {exc}")
    return cfg


def is_force_prefix(text: str, cfg: Dict[str, Any]) -> bool:
    t = (text or "").strip()
    for p in cfg.get("force_prefixes", []):
        p = str(p).strip()
        if p and (t == p or t.startswith(p + " ") or t.startswith(p + ":") or t.startswith(p + "：") or t.startswith(p + "，") or t.startswith(p + ",")):
            return True
    return False


def strip_force_prefix(text: str, cfg: Dict[str, Any]) -> str:
    t = (text or "").strip()
    prefixes = sorted([str(x) for x in cfg.get("force_prefixes", []) if str(x).strip()], key=len, reverse=True)
    for p in prefixes:
        if t == p:
            return ""
        m = re.match(rf"^{re.escape(p)}(?:\s+|[:：，,]\s*)(.*)$", t, flags=re.S)
        if m:
            return m.group(1).strip()
    return t


class RouteCandidateFilter(CustomFilter):
    """接管已唤醒的普通消息，让 Gemma 先做一次路由判断。"""

    def filter(self, event: AstrMessageEvent, cfg) -> bool:  # noqa: ANN001
        router_cfg = load_cfg()
        if not router_cfg.get("auto_route", True):
            return False
        if not getattr(event, "is_at_or_wake_command", False):
            return False
        text = (event.get_message_str() or "").strip()
        if not text:
            return False
        if is_redteam_mode_enabled(event.unified_msg_origin):
            return False
        if is_force_prefix(text, router_cfg):
            return False
        first = re.split(r"\s+", text, maxsplit=1)[0].strip().lstrip("/!！")
        if first in COMMAND_PREFIXES:
            return False
        return True


def is_redteam_mode_enabled(session_id: str) -> bool:
    """红队模式开启时，普通消息交给 redteam_mode，不让普通 GLM 路由器抢答。"""
    try:
        path = Path("data/config/redteam_mode.json")
        if not path.exists():
            return False
        state = json.loads(path.read_text(encoding="utf-8"))
        return bool((state.get("enabled_sessions") or {}).get(session_id, False))
    except Exception:
        return False


@star.register(
    name="glm_router",
    desc="让 Gemma 判断任务难度，必要时自动交给 GLM；普通聊天仍走 Gemma。",
    author="Codex",
    version="0.2.2",
)
class Main(star.Star):
    """Gemma -> GLM 智能路由器

    用法：
    - /glm 你的问题：强制使用 GLM。
    - /高级 你的问题、/深度 你的问题：强制使用 GLM。
    - 普通唤醒消息会先由 Gemma 判断难度，复杂任务自动交给 GLM。
    """

    def __init__(self, context: star.Context) -> None:
        self.context = context
        self.cfg = self._ensure_config()

    def _ensure_config(self) -> Dict[str, Any]:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        cfg = load_cfg()
        if not CONFIG_PATH.exists():
            CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        return cfg

    @filter.command("glm", alias={"GLM", "高级", "深度", "复杂", "大脑"})
    async def glm_command(self, event: AstrMessageEvent):
        """强制使用 GLM 回答。"""
        self.cfg = load_cfg()
        prompt = strip_force_prefix(event.get_message_str(), self.cfg)
        if not prompt:
            yield event.plain_result("用法：/glm 你的问题（或 /高级 你的问题）")
            return
        if is_prompt_change_request(prompt):
            event.call_llm = True
            yield event.plain_result(PROMPT_POLICY_REPLY)
            return
        async for result in self._answer_with_glm(event, prompt, manual=True):
            yield result

    @filter.custom_filter(RouteCandidateFilter)
    async def auto_route(self, event: AstrMessageEvent):
        """先让 Gemma 做意图/难度识别，再决定是否升级到 GLM。"""
        self.cfg = load_cfg()
        prompt = (event.get_message_str() or "").strip()
        if not prompt:
            return
        if is_prompt_change_request(prompt):
            event.call_llm = True
            yield event.plain_result(PROMPT_POLICY_REPLY)
            return

        decision = await self._judge_route_with_gemma(event, prompt)
        route = decision.get("route", "gemma")
        difficulty = int(decision.get("difficulty", 1) or 1)
        reason = str(decision.get("reason", ""))[:50]
        logger.info(f"glm_router decision: route={route}, difficulty={difficulty}, reason={reason}")

        if route != "glm":
            return

        async for result in self._answer_with_glm(event, prompt, manual=False):
            yield result

    async def _judge_route_with_gemma(self, event: AstrMessageEvent, prompt: str) -> Dict[str, Any]:
        try:
            provider_id = str(self.cfg.get("router_provider_id") or "").strip()
            provider = self.context.get_provider_by_id(provider_id) if provider_id else self.context.get_using_provider()
            if provider is None:
                return {"route": "gemma", "difficulty": 1, "reason": "无路由模型"}

            api_base = str(provider.provider_config.get("api_base", "")).rstrip("/")
            model_cfg = provider.provider_config.get("model_config", {}) or {}
            model = model_cfg.get("model") or provider.get_model()
            keys = provider.provider_config.get("key", ["ollama"])
            key = keys[0] if keys else "ollama"
            url = api_base + "/chat/completions"
            history = await self._recent_history_for_router(event)
            user_payload = {
                "message": prompt,
                "has_image": any(isinstance(comp, Image) for comp in event.get_messages()),
                "recent_context": history,
            }
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                ],
                "temperature": float(self.cfg.get("router_temperature", 0)),
                "max_tokens": int(self.cfg.get("router_max_tokens", 128)),
                "reasoning_effort": "none",
            }
            timeout = aiohttp.ClientTimeout(total=float(self.cfg.get("router_timeout_seconds", 8)))
            async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
                async with session.post(url, json=payload, headers={"Authorization": f"Bearer {key}"}) as resp:
                    raw = await resp.text()
                    if resp.status >= 400:
                        logger.warning(f"glm_router Gemma 判断失败 HTTP {resp.status}: {raw[:300]}")
                        return {"route": "gemma", "difficulty": 1, "reason": "路由失败"}
                    data = json.loads(raw)
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return self._parse_decision(content)
        except Exception as exc:
            logger.warning(f"glm_router Gemma 判断异常，默认走 Gemma: {type(exc).__name__}: {exc}")
            return {"route": "gemma", "difficulty": 1, "reason": "路由异常"}

    def _parse_decision(self, content: str) -> Dict[str, Any]:
        text = (content or "").strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
        m = re.search(r"\{.*\}", text, flags=re.S)
        if m:
            text = m.group(0)
        try:
            obj = json.loads(text)
        except Exception:
            logger.warning(f"glm_router JSON 解析失败，默认走 Gemma: {content[:200]}")
            return {"route": "gemma", "difficulty": 1, "reason": "JSON失败"}
        difficulty = int(obj.get("difficulty", 1) or 1)
        difficulty = max(1, min(5, difficulty))
        route = str(obj.get("route", "")).lower().strip()
        threshold = int(self.cfg.get("glm_threshold", 4))
        if route not in {"gemma", "glm"}:
            route = "glm" if difficulty >= threshold else "gemma"
        if difficulty >= threshold and route != "gemma":
            route = "glm"
        elif route == "glm" and difficulty < threshold:
            # 允许模型强判 glm，但不要让低难度误升太多。
            route = "gemma"
        return {"route": route, "difficulty": difficulty, "reason": str(obj.get("reason", ""))}

    async def _answer_with_glm(self, event: AstrMessageEvent, prompt: str, manual: bool):
        provider_id = str(self.cfg.get("glm_provider_id", "glm5_default"))
        provider = self.context.get_provider_by_id(provider_id)
        if provider is None:
            if manual:
                yield event.plain_result(f"GLM 提供商 {provider_id} 未启用或未加载。")
            return

        try:
            conversation, full_history = await self._load_conversation(event)
            contexts = self._trim_contexts(full_history, int(self.cfg.get("glm_context_messages", 20)))
            contexts = self._sanitize_contexts(contexts)
            prompt_for_llm, system_prompt, contexts = self._decorate_like_astrbot(event, prompt, conversation, contexts)
            image_urls = self._collect_image_urls(event) if self.cfg.get("pass_images_to_glm", False) else []

            if manual and self.cfg.get("advanced_code_execution", True):
                code_agent_text = await self._try_code_agent(
                    event=event,
                    provider=provider,
                    prompt=prompt,
                    contexts=contexts,
                    system_prompt=system_prompt,
                    conversation=conversation,
                )
                if code_agent_text is not None:
                    if self.cfg.get("save_history", True):
                        await self._save_history(event, conversation, full_history, prompt, code_agent_text)
                    event.call_llm = True
                    prefix = str(self.cfg.get("reply_prefix", ""))
                    yield event.plain_result(prefix + code_agent_text)
                    return

            logger.info(f"glm_router -> {provider_id}, manual={manual}, prompt_chars={len(prompt)}, context_messages={len(contexts)}")
            resp = await provider.text_chat(
                prompt=prompt_for_llm,
                session_id=event.unified_msg_origin,
                image_urls=image_urls,
                func_tool=None,
                contexts=contexts,
                system_prompt=system_prompt,
                conversation=conversation,
            )
            if resp.role != "assistant":
                raise RuntimeError(resp.completion_text or f"GLM 返回 role={resp.role}")
            text = (resp.completion_text or "").strip()
            if not text and getattr(resp, "result_chain", None):
                text = str(resp.result_chain)
            if not text:
                text = "GLM 返回为空。"
            if self.cfg.get("save_history", True):
                await self._save_history(event, conversation, full_history, prompt, text)
            event.call_llm = True
            prefix = str(self.cfg.get("reply_prefix", ""))
            yield event.plain_result(prefix + text)
        except Exception as exc:
            logger.error(traceback.format_exc())
            if manual:
                event.call_llm = True
                yield event.plain_result(f"GLM 路由失败：{type(exc).__name__}: {exc}")
            # 自动路由失败时不拦截，回落到默认 Gemma。
            return

    def _code_exec_allowed(self, event: AstrMessageEvent) -> bool:
        if not self.cfg.get("advanced_code_exec_admin_only", True):
            return True
        allowed = [str(x) for x in self.cfg.get("advanced_code_exec_allowed_user_ids", []) if str(x).strip()]
        if not allowed:
            try:
                admins = self.context.get_config().get("admins_id", [])
                allowed = [str(x) for x in admins if str(x).strip()]
            except Exception:
                allowed = []
        return str(event.get_sender_id()) in allowed

    def _looks_like_code_exec_task(self, prompt: str) -> bool:
        text = (prompt or "").lower()
        if not text.strip():
            return False
        triggers = (
            "执行", "运行", "跑一下", "跑下", "跑代码", "跑py", "跑 python", "跑python", "run ", "execute",
            "计算", "算一下", "算下", "求值", "求结果", "验证", "模拟", "仿真", "统计", "抽样",
            "evaluate", "calculate", "compute", "simulate", "verify",
        )
        if any(t in text for t in triggers):
            return True
        # 简单数学表达式也允许自动执行，例如：/高级 2**100 是多少
        if re.search(r"\d\s*([+\-*/%]|\*\*|//)\s*\d", text) and any(w in text for w in ("多少", "等于", "结果", "=?", "？", "?")):
            return True
        return False

    def _load_code_runner_cfg(self) -> Dict[str, Any]:
        cfg = {
            "runner_url": str(self.cfg.get("code_runner_url") or "http://glm-code-runner:8879/run"),
            "runner_token": str(self.cfg.get("code_runner_token") or ""),
            "timeout": float(self.cfg.get("code_exec_timeout_seconds", 8) or 8),
            "max_reply_chars": int(self.cfg.get("code_exec_max_reply_chars", 5000) or 5000),
            "show_code": bool(self.cfg.get("code_exec_show_code", False)),
        }
        try:
            path = Path("data/config/glm_code_space.json")
            if path.exists():
                other = json.loads(path.read_text(encoding="utf-8"))
                if not cfg["runner_token"]:
                    cfg["runner_token"] = str(other.get("runner_token") or "")
                if not self.cfg.get("code_runner_url"):
                    cfg["runner_url"] = str(other.get("runner_url") or cfg["runner_url"])
                if not self.cfg.get("code_exec_timeout_seconds"):
                    cfg["timeout"] = float(other.get("run_timeout_seconds", cfg["timeout"]) or cfg["timeout"])
        except Exception as exc:
            logger.warning(f"glm_router 读取代码执行配置失败: {exc}")
        return cfg

    async def _try_code_agent(
        self,
        event: AstrMessageEvent,
        provider: Any,
        prompt: str,
        contexts: List[Dict[str, Any]],
        system_prompt: str,
        conversation: Any,
    ) -> Optional[str]:
        if not self._code_exec_allowed(event):
            return None
        if not self._looks_like_code_exec_task(prompt):
            return None
        runner_cfg = self._load_code_runner_cfg()
        if not runner_cfg.get("runner_url") or not runner_cfg.get("runner_token"):
            logger.warning("glm_router code agent skipped: runner_url/runner_token missing")
            return None
        try:
            recent = self._sanitize_contexts(contexts[-6:])
            planner_payload = {
                "user_task": prompt,
                "recent_context": recent,
            }
            logger.info(f"glm_router code-agent planning, prompt_chars={len(prompt)}")
            resp = await provider.text_chat(
                prompt=json.dumps(planner_payload, ensure_ascii=False),
                session_id=event.unified_msg_origin,
                image_urls=[],
                func_tool=None,
                contexts=[],
                system_prompt=ADVANCED_CODE_EXEC_SYSTEM,
                conversation=None,
            )
            obj = parse_json_object(resp.completion_text or "")
            mode = str(obj.get("mode") or "answer").lower().strip()
            if mode != "python":
                return None
            code = str(obj.get("code") or "").strip()
            if not code:
                return None
            reason = str(obj.get("reason") or "需要执行代码")[:300]
            run_result = await self._run_python_in_sandbox(runner_cfg, code)
            summary = await self._summarize_code_result(
                provider=provider,
                event=event,
                task=prompt,
                reason=reason,
                code=code,
                run_result=run_result,
                runner_cfg=runner_cfg,
                system_prompt=system_prompt,
                conversation=conversation,
            )
            return summary
        except Exception as exc:
            logger.warning(f"glm_router code-agent failed, fallback to normal GLM: {type(exc).__name__}: {exc}")
            return None

    async def _run_python_in_sandbox(self, runner_cfg: Dict[str, Any], code: str) -> Dict[str, Any]:
        timeout_seconds = float(runner_cfg.get("timeout", 8) or 8)
        payload = {"language": "python", "code": code, "timeout": timeout_seconds}
        timeout = aiohttp.ClientTimeout(total=timeout_seconds + 20)
        headers = {"Authorization": f"Bearer {runner_cfg.get('runner_token', '')}"}
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            async with session.post(str(runner_cfg.get("runner_url")), json=payload, headers=headers) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    raise RuntimeError(f"runner HTTP {resp.status}: {data}")
                return data

    async def _summarize_code_result(
        self,
        provider: Any,
        event: AstrMessageEvent,
        task: str,
        reason: str,
        code: str,
        run_result: Dict[str, Any],
        runner_cfg: Dict[str, Any],
        system_prompt: str,
        conversation: Any,
    ) -> str:
        max_chars = int(runner_cfg.get("max_reply_chars", 5000) or 5000)
        payload = {
            "用户任务": task,
            "执行原因": reason,
            "代码": code,
            "执行结果": run_result,
        }
        try:
            resp = await provider.text_chat(
                prompt=json.dumps(payload, ensure_ascii=False),
                session_id=event.unified_msg_origin,
                image_urls=[],
                func_tool=None,
                contexts=[],
                system_prompt=(system_prompt or "") + "\n" + CODE_EXEC_SUMMARY_SYSTEM,
                conversation=conversation,
            )
            text = (resp.completion_text or "").strip()
            if text:
                if runner_cfg.get("show_code", False):
                    text += "\n\n执行代码：\n" + clip_text(code, max_chars // 2)
                return clip_text(text, max_chars)
        except Exception as exc:
            logger.warning(f"glm_router code summary failed: {type(exc).__name__}: {exc}")

        status = "成功" if run_result.get("ok") else ("超时" if run_result.get("timed_out") else "失败")
        parts = [
            f"代码沙箱执行：{status}",
            f"原因：{reason}",
            f"退出码：{run_result.get('returncode')}，耗时：{run_result.get('elapsed_ms')}ms",
            "stdout:\n" + (run_result.get("stdout") or "（空）"),
        ]
        if run_result.get("stderr"):
            parts.append("stderr:\n" + str(run_result.get("stderr")))
        if runner_cfg.get("show_code", False):
            parts.append("code:\n" + code)
        return clip_text("\n\n".join(parts), max_chars)

    async def _recent_history_for_router(self, event: AstrMessageEvent) -> List[Dict[str, str]]:
        try:
            conversation, history = await self._load_conversation(event)
            limit = int(self.cfg.get("router_context_messages", 4))
            return self._sanitize_contexts(history[-limit:]) if limit > 0 else []
        except Exception:
            return []

    async def _load_conversation(self, event: AstrMessageEvent) -> Tuple[Any, List[Dict[str, Any]]]:
        cm = self.context.conversation_manager
        cid = await cm.get_curr_conversation_id(event.unified_msg_origin)
        if not cid:
            cid = await cm.new_conversation(event.unified_msg_origin)
        conversation = await cm.get_conversation(event.unified_msg_origin, cid)
        try:
            history = json.loads(conversation.history or "[]")
            if not isinstance(history, list):
                history = []
        except Exception:
            history = []
        return conversation, history

    def _trim_contexts(self, contexts: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
        if limit <= 0:
            return []
        return copy.deepcopy(contexts[-limit:])

    def _sanitize_contexts(self, contexts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        cleaned: List[Dict[str, Any]] = []
        for item in contexts:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            if role not in {"system", "user", "assistant", "tool"}:
                continue
            rec = {"role": role, "content": item.get("content", "")}
            content = rec["content"]
            if isinstance(content, list):
                parts: List[str] = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        parts.append(str(part.get("text", "")))
                rec["content"] = "\n".join([p for p in parts if p]).strip() or "[图片]"
            elif not isinstance(content, str):
                rec["content"] = str(content)
            cleaned.append(rec)
        return cleaned

    def _decorate_like_astrbot(
        self,
        event: AstrMessageEvent,
        prompt: str,
        conversation: Any,
        contexts: List[Dict[str, Any]],
    ) -> Tuple[str, str, List[Dict[str, Any]]]:
        cfg = self.context.get_config()
        provider_settings = cfg.get("provider_settings", {})
        system_prompt = ""
        prompt_for_llm = prompt

        if provider_settings.get("prompt_prefix"):
            prompt_for_llm = str(provider_settings.get("prompt_prefix")) + prompt_for_llm

        if provider_settings.get("identifier"):
            sender = getattr(event.message_obj, "sender", None)
            user_id = getattr(sender, "user_id", event.get_sender_id())
            nickname = getattr(sender, "nickname", event.get_sender_name())
            prompt_for_llm = f"\n[User ID: {user_id}, Nickname: {nickname}]\n" + prompt_for_llm

        if provider_settings.get("datetime_system_prompt"):
            current_time = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M (%Z)")
            system_prompt += f"\nCurrent datetime: {current_time}\n"

        try:
            persona_id = getattr(conversation, "persona_id", "") if conversation else ""
            if (not persona_id) and persona_id != "[%None]":
                persona_id = self.context.provider_manager.selected_default_persona.get("name")
            if persona_id and persona_id != "[%None]":
                persona = next((p for p in self.context.provider_manager.personas if p.get("name") == persona_id), None)
                if persona:
                    if persona.get("prompt"):
                        system_prompt += str(persona.get("prompt"))
                    if persona.get("_mood_imitation_dialogs_processed"):
                        system_prompt += "\nHere are few shots of dialogs, you need to imitate the tone of B in the following dialogs to respond:\n"
                        system_prompt += str(persona.get("_mood_imitation_dialogs_processed"))
                    begin_dialogs = persona.get("_begin_dialogs_processed") or []
                    if begin_dialogs:
                        contexts = copy.deepcopy(begin_dialogs) + contexts
        except Exception as exc:
            logger.warning(f"glm_router 注入 persona 失败: {exc}")

        try:
            for comp in event.message_obj.message:
                if isinstance(comp, Reply) and getattr(comp, "message_str", ""):
                    sender_info = f"(Sent by {comp.sender_nickname})" if getattr(comp, "sender_nickname", "") else ""
                    system_prompt += f"\nUser is quoting the message{sender_info}: {comp.message_str}, please consider the context."
                    break
        except Exception:
            pass
        return prompt_for_llm, system_prompt, contexts

    def _collect_image_urls(self, event: AstrMessageEvent) -> List[str]:
        urls: List[str] = []
        for comp in event.get_messages():
            if isinstance(comp, Image):
                value = getattr(comp, "url", "") or getattr(comp, "file", "")
                if value:
                    urls.append(value)
        return urls

    async def _save_history(self, event: AstrMessageEvent, conversation: Any, full_history: List[Dict[str, Any]], prompt: str, response_text: str) -> None:
        if not conversation:
            return
        history = copy.deepcopy(full_history)
        history.append({"role": "user", "content": prompt})
        history.append({"role": "assistant", "content": response_text})
        history = [x for x in history if isinstance(x, dict) and "_no_save" not in x]
        await self.context.conversation_manager.update_conversation(event.unified_msg_origin, conversation.cid, history=history)
