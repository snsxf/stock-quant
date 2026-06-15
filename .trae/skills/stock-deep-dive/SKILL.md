---
name: "个股深度分析"
description: "作为投资分析中枢的子路径，处理无持仓上下文的单标的深度研究。仅当中枢路由到本 Skill 或用户明确要求时调用。"
---

# 个股深度分析 Skill (Stock Deep-Dive Skill)

> 本 skill 用于对**单个标的**做"机构研究员级"深度分析，目标是判断估值水平 + 输出可执行的买入/持有/卖出动作。
> 适配用户画像：AI 算力链 Alpha 选手，强调 BOM 渗透 + 物理瓶颈理论 + 真 Alpha 验证（Beta 中性框架）+ 实时数据强制。
> 与 `holdings-analysis`（持仓组合复盘）、`us-market-daily`（大盘环境）的关系：本 skill 聚焦**单标的微观研究**，是前两者的"放大镜"。
> **公共规则继承**：本 Skill 继承 `投资分析中枢` 的公共 Hard Rules；若与本文件局部规则冲突，以 `投资分析中枢` 为准。

---

## 一、调用条件（Trigger Conditions）

**必须立即调用本 skill 的场景**：

1. 用户问 "分析一下 XXX" / "深度分析 XXX" / "帮我看一下 XXX"（XXX = 单个标的）
2. 用户问 "XXX 现在能不能买 / 能不能拿 / 该不该卖"
3. 用户问 "XXX 估值贵不贵 / 是不是高估 / 还有多少上涨空间"
4. 用户问 "XXX 长期价值 / 三年逻辑 / 中长期持有可行性"
5. 用户问 "XXX 期权怎么做 / Bull Call / CSP / Cover Call 思路"
6. 用户问 "XXX vs YYY 选哪个更好"（多标的对比时对每只跑一次）
7. 用户提供财报 / 新闻链接，要求结合最新事件给操作建议

**禁止跳过的前置动作**（v1.0 Hard Gate）：

- ✅ 必须先用 `mcp_stock-quant_get_quote` 确认实时价格 + 数据时效
- ✅ 必须用 `mcp_stock-quant_daily_brief` 拿基本面 + 技术 + 期权 + 资金流六合一
- ✅ 美股 / 港股必须用 `mcp_stock-quant_market_env` 拉大盘环境（VIX/SPY/QQQ/SMH）做 Beta 锚定
- ✅ 必须用 `mcp_stock-quant_sentiment_summary` 拿新闻 + 社区情绪
- ✅ 必须用 `mcp_stock-quant_sector_analysis` 拿同板块 peers 做横向对比
- ❌ **禁止仅凭训练记忆给数字**（PE / 营收 / 目标价 / 持仓比例等，全部必须 MCP 或 WebSearch 实时拉取）
- ❌ **禁止跳过 13F 持仓追踪**（巴菲特 / Burry / 木头姐 / Pelosi / 桥水等大佬动向）

---

## 一.A、职责边界与路由优先级（统一规则）

### 1. 全局路由优先级

> 全局优先级矩阵 P0–P4 统一定义在 [投资分析中枢 §3](../investment-router/SKILL.md)。本 Skill 仅声明在该矩阵中的角色：

| 场景 | 本 Skill 处理方式 |
|---|---|
| 单标的、无持仓上下文，问估值/商业模式/长期价值/能否买入 | **主导**，按 1.4 表选模式跑分析 |
| 多个单标的对比，但无持仓上下文 | **主导**，逐只快速版 + 横向打分 |
| 出现持仓截图、成本、P&L、期权腿、Roll/止损/止盈 | **让渡** `持仓分析`；可被引用做基本面补充 |
| 问大盘/指数/板块/盘前盘后 | **让渡**对应市场日报 |
| 跨市场映射 | **协同**对应市场日报，仅提供单标的穿透结论 |

### 2. 本 Skill 的接管范围

- ✅ 接管：单个标的的商业模式、护城河、财务质量、估值、情绪、技术、期权策略。
- ✅ 接管：用户问“XXX 现在能不能买”，且没有给出自己的仓位/成本/P&L。
- ✅ 接管：用户问“XXX 是否值得长期持有”，但语义是研究标的质量，而不是处理当前账户仓位。
- ❌ 不接管：用户已给出“我持有 XXX / 成本 $X / 亏损 X% / 期权腿”等仓位信息，必须转 `持仓分析`。
- ❌ 不接管：用户问大盘或板块强弱，必须转市场日报 Skill。

### 3. 与其他 Skill 的协同方式

- 与 `持仓分析`：本 Skill 输出“基本面/估值/护城河结论”，持仓分析负责仓位、止损、Roll、EV 和机会成本。
- 与 `美股市场每日分析`：本 Skill 可引用 VIX/SPY/QQQ/SMH 环境，但不替代美股日报的大盘扫描。
- 与 `A股市场每日分析`：A 股个股必须复用 A 股 Skill 的 BOM 穿透、自嗨审计、换手率和 T+1 规则。
- 输出时必须说明：“本轮主 Skill = 个股深度分析；协同引用 = [如有]”。

### 4. 分析深度模式（避免默认过重）

| 模式 | 触发场景 | 必跑内容 | 可跳过内容 |
|---|---|---|---|
| **快速决策版** | 用户问“能不能买/卖/追/等回调”，未要求深度报告 | 实时价、估值快照、技术信号、资金流、市场环境、结论与 if-then 点位 | 管理层深挖、完整 13F、DCF、长篇行业史 |
| **完整深度版** | 用户明确说“深度分析/长期价值/建仓前完整研究” | 六层框架全跑：商业模式、财务估值、情绪、技术、期权、决策 | 不跳过，缺数据则标缺失 |
| **财报专项版** | 用户问“财报后/财报前/指引/业绩超预期吗” | 财报数字、Guidance、卖方预期差、电话会要点、IV crush、财报后交易计划 | 非核心长期行业背景可压缩 |
| **对比版** | 用户问“XXX vs YYY 选哪个” | 每只标的快速版 + 横向评分表 + 首选/备选 | 单只完整长报 |

**默认选择规则**：
- 用户未指定“深度/完整”时，默认使用 **快速决策版**。
- 若快速版结论为“可建仓 / 高争议 / 数据冲突 / 重大催化临近”，再升级到 **完整深度版**。
- 美股期权策略只有在用户明确需要、或标的流动性/IV 环境适合时展开；否则只给期权可行性判断。
- A 股标的默认跳过期权层，但必须保留换手率、涨跌停、T+1、BOM 审计。

---

## 二、六层分析框架（核心方法论）

### Layer 1：商业模式与护城河（Business & Moat）

