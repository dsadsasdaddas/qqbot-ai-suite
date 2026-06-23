import copy
import json
import re
import traceback
from pathlib import Path
from typing import Any, Dict

import aiohttp
import astrbot.api.star as star
import astrbot.api.event.filter as filter
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

CONFIG_PATH = Path("data/config/glm_code_space.json")

DEFAULT_CONFIG: Dict[str, Any] = {
    "glm_provider_id": "glm5_default",
    "runner_url": "http://172.23.0.1:8879/run",
    "runner_token": "",
    "gen_timeout_seconds": 60,
    "run_timeout_seconds": 8,
    "max_reply_chars": 5000,
    "show_code": False,
    "allowed_user_ids": [],
}

CODEGEN_SYSTEM = """
你是 GLM 代码执行 worker。你需要为用户任务编写一段可直接运行的 Python 3 代码。
执行环境是一次性沙箱：无网络、无宿主目录、只适合计算、文本处理、模拟、验证思路、小型数据分析。
要求：
- 只输出 JSON，不要 Markdown，不要解释，不要代码围栏。
- JSON schema: {"language":"python","reason":"一句话说明思路","code":"完整 Python 代码"}
- code 必须自包含，结果用 print 输出。
- 不要访问网络、不要读写宿主路径、不要尝试提权、不要死循环。
- 如果任务不适合代码执行，也输出一段安全的 Python 代码说明原因并 print 可行替代方案。
""".strip()

COMMAND_RE = re.compile(r"^[/!！\s]*(?:glmcode|gcode|代码执行|跑代码)(?:\s+|[:：,，]\s*|$)(.*)$", re.I | re.S)
RUNPY_RE = re.compile(r"^[/!！\s]*(?:runpy|跑py)(?:\s+|[:：,，]\s*|$)(.*)$", re.I | re.S)


def load_cfg() -> Dict[str, Any]:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    try:
        if CONFIG_PATH.exists():
            user_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(user_cfg, dict):
                cfg.update(user_cfg)
    except Exception as exc:
        logger.warning(f"glm_code_space 配置读取失败: {exc}")
    return cfg


