---
name: "美股市场每日分析"
description: "作为投资分析中枢的子路径，处理美股大盘、宏观、VIX、利率、板块轮动和候选初筛。仅当中枢路由到本 Skill 或用户明确要求时调用。"
---

# 美股市场每日分析 Skill (US Market Daily Skill)

> 本 skill 用于在任何交易日（盘前 / 盘中 / 盘后 / 休市次日）对美股市场进行**结构化、量化、可执行**的全景分析，对标华尔街晨会纪要 + 量化研究所盘后日报。
> 适配用户画像：AI 算力链 Alpha 选手，深度依赖 Futu OpenD + yfinance + akshare + Finnhub 数据源，决策强调数据时效性和因子拆解。
> **公共规则继承**：本 Skill 继承 `投资分析中枢` 的公共 Hard Rules；若与本文件局部规则冲突，以 `投资分析中枢` 为准。

---

## 一、调用条件（Trigger Conditions）

**必须立即调用本 skill 的场景**：

1. 用户问 "分析一下今天的美股市场" / "今天美股怎么样" / "美股盘前/盘中/盘后看一下"
2. 用户问 "标普 / 纳指 / 道指 / 罗素 现在什么状态"
3. 用户问 "VIX 是不是要爆" / "现在风险是不是上来了"
4. 用户问 "半导体 / Mag7 / AI / 银行股 / 能源 今天怎么样"
5. 用户问 "美股值不值得追" / "今天该买啥"
6. 用户在 A 股开盘前问 "海外昨晚怎么走的"（用于 A 股映射决策）
7. 用户上传美股持仓但**没有具体标的列表**，要求大盘环境判断

**禁止跳过的前置动作**：
- 必须先确认 `(now_et, market_phase, data_age)`，标注盘前 / 盘中 / 盘后 / 休市
- 必须用 `mcp_stock-quant_market_report` 跑一次大盘 + 默认股票池（拿 DirectionScore + 推荐策略）
- 必须单独拉 `VIX / VVIX / DXY / TNX(10Y)` 等宏观风险锚（VIX 走 yfinance fallback）
- **禁止直接给结论**，所有"排名 / 排序"必须基于实时 peer cohort 数据，禁用 LLM 训练记忆 inference

---

## 一.A、职责边界与路由优先级（统一规则）

### 1. 全局路由优先级

> 全局优先级矩阵 P0–P4 统一定义在 [投资分析中枢 §3](../investment-router/SKILL.md)。本 Skill 仅声明在该矩阵中的角色：

| 场景 | 本 Skill 处理方式 |
|---|---|
| 问美股大盘/盘前/盘中/盘后/标普/纳指/板块/市场怎么样 | **主导**，输出宏观/指数/板块/风险温度 |
| 问"哪几个标的值得深挖 / 今天候选交易" | **主导初筛**，单股估值/期权 strike 交给个股或持仓 Skill |
| 出现持仓截图、成本、期权腿、Roll/止损/止盈 | **让渡** `持仓分析`，仅提供大盘/波动率背景 |
| 单只美股估值/护城河/能否买入，无持仓 | **让渡** `个股深度分析`，仅提供市场环境 |
| 问"昨夜美股影响今天/明天 A 股吗" | **协同** `A股市场每日分析`，本 Skill 提供海外前导信号 |

### 2. 本 Skill 的接管范围

- ✅ 接管：美股大盘、指数、宏观、VIX/VVIX、利率、美元、板块轮动、风险偏好温度。
- ✅ 接管：Mag7、半导体、AI 软件、金融、能源等板块的相对强弱和候选标的初筛。
- ✅ 接管：海外前导信号总结，为 A 股 Skill 提供 NVDA/TSM/AVGO/SMH/SOXX 映射依据。
- ❌ 不接管：用户给出自己的持仓成本、仓位、盈亏、期权腿，必须转 `持仓分析`。
- ❌ 不接管：用户只问单个公司基本面/估值/护城河，必须转 `个股深度分析`。

### 3. 与其他 Skill 的协同方式