#### 1.1 行业分析（Top-Down）
| 维度 | 必查项 | 数据源 |
|------|--------|--------|
| 行业规模 | TAM / SAM / SOM | 公司投资者关系 + 第三方咨询（Gartner/IDC） |
| 行业增速 | YoY / 5年 CAGR | 同上 + WebSearch |
| 行业生命周期 | 萌芽 / 成长 / 成熟 / 衰退 | 定性判断 + 渗透率数据 |
| 主线驱动力 | AI / 政策 / 技术革命 / 消费降级 / 老龄化 | 主题映射 |

#### 1.2 竞争格局（Five Forces 简版）
- **集中度**：CR3 / CR5（前 3/5 玩家市占率）
- **直接对手**：列出 3-5 家同业 + 当前估值（PE/PS）
- **替代风险**：技术/路线替代（如 CPO 被铜缆/LPO 替代、InP 被硅光替代）
- **议价能力**：上下游议价（毛利率趋势是最直观证据）

#### 1.3 护城河评级（v1.0 五维评级，每项 1-5 分，总分 25 分）
| 维度 | 5分 | 3分 | 1分 |
|------|-----|-----|-----|
| **网络效应** | Meta/Visa | LinkedIn | 0 网络效应 |
| **转换成本** | 企业 SaaS（SAP/CRM） | iPhone 生态 | 大宗商品 |
| **规模效应** | TSMC/Costco | NVDA 软件栈 | 同质化制造 |
| **无形资产** | NVDA CUDA / Apple 品牌 | 专利组合 | 弱品牌 |
| **成本优势** | Saudi Aramco | 工业富联 | 末位代工 |

护城河总分映射：
- **20-25 分**：S 级（NVDA / TSMC / MSFT 这类全球垄断）
- **15-19 分**：A 级（AMD / Meta / GOOGL 类强护城河）
- **10-14 分**：B 级（一般竞争力）
- **< 10 分**：C 级（无护城河 / 周期股 / 同质化）

#### 1.4 弹性判断（Alpha / Beta 拆解）
- **Beta**：相对 SPY（美股）/ HSI（港股）/ 沪深 300（A 股）的 60 日 / 252 日 Beta
  - Beta > 1.5 = 高弹性 / 风险偏好代理（如 ARM/PLTR/TSLA）
  - Beta 0.8-1.2 = 大盘股（NVDA/MSFT 后期）
  - Beta < 0.8 = 防御股（XLU/MSTR-反向）
- **Alpha**：剔除 Beta 后超额收益（CAPM 残差）
  - **真 Alpha**：来自护城河 / 物理瓶颈 / 政策红利（持续性 > 12 月）
  - **伪 Alpha**：来自情绪 / 拉高出货 / 一次性事件（持续性 < 3 月）
- **强制输出**：本标的当前 Alpha 判断为"真" or "伪"，给依据

#### 1.5 企业文化与管理层
- **CEO 任职年限 + 业绩**：Tenure < 2 年警示稳定性，> 10 年看是否进入"传承陷阱"
- **股权激励 / 内部人交易**（OpenInsider 数据）：高管近 6 月净买入 = 强信号
- **资本配置**：买回 / 分红 / 并购 / 资本开支拆解（FCF 用在哪里）
- **外界评价**：Glassdoor 评分 + 客户/合作伙伴口碑（WebSearch）
- **重大丑闻 / 诉讼 / SEC 调查**：必查（WebSearch + 8-K filings）

#### 数据源可信度分级（适用于 1.D / 1.E / 1.F）

| 等级 | 来源 | 用法 |
|---|---|---|
| 🟢 一级（强制） | SEC EDGAR / 巨潮 / 公司官网 PR / BIS Entity List / 商务部公告 | 法定披露，可直接引用 |
| 🟡 二级（参考） | TipRanks / StockAnalysis.com / yfinance / akshare / Futu OpenD / IDC / Gartner 摘要 | 聚合源，需标注采集时间 |
| 🟠 三级（线索） | TrendForce / DigiTimes / SemiAnalysis / TechInsights / Yole 顶层摘要 / X 大 V / Reddit | 仅作 idea generator，**禁止单独作为决策依据** |
| 🔴 禁区 | 雪球零散观点 / 推特无来源截图 / 微信公众号转载 | 直接拒绝，符合 [投资分析中枢 §2.8](../investment-router/SKILL.md) |

#### 1.D 产业链穿透（Supply Chain Map）

> **目的**：一句话讲清"标的处于产业链哪个位置，当前/潜在的核心瓶颈在哪"。
> **输出**：1 段产业链描述（≤ 5 行）+ 1 行核心瓶颈。

**模板**：

```
产业链描述：
  上游 [关键原材料/设备] → 中游 [本公司位置] → 下游 [终端客户/产品]
  关键合作方：[供 / 用 / 替代]

核心瓶颈（当前 / 潜在）：
  [一句话描述卡脖子点 + 触发情景]
  例："CoWoS 先进封装由 TSMC 独供，月产能 75-80k 片是 NVDA 出货量的物理上限；
        若 TSMC 扩产不及预期或台海地缘冲突 → 直接锁死营收增长"
```

**数据源**：SEC 10-K segment / 公司 IR / 终端拆解（TechInsights/iFixit/SemiAnalysis 顶层）/ 行业研报（TrendForce/IDC/Gartner 顶层）/ BIS Entity List。

#### 1.E 产业链各位置评级 & 利润分配打分（核心）

> **目的**：用一张表回答"这条链上谁是赢家、谁是打工人"。
> **方法**：列出产业链关键 **5-10 个位置 / 公司**，各打两个分（位置评级 + 利润分配），1-5 分制，配一句依据。
> **核心洞察**：位置好 ≠ 利润高（如苹果产业链：立讯位置关键但利润分配低；苹果位置上层但利润分配独享）。

**评分维度**：

| 维度 | 1 分 | 3 分 | 5 分 |
|---|---|---|---|
| **位置评级** | 完全可替代 / 同质化 | 关键供应商但有二供 | 独家卡位 / 不可替代 |
| **利润分配** | 毛利 < 15% / 议价权弱 | 毛利 20-35% / 与客户共担周期 | 毛利 > 50% / 独享终端定价权 |

**输出模板**：

| 产业链位置 | 代表公司 | 位置评级 | 利润分配 | 一句话依据 |
|---|---|---|---|---|
| 上游 [...] | [Ticker] | X/5 | Y/5 | 毛利 X% / 是否独供 / 是否被压榨 |
| 中游 [...] | **本标的** | X/5 | Y/5 | 同上 |
| 中游：A 股映射 [...] | [A 股 Ticker] | X/5 | Y/5 | 毛利 X% / 与本标的的依赖关系 |
| 下游 [...] | [Ticker] | X/5 | Y/5 | 同上 |

**强制规则**：

1. 必须包含**本标的**所在行
2. 必须给出 1 个上游 + 1 个下游对比锚点
3. **A 股映射强制**：美股 / 港股标的必须至少给 **1 行 A 股供应链对照**（用于跨市场套利与卡脖子理论验证）；A 股标的反向给 1 行美股 / 海外对照
4. **"打工人池"显式标记**：所有 ODM / 模组厂 / 同质化代工（典型如立讯精密、工业富联、沪电股份、中际旭创等）默认归入"**卡位打工人**"画像，行尾以 `→ 打工人` 显式标注，便于快速识别"位置高 + 利润低"反差信号
5. 终端品牌（如苹果）若与中游供应商利润分配差距 ≥ 2 分，必须显式标注"利润倒挂 / 苹果税效应"
6. 给一行结论：`本标的位置 X / 利润 Y → 角色 = [垄断者 / 共赢者 / 卡位打工人 / 周期搬运工]`

**角色映射**：

- 位置 ≥ 4 + 利润 ≥ 4 → **垄断者**（NVDA、TSMC、苹果）
- 位置 ≥ 4 + 利润 ≤ 2 → **卡位打工人**（立讯、富士康、中际旭创早期）
- 位置 ≤ 2 + 利润 ≥ 4 → **品牌溢价**（罕见，需警惕反噬）
- 位置 ≤ 2 + 利润 ≤ 2 → **周期搬运工**（同质化代工 / 大宗）

#### 1.F（已合并入 1.D / 1.E）

> 原 1.F 价值链分润（VCM）的核心定量逻辑（毛利率 / 议价权 / 终端 ASP 占比）已并入 1.E "利润分配"打分中。
> 不再单独输出 VCM 公式表，避免与 1.E 重复。

---

### Layer 2：财务状况与估值水平（Financials & Valuation）

#### 2.1 核心财务指标（强制 5 张表）

**A. 营收质量（最近 8 季度）— 强制整体 + 分部双视角**

> ⚠️ **硬规则（v1.4）**：禁止只看总营收。必须同时给出**整体（Top-line）+ 分部（Segment-level）**两套数据，并显式标注**市场最关注的核心分部**。
>
> ⚠️ **硬规则（v1.5）数据源契约**：分部营收数据**必须来自 SEC 10-Q / 10-K segment reporting 或公司 IR PPT 原始披露**。
>
> - ❌ **禁止**从"总营收 × 历史占比"反推分部数字
> - ❌ **禁止**用 StockAnalysis.com Standardized 模板代替分部（该模板不含 segment）
> - ❌ **禁止**凭训练记忆给分部 YoY / 占比
> - ✅ **强制**显式标注每个分部数字的来源（10-Q / 10-K / IR Deck / Earnings Release 哪一份文件 + 页码或表号）
> - ⚠️ 若最新季度 10-Q 尚未发布（财报当日 / 当周），必须显式标记 `⚠️ 估算（10-Q 未发布，依据 earnings release 表 X）`，并在结论中降级为🟠三级源
> - 🚫 违反本规则的分部数字一律视为"包装估算"，等同 §2.8 数据源禁区
>
> **典型样例**：
> - **MSFT** → 总营收 + 分部（Cloud/Azure ↑ 主线 / Productivity / More Personal Computing），市场只为 **Azure 增速** 定价
> - **NVDA** → 总营收 + 分部（Data Center 88% ↑ 主线 / Gaming / Pro Vis / Auto），市场只看 **Data Center YoY**
> - **AAPL** → 总营收 + 分部（iPhone / Services 主线 / Mac / iPad / Wearables），市场为 **Services 毛利 75%** 给溢价
> - **SPCX**（即将上市）→ 必须拆 **AI / 航天 / 星链**，三块差异巨大，整体平均会掩盖真实增速分化
> - **GOOGL** → Search / YouTube / Cloud / Other Bets，市场只看 **Cloud + Search 抗 AI 替代**
>
| 字段 | 必查 | 备注 |
|------|------|------|
| 总营收 + YoY / QoQ | yfinance Ticker.quarterly_income_stmt | 整体 anchor |
| **分部营收（Segment）+ 各自 YoY / 占比** | 公司投资者关系 PPT / 10-Q segment reporting | **强制项**，必须列出每个分部独立 YoY |
| **核心分部识别** | 标注"市场定价的主线分部" | 强制一句话说明"市场只为 X 定价" |
| 客户集中度 | 10-K Risk Factors | |
| 地域分布 | 同上 | |
| 经常性 vs 一次性 | 财报电话会 | |

**B. 盈利能力**
- 毛利率 / 营业利润率 / 净利率（趋势 + 同业对比）
- ROE / ROIC / ROA（杜邦拆解）
- **盈利质量警示**：
  - GAAP vs Non-GAAP 差距 > 20% → 警示股权激励泡沫
  - 应收账款增速 > 营收增速 → 警示渠道压货
  - 经营现金流 / 净利润 < 0.8 → 警示利润含金量

**C. 现金流（最重要！）**
- OCF（经营现金流）/ FCF（自由现金流）/ 资本开支
- **现金流是检验真假 Alpha 的核心**：营收增长但 OCF 不增 = 财务造假高风险

**D. 资产负债表健康度**
- Net Debt / EBITDA（< 2 健康，> 4 警示）
- 流动比率 / 速动比率
- 商誉 / 总资产（高商誉 = 并购炸弹风险）

**E. 盈利前瞻（Guidance）**
- 公司最新季度 / 全年指引（Beat / In-line / Miss 历史记录）
- 卖方一致预期（Forward EPS / Revenue 一致预期，StockAnalysis.com）
- 可持续性判断：未来 4 季度增长引擎是 AI? 周期复苏? 价格战? 一次性补贴?

#### 2.2 估值水平（多维交叉验证）

| 估值方法 | 适用场景 | 输出 |
|---------|---------|------|
| **PE_TTM** | 稳定盈利股 | 当前值 + 5 年区间 + 行业中位数 |
| **Forward PE** | 成长股 | 基于卖方一致预期 |
| **PEG** | 高增长股 | PE / 未来 3 年 EPS CAGR；< 1 低估，> 2 高估 |
| **PB** | 金融 / 重资产 | 银行 / REIT 必用 |
| **PS** | 未盈利成长股 | 软件 / 生物科技 |
| **EV/EBITDA** | 跨资本结构对比 | 并购视角 |
| **DCF** | 现金流稳定股 | 简化 2 阶段 DCF（增长期 + 永续期） |
| **mNAV** | 加密代理 / 资产持有公司 | MSTR / GBTC 类，市值 / NAV 溢价 |
| **历史百分位** | 全部 | 当前 PE 处于过去 5/10 年的百分位（< 30% 低估，> 80% 高估）|