def save_cfg(cfg: Dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def strip_glmcode_command(text: str) -> str:
    m = COMMAND_RE.match(text or "")
    return (m.group(1) if m else text or "").strip()


def strip_runpy_command(text: str) -> str:
    m = RUNPY_RE.match(text or "")
    return (m.group(1) if m else text or "").strip()


def parse_codegen_json(content: str) -> Dict[str, Any]:
    text = (content or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
    try:
        obj = json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, flags=re.S)
        if not m:
            raise ValueError("GLM 没有返回 JSON")
        obj = json.loads(m.group(0))
    if not isinstance(obj, dict):
        raise ValueError("GLM JSON 不是对象")
    code = obj.get("code")
    if not isinstance(code, str) or not code.strip():
        raise ValueError("GLM JSON 缺少 code")
    obj["language"] = str(obj.get("language") or "python")
    obj["reason"] = str(obj.get("reason") or "")
    obj["code"] = code
    return obj


def clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[省略 {len(text) - limit} 字]"


@star.register(
    name="glm_code_space",
    desc="让 GLM 生成 Python 代码并在隔离 Docker 沙箱中执行。",
    author="Codex",
    version="0.1.0",
)
class Main(star.Star):
    def __init__(self, context: star.Context) -> None:
        self.context = context
        cfg = load_cfg()
        if not CONFIG_PATH.exists():
            save_cfg(cfg)

    def _is_allowed(self, event: AstrMessageEvent, cfg: Dict[str, Any]) -> bool:
        allowed = [str(x) for x in cfg.get("allowed_user_ids", []) if str(x).strip()]
        if not allowed:
            return True
        return str(event.get_sender_id()) in allowed

    @filter.command("glmcode", alias={"gcode", "代码执行", "跑代码"})
    async def glmcode_command(self, event: AstrMessageEvent):
        """让 GLM 写代码并在沙箱执行：/glmcode 计算 1..100 的和"""
        event.call_llm = True
        cfg = load_cfg()
        if not self._is_allowed(event, cfg):
            yield event.plain_result("代码执行空间当前只对白名单用户开放。")
            return
        task = strip_glmcode_command(event.get_message_str())
        if not task:
            yield event.plain_result("用法：/glmcode 你的任务，例如：/glmcode 计算 1 到 100 的和")
            return
        async for result in self._generate_and_run(event, task, cfg):
            yield result

    @filter.command("runpy", alias={"跑py"})
    async def runpy_command(self, event: AstrMessageEvent):
        """直接在沙箱执行 Python：/runpy print(1+1)"""
        event.call_llm = True
        cfg = load_cfg()
        if not self._is_allowed(event, cfg):
            yield event.plain_result("代码执行空间当前只对白名单用户开放。")
            return
        code = strip_runpy_command(event.get_message_str())
        if not code:
            yield event.plain_result("用法：/runpy print(1+1)")
            return
        run_result = await self._run_code(cfg, code)
        yield event.plain_result(self._format_result("直接执行 Python", "", code, run_result, cfg))

    async def _generate_and_run(self, event: AstrMessageEvent, task: str, cfg: Dict[str, Any]):
        provider_id = str(cfg.get("glm_provider_id", "glm5_default"))
        provider = self.context.get_provider_by_id(provider_id)
        if provider is None:
            yield event.plain_result(f"GLM provider {provider_id} 未加载。")
            return
        try:
            logger.info(f"glm_code_space: generating code via {provider_id}, task_chars={len(task)}")
            resp = await provider.text_chat(
                prompt=(
                    "用户任务：\n"
                    f"{task}\n\n"
                    "请输出严格 JSON，包含 language/reason/code。"
                ),
                contexts=[],
                system_prompt=CODEGEN_SYSTEM,
                func_tool=None,
                image_urls=[],
                session_id=event.unified_msg_origin,
            )
            obj = parse_codegen_json(resp.completion_text or "")
            code = obj["code"]
            reason = obj.get("reason", "")
            run_result = await self._run_code(cfg, code)
            yield event.plain_result(self._format_result(task, reason, code, run_result, cfg))
        except Exception as exc:
            logger.error(traceback.format_exc())
            yield event.plain_result(f"GLM 代码执行失败：{type(exc).__name__}: {exc}")

    async def _run_code(self, cfg: Dict[str, Any], code: str) -> Dict[str, Any]:
        url = str(cfg.get("runner_url", "")).strip()
        token = str(cfg.get("runner_token", "")).strip()
        if not url or not token:
            raise RuntimeError("runner_url/runner_token 未配置")
        timeout_seconds = float(cfg.get("run_timeout_seconds", 8))
        payload = {"language": "python", "code": code, "timeout": timeout_seconds}
        timeout = aiohttp.ClientTimeout(total=timeout_seconds + 20)
        headers = {"Authorization": f"Bearer {token}"}
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    raise RuntimeError(f"runner HTTP {resp.status}: {data}")
                return data

    def _format_result(self, task: str, reason: str, code: str, result: Dict[str, Any], cfg: Dict[str, Any]) -> str:
        ok = bool(result.get("ok"))
        timed_out = bool(result.get("timed_out"))
        status = "成功" if ok else ("超时" if timed_out else "失败")
        stdout = result.get("stdout") or ""
        stderr = result.get("stderr") or ""
        rc = result.get("returncode")
        elapsed = result.get("elapsed_ms")
        max_chars = int(cfg.get("max_reply_chars", 5000))
        parts = [
            f"GLM 代码沙箱执行：{status}",
            f"任务：{clip(task, 300)}",
        ]
        if reason:
            parts.append(f"思路：{clip(reason, 500)}")
        parts.append(f"退出码：{rc}，耗时：{elapsed}ms")
        if stdout:
            parts.append("stdout:\n" + clip(stdout, max_chars))
        else:
            parts.append("stdout: （空）")
        if stderr:
            parts.append("stderr:\n" + clip(stderr, max_chars // 2))
        if cfg.get("show_code", False):
            parts.append("code:\n" + clip(code, max_chars))
        return clip("\n\n".join(parts), max_chars + 1500)