- 与 `持仓分析`：本 Skill 输出 VIX/利率/板块环境，持仓分析负责最终仓位、止损、止盈、Roll 和 EV。
- 与 `个股深度分析`：本 Skill 可筛出 Top Bullish/Top Bearish，但具体公司研究和期权合约细化由个股 Skill 主导。
- 与 `A股市场每日分析`：本 Skill 提供海外前导信号；A 股开盘映射、BOM 审计和操作节奏由 A 股 Skill 主导。
- 输出时必须说明：“本轮主 Skill = 美股市场每日分析；协同引用 = [如有]”。

---

## 二、四层分析框架

### Layer 1：时间锚 & 市场阶段（必填，禁止省略）

输出第一行**强制**包含：
```
(now_et: YYYY-MM-DD HH:MM ET, market_phase: pre-market | regular | after-hours | closed-holiday | closed-weekend, data_age: 实时 / 延迟15min / 节前最后一个交易日收盘)
```

**特殊场景**：
- 休市日（Memorial Day / Thanksgiving / Christmas / Independence Day / MLK / Presidents / Good Friday / Juneteenth / Labor Day）必须显式标注，并将分析视角切到"节前最后一日 + 节后第一日预演"
- 半日市（Black Friday / Christmas Eve）13:00 ET 收盘必须提示
- 期权三重 / 四重见证日（每季三个月的第三个周五）必须提示对冲压力

### Layer 2：大盘指数全景（5 大指数 + ETF 强弱）

| 类别 | 标的 | 必查指标 |
|------|------|---------|
| 核心指数 | **SPY (S&P 500)** | 现价、当日涨跌、日内 OHLC、成交额、与 200MA 距离、52w 高低距离 |
| 核心指数 | **QQQ (Nasdaq 100)** | 同上 + QQQ/SPY 比值（成长/价值切换信号） |
| 核心指数 | DIA (Dow 30) | 同上（用于价值轮动判断） |
| 核心指数 | IWM (Russell 2000) | 同上（小盘股 = 风险偏好温度计） |
| 板块 ETF | XLK / XLF / XLE / XLV / XLY / XLP / XLI / XLU / XLRE | 当日涨跌（用于轮动表） |
| 半导体 | **SMH / SOXX** | 单日涨幅（>= +3% 或 <= -3% 触发"主线信号"） |
| 中概 | KWEB / FXI | 与 A/H 股联动判断 |

输出形式：**Markdown 表格 + 板块强弱热力轮动条**（领涨 / 领跌 / 跑输 / 跑赢）。

### Layer 3：宏观环境（政策 / 情绪 / 资金 / 利率）

| 维度 | 必查项 | 数据源 |
|------|--------|--------|
| **风险情绪** | VIX / VVIX / SKEW / Put-Call Ratio | yfinance + CBOE |
| **利率** | 10Y (^TNX) / 2Y (^IRX) / 30Y / 2s10s 利差 | yfinance |
| **美元** | DXY / EUR/USD / USD/JPY | yfinance |
| **大宗** | WTI (CL=F) / BRENT / Gold (GC=F) / Copper (HG=F) / BTC | yfinance |
| **政策日历** | 当日 + 未来 3 日：FOMC 议息 / FOMC 会议纪要 / 鲍威尔讲话 / 非农 / CPI / PCE / 零售销售 / GDP / PPI / 消费者信心 / ISM | Trading Economics / 财联社 / akshare |
| **政治** | 国会立法窗口 / 政府关门倒计时 / 关税 / 制裁名单 | WebSearch |
| **资金** | ICI 共同基金流 / ETF 净申购 / 北水南下 / 期权 Dark Pool 净 Delta | Futu / akshare |
| **情绪** | AAII Bull-Bear / NAAIM 暴露指数 / Fear & Greed Index | CNN F&G |

**输出原则**：
- VIX 区间映射：`< 13 极低 / 13-18 calm / 18-22 中性 / 22-30 警戒 / > 30 恐慌`
- VVIX > 100 提示尾部风险溢价上行；VIX 期限结构倒挂（VIX > VIX3M）= 高警戒
- 利率：10Y 突破 4.5% 对成长股估值压力；2s10s 倒挂 / 解倒挂的相对位置必须报
- DXY > 105 对成长股不利、对大宗与新兴市场不利
- 政策：每个事件必须给"市场预期" + "若超预期 / 不及预期"双向情景

### Layer 4：重点板块剖析（强制 5 板块 + 可选）

#### 强制 5 板块
1. **半导体（Semis）**：SMH / SOXX 涨跌 + 龙头：NVDA / TSM / AMD / AVGO / ASML / MU / AMAT / LRCX / KLAC
2. **MAG7（七巨头）**：AAPL / MSFT / GOOGL / AMZN / META / NVDA / TSLA → 给出"七雄涨跌表"+ 等权 vs 加权对比
3. **金融（Financials）**：XLF + JPM / BAC / GS / MS（监测利率敏感度）
4. **能源（Energy）**：XLE + XOM / CVX / OXY（监测原油 Beta）
5. **AI/软件**：PLTR / SNOW / CRM / ORCL / NOW（高 Beta 风险偏好温度计）

#### 可选板块（按主线轮动加挂）
- 中概股：KWEB / BABA / PDD / BIDU / JD
- 生物医药：XBI / IBB + LLY / NVO（GLP-1 主题）
- 防御板块：XLP / XLU
- 加密敞口：COIN / MSTR / IBIT / MARA
- 工业 / 国防：LMT / RTX / NOC / GE
- **中国 AI 算力链海外映射**（项目记忆强相关）：NVDA / AMD / TSM / AVGO 是 A 股工业富联 / 澜起 / 沪电 / 新易盛 的直接前导信号

### Layer 5：重点关注股池（用户定制版 v1.5 — 2026-07-01 更新）

#### A. 用户当前持仓（Holdings — 必须每日扫描，含 P&L 视角）

**列表按权重降序排列**（第一位权重最高）：
```
NVDA, GOOGL, DRAM, AVGO, MSFT, META, SNDK, MRVL, SPCX
```

| # | Ticker | 全称 | 类别 | 特殊处理规则 |
|---|--------|------|------|-------------|
| 1 | **NVDA** | NVIDIA | 半导体核心 / Mag7 / GPU 龙头 | AI 算力链总龙头；财报夜 IV crush 警示；**硬止损锚 192.13**（跌破减仓 50%） |
| 2 | **GOOGL** | Alphabet | Mag7 / AI 软件 / GCP 云 | 关注反垄断进展 + Gemini / 搜索广告分化；GCP 属"Hyperscaler 双身份"，Meta 出租算力事件后可能跟进 |
| 3 | **DRAM** | **Roundhill Memory ETF** ✅ | 半导体 / 存储主题 ETF（**真实标的**） | ✅ **Futu OpenD 已确认真身**：US.DRAM = Roundhill 发行的存储主题 ETF（2025+ 发行的新品，训练记忆不覆盖，禁止用记忆判断）。**分析时直接查 US.DRAM 报价 + 技术面**，配合底层重仓（MU / 三星 ADR / 海力士 ADR / SNDK）做联动。禁止再当作"陌生 ticker"或"用户昵称"处理。 |
| 4 | **AVGO** | Broadcom | 半导体 / ASIC 龙头 / Mag7 边缘 | Google TPU / Meta 自研 ASIC 定制芯片供应商；相对 NVDA 抗跌（Meta 出租算力事件中 -1.94% vs NVDA -1.57%）；PE 60x 高估值敏感 |
| 5 | **MSFT** | Microsoft | Mag7 / AI 软件 / Azure 双身份 | OpenAI 商业化进度 + Azure AI 增速；Copilot 变现；**Azure 潜在跟进算力出租** = 双身份估值机会 |
| 6 | **META** | Meta Platforms | Mag7 / 广告 + **算力出租** 双引擎 | ⭐ 2026-07-01 事件重估：从"最大 GPU 买方"变为"GPU 供应商"，商业模式重定价；关注 Reality Labs 烧钱 vs Reels 变现 + 出租收入分部披露 |
| 7 | **SNDK** | SanDisk | 闪存 / NAND | 2025 从西部数据分拆 IPO，注意流动性；与 HBM/DRAM 主题弱相关；⚠️ **不要与 DRAM 混淆分类**（NAND ≠ DRAM，SNDK 是 NAND） |
| 8 | **MRVL** | Marvell | 定制 ASIC / 互联 / 光模块 | AVGO 之外的 ASIC 第二名；AI 交换机与光模块受益；与 AVGO 存在**份额博弈**（客户订单排他） |
| 9 | **SPCX** | **Space Exploration Technologies Corp (SpaceX)** ✅ | 太空 / 商业航天 / AI 垂直整合 | ✅ **已 IPO 上市**：SpaceX 于 **2026-06-12** 在纳斯达克上市，ticker = SPCX，发行价 $135，首日收盘 $160.95（+19.22%），IPO 募资 $75B（史上最大），估值 $1.77T。**分析时直接查 US.SPCX 报价 + 技术面**，配合 DXYZ / RKLB / ASTS 作板块 Beta 参照。禁止再当作"未上市"或"清盘 ETF"处理（此判断源于 2023 年过时训练记忆，已违反 §2 硬约束）。 |

**🔧 持仓特殊处理规则**：

1. **每只持仓必须显式给出"当前位置 vs MA20 / MA50"**，并标注是否触发"破位硬止损"
2. **NVDA 硬止损**：跌破 192.13 强制减仓 50%（项目记忆持续锚点）
3. **半导体主线权重高**：NVDA / AVGO / MRVL / SNDK / DRAM 五只均属半导体或存储，**总持仓半导体敞口约占 5/9 ≈ 55%**，一旦触发"SOXX 单日 ≥ 5%"或"SMH 破 20MA"等主线信号，必须给"整仓 Beta 冲击"总量估算，禁止只看单股
4. **DRAM = Roundhill Memory ETF**（v1.7 修正）：真实 ticker，Cboe BZX 上市 2026-04-02，AUM ~$24B，重仓 Samsung / SK Hynix / Micron / SNDK / Kioxia / WDC / STX；分析时**直接查 US.DRAM**，配合底层原厂做联动
5. **SPCX = SpaceX 正股**（v1.7 修正）：真实 ticker，2026-06-12 IPO，Nasdaq 上市，发行价 $135；分析时**直接查 US.SPCX**，配合 DXYZ / RKLB / ASTS 做板块 Beta 参照
6. **⚠️ 训练记忆红线**：涉及 2025-08 之后新发行/新上市的产品（DRAM / SPCX / RAM 等），**必须先跑 OpenD 或 WebSearch 验证**，禁止用"我记得 2023 年 XX 已清盘 / XX 未上市"这类**过时训练记忆**判断——这条已经违反过两次，教训见 v1.7
7. **AVGO + MRVL 相关性**：两者均为 ASIC 玩家，客户订单存在替代关系，**净敞口 = |AVGO 权重 - MRVL 权重|**（对冲后剩余），不能简单相加
8. **META 双引擎特别监测**：算力出租收入分部披露前，每次财报季必须查是否有"Compute Rental / Infrastructure Services"新分部；出租收入 > $5B 年化 = 强利好，< $1B = 逻辑证伪
9. **不再持仓的老标的清单归档**（v1.4 前旧持仓）：ARM / AMD / TSEM / SATS / MSTR / TLT / RKLB / OXY / VIX 已于 2026-07-01 从 Holdings 清出。**新版分析不再默认扫描这些标的**；但若用户明确询问，仍可查询报价并给上下文

#### B. 用户兴趣观察池（Watchlist — v1.5 精简为 2 只，按需启用）

**列表**：
```
TSM, AAPL
```

| # | Ticker | 全称 | 主线 | 备注 |
|---|--------|------|------|------|
| 1 | **TSM** | TSMC 台积电 | 半导体代工龙头 | A 股算力链最强前导信号；给 NVDA/AMD/AVGO/MRVL 全部代工；52w 高 479 关注回踩支撑；**从持仓端出，进观察池 = 用户认为已充分间接暴露（AVGO+MRVL+NVDA 都是 TSM 客户）** |
| 2 | **AAPL** | Apple | Mag7 / 消费电子 | 关注 Apple Intelligence / 中国 iPhone 销售；相对 Mag7 其它成员**独立性强**（无 GPU 云业务，不受 Meta 算力出租直接冲击）；2026-07-01 盘中 +2.13% 意外独强 |

**🔧 观察池处理规则**：
1. 观察池只跑 DirectionScore + 报价快照，**不占用 5.A 持仓深扫的分析额度**
2. TSM 与 NVDA/AVGO/MRVL 强相关（BOM 上游），当 TSM 破位 20MA 时，持仓端所有半导体股必须给二次冲击评估
3. AAPL 作为持仓组合的"非 AI Capex 对照锚"，帮助判断当日下跌是**板块问题**还是**大盘系统问题**

#### C. 输出时的优先级规则
1. **Holdings 区段**永远显示在 Watchlist 之前
2. 持仓部分必须给"今日盈亏温度计"（红 / 黄 / 绿）和"是否触发硬规则止损 / 移动止盈"
3. **Holdings 显示顺序按权重降序**（用户 v1.5 已明确排序：NVDA > GOOGL > DRAM > AVGO > MSFT > META > SNDK > MRVL > SPCX）
4. Watchlist 部分跑 DirectionScore Top N（N = 用户问题中提及的，默认 2 即全池）
5. **Mag7 / 半导体板块表**自动从持仓 + 观察池中抽取交集（NVDA / GOOGL / MSFT / META / AVGO / MRVL / SNDK / TSM / AAPL）
6. **DRAM / SPCX 均为真实 ticker**（v1.7 修正）：DRAM = Roundhill Memory ETF（2026-04-02 上市），SPCX = SpaceX 正股（2026-06-12 IPO）；**直接查 US.DRAM / US.SPCX 报价**，禁止再用"数据缺口"或"用户昵称"处理

#### D. 每只标的必查 6 字段：
| 字段 | 说明 |
|------|------|
| Spot + Pre/After | `mcp_stock-quant_get_quote` |
| MA / RSI / MACD / Boll | `mcp_stock-quant_get_signals` |
| DirectionScore | `mcp_stock-quant_market_report` 输出 |
| 资金流 | `mcp_stock-quant_futu_capital_flow` |
| IV Regime | `mcp_stock-quant_daily_brief` |
| 推荐策略 | `mcp_stock-quant_option_decision` |

---

## 三、输出模板（强制结构）

```markdown
# 🇺🇸 美股市场分析（YYYY-MM-DD）

> 本轮主路径：美股市场每日分析
> 协同路径：<无 / 个股深度分析（Top标的深挖） / A股市场每日分析（次日映射） / 持仓分析（账户决策）>
> 数据状态：<实时 / 延迟15min / 上一交易日收盘 / 节前最后一个交易日收盘 / 数据缺失>
> 时间锚：<now_et, market_phase: pre-market/regular/after-hours/closed-weekend/closed-holiday, data_age>
> 数据源声明：Futu OpenD + yfinance + akshare + Finnhub（免费层仅信任 recommendation_trends + company_news）

## 一、大盘指数全景
（SPY / QQQ / DIA / IWM / SMH / SOXX / 板块 ETF 表格）

## 二、宏观环境
- 利率 / 美元 / 大宗
- VIX / VVIX / Put-Call / SKEW
- 政策日历（今日 + 未来 3 日）
- 情绪指标（AAII / F&G / NAAIM）

## 三、重点板块剖析
1. 半导体（SMH/SOXX + 龙头表）
2. MAG7（七雄涨跌表）
3. 金融 / 能源 / AI 软件
4. 当日主线板块（动态）

## 四、风险温度计
- VIX 等级 + 期限结构（contango / backwardation）
- 利率风险
- 政策黑天鹅清单

## 五、重点股票池
### 5.A 持仓全扫（Holdings — 9 只，按权重排序）
- 表格：Ticker / 现价 / 当日 % / vs MA20 / vs MA50 / RSI14 / DirectionScore / 是否触发止损 / 操作建议
### 5.B 观察池打分（Watchlist — 2 只，DirectionScore 排序）
- Top Bullish 3 + Top Bearish 3
- 对最强多/空票自动跑期权策略
### 5.C 特殊标的快照（v1.5 精简，聚焦当前 9 只持仓 + 2 只观察）
- **META 双引擎监测**：广告收入增速 + **算力出租收入分部披露**（若已披露，给年化 run-rate；若未披露，给管理层沟通线索）
- **AVGO vs MRVL ASIC 份额博弈**：给 Google TPU / Meta 自研 ASIC / Amazon Trainium 三大客户的最新订单归属
- **DRAM = Roundhill Memory ETF**：真实 ticker，直接查 US.DRAM 报价 + 技术面，配合底层重仓（Samsung / SK Hynix / MU / SNDK）做联动分析
- **SPCX = SpaceX 正股**（2026-06-12 Nasdaq IPO）：真实 ticker，直接查 US.SPCX 报价 + 技术面，配合 DXYZ / RKLB / ASTS 做板块 Beta 参照
- **SNDK NAND vs DRAM 分类澄清**：SNDK 是 NAND 闪存，不要混入 DRAM 主题
- **NVDA 硬止损锚 192.13**：每日必查（跌破减仓 50%）
- **MAG7 + 半导体交集表**：NVDA / GOOGL / MSFT / META / AVGO / MRVL / SNDK / TSM / AAPL（持仓+观察池交集）

## 六、总结评分卡
| 维度 | 评分 1-10 | 说明 |
|------|----------|------|
| 多空方向确信度 | | |
| 技术面信号强度 | | |
| 基本面支撑力度 | | |
| 期权策略性价比 | | |
| 整体风险收益比 | | |

## 七、明日（或下一交易日）行动清单
1. 首选交易（含具体 ticker / strike / 到期日）
2. 次选交易
3. 观望禁区（不追的板块 / 标的）
4. A 股 / 港股映射操作（项目记忆联动）
```