**估值结论模板**（强制三选一）：
- 🟢 **低估**（PE/Forward PE/PEG 至少 2 项 < 历史 30 百分位 + 同业中位数）
- 🟡 **合理**（介于低估与高估之间）
- 🔴 **高估**（PE/PEG 至少 2 项 > 历史 80 百分位 或 PEG > 2）

---

### Layer 3：市场情绪与分析师预期（Sentiment & Consensus）

#### 3.1 华尔街共识
| 字段 | 数据源 |
|------|--------|
| 卖方评级分布（Buy/Hold/Sell 数量） | StockAnalysis.com / TipRanks |
| 平均目标价 + 高/低范围 | StockAnalysis.com / Finnhub recommendation_trends |
| 距当前股价 Upside | 计算 |
| 近 30 天评级变化（Upgrade/Downgrade） | Finnhub recommendation_trends ✅ |
| 近 30 天目标价上调 / 下调 | StockAnalysis.com |

**禁用 Finnhub 免费层** `price_target` 字段（无目标价数据，必须走 StockAnalysis.com）。

#### 3.2 机构 / 大佬 / 13F 持仓追踪（v1.0 强制章节）

| 玩家 | 必查内容 | 数据源 |
|------|---------|--------|
| **Berkshire（巴菲特）** | 13F 持仓 + 增减仓 | WhaleWisdom / Dataroma |
| **Burry / Scion** | 同上（关注做空 / Put 仓位） | WhaleWisdom |
| **Cathie Wood / ARKK** | 每日 ARK Invest 官网披露 | ark-invest.com |
| **Pelosi / 国会** | 国会议员交易 | unusualwhales.com / Capitol Trades |
| **Bridgewater / Citadel / Renaissance** | 13F | WhaleWisdom |
| **公司内部人** | 高管 6 个月内买卖 | OpenInsider ✅（非 Finnhub）|
| **机构总持仓比例** | Institutional Ownership % | yfinance Ticker.major_holders |
| **空头比例** | Short Interest % | yfinance Ticker.info `shortPercentOfFloat` |

**真 Alpha 信号清单**：
- ✅ 巴菲特连续 2 季度增仓
- ✅ 公司高管 6 月内净买入金额 > $1M
- ✅ 机构持仓比例上升 + 空头比例下降
- ❌ 木头姐 + Reddit / X 散户高度集中 → 警示"拉高出货"

#### 3.3 散户情绪
| 数据源 | 解读 |
|--------|------|
| `mcp_stock-quant_sentiment_summary` 内含 StockTwits / Reddit / Finnhub social | 直接调用 |
| Bull/Bear/Neutral 占比 | Bull > 70% 警示过热，Bear > 60% 关注反转 |
| Reddit WSB 提及量异常 | 异常飙升 = Meme 风险 |
| Google Trends | 关键词搜索热度 |

#### 3.4 关键催化剂日历（未来 90 天）⭐ 决策权重核心

> ⚠️ **硬规则（v1.4）**：催化剂是**短期 0-30 天 alpha 的全部来源**，本节为**决策影响 Top 1**，禁止只列日期不分析。
>
> **强制输出 4 项**：
> 1. **催化剂列表**：日期 + 类型 + 内容 + 一致预期（如有）
> 2. **真假 Alpha 标记**：每条催化剂标注"真 / 伪 / 已 priced in / 反向"
> 3. **预期差判断**：与卖方一致预期对比，标注"超预期概率 / 不及预期概率"
> 4. **交易窗口**：每条给出"建议提前 N 天进场 / 兑现日 T+0/+1 操作"

| 类型 | 内容 | 数据源 |
|------|------|--------|
| 财报日 | yfinance `Ticker.earnings_dates` | |
| 产品发布 | WebSearch + 公司官网 | |
| 监管批准 / FDA / FTC | WebSearch | |
| 分析师日 / Capital Markets Day | 公司 IR | |
| 股东大会 / 拆股 / 回购计划 | 8-K filings | |
| 行业大事件 | CES / GTC / WWDC / Computex / 选举 / FOMC | |
| 解锁 / 减持 / 内部人 | OpenInsider + Form 4 | |

---

### Layer 4：技术分析（Technicals）

#### 4.1 价格趋势
| 时间维度 | 必查 |
|---------|------|
| 实时价 + 当日 OHLC | `mcp_stock-quant_get_quote` |
| 盘前 / 盘后 | 同上（美股专属） |
| 5 日 K 线 | `mcp_stock-quant_get_history(period='5d', interval='15m')` |
| 1 月 / 3 月 / 1 年 K 线 | `mcp_stock-quant_get_history` |
| 52 周高 / 低 + 距当前价 % | 计算 |

#### 4.2 均线系统
- **MA20 / MA50 / MA200**：当前价相对位置 + 多头/空头排列
- **EMA 9 / 21**：短线交易锚
- **关键支撑 / 阻力位**：基于近 6 月 K 线密集成交区
- **趋势判断**：
  - 多头排列（价 > MA20 > MA50 > MA200）= 强趋势
  - 跌破 MA50 + MA50 转头向下 = 中期趋势破坏
  - 跌破 MA200 = 长期熊市信号

#### 4.3 资金流向（v1.0 重点 / v1.4 升级为决策权重核心 ⭐）

> ⚠️ **硬规则（v1.4）**：资金流是**最难造假的硬信号**，本节与催化剂并列**决策影响 Top 1-2**，禁止只列数字不解读。
>
> **强制区分"真信号 vs 噪音"**：
>
> | 真信号（高权重） | 噪音（低权重 / 反向） |
> |---|---|
> | 内部人交易（Form 4，6 月维度） | 单日资金流 |
> | 13F 增减仓（连续 2+ 季度） | ETF 被动 rebalancing |
> | 期权 OI **周维度**变化 + 异常流（vol/oi > 5） | 投行做市单 / HFT 大单 |
> | Dark Pool Print 异常放量 | 散户主导净流入（往往反向） |
> | 主权基金 / Berkshire 一级动作 | 卖方推荐目标价跟涨 |
>
> **强制输出 3 项**：
> 1. **当前资金流方向**（净流入/流出 + 真信号 vs 噪音判断）
> 2. **内部人 + 13F 趋势**（近 6 月维度，禁用单日）
> 3. **期权异常流**（vol/oi 倍数、行权价集中度、买保护 vs 投机判断）

| 指标 | 数据源 | 真假信号判定 |
|------|--------|---|
| 主力净流入（大单/特大单） | `mcp_stock-quant_futu_capital_flow`（港美股部分支持） | ⚠️ 需结合 smart money 标签 |
| 内部人交易（Form 4） | OpenInsider | 🟢 真信号（≥ $50M 净卖出 = 强警示）|
| 13F 持仓变化 | WhaleWisdom / Dataroma | 🟢 真信号（连续 2+ 季度）|
| Dark Pool Print | `WebSearch unusualwhales darkpool [ticker]` | 🟢 真信号 |
| 期权异常流 vol/oi | 期权链 + Greeks 推算 | 🟢 真信号（vol/oi > 5 = 异常）|
| 期权净 Delta（C-P 价差） | 期权链 | 🟡 中等 |
| ETF 资金流 | etf.com | 🔴 噪音（被动 rebalancing） |

#### 4.4 技术指标
| 指标 | 阈值 | 数据源 |
|------|------|--------|
| RSI14 | > 70 超买 / < 30 超卖 / **> 75 触发用户硬约束预警** | `mcp_stock-quant_get_signals` |
| MACD | 金叉 / 死叉 / 柱背离 | 同上 |
| 布林带 | 上轨 / 中轨 / 下轨位置 | 同上 |
| 换手率 | **A 股单日 > 10% 或两日累计 ~25% = 派发信号**（用户硬约束） | `get_quote` `turnover_rate` |
| ATR(14) | 用于止损位设置 | 计算 |

#### 4.5 异动扫描（用户偏好）
- 必跑 `mcp_stock-quant_full_anomaly_scan`：资金 / 衍生品 / 技术三件套
- 重点识别：突破 / 大宗交易 / 股东减持 / 期权异常成交

---

### Layer 5：期权分析（仅美股 + 港股部分）

> A 股因期权品种少，本层跳过；ETF 期权（如 50ETF / 沪深300 期权）按用户需求加挂。

#### 5.1 期权链概览
| 字段 | 数据源 |
|------|--------|
| ATM IV + IV Rank（5 年百分位） | `mcp_stock-quant_daily_brief` |
| **IV Term Structure**（5 个最近到期日的 IV） | 同上 |
| Max Pain（最近月） | 同上 |
| Put-Call Ratio | 同上 |
| 30/60/90 日实际波动率（HV） | 历史计算 |
| **IV Regime**：高 / 中 / 低（决定卖方 vs 买方策略偏向） | `mcp_stock-quant_option_decision` |

#### 5.2 异常期权流（Unusual Options Activity）
- 大单买入 Call / Put（OI 增量 + 远高于 Volume / OI 比）
- 远期 Deep OTM 异常吸筹（"聪明钱"信号）
- 数据源：`unusualwhales.com` / `mcp_stock-quant_derivatives_anomaly`

#### 5.3 期权策略推荐（自动跑）
强制调用 `mcp_stock-quant_option_decision(symbol)` 拿系统输出，输出至少 2 套候选：
- **方向打分 + IV 环境匹配**
- **具体合约**：strike / 到期日 / 当前 mid 价 / Greeks
- **盈亏图**：max profit / max loss / breakeven
- **退出条件**：止盈 50%-70% 时间价值 / 止损 -50% 或破位 / Roll out 触发条件

---

### Layer 6：决策建议（Decisions — 强制输出格式）

> **核心要求**：每条建议必须包含**具体日期 + 具体点位 + 具体退出条件**。
> **禁止模糊表述**："建议关注"、"逢低吸纳"、"适当配置" 等通通禁止。

#### 6.1 正股策略（长期,目标持有 > 3 个月）

```
【正股策略】
方向：BUY / HOLD / SELL / AVOID
仓位建议：核心仓 X% / 试探仓 Y% / 不建议
入场分批计划：
  - 第一档：现价 ~ $XXX（仓位 30%）
  - 第二档：回踩 MA50 = $YYY（仓位 40%）
  - 第三档：跌破后反弹站稳 $ZZZ（仓位 30%）
止损线：MA200 = $AAA（中长期破位）/ 或 -15% 硬止损
止盈分批：
  - 第一目标：分析师一致目标价 $BBB（减仓 1/3）
  - 第二目标：历史 PE 80 百分位对应 $CCC（再减 1/3）
  - 第三目标：长持 / Trailing Stop
持有时长：> 2 月（目标 6-12 月）
关键检查点：
  - 财报日 YYYY-MM-DD：若 Guidance 下调 → 立即减仓 50%
  - 行业事件 X：若不及预期 → 评估退出
退出触发：
  - 基本面：营收 YoY 转负 / FCF 连续 2 季度 < 0
  - 技术面：周线收盘跌破 MA50（中期）/ MA200（长期）
  - 估值：PE 突破历史 95 百分位（极度高估）
```

#### 6.2 期权策略（目标持有 > 3 周）

> **强制输出规范（v1.6）**：
> 1. **策略类型必须中文名 + 英文对照**：如 `卖出牛市看跌价差（Bull Put Spread）` / `卖出熊市看涨价差（Bear Call Spread）` / `卖出现金担保看跌期权（Cash-Secured Put）` / `备兑看涨（Covered Call）` / `铁鹰式（Iron Condor）` / `日历价差（Calendar Spread）`。
> 2. **每条合约腿必须显式标注买入/卖出方向**：使用 **`卖出 Put $XXX`** / **`买入 Call $YYY`** 格式（中文方向词加粗），禁止仅写 `Long Call` / `Short Put` 而无中文方向。
> 3. **策略名首词必须含动词**：以 `卖出XX价差` / `买入XX价差` 开头，避免用户混淆净 Debit/Credit 方向。
>
> **强制流程规范（v1.7 新增）—— 期权报价禁推断**：
> 1. **推荐期权策略前必须先调 `get_option_chain` 拉实时合约价 + IV + Greeks**，禁止凭印象/记忆/旧 brief 数据估算 mid/last。
> 2. **引用具体 strike 时必须标注实时 last 或 mid + Δ + IV + 数据时间**，例如：`卖出 Put $260 last $17.80 / Δ -0.42 / IV 138% (T+0 12:18 ET)`。
> 3. **若 chain 被 truncate 没覆盖目标 strike**：必须二次调用 `get_option_chain(min_strike, max_strike)` 缩范围或要求用户提供实时 mid，禁止外推。
> 4. **盘中 spot 大幅波动后（>±5% 单日 或 >±3% 单小时），必须重新拉链**，不得沿用早盘/前一日 brief 数据；尤其在 IV 飙升场景，ATM/近 ATM 期权价格可能瞬间翻倍。
> 5. **报价偏差红线**：若用户指出实时报价与我给的 mid 偏差 > 30%，立即停止当前策略推荐，重新拉链 + 公开认错 + 修正风险收益比。
> 6. **价差类策略最低数据要求**：必须同时给出两条腿的实时 last/mid + Net Credit/Debit + 风险收益比 = Max Profit / Max Loss + 盈亏平衡距 spot 百分比。

