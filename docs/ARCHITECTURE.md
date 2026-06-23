# Architecture

## Runtime services

| Service | Role |
|---|---|
| `napcat` | QQ 登录与 OneBot v11 适配 |
| `astrbot` | Bot 主框架、插件运行时、会话管理 |
| `ollama-gemma4` | 可选本地 OpenAI-compatible LLM |
| `dora-vertex-proxy` | Google Vertex 图片模型代理 |
| `glm-code-runner` | Python 代码执行沙箱调度器 |
| `group_memory` plugin | 群消息记忆、周期总结、回答前注入群画像 |
| `group_style_learner` plugin | 学习群友说话风格、口癖、句长、禁忌表达 |
| `group_participation` plugin | 参与策略：直接问答、偶尔插嘴、冷却控制 |
| `egg_persona` plugin | “一个蛋”群友型人格注入 |

## Message flow

```mermaid
sequenceDiagram
  participant QQ
  participant NapCat
  participant AstrBot
  participant Plugin
  participant Model

  QQ->>NapCat: group/friend message
  NapCat->>AstrBot: OneBot event
  AstrBot->>Plugin: command/filter dispatch
  Plugin->>Model: LLM/Image/Runner request
  Model-->>Plugin: result
  Plugin-->>AstrBot: message chain
  AstrBot-->>NapCat: send_msg
  NapCat-->>QQ: text/image reply
```

## Image generation flow

```mermaid
flowchart LR
  A["/生图 1/2/3 prompt"] --> B["dora_imagegen"]
  B --> C["tier parser"]
  C --> D["dora-vertex-proxy"]
  D --> E["Google Vertex AI"]
  E --> D --> F["imageBase64"]
  F --> G["/AstrBot/data/temp/dora_imagegen"]
  G --> H["NapCat sends image"]
```

## Advanced GLM + code execution

```mermaid
flowchart TD
  A["/高级 task"] --> B["glm_router"]
  B --> C{"looks like execution?"}
  C -- no --> D["GLM text answer"]
  C -- yes --> E["GLM plans Python JSON"]
  E --> F["glm-code-runner /run"]
  F --> G["docker run --network none --read-only"]
  G --> H["stdout/stderr"]
  H --> I["GLM summary"]
```


## Group memory flow

```mermaid
flowchart TD
  A["群聊消息"] --> B["group_memory collect"]
  B --> C["data/group_memory/*.json"]
  C --> D{"达到总结阈值?"}
  D -- yes --> E["GLM/Gemma summarizer"]
  E --> C
  C --> F["on_llm_request 注入群画像"]
  F --> G["Gemma/GLM 回复"]
```


## Egg persona / participation flow

```mermaid
flowchart TD
  A["群消息"] --> B["group_memory 记录事实/群梗"]
  A --> C["group_style_learner 学习说话风格"]
  A --> D["group_participation 打分"]
  B --> D
  C --> D
  D --> E{"该说话吗?"}
  E -- 否 --> F["只潜水/记忆"]
  E -- 直接问它 --> G["一个蛋直接回答"]
  E -- 高价值插嘴 --> H["一个蛋短句插嘴"]
  G --> I["Gemma/GLM"]
  H --> I
  C --> J["egg_persona 注入真人群友风格"]
  J --> I
```