---

## 四、本地附加硬约束（继承中枢 Hard Rules）

> 公共规则统一继承 [投资分析中枢 §2](../investment-router/SKILL.md)：
> - §2.1 实时数据与时效；§2.2 数据源标注；§2.3 MCP 故障与降级；§2.4 时间锚；§2.5 休市与交易制度；§2.6 决策输出纪律；§2.7 投资纪律；§2.8 数据源可信度禁区（含 Finnhub 免费层禁区）。
>
> 本节只保留**美股市场每日分析**领域附加规则。

### 4.1 美股市场特有
1. **排名禁忌**：mega-cap 排名（市值 / 营收）必须实时拉 peer cohort，禁用训练记忆。
2. **VIX < 13 警告**：极低波动 = 复杂度堆积，必须提示 "long volatility / 买保险" 思路。
3. **追高禁令**：板块 ETF 单日 ≥ 5%（如 SOXX +5.66% 实例）必须给出"次日均值回归概率 ~65%"的反向警示。
4. **VIX 期权三件套**：用户持有 VIX 期权时必须输出 VIX 现货 + VX 期货 1-5 月期限结构 + VVIX，**禁止用 SPY 期权 IV 套到 VIX 期权**。
5. **三重/四重见证日**：每季三个月的第三个周五必须显式提示对冲压力与异常成交。

### 4.2 跨市场映射强制
6. **A 股映射**：项目记忆中标识为 S/A 级 Alpha 资产（工业富联 / 澜起 / 新易盛 / 沪电）必须在第七章"映射操作"中给出对应触发条件。
7. **海外前导信号导出**：当 A 股 Skill 反向引用时，必须以 SMH/NVDA/TSM/AVGO 隔夜收盘 + 盘后异动作为映射依据。

### 4.3 格式约束
8. **价格格式**：所有价格保留两位小数；A 股用 ¥；港股可 ¥/HK$；美股 USD。
9. **Python 列表格式**：数字不带引号，字符串用引号（项目偏好）。

---

## 五、数据源映射表（避免反复试错）

| 数据 | 首选 | 备选 | 失败兜底 |
|------|------|------|---------|
| 美股实时报价 | `mcp_stock-quant_get_quote` (futu) | yfinance | WebSearch |
| 盘前盘后 | Futu `pre_market/after_market` 字段 | yfinance `pre/post` | — |
| 期权链 | Futu OpenD | — | — |
| 期权 Greeks | `mcp_stock-quant_calc_greeks` | — | — |
| VIX | yfinance `^VIX` | — | — |
| 利率 | yfinance `^TNX/^IRX/^TYX` | — | — |
| 财报日 | yfinance `Ticker.earnings_dates` | StockAnalysis.com | **禁用 Finnhub** |
| 内部人交易 | yfinance `insider_transactions` | OpenInsider | **禁用 Finnhub** |
| 分析师目标价 | StockAnalysis.com | TipRanks | **禁用 Finnhub price_target** |
| 公司新闻 | Finnhub `company_news` ✅ | yfinance `Ticker.news` | WebSearch |
| 评级变动 | Finnhub `recommendation_trends` ✅ | — | — |
| 政策日历 | Trading Economics | 财联社 | 巨潮 |
| 情绪指标 | CNN Fear & Greed | AAII | NAAIM |