```
【期权策略】
策略类型：<中文名>（<英文对照>）  例：卖出牛市看跌价差（Bull Put Spread）
方向锚定：基于 Layer 6.1 正股方向
合约腿（每条必须含中文买卖方向）：
  - **卖出** Put $K1，到期 YYYY-MM-DD，mid $X.XX，Δ 0.XX
  - **买入** Put $K2，到期 YYYY-MM-DD，mid $Y.YY，Δ 0.YY
净 Debit / Credit：$Z.ZZ
最大盈利：$AAA
最大亏损：$BBB
盈亏平衡：$CCC
当前 IV / IV Rank：XX% / 第 YY 百分位
持有计划：
  - 入场：盘后 / 次日开盘市价（注明限价单 limit）
  - 中途盈利退出：到达 50% max profit 平仓
  - 中途亏损退出：到达 -50% debit 或 -2× credit 砍仓
  - 时间退出：到期前 7 天若未达目标，强制 Roll out 或平仓（避免 Theta 加速）
持有时长：> 3 周（目标 21-45 DTE）
风险旗：
  - IV crush 警告：财报日前后 / FOMC 前后
  - Term Structure backwardation 警告：买方策略风险加剧
```

---

## 三、输出模板（强制结构）

```markdown
# 🔍 个股深度分析：[Ticker] [公司名]（YYYY-MM-DD）

> 本轮主路径：个股深度分析（<快速决策版 / 完整深度版 / 财报专项版 / 对比版>）
> 协同路径：<无 / 美股市场每日分析（仅环境引用） / A股市场每日分析（BOM 审计） / 持仓分析（基本面补充）>
> 数据状态：<实时 / 延迟15min / 上一交易日收盘 / 数据缺失>
> 时间锚：<now_local, market, market_phase, data_age>
> 数据源：Futu OpenD + yfinance + akshare + StockAnalysis.com + WebSearch
> 实时报价：$XXX.XX（数据时间 HH:MM）

## 一、商业模式与护城河
1. **行业**：[赛道] / TAM $XXB / 5 年 CAGR XX%
2. **竞争格局**：CR3 = XX% / 直接对手 [A/B/C]
3. **护城河评级（XX/25 分）**：
   | 维度 | 分数 | 依据 |
   |------|------|------|
   | 网络效应 | X | ... |
   | 转换成本 | X | ... |
   | 规模效应 | X | ... |
   | 无形资产 | X | ... |
   | 成本优势 | X | ... |
   - **结论**：S/A/B/C 级护城河
4. **Alpha/Beta 拆解**：
   - 252日 Beta = X.XX（vs SPY/HSI/沪深300）
   - 当前 Alpha 性质判断：**真 / 伪**（依据：...）
5. **管理层**：CEO XX，任期 XX 年；近 6 月内部人净买入 $X
6. **风险事件**：[列举]
7. **产业链穿透（1.D）**：
   - 上游 [...] → 中游 **本标的 [...]** → 下游 [...]
   - **核心瓶颈**：[一句话 + 触发情景]
8. **产业链评级 & 利润分配（1.E）**：
   | 位置 | 公司 | 位置评级 | 利润分配 | 依据 |
   | 上游 [...] | [Ticker] | X/5 | Y/5 | ... |
   | 中游 [...] | **本标的** | X/5 | Y/5 | ... |
   | 中游：A 股映射 [...] | [A 股 Ticker] | X/5 | Y/5 | ... → 打工人 |
   | 下游 [...] | [Ticker] | X/5 | Y/5 | ... |
   - 行数要求：5-10 行（覆盖完整产业链，不只挑极值）
   - **本标的角色**：垄断者 / 共赢者 / 卡位打工人 / 周期搬运工
   - **利润倒挂提示**：[如有，标注品牌税 / 苹果税效应]

## 二、财务与估值
### 2.1 核心指标（最近 4 季度）— 整体 + 分部双视角（v1.4 强制）

**整体（Top-line）**：

| 季度 | 总营收 YoY | 毛利率 | 营业利润率 | OCF/净利 | 备注 |

**分部（Segment）— 必须列出每个分部 YoY + 占比 + 市场定价主线**：

| 季度 | 分部 A YoY / 占比 | 分部 B YoY / 占比 | 分部 C YoY / 占比 | 主线分部 | 数据源（10-Q / IR） |
| QX |  |  |  | **市场只为 X 定价** | SEC 10-Q FY27Q1 表 X / IR Deck p.X |

> 强制示例：
> - MSFT → Cloud (主线) / Productivity / MPC
> - NVDA → Data Center (主线) / Gaming / Pro Vis / Auto
> - AAPL → iPhone / **Services (主线)** / Mac / iPad / Wearables
> - SPCX → AI / 航天 / 星链（必须三段拆开，整体平均会掩盖增速分化）

### 2.2 盈利前瞻
- 公司指引：QX 营收 $X-$Y bn（vs 卖方一致 $Z bn）
- **主线分部 Guidance**（强制）：QX 主线分部 $X-$Y bn / YoY +X%
- 历史 Beat 率：X/8（最近 8 季度）
- 增长引擎可持续性判断：[强 / 中 / 弱]

### 2.3 估值水平
| 指标 | 当前值 | 5年区间 | 5年百分位 | 行业中位数 | 结论 |
| PE_TTM | | | | | |
| Forward PE | | | | | |
| PEG | | | | | |
| PB | | | | | |
| EV/EBITDA | | | | | |

- **综合估值结论**：🟢 低估 / 🟡 合理 / 🔴 高估（依据：...）

## 三、市场情绪与共识
### 3.1 华尔街
- 卖方评级：[X 买入 / Y 持有 / Z 卖出]
- 平均目标价：$XXX.XX（Upside +X%）
- 近 30 天评级变化：上调 X / 下调 Y

### 3.2 机构与大佬持仓（13F）
| 玩家 | 持仓变化 | 最新季度动作 |
| Berkshire | | |
| ARKK | | |
| Pelosi | | |
| Burry | | |
- **机构总持仓**：XX%
- **空头比例**：XX%
- **公司内部人 6 月动作**：净买入 $X / 净卖出 $X

### 3.3 散户与社区
- StockTwits Bull/Bear: XX/YY
- Reddit WSB 提及量：[正常 / 异常]
- **真假 Alpha 信号矩阵**：[列举]

### 3.4 催化剂日历（未来 90 天）⭐ 决策权重核心（v1.4 强化）
| 日期 | 事件 | 一致预期 | 真假 Alpha | 预期差判断 | 交易窗口 |
|---|---|---|---|---|---|
|  |  |  | 真 / 伪 / priced in / 反向 | 超预期概率 X% | 提前 N 天进 / T+0 减仓 |

## 四、技术分析
| 周期 | 现价 vs MA | RSI | MACD | 布林带 | 趋势 |
| 日线 | | | | | |
| 周线 | | | | | |

- **支撑位**：$X / $Y / $Z
- **阻力位**：$A / $B / $C
- **资金流（v1.4 强制四段）** ⭐：
  - 当前方向：净流入 / 流出 $XX M（真信号 / 噪音判定）
  - 内部人 6 月：净买入 / 卖出 $XX M（OpenInsider）
  - 13F 趋势：连续 X 季度增 / 减仓的大佬清单
  - 期权异常流：vol/oi > 5 的合约清单 + 行权价集中度
- **异动扫描**：[资金 / 衍生品 / 技术 三件套结果]

## 五、期权分析（仅美股 / 港股大蓝筹）
- ATM IV：XX%（IV Rank 第 YY 百分位）
- IV Term Structure：contango / backwardation
- Max Pain（最近月）：$XXX
- Put-Call Ratio：X.XX
- 异常期权流：[列举]

## 六、决策建议
### 6.1 正股策略（> 2 月）
- 方向：[BUY/HOLD/SELL/AVOID]
- 仓位：核心 X% / 试探 Y%
- 入场分档：[具体点位 + 仓位]
- 止损：[具体点位]
- 止盈：[具体点位 + 减仓比例]
- 关键检查点：[财报日 / 行业事件]
- 退出触发：[基本面 / 技术 / 估值三类]

### 6.2 期权策略（> 3 周）
- 策略类型：[具体]
- 合约腿（具体 strike + expiry + mid）
- Net Debit/Credit / Max Profit / Max Loss / Breakeven
- 入场限价 / 止盈止损 / Roll out 触发条件

## 七、综合评分卡
| 维度 | 评分 1-10 | 说明 |
|------|----------|------|
| 商业模式 / 护城河 | | |
| 财务质量 | | |
| 估值水平（越低估分越高） | | |
| 市场情绪 | | |
| 技术面 | | |
| 期权性价比 | | |
| **综合方向确信度** | | |

## 八、与项目记忆的联动
- A 股映射（若适用）：NVDA/AVGO → 中际旭创/新易盛/天孚通信
- BOM 渗透位置：[标的在 AI 算力链 BOM 中的角色]
- 真 Alpha 验证（Beta 中性框架）：[结论]
- 用户硬约束触发检查：RSI > 75 / 换手率 > 10% / PE > 150 小盘股准入
```

