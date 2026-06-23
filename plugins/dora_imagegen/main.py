import asyncio
import base64
import json
import re
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import aiohttp
import astrbot.api.star as star
import astrbot.api.event.filter as filter
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Image


DEFAULT_TIERS: Dict[str, Dict[str, Any]] = {
    "1": {
        "name": "快速",
        "model": "gemini-2.5-flash-image",
        "cooldown_seconds": 15,
        "timeout_seconds": 120,
        "description": "最快，适合表情包、头像、简单图标、随手草图。",
        "style_prompt": "快速成图优先，画面主体明确，构图简洁，细节够用，不要过度复杂。",
    },
    "2": {
        "name": "标准",
        "model": "gemini-2.5-flash-image",
        "cooldown_seconds": 30,
        "timeout_seconds": 180,
        "description": "默认档，速度和质量均衡，适合大多数生图。",
        "style_prompt": "质量和速度均衡，画面完整，主体清楚，光影自然，细节丰富但不过度堆叠。",
    },
    "3": {
        "name": "精细",
        "model": "gemini-3.1-flash-image",
        "cooldown_seconds": 75,
        "timeout_seconds": 260,
        "description": "更慢但更细，适合复杂场景、海报、角色、参考图重绘。",
        "style_prompt": "高质量精细成图，复杂构图也要稳定，细节丰富，材质、光影、氛围、背景层次完整。",
    },
}