---

## 六、典型调用示例

**用户**: "分析一下今天的美股市场"

**助手执行步骤**：
1. 立即写出 `(now_et, market_phase, data_age)` 时间锚
2. 并行调用：
   - `mcp_stock-quant_market_report()` → 拿大盘 + Top 12 股池打分
   - `mcp_stock-quant_get_quote("US.VIX")` / `get_history("^VIX")` → VIX 状态
3. 按"输出模板"七章输出
4. 第七章必须给"明日行动清单"，含 A 股映射（项目记忆联动）

---

## 七、版本与维护

- v1.0 2026-05-26 初版（基于 Memorial Day 节前美股复盘对话沉淀）
- v1.1 2026-05-26 加入用户定制持仓（13 只）+ 观察池（18 只）+ 特殊标的处理规则（VIX/MSTR/TLT/SOXL）
- v1.2 2026-05-26 修正 VIX→VIX 期权交易逻辑（VX 期货+VVIX，区别 VXX）；NASA→Tema Space Innovators ETF（含 ASTS/RKLB/SATS 重仓），ASTS 独立列出；新增"太空板块"快照
- v1.3 2026-05-26 新增硬约束 #11（MCP 不可用必须立即提醒重启，禁止用缓存/记忆装实时）+ #12（休市日必须用日历校验，禁用记忆，Memorial Day=5月最后周一）
- **v1.4 2026-05-28 项目迁移**：从 `/Users/bytedance/Documents/trae_projects/test/.trae/skills/` 迁入 `stock-quant` 项目；跨链路径已更新
- **v1.5 2026-07-01 持仓换仓大改**：
  - Holdings 从 13 只精简为 9 只：`NVDA, GOOGL, DRAM, AVGO, MSFT, META, SNDK, MRVL, SPCX`（按权重降序）
  - Watchlist 从 18 只精简为 2 只：`TSM, AAPL`
  - 清仓归档：ARM / AMD / TSEM / SATS / MSTR / TLT / RKLB / OXY / VIX
  - 新持仓：DRAM（用户声明存储 ETF，⚠️ 待澄清 ticker）、SNDK（NAND）、MRVL（ASIC 二号）、SPCX（用户声明 SpaceX 代理，⚠️ SpaceX 未上市，待澄清工具）
  - 新增 DRAM / SPCX 数据缺口硬提示规则
  - 5.C 特殊标的快照重写，去掉 VIX 三件套 / MSTR mNAV / TLT / OXY / 太空板块（对应持仓已清空），新增 META 双引擎监测 + AVGO vs MRVL ASIC 博弈
- **v1.6 2026-07-02 昵称映射修正（后被 v1.7 推翻）**：错误地把 DRAM/SPCX 认成用户自定义昵称，实际上两者都是真实 ticker
- **v1.7 2026-07-02 训练记忆红线事件**（⚠️ **重大教训**）：
  - **触发**：用户质疑"SPCX 6.12 已上市，你为什么用清盘 ETF 判断，是不是违反了禁止用过时训练数据规则？"
  - **根因**：训练数据截止 2025-08，之后发生的两大事件均未覆盖：
    1. **DRAM = Roundhill Memory ETF**（2026-04-02 上市，Cboe BZX，AUM $24B，重仓三星/海力士/美光）
    2. **SPCX = SpaceX 正股**（2026-06-12 Nasdaq IPO，发行价 $135，首日收盘 $160.95，估值 $1.77T，史上最大 IPO）
  - **v1.5 和 v1.6 均违反 §2 硬约束**：拿 2023 年"Tuttle SPAC ETF 已清盘 / SpaceX 未上市"的记忆装 2026 年现实
  - **修正**：Holdings 第 3/9 行改为真实 ticker 定义；持仓规则 #4/#5 更新为直接查 US.DRAM / US.SPCX；**新增规则 #6 训练记忆红线**（2025-08 后新品必须先跑 OpenD 或 WebSearch 验证）
  - **教训（永久约束）**：**任何"某标的已清盘/未上市/不存在"的判断，必须先跑 OpenD get_quote 验证有无实时报价 + WebSearch 验证最新 IPO/退市公告**，禁止直接用训练记忆下结论
- 维护者：用户 + Claude（每月一次校准 macro 日历窗口）
