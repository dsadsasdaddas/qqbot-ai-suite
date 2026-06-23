import astrbot.api.star as star
import astrbot.api.event.filter as filter
from astrbot.api.event import AstrMessageEvent


@star.register(
    name="help_guard",
    desc="禁止在群聊公开展示 /help 和 /plugin 等机器人内部信息",
    author="Codex",
    version="0.1.0",
)
class Main(star.Star):
    def __init__(self, context: star.Context) -> None:
        self.context = context

    async def _block_in_group(self, event: AstrMessageEvent) -> bool:
        if event.is_private_chat():
            return False
        event.call_llm = True
        await event.send(event.plain_result("群聊不公开机器人帮助信息，请私聊我使用 /help。"))
        event.stop_event()
        return True

    @filter.command("help")
    async def help_guard(self, event: AstrMessageEvent):
        await self._block_in_group(event)

    @filter.command("plugin")
    async def plugin_guard(self, event: AstrMessageEvent, oper1: str = None, oper2: str = None):
        await self._block_in_group(event)

    @filter.command("sid")
    async def sid_guard(self, event: AstrMessageEvent):
        await self._block_in_group(event)
