# stock-quant

可复用的港股 / 美股 / A 股 + 期权量化分析工具库。

## 特性

- **双数据源**：yfinance（美股免费）+ 富途 OpenD（港美股 + 期权 Greeks 免费）
- **统一接口**：上层代码通过 `DataSource` 抽象，底层可热切换
- **可复用**：其他项目通过 `uv pip install -e ~/codes/my_quant/stock-quant` 即可引用

## 快速开始

```bash
cd ~/codes/my_quant/stock-quant
source .venv/bin/activate
uv sync --extra dev
uv run python scripts/daily_report.py
```

## 其他项目复用

```bash
cd ~/codes/my_quant/another-project
uv pip install -e ~/codes/my_quant/stock-quant
```

```python
from stock_quant import YahooSource, FutuSource
```

## 目录结构

```
src/stock_quant/
├── config.py             # 全局配置
├── datasource/           # 行情数据源
│   ├── base.py           # 抽象接口
│   ├── yahoo.py          # yfinance 实现
│   └── futu.py           # 富途 OpenD 实现
├── analysis/             # 技术指标 / 期权定价（待扩展）
├── sentiment/            # 市场情绪（待扩展）
└── utils/                # 工具函数
```

常用命令: 
cd ~/codes/my_quant/stock-quant
source .venv/bin/activate

# 跑全链路自检（网络就绪后）
~/.local/bin/uv run python scripts/daily_report.py

# 跑单测
~/.local/bin/uv run pytest

# 启动 Jupyter
~/.local/bin/uv run --with jupyterlab jupyter lab

# 在别的项目里复用
cd ~/codes/my_quant/some_other_project
~/.local/bin/uv pip install -e ~/codes/my_quant/stock-quant

## 命令行工具 (CLI)

```bash
# 1. 盘前简报（报价/技术/资金流/期权/催化剂/情绪）
stock-quant brief TSLA

# 2. 期权决策建议
stock-quant decide TSLA

# 3. 跨标的期权筛选（富途类选股器）
stock-quant screen TSLA --direction sell_put --dte 21-50 --delta 0.15-0.35 --min-yield 15

# 4. 资讯与社区情绪打分
stock-quant sentiment TSLA

# 5. 大盘扫描与全景期权推荐
stock-quant market
# 或者自定义股票池
stock-quant market --watchlist AAPL,TSLA,META

# 6. 仅获取技术信号
stock-quant signals TSLA
```

## 在 Trae 中作为 MCP Server 使用

stock-quant 内置 21 个 MCP 工具，可在 Trae 等支持 MCP 协议的 AI IDE 里直接调用。

### 1. 启动 FutuOpenD（建议常驻）

> 港美股 + 期权 Greeks + 资金流 都依赖本地 FutuOpenD，默认监听 127.0.0.1:11111。

### 2. 配置 Trae MCP

将仓库根目录下 [`trae_mcp_config.json`](./trae_mcp_config.json) 内容合并到 Trae 的 MCP 配置中：

```json
{
  "mcpServers": {
    "stock-quant": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/Users/bytedance/codes/my_quant/stock-quant",
        "stock-quant-mcp"
      ],
      "env": {
        "FUTU_HOST": "127.0.0.1",
        "FUTU_PORT": "11111"
      }
    }
  }
}
```

### 3. 可用工具一览（21 个）

| 类别 | 工具 | 说明 |
| --- | --- | --- |
| 行情 | `get_quote` / `get_history` / `get_signals` | 实时价 / K 线 / 技术指标 |
| 期权 | `get_option_chain` / `calc_greeks` / `screen_options` / `parse_option_code` | 期权链 / BS 定价 / 跨标的筛选 / OCC 解析 |
| 资金流 | `futu_capital_flow` | 主力 / 大单 / 小单分布（聪明钱） |
| 综合 | `daily_brief` / `option_decision` | 盘前/期权决策综合报告 |
| 大盘 | `market_env` / `market_report` | VIX / 三大指数 / 全景扫描 |
| 盘中 | `intraday_analysis` | 15min K 线 + VWAP + ORB + Volume Profile |
| 情绪 | `sentiment_summary` | 资讯归因 + 中英双语 + 社区情绪 |
| 板块 | `sector_analysis` | 标的所属概念板块 + 同板块成分股 |
| 选股 | `screen_stocks` | 富途条件选股（市值/PE/动量） |
| 行情深度 | `order_book` | 实时买卖盘档位 |
| 异动 | `capital_anomaly` / `derivatives_anomaly` / `technical_anomaly` / `full_anomaly_scan` | 资金/衍生品/技术面异动信号（对齐富途三件套）+ 全维度扫描 |

详细参数见 `src/stock_quant/mcp_server.py` 中各 `@mcp.tool()` 函数 docstring。

### 4. 与官方富途 Skill 的关系

- 本项目走 **MCP 路径**（Trae 原生支持），不依赖 Claude Code Skill 形态。
- `docs/futu_skills_reference/` 仅备份官方 Skill 文档，不参与运行；可定期 `bash docs/futu_skills_reference/update.sh` 同步。
- 二者关系：富途官方 Skill 是"数据/工具层"，stock-quant 是"决策/聚合层"，可叠加共存。
```