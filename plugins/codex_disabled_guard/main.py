import astrbot.api.star as star
import astrbot.api.event.filter as filter
from astrbot.api.event import AstrMessageEvent

@star.register(name="codex_disabled_guard", desc="Codex 已禁用，提示使用 /cc。", author="Codex", version="0.1.0")
class Main(star.Star):
    def __init__(self, context: star.Context):
        self.context = context

    @filter.command("codex", alias={"代码助手", "编程"})
    async def codex_disabled(self, event: AstrMessageEvent):
        event.call_llm = False
        yield event.plain_result("Codex 功能已取消，不再调用 Codex CLI/Auth。现在用 /cc 或 /claude：\n/cc status\n/cc 你的长程任务\n/cc jobs\n/cc continue")