@star.register(
    name="dora_imagegen",
    desc="使用 Google Vertex Gemini 图片模型为 QQ Bot 提供 /生图 1/2/3 档生图命令",
    author="Codex",
    version="0.2.0",
)
class Main(star.Star):
    def __init__(self, context: star.Context) -> None:
        self.context = context
        self.config_path = Path("data/config/dora_imagegen.json")
        self.config = self._load_config()
        self.cooldowns: Dict[str, float] = {}
        self.max_concurrent = max(1, int(self.config.get("max_concurrent", 1) or 1))
        self.semaphore = asyncio.Semaphore(self.max_concurrent)
        self.queue_lock = asyncio.Lock()
        self.inflight = 0
        self.waiting = 0

    def _load_config(self) -> Dict[str, Any]:
        default: Dict[str, Any] = {
            "endpoint": "http://172.23.0.1:8877/api/google-vertex/generate-frame",
            "api_token": "",
            "timeout_seconds": 180,
            "cooldown_seconds": 30,
            "max_concurrent": 1,
            "default_tier": "2",
            "output_dir": "/AstrBot/data/temp/dora_imagegen",
            "tiers": DEFAULT_TIERS,
        }
        if self.config_path.exists():
            try:
                user_cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
                if isinstance(user_cfg, dict):
                    user_tiers = user_cfg.get("tiers") if isinstance(user_cfg.get("tiers"), dict) else {}
                    default.update(user_cfg)
                    tiers = {k: dict(v) for k, v in DEFAULT_TIERS.items()}
                    for tier_id, tier_cfg in user_tiers.items():
                        if isinstance(tier_cfg, dict):
                            base = tiers.get(str(tier_id), {})
                            merged = dict(base)
                            merged.update(tier_cfg)
                            tiers[str(tier_id)] = merged
                    default["tiers"] = tiers
            except Exception as exc:
                logger.warning(f"dora_imagegen 配置读取失败，使用默认配置: {exc}")
        return default

    def _parse_command_and_tier(self, event: AstrMessageEvent) -> Tuple[str, str]:
        text = event.get_message_str().strip()
        text = re.sub(r"^[/!！\s]*(画图|生图|绘图|image)\b", "", text, flags=re.I).strip()
        default_tier = str(self.config.get("default_tier") or "2")
        tier_id = default_tier if default_tier in self._tiers() else "2"

        # 支持：/生图 1 一只猫、/生图 1档 一只猫、/生图 3：电影海报
        m = re.match(r"^([123])(?:\s+|[档挡:：，,、.\-]+|$)(.*)$", text, flags=re.S)
        if m:
            tier_id = m.group(1)
            text = (m.group(2) or "").strip()
        return tier_id, text

    def _tiers(self) -> Dict[str, Dict[str, Any]]:
        tiers = self.config.get("tiers")
        return tiers if isinstance(tiers, dict) else DEFAULT_TIERS

    def _get_tier(self, tier_id: str) -> Dict[str, Any]:
        tier = self._tiers().get(str(tier_id)) or self._tiers().get("2") or DEFAULT_TIERS["2"]
        merged = dict(DEFAULT_TIERS.get(str(tier_id), {}))
        merged.update(tier)
        return merged

    def _format_tiers_help(self) -> str:
        lines = [
            "用法：/生图 [1|2|3] 描述",
            "例：/生图 1 熊猫震惊表情包",
            "例：/生图 2 一只穿宇航服的橘猫，电影感",
            "例：/生图 3 赛博朋克城市夜景，高细节海报",
            "",
            "档位：",
        ]
        for tier_id in ("1", "2", "3"):
            tier = self._get_tier(tier_id)
            lines.append(
                f"{tier_id}档 {tier.get('name', '')}：{tier.get('description', '')}"
                f" 冷却 {int(float(tier.get('cooldown_seconds', 0) or 0))}s"
            )
        lines.append("不写档位默认 2 档。")
        return "\n".join(lines)

    def _build_image_prompt(self, prompt: str, tier_id: str, refs: List[str]) -> str:
        tier = self._get_tier(tier_id)
        ref_rule = "如提供参考图，优先保持参考图的主体身份、构图或风格要求。" if refs else ""
        return (
            "请直接生成一张图片，必须返回 IMAGE 模态，不要只回复文字；"
            "画面中不要出现解释文字、水印、UI、边框、无关标签。"
            f"当前为 {tier_id}档「{tier.get('name', '')}」。"
            f"档位要求：{tier.get('style_prompt', '')}"
            f"{ref_rule}"
            "用户需求：" + prompt
        )

    def _check_cooldown(self, event: AstrMessageEvent, tier_id: str) -> float:
        key = event.get_sender_id() or event.unified_msg_origin
        tier = self._get_tier(tier_id)
        cooldown = float(tier.get("cooldown_seconds", self.config.get("cooldown_seconds", 30)) or 0)
        now = time.monotonic()
        last = self.cooldowns.get(key, 0.0)
        remaining = cooldown - (now - last)
        if remaining > 0:
            return remaining
        self.cooldowns[key] = now
        return 0.0

    async def _collect_reference_images(self, event: AstrMessageEvent) -> List[str]:
        refs: List[str] = []
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            for comp in event.get_messages():
                if not isinstance(comp, Image):
                    continue
                file_value = getattr(comp, "file", "") or ""
                url_value = getattr(comp, "url", "") or ""
                if file_value.startswith("base64://"):
                    refs.append("data:image/png;base64," + file_value[len("base64://") :])
                elif file_value.startswith("http://") or file_value.startswith("https://"):
                    async with session.get(file_value) as resp:
                        resp.raise_for_status()
                        mime = resp.headers.get("content-type", "image/png").split(";", 1)[0]
                        refs.append(f"data:{mime};base64," + base64.b64encode(await resp.read()).decode("ascii"))
                elif url_value.startswith("http://") or url_value.startswith("https://"):
                    async with session.get(url_value) as resp:
                        resp.raise_for_status()
                        mime = resp.headers.get("content-type", "image/png").split(";", 1)[0]
                        refs.append(f"data:{mime};base64," + base64.b64encode(await resp.read()).decode("ascii"))
        return refs[:3]

    async def _call_proxy(self, prompt: str, refs: List[str], tier_id: str) -> Tuple[str, str, str]:
        endpoint = self.config["endpoint"]
        tier = self._get_tier(tier_id)
        headers = {"Content-Type": "application/json"}
        token = self.config.get("api_token")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload: Dict[str, Any] = {
            "prompt": self._build_image_prompt(prompt, tier_id, refs),
            "model": tier.get("model") or "gemini-2.5-flash-image",
        }
        if refs:
            payload["referenceImages"] = refs

        timeout_seconds = float(tier.get("timeout_seconds", self.config.get("timeout_seconds", 180)) or 180)
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            async with session.post(endpoint, headers=headers, json=payload) as resp:
                text = await resp.text()
                try:
                    data = json.loads(text)
                except Exception:
                    data = {"success": False, "message": text[:500]}
                if resp.status >= 400 or not data.get("success"):
                    raise RuntimeError(data.get("message") or f"HTTP {resp.status}")
                image_base64 = data.get("imageBase64")
                if not image_base64:
                    raise RuntimeError("代理没有返回 imageBase64")
                return image_base64, data.get("mimeType") or "image/png", data.get("model") or payload["model"]

    def _save_image(self, image_base64: str, mime_type: str, user_id: str, tier_id: str) -> str:
        suffix = "jpg" if mime_type == "image/jpeg" else "webp" if mime_type == "image/webp" else "png"
        out_dir = Path(self.config.get("output_dir") or "/AstrBot/data/temp/dora_imagegen")
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{int(time.time())}_{user_id}_tier{tier_id}_{secrets.token_hex(4)}.{suffix}"
        out.write_bytes(base64.b64decode(image_base64))
        return str(out)

    async def _enter_generation_slot(self, event: AstrMessageEvent) -> None:
        queued_position = 0
        async with self.queue_lock:
            if self.inflight >= self.max_concurrent:
                self.waiting += 1
                queued_position = self.waiting
        if queued_position:
            await event.send(event.plain_result(f"前面还有 {queued_position} 个生图任务，已排队。"))

        await self.semaphore.acquire()
        async with self.queue_lock:
            if queued_position:
                self.waiting = max(0, self.waiting - 1)
            self.inflight += 1

    async def _leave_generation_slot(self) -> None:
        async with self.queue_lock:
            self.inflight = max(0, self.inflight - 1)
        self.semaphore.release()

    @filter.command("画图", alias={"生图", "绘图", "image"})
    async def imagegen(self, event: AstrMessageEvent):
        tier_id, prompt = self._parse_command_and_tier(event)
        event.call_llm = True

        if not prompt or prompt.lower() in {"help", "帮助", "档位", "菜单", "?", "？"}:
            await event.send(event.plain_result(self._format_tiers_help()))
            event.stop_event()
            return

        remaining = self._check_cooldown(event, tier_id)
        if remaining > 0:
            await event.send(event.plain_result(f"生图冷却中，请 {remaining:.0f} 秒后再试。"))
            event.stop_event()
            return

        tier = self._get_tier(tier_id)
        await event.send(event.plain_result(f"收到，{tier_id}档「{tier.get('name', '')}」正在生图，请稍等……"))
        await self._enter_generation_slot(event)
        try:
            refs = await self._collect_reference_images(event)
            logger.info(
                f"dora_imagegen tier={tier_id} name={tier.get('name', '')} "
                f"model={tier.get('model')} prompt_chars={len(prompt)} refs={len(refs)}"
            )
            image_base64, mime_type, used_model = await self._call_proxy(prompt, refs, tier_id)
            path = self._save_image(image_base64, mime_type, event.get_sender_id() or "user", tier_id)
            logger.info(f"dora_imagegen done tier={tier_id} model={used_model} path={path}")
            await event.send(event.image_result(path))
            event.stop_event()
            return
        except Exception as exc:
            logger.error(f"dora_imagegen 生图失败: {exc}")
            await event.send(event.plain_result(f"生图失败：{exc}"))
            event.stop_event()
            return
        finally:
            await self._leave_generation_slot()