---

## 四、本地附加硬约束（继承中枢 Hard Rules）

> 公共规则统一继承 [投资分析中枢 §2](../investment-router/SKILL.md)：
> - §2.1 实时数据与时效（禁训练记忆 / 禁 24h 旧数据 / data_age 必填）
> - §2.2 数据源标注（_source、计算公式、多源冲突）
> - §2.3 MCP 故障与降级
> - §2.4/§2.5 时间锚 + 休市校验
> - §2.6 决策输出纪律（具体点位 + if-then + 失效条件）
> - §2.7 投资纪律（RSI > 75 / A 股换手率 / CPO 准入 / 流动性陷阱 / 真 Alpha 检验 / BOM 穿透）
> - §2.8 数据源可信度禁区（Finnhub 免费层 / 13F / 内部人 / 目标价）
>
> 本节只保留**个股深度分析**领域附加规则。

### 4.1 分析维度完整性
1. **六层框架与模式**：完整深度版必须六层全跑，缺数据则标 `❌ 数据缺失`，不得省略整层；快速决策版按 [一.A.4](#) 表跑必跑层；财报/对比版按对应模式输出。
2. **决策具体化叠加**：在中枢 §2.6 之上，单标的决策必须包含**护城河等级 + 估值百分位 + 真假 Alpha 判定**三件套。
3. **正股 + 期权双层**：美股标的必须给两套（除非用户明确弃权期权）；A 股不出期权策略（除非 ETF 期权特殊指定）。

### 4.2 个股研究强制清单
4. **13F 必查**：巴菲特 / Burry / 木头姐 / Pelosi / 桥水等大佬持仓变动，季度披露日历同步。
5. **管理层穿透**：CEO 任期 + 内部人交易（OpenInsider）+ Glassdoor + SEC 8-K 重大事件。
6. **真假 Alpha 矩阵**：必须给"真 Alpha 信号"与"伪 Alpha 信号"对照表，并给出当前判定。
7. **横向对比强制**：同板块至少给 3 家 peer 估值对比（PE / PS / PEG / EV/EBITDA），peer 池必须实时拉取。
8. **A 股个股专项**：必查换手率、PE > 100 自嗨预警、海外 BOM 关系、股权穿透（参考 A 股 Skill v1.5/v1.6）。

### 4.3 输出纪律
9. **禁止叙事漂移**：当用户对前期判断反问时，必须回到客观数据重检，禁止"为合理化结论而扭曲数据"。
10. **禁止 FOMO 表述**：不用"错过就晚了"、"必须立即买入"等情绪语。
11. **价格格式**：USD 两位小数；港股 HK$；A 股 ¥（继承中枢 §2.6 决策纪律）。

---

## 五、数据源映射表

| 数据 | 首选 | 备选 | 失败兜底 |
|------|------|------|---------|
| 实时报价 | `mcp_stock-quant_get_quote` | yfinance | WebSearch |
| 基本面 6 合 1 | `mcp_stock-quant_daily_brief` | — | — |
| 技术信号 | `mcp_stock-quant_get_signals` | — | — |
| 大盘环境 | `mcp_stock-quant_market_env` | — | — |
| 资金流 | `mcp_stock-quant_futu_capital_flow` | — | — |
| 板块 + Peers | `mcp_stock-quant_sector_analysis` | — | — |
| 异动扫描 | `mcp_stock-quant_full_anomaly_scan` | — | — |
| 新闻 + 社区情绪 | `mcp_stock-quant_sentiment_summary` | — | — |
| 期权链 | `mcp_stock-quant_get_option_chain` | — | — |
| 期权策略 | `mcp_stock-quant_option_decision` | — | — |
| 期权筛选 | `mcp_stock-quant_screen_options` | — | — |
| 财报日 | yfinance `Ticker.earnings_dates` | StockAnalysis.com | 公司 IR |
| 卖方目标价 | StockAnalysis.com | TipRanks / Finnhub recommendation_trends | WebSearch |
| 13F 持仓 | WhaleWisdom | Dataroma | unusualwhales |
| 国会议员交易 | unusualwhales / Capitol Trades | — | — |
| ARK 持仓 | ark-invest.com | — | — |
| 内部人交易 | OpenInsider | yfinance insider_transactions | — |
| 估值历史百分位 | StockAnalysis.com / Koyfin | yfinance | 计算 |
| Dark Pool | unusualwhales | — | WebSearch |
| 行业 / TAM 数据 | 公司 IR PPT | Gartner / IDC | WebSearch |
| **L1 产品 BOM** | SEC EDGAR 10-K segment | 巨潮年报 / `mcp_stock-quant_sentiment_summary` | WebSearch |
| **终端产品 BOM 拆解** | TechInsights / iFixit | SemiAnalysis | WebSearch |
| **L2 工艺 BOM / 专利** | USPTO Patent Search | Google Patents / Espacenet | WebSearch |
| **L2 工艺 BOM / 峰会** | OFC / ISSCC / Hot Chips / Computex 官网 | 公司 IR Day | WebSearch |
| **L3 产能 / 良率** | TrendForce | DigiTimes / Counterpoint | WebSearch |
| **L3 客户认证 / PO** | 公司 PR Newswire | 8-K / earnings transcript | WebSearch |
| **L3 地缘管制** | BIS Entity List（bis.doc.gov） | 商务部出口管制公告 | WebSearch |
| **客户切换成本** | earnings call Q&A | seekingalpha transcript | WebSearch 关键词组合 |
| **VCM 终端 ASP** | 终端厂商财报 | IDC / Gartner / Statista | WebSearch |
| **VCM 行业出货量** | IDC / Gartner / TrendForce | Counterpoint | WebSearch |

---

## 六、典型调用示例

### 示例 A：用户问"分析一下 NVDA"
**助手执行步骤**：
1. 立即输出时间锚 + 实时报价
2. 并行调用：
   - `mcp_stock-quant_daily_brief("NVDA")`
   - `mcp_stock-quant_market_env()`
   - `mcp_stock-quant_sentiment_summary("NVDA", market="US")`
   - `mcp_stock-quant_sector_analysis("NVDA")`
   - `mcp_stock-quant_full_anomaly_scan("NVDA")`
   - WebSearch："NVDA 13F latest [year] Berkshire ARK"
   - WebSearch："NVDA analyst target price StockAnalysis"
3. 按六层框架 + 输出模板逐章产出
4. 决策建议必须给出具体日期 / 点位 / 退出条件
5. 联动项目记忆：A 股映射（中际旭创 / 新易盛）+ BOM 渗透位置 + 真 Alpha 验证

### 示例 B：用户问"MU 现在能不能买"
**重点强化**：
- Layer 2 估值：HBM 周期 vs 历史 PE 百分位
- Layer 3 催化剂：财报日 + HBM3E 出货时间表
- Layer 6 决策：明确"BUY 至 $X / HOLD 至 $Y / SELL 跌破 $Z"

### 示例 C：用户问"中际旭创 还能拿多久"（A 股）
**特殊处理**：
- 跳过期权 Layer 5
- Layer 4 强制查换手率（用户硬约束）
- Layer 2 强制 PE > 150 警示（CPO 准入规则）
- 联动 NVDA / AVGO 海外前导信号（项目记忆）

---

## 七、版本与维护

- **v1.5 2026-05-31** §2.1 分部数据源契约：
  - 分部营收**必须来自 SEC 10-Q/10-K segment reporting 或 IR 原始披露**；禁止用总营收 × 占比反推；禁止凭记忆给数字
  - 强制每个分部数字注明数据来源文件 + 表号 / 页码
  - 10-Q 未发布时必须显式标 `⚠️ 估算` 并降级为 🟠 三级源
  - 输出模板 §二.2.1 分部表新增"数据源"列
- **v1.4 2026-05-31** 决策权重显式强化：
  - §2.1 营收质量改为**整体 + 分部双视角强制**（MSFT/NVDA/AAPL/SPCX/GOOGL 强制示例），输出模板 §二.2.1 同步分两表 + 主线分部识别 + §二.2.2 主线 Guidance 强制
  - §3.4 催化剂日历升级为**决策权重 Top 1**：强制 4 项输出（列表 / 真假 Alpha 标记 / 预期差 / 交易窗口）；输出模板 §三.3.4 表头扩展
  - §4.3 资金流向升级为**决策权重 Top 1-2**：强制"真信号 vs 噪音"对照表 + 输出模板 §四资金流改为四段（当前方向 / 内部人 / 13F / 期权异常流）
- **v1.3 2026-05-31** Layer 1 §1.E 强化：
  - 行数 3-6 → **5-10**，覆盖完整产业链而非只挑极值
  - 新增**A 股映射强制规则**：美股 / 港股标的必须至少 1 行 A 股供应链对照（卡脖子理论 + 跨市场套利）；A 股反向给海外对照
  - 新增**"打工人池"显式标记**：所有 ODM / 模组厂 / 同质化代工默认行尾标 `→ 打工人`，便于快速识别反差信号
  - 输出模板 §一.8 同步增加 A 股映射行 + 行数要求
- **v1.2 2026-05-31** Layer 1 简化：
  - **1.D 重写**：从"三层 BOM 穿透 + 4 项打分"简化为"产业链穿透（1 段描述 + 1 行核心瓶颈）"
  - **1.E 重写**：从"客户切换成本矩阵"重定义为"产业链各位置评级 & 利润分配打分"，1-5 分双维度，配角色映射（垄断者 / 共赢者 / 卡位打工人 / 周期搬运工）
  - **1.F 收敛**：原 VCM 定量逻辑并入 1.E 利润分配打分，不再单独输出
  - 输出模板 §一 7/8 改为简化版双行 + 三行表
- **v1.1 2026-05-31** Layer 1 增补：
  - 新增 §1.D 三层 BOM 穿透（L1 产品 / L2 工艺 / L3 瓶颈）+ 4 项打分（产能/技术/地缘/IP）
  - 新增 §1.E 客户切换成本矩阵（认证周期 / 切换成本 / 历史案例 / 二供份额 / 替代弹性）
  - 新增 §1.F 价值链分润 VCM（原拟放 Layer 2，已上移到 Layer 1 作为护城河量化的最后一环）
  - 新增数据源可信度分级表（🟢 一级 / 🟡 二级 / 🟠 三级 / 🔴 禁区）
  - 输出模板 §一 增补 7/8/9 三个子项；数据源映射表追加 BOM / 切换成本 / VCM 9 行
- **v1.0 2026-05-31** 初版：
  - 整合用户六大需求（商业模式 / 财务估值 / 市场情绪 / 技术 / 期权 / 决策建议）
  - 内嵌项目记忆硬约束（RSI 75 / 换手率 25% / CPO 准入 / 真 Alpha Beta 中性验证 / BOM 渗透）
  - 数据源映射表强调 OpenInsider / WhaleWisdom / StockAnalysis.com / unusualwhales
  - 决策建议强制"日期 + 点位 + 退出条件"三件套
  - 双层策略：正股 > 2 月 + 期权 > 3 周
  - Hard Ban #1：禁止训练记忆数字，必须实时拉取
- 维护者：用户 + Claude（每月校准估值百分位口径 + 13F 季度披露日历）
