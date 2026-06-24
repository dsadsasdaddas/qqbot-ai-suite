# CLAUDE.md

这是 QQBot 的 Claude Code 离线注入工作区说明。

## 运行方式
- 你通过 Claude Code CLI 运行，但模型请求会被本地 Anthropic 兼容代理转发给 GLM。
- 不依赖 Claude Auth，不调用 Anthropic 官方账号。
- 默认 YOLO 权限：`--dangerously-skip-permissions` 和 `--permission-mode bypassPermissions`。
- 长期工作区：`/home/wzu/qqbot`。
- 长期状态目录：`/home/wzu/qqbot/.agent_state`。

## 长程任务规则
- 每次开始先读取 `.agent_state/MEMORY.md`、`TASKS.md`、`HANDOFF.md`、`LOG.md` 尾部。
- 重要事实、决策、待办、交接必须写回 `.agent_state`。
- 不要依赖 QQ 聊天上下文保存长期状态。
- 修改机器人提示词/人格/全局规则之前必须得到用户明确授权；普通代码和部署配置可按任务修改。

## 权限与执行
- runner 可按部署配置挂载工作区和 Docker socket。
- 高权限宿主机操作应走人工确认的运维流程。
- 需要执行命令时优先使用 Bash 工具。
