import copy
import json
import re
import secrets
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Tuple

import aiohttp
import astrbot.api.star as star
import astrbot.api.event.filter as filter
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

CONFIG_PATH = Path("data/config/dora_game_builder.json")

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "gateway_url": "http://dora-game-gateway:8890",
    "gateway_token": "",
    "gateway_token_file": "",
    "glm_provider_id": "glm5_default",
    "ttl_seconds": 1800,
    "max_prompt_chars": 1200,
    "max_code_chars": 18000,
    "admin_only_create": True,
    "allowed_user_ids": [],
    "public_hint": "链接有效期约 30 分钟。",
}

DORA_GAME_SYSTEM = r"""
你是 Dora SSR 小游戏生成器。你要根据用户需求生成一个可运行的 Dora SSR Lua 项目。

只输出严格 JSON，不要 Markdown，不要代码围栏，不要解释。
JSON schema:
{
  "title": "游戏标题，20字内",
  "description": "一句话玩法说明",
  "files": {
    "init.lua": "完整 Dora Lua 代码"
  }
}

Dora Lua 约束：
- init.lua 必须自包含，可以直接运行。
- 使用 `local _ENV = Dora`。
- 使用基础 API：Director、Node、DrawNode、Vec2、Color、Label、Keyboard、Mouse、App。
- 优先用 DrawNode 绘制几何图形，不依赖外部图片/音频。
- 字体可用：Label("sarasa-mono-sc-regular", 28)。
- 使用 `root:schedule(function(dt) ... return false end)` 做主循环。
- 支持键盘方向键/WASD，鼠标点击也可以。
- 默认窗口约 1280x720，坐标中心为 (0,0)。
- 代码要短、稳定、能跑，玩法先做 MVP。

安全限制：
- 禁止 os.execute、io.popen、loadfile、dofile、package.loadlib。
- 禁止网络、HttpClient、Git、读取绝对路径、写宿主文件。
- 不要访问 /home、/root、/etc、/var/run/docker.sock。

参考骨架：
local _ENV = Dora
Director.clearColor = Color(0xff101020)
local root = Node()
Director.entry:addChild(root)
local draw = DrawNode()
root:addChild(draw)
local label = Label("sarasa-mono-sc-regular", 28)
if label then
  label.text = "标题 / 分数"
  label.position = Vec2(0, 320)
  Director.ui:addChild(label)
end
root:schedule(function(dt)
  draw:clear()
  -- update state
  -- draw:drawDot(Vec2(0, 0), 20, Color(0xffffcc00))
  return false
end)
""".strip()

DANGEROUS_PATTERNS = [
    re.compile(p, re.I)
    for p in [
        r"\bos\.execute\b", r"\bio\.popen\b", r"\bloadfile\b", r"\bdofile\b",
        r"\bpackage\.loadlib\b", r"\bHttpClient\b", r"\bGit\b",
        r"require\s*\(?\s*[\"']socket", r"require\s*\(?\s*[\"']http", r"\.\./",
        r"/var/run/docker\.sock", r"/home/", r"/root/", r"/etc/",
    ]
]


def load_cfg() -> Dict[str, Any]:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    try:
        if CONFIG_PATH.exists():
            user_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(user_cfg, dict):
                cfg.update(user_cfg)
    except Exception as exc:
        logger.warning(f"dora_game_builder 配置读取失败: {exc}")
    token_file = str(cfg.get("gateway_token_file") or "").strip()
    if token_file:
        try:
            p = Path(token_file)
            if p.exists():
                cfg["gateway_token"] = p.read_text(encoding="utf-8").strip()
        except Exception as exc:
            logger.warning(f"dora_game_builder token_file 读取失败: {exc}")
    return cfg


