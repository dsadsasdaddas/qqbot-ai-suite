import astrbot.api.star as star
import astrbot.api.event.filter as filter
from astrbot.api.event import AstrMessageEvent


PUBLIC_HELP = """一个蛋可用指令：

【聊天】
直接 @我 / 叫 一个蛋、蛋蛋、小蛋、bot、机器人，我会按群聊语境回答。

【生图】
/生图 1 描述     快速档：表情包、头像、简单图
/生图 2 描述     标准档：默认，速度/质量均衡
/生图 3 描述     精细档：复杂场景、海报、参考图重绘
别名：/画图 /绘图 /image
参考图：把图片和 /生图 文案发在同一条消息里。

【高级问答 / 代码】
/高级 问题       走 GLM，复杂问题用
/跑代码 任务     GLM 写 Python 并在 Docker 沙盒执行
/跑py 代码       直接执行 Python，例如 /跑py print(1+1)
别名：/glm /深度 /复杂 /大脑 /代码执行 /glmcode /gcode

【群记忆】
/群记忆 查看
/群记忆 最近
/群记忆 状态
/群记忆 总结     管理员
/群记忆 开启|关闭|清空  管理员
别名：/记忆 /群脑

【群风格】
/群风格 查看
/群风格 总结     管理员
/群风格 开启|关闭|清空  管理员
别名：/风格

【参与策略】
/参与策略 查看
/参与策略 安静|正常|活跃|嘴欠  管理员
别名：/参与 /插嘴

【Dora 小游戏】
/做游戏 需求     生成并启动 Dora SSR 小游戏
/游戏 状态 id
/游戏 日志 id
/游戏 停止 id
/游戏 列表

提示：/plugin、/sid 等内部信息不在群里公开。""".strip()


@star.register(
    name="help_guard",
    desc="提供产品化 /help，并禁止在群聊公开展示 /plugin 和 /sid 等内部信息",
    author="Codex",
    version="0.2.0",
)
class Main(star.Star):
    def __init__(self, context: star.Context) -> None:
        self.context = context

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            admins = [str(x) for x in self.context.get_config().get("admins_id", [])]
            return str(event.get_sender_id()) in admins
        except Exception:
            return False

    def _format_help(self, event: AstrMessageEvent) -> str:
        text = PUBLIC_HELP
        if self._is_admin(event):
            text += """

【管理员补充】
/redteam 或 /红队：管理增强模式（仅管理员按配置使用）。

【Claude Code / GLM 长程任务】
/cc 你的长程任务     提交后台 job，立即返回 job_id
/cc jobs             查看任务队列
/cc log <job_id>     查看指定任务日志/结果
/cc last             查看最近任务结果
/cc stop <job_id>    取消任务
/cc status           查看 Rust hookd / Claude runner 状态
/cc continue         继续上次任务
/ccmode on|off       开关 CC 直聊模式，开启后普通打字直接进 Claude Code
/cctask new|use|list 切换任务槽
Claude 斜杠：CC 模式下直接发 /compact；若被 AstrBot 抢走，用 //compact 或 /cc /compact
别名：/claude /claudecode /克劳德
说明：/cc 现在走 Rust hookd，不阻塞 AstrBot；任务完成会尝试自动回推。"""
        return text

    async def _block_in_group(self, event: AstrMessageEvent) -> bool:
        if event.is_private_chat():
            return False
        event.call_llm = True
        await event.send(event.plain_result("这个内部指令不在群里公开。可用 /help 查看一个蛋的公开指令。"))
        event.stop_event()
        return True

    @filter.command("help")
    async def help_guard(self, event: AstrMessageEvent):
        event.call_llm = True
        await event.send(event.plain_result(self._format_help(event)))
        event.stop_event()

    @filter.command("plugin")
    async def plugin_guard(self, event: AstrMessageEvent, oper1: str = None, oper2: str = None):
        await self._block_in_group(event)

    @filter.command("sid")
    async def sid_guard(self, event: AstrMessageEvent):
        await self._block_in_group(event)
