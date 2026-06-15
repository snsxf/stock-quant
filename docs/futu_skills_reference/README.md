# 富途 Skills 官方文档本地备份

本目录用于备份富途官方 Skill/OpenAPI 文档，防止官方页面变更或下线导致信息不可追溯。

## 文件说明

| 文件 | 来源 | 用途 |
| --- | --- | --- |
| `futu-install.md` | https://www.futunn.com/skills/futu-install.md | Claude Code Skill 安装/接入指引 |
| `openapi-intro-ai.html` | https://openapi.futunn.com/futu-api-doc/intro/ai.html | OpenAPI AI 接入章节 |
| `skillhub.html` | https://www.futunn.com/skillhub | 官方 Skill 能力全景页 |
| `archive/YYYYMMDD_HHMMSS/` | 历史快照 | 每次运行 `update.sh` 自动保留，最多 12 份 |

## 手动同步

```bash
cd docs/futu_skills_reference
bash update.sh
```

## 定期同步（crontab，每周一 09:00）

```cron
0 9 * * 1  cd /Users/bytedance/codes/my_quant/stock-quant/docs/futu_skills_reference && bash update.sh >> update.log 2>&1
```

## 与本项目的关系

stock-quant 当前走 **MCP 路径**（仅 Trae），并未加载富途 Skill Markdown。此备份仅作为：

1. 官方能力清单的离线参考；
2. 未来若需要扩展新工具（如 push 推送、期货、组合管理等）时的设计输入；
3. 合规/审计留痕。

> 已在 MCP 层覆盖的能力：行情、期权链、资金流、K 线、新闻情绪、社区情绪、板块联动、逐笔档位、标的筛选、盘中 15min 分析、宏观环境、期权决策。未覆盖（刻意不做）：下单交易、账户持仓、期货、实时 push、OpenD 安装。