def save_cfg(cfg: Dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"\n...[省略 {len(text) - limit} 字]"


def parse_json_object(content: str) -> Dict[str, Any]:
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
    return obj


def strip_create_command(text: str) -> str:
    return re.sub(r"^[/!！\s]*(做游戏|游戏制作|dora游戏|小游戏)\b", "", (text or "").strip(), flags=re.I).strip()


def strip_manage_command(text: str) -> Tuple[str, str]:
    text = re.sub(r"^[/!！\s]*(游戏|dora)\b", "", (text or "").strip(), flags=re.I).strip()
    if not text:
        return "帮助", ""
    parts = text.split(maxsplit=1)
    action = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""
    aliases = {
        "help": "帮助", "?": "帮助", "？": "帮助",
        "status": "状态", "查看": "状态",
        "list": "列表", "ls": "列表",
        "stop": "停止", "delete": "停止", "rm": "停止",
        "logs": "日志", "log": "日志",
    }
    return aliases.get(action, action), rest


def validate_generated(obj: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    files = obj.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("JSON 缺少 files")
    if "init.lua" not in files:
        raise ValueError("当前只支持生成 init.lua")
    clean_files: Dict[str, str] = {}
    total = 0
    for path, content in files.items():
        if not isinstance(path, str) or not isinstance(content, str):
            raise ValueError("files 路径和内容必须是字符串")
        path = path.replace("\\", "/").lstrip("/")
        if ".." in Path(path).parts:
            raise ValueError(f"不安全路径：{path}")
        total += len(content)
        if total > int(cfg.get("max_code_chars", 18000) or 18000):
            raise ValueError("生成代码太长")
        for pattern in DANGEROUS_PATTERNS:
            if pattern.search(content):
                raise ValueError(f"代码包含禁止模式：{pattern.pattern}")
        clean_files[path] = content
    title = str(obj.get("title") or "Dora 小游戏")[:40]
    desc = str(obj.get("description") or "")[:160]
    return {"title": title, "description": desc, "files": clean_files}


def make_game_id(sender_id: str) -> str:
    sid = re.sub(r"\D+", "", str(sender_id or "0"))[-6:] or "user"
    return f"g{sid}{int(time.time()) % 100000:x}{secrets.token_hex(3)}"


@star.register(
    name="dora_game_builder",
    desc="用 GLM 生成 Dora SSR 小游戏，并通过隔离 Docker Runtime + noVNC 返回网页预览。",
    author="Codex",
    version="0.1.0",
)
class Main(star.Star):
    def __init__(self, context: star.Context) -> None:
        self.context = context
        cfg = load_cfg()
        if not CONFIG_PATH.exists():
            save_cfg(cfg)

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            admins = [str(x) for x in self.context.get_config().get("admins_id", [])]
            return str(event.get_sender_id()) in admins
        except Exception:
            return False

    def _is_allowed(self, event: AstrMessageEvent, cfg: Dict[str, Any]) -> bool:
        allowed = [str(x) for x in cfg.get("allowed_user_ids", []) if str(x).strip()]
        if allowed and str(event.get_sender_id()) in allowed:
            return True
        if cfg.get("admin_only_create", True):
            return self._is_admin(event)
        return True

    def _headers(self, cfg: Dict[str, Any]) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        token = str(cfg.get("gateway_token") or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _generate_game(self, event: AstrMessageEvent, prompt: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
        provider_id = str(cfg.get("glm_provider_id") or "glm5_default")
        provider = self.context.get_provider_by_id(provider_id)
        if provider is None:
            raise RuntimeError(f"GLM provider {provider_id} 未加载")
        user_prompt = (
            "用户想要的 Dora SSR 小游戏：\n"
            f"{clip(prompt, int(cfg.get('max_prompt_chars', 1200) or 1200))}\n\n"
            "请生成一个最小可玩版本，必须严格输出 JSON。"
        )
        resp = await provider.text_chat(
            prompt=user_prompt,
            contexts=[],
            system_prompt=DORA_GAME_SYSTEM,
            func_tool=None,
            image_urls=[],
            conversation=None,
            session_id=f"dora_game_builder:{event.unified_msg_origin}",
        )
        obj = parse_json_object(resp.completion_text or "")
        return validate_generated(obj, cfg)

    async def _gateway_request(self, method: str, path: str, cfg: Dict[str, Any], json_body: Dict[str, Any] = None) -> Dict[str, Any]:
        base = str(cfg.get("gateway_url") or "").rstrip("/")
        if not base:
            raise RuntimeError("gateway_url 未配置")
        timeout = aiohttp.ClientTimeout(total=180)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            async with session.request(method, base + path, json=json_body, headers=self._headers(cfg)) as resp:
                text = await resp.text()
                try:
                    data = json.loads(text)
                except Exception:
                    raise RuntimeError(f"gateway HTTP {resp.status}: {text[:500]}")
                if resp.status >= 400 or not data.get("success"):
                    raise RuntimeError(str(data.get("message") or data)[:1000])
                return data

    def _format_help(self) -> str:
        return (
            "Dora 游戏命令：\n"
            "/做游戏 需求  - 生成并启动一个 Dora SSR 小游戏\n"
            "/游戏 状态 id\n"
            "/游戏 日志 id\n"
            "/游戏 停止 id\n"
            "/游戏 列表\n"
            "例：/做游戏 做一个躲避弹幕小游戏，玩家是猫，30秒计分"
        )

    @filter.command("做游戏", alias={"游戏制作", "dora游戏", "小游戏"})
    async def create_game(self, event: AstrMessageEvent):
        event.call_llm = True
        cfg = load_cfg()
        if not cfg.get("enabled", True):
            yield event.plain_result("Dora 游戏生成当前未开启。")
            return
        if not self._is_allowed(event, cfg):
            yield event.plain_result("Dora 游戏生成当前只开放给管理员/白名单。")
            return
        prompt = strip_create_command(event.get_message_str())
        if not prompt or prompt.lower() in {"help", "帮助", "?", "？"}:
            yield event.plain_result(self._format_help())
            return
        game_id = make_game_id(str(event.get_sender_id() or "user"))
        yield event.plain_result(f"收到，正在生成 Dora 小游戏 {game_id}，这玩意儿要启动引擎，稍等一下。")
        try:
            game = await self._generate_game(event, prompt, cfg)
            payload = {
                "game_id": game_id,
                "title": game.get("title") or game_id,
                "files": game["files"],
                "ttl_seconds": int(cfg.get("ttl_seconds", 1800) or 1800),
            }
            data = await self._gateway_request("POST", "/api/games", cfg, payload)
            url = data.get("url") or (data.get("game") or {}).get("url")
            desc = game.get("description") or ""
            hint = str(cfg.get("public_hint") or "")
            yield event.plain_result(
                f"Dora 小游戏已启动：{game.get('title') or game_id}\n"
                f"ID：{game_id}\n"
                f"玩法：{desc}\n"
                f"预览：{url}\n"
                f"{hint}\n"
                "浏览器打开后用键盘/鼠标玩。"
            )
        except Exception as exc:
            logger.error(traceback.format_exc())
            yield event.plain_result(f"Dora 游戏生成失败：{type(exc).__name__}: {exc}")

    @filter.command("游戏", alias={"dora"})
    async def manage_game(self, event: AstrMessageEvent):
        event.call_llm = True
        cfg = load_cfg()
        action, rest = strip_manage_command(event.get_message_str())
        if action in {"帮助"}:
            yield event.plain_result(self._format_help())
            return
        if action == "列表":
            try:
                data = await self._gateway_request("GET", "/api/games", cfg)
                games = data.get("games") or []
                if not games:
                    yield event.plain_result("暂无 Dora 游戏 session。")
                    return
                rows = []
                for g in games[-10:]:
                    rows.append(f"{g.get('id')} | {g.get('title')} | {g.get('status')} | 过期 {g.get('expires_at')}")
                yield event.plain_result("最近 Dora 游戏：\n" + "\n".join(rows))
            except Exception as exc:
                yield event.plain_result(f"查询失败：{exc}")
            return
        if not rest:
            yield event.plain_result("需要游戏 ID。例：/游戏 状态 gxxxx")
            return
        game_id = rest.split()[0]
        try:
            if action == "状态":
                data = await self._gateway_request("GET", f"/api/games/{game_id}", cfg)
                g = data.get("game") or {}
                yield event.plain_result(
                    f"Dora 游戏状态：{g.get('id')}\n"
                    f"标题：{g.get('title')}\n"
                    f"状态：{g.get('status')} / runtime={g.get('runtime_status')}\n"
                    f"预览：{g.get('url')}"
                )
                return
            if action == "日志":
                data = await self._gateway_request("GET", f"/api/games/{game_id}/logs?tail=160", cfg)
                logs = clip(str(data.get("logs") or ""), 3500)
                yield event.plain_result(f"{game_id} 日志：\n{logs or '无日志'}")
                return
            if action == "停止":
                if cfg.get("admin_only_create", True) and not self._is_allowed(event, cfg):
                    yield event.plain_result("只有管理员/白名单可以停止 Dora 游戏。")
                    return
                await self._gateway_request("DELETE", f"/api/games/{game_id}", cfg)
                yield event.plain_result(f"已停止 Dora 游戏：{game_id}")
                return
            yield event.plain_result(self._format_help())
        except Exception as exc:
            yield event.plain_result(f"操作失败：{exc}")
