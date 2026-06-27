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

### Layer 5：重点关注股池（用户定制版 v1.1）

#### A. 用户当前持仓（Holdings — 必须每日扫描，含 P&L 视角）
```
NVDA, GOOGL, ARM, MSFT, META, AMD, TSEM, SATS, MSTR, TLT, RKLB, OXY, VIX
```

| Ticker | 全称 | 类别 | 特殊处理规则 |
|--------|------|------|-------------|
| NVDA | NVIDIA | 半导体核心 / Mag7 | AI 算力链总龙头，财报夜（5/28）IV crush 警示 |
| GOOGL | Alphabet | Mag7 / AI 软件 | 关注反垄断进展 + Gemini / 搜索广告分化 |
| ARM | Arm Holdings | 半导体 / IP | 高 PE，IV 高，Beta 大；与 SoftBank 持股动态联动 |
| MSFT | Microsoft | Mag7 / AI 软件 | OpenAI 商业化进度 + Azure AI 增速 |
| META | Meta Platforms | Mag7 / 广告 | Reality Labs 烧钱 vs Reels 变现 |
| AMD | AMD | 半导体 / GPU 二号位 | MI350/MI400 节奏，AI 服务器份额 |
| **TSEM** | **Tower Semiconductor** | 半导体 / 模拟 / 代工 | ⚠️ 流动性弱，需查 spread；当 Intel-TSEM 收购曾流产历史背景 |
| **SATS** | **EchoStar** | 卫星通信 | ⚠️ 高波动 + 债务重组主题，关注与 RKLB / ASTS 联动 |
| **MSTR** | **MicroStrategy** | 加密代理 | 与 BTC/IBIT/COIN 同步看，溢价 / 折价 NAV 跟踪 |
| **TLT** | **iShares 20+Y Treasury** | 长债 ETF | 与 10Y/30Y 收益率反向；宏观对冲；查久期风险 |
| **RKLB** | **Rocket Lab** | 太空 / 国防 | Neutron 进度 + 收购 Mynaric，与 SATS / ASTS 同板块 |
| **OXY** | **Occidental Petroleum** | 能源 | Buffett 持股；与 WTI Beta 高相关；CCS 业务催化剂 |
| **VIX** | **CBOE 波动率指数（用户交易其期权）** | 风险对冲 / 期权策略 | ⚠️ VIX 现货不可交易；用户实际交易 **VIX 期权（VIX options，根的是 VIX 期货 `VX!`，而非 VIX 现货）**。**必须输出 VIX 现货 + VX 期货前 5 个月期限结构 + ATM IV / IVRank + Put-Call Ratio + 用户头寸的 Δ/Vega/Theta 暴露**。注意 VIX 期权结算特殊（AM-settled，三周三 8:30 ET 用 SOQ 结算），**不可错把 VIX 现货 IV 套到 VIX 期权**——VIX 期权波动率应看 VVIX。 |

**🔧 持仓特殊处理规则**：
1. 每只持仓必须显式给出"当前位置 vs MA20 / MA50"，并标注是否触发"破位硬止损"
2. **TLT**：与利率联动，必须同步给出 10Y/30Y 数值变化（不能只看股价）
3. **VIX 期权**（用户实际持仓）：
   - 标的根基：**VIX 期权根的是 VIX 期货合约（VX1!/VX2!...）**，每个到期月对应一根 VIX 期货
   - 必出三件套：① **VIX 现货** ② **VX 期货 1-5 月期限结构**（contango / backwardation） ③ **VVIX**（VIX 期权的隐含波动率）
   - **VIX 期权 IV 必须用 VVIX，不能套用 SPY 期权 IV 体系**
   - **结算特殊**：到期日为标的月份的"第三个周三前 30 天"那个周三，AM-settle 用 SOQ
   - **均值回归 Beta**：VIX 现货长期均值 ~19，超过 25 必给"卖 Call/Put 收割"思路；低于 14 必给"买 Call 抗黑天鹅"思路
   - **报告中标注用户头寸的方向**（多 Call / 多 Put / Spread / Short vol / Calendar）
4. **MSTR**：必须算 mNAV (= 市值 / 持有 BTC 市值)，溢价 > 2.0 警告，< 1.2 价值
5. **TSEM / SATS / RKLB**：流动性较弱，期权链稀薄，**禁止裸买深 OTM**
6. **OXY**：原油 Beta 1.5+，必须给 WTI 当日价 + 库存日历

#### B. 用户兴趣观察池（Watchlist — 按需启用，每日跑 DirectionScore）
```
MU, APP, SNDK, NASA, ASTS, CEG, BE, MRVL, AVGO, VRT, INTC, TSM, LITE, AAPL, GS, AXTI, SOXL, SPMO, GLD
```

| Ticker | 全称 | 主线 | 备注 |
|--------|------|------|------|
| MU | Micron | HBM3E / DRAM | 与 NVDA / AVGO BOM 强联动 |
| APP | AppLovin | AdTech / AI 广告 | 高 Beta，财报杀手 |
| SNDK | SanDisk | 闪存 / NAND | 2025 从西部数据分拆 IPO，注意流动性 |
| **NASA** | **Tema Space Innovators ETF (NYSEARCA: NASA)** | **太空主题 ETF** | ⚠️ ETF AUM、前十大持仓、SpaceX/SPV 权重、溢价/折价、IPO 催化必须实时查基金官网/ETF.com/公告，禁止沿用历史权重；若已持 RKLB/SATS/ASTS，需检查重复暴露 |
| **ASTS** | **AST SpaceMobile** | 卫星宽带 / NASA ETF 核心成分候选 | 与 RKLB / SATS / NASA ETF 同板块 Beta 极高；实际 ETF 权重必须实时查询；**手机直连卫星**主题；财报夜 IV crush 警告 |
| CEG | Constellation Energy | 核电 / AI 数据中心电力 | 与 VST / NRG / OKLO 同板块 |
| BE | Bloom Energy | 燃料电池 / 数据中心电力 | 高 Beta，AI 配套 |
| MRVL | Marvell | 定制 ASIC / 互联 | AVGO 之外的 ASIC 第二名 |
| AVGO | Broadcom | Mag7 边缘 / ASIC 龙头 | Google TPU / Meta 自研 ASIC |
| VRT | Vertiv | 数据中心散热 / 配电 | AI 配套黄金股，与沪电/工业富联映射 |
| INTC | Intel | 半导体老大哥 | 18A 进度 + 政府投资催化剂 |
| TSM | TSMC | 半导体代工龙头 | A 股算力链最强前导 |
| LITE | Lumentum | 光模块 / 激光 | 与新易盛/中际旭创海外映射 |
| AAPL | Apple | Mag7 | 关注 Apple Intelligence / 中国 iPhone 销售 |
| GS | Goldman Sachs | 投行 / XLF | 利率敏感，IPO 复苏代理 |
| AXTI | AXT Inc | III-V 化合物半导体 | 小盘高波动，与 InP/GaAs 周期 |
| **SOXL** | **3× 半导体多头 ETF** | 杠杆 | ⚠️ 杠杆 ETF：真实风险是**2-3倍放大的回撤/爆仓**，而非"必然磨损"。每日复利在单边趋势市是增益、在震荡市才是拖累（拖累量级 ≈ 0.5·L·(L-1)·σ²·天数，由标的波动率决定）。判断持有与否看标的趋势方向 + 波动率，禁止只用"路径损耗"一刀切劝退 |
| SPMO | Invesco S&P 动量 ETF | 因子 | 动量因子 vs SPY 跟踪误差监控 |
| GLD | SPDR 黄金 ETF | 避险 | 与 DXY 反向 + 央行购金主题 |

**🔧 太空主题专项规则**（NASA + ASTS + RKLB + SATS 联动板块）：
- 每次输出必须给"太空板块涨跌表"：**NASA / ASTS / RKLB / SATS / PL / LUNR / FLY**
- 关注 **SpaceX IPO / 融资 / 二级 SPV 重估时间窗**：必须实时查 Polymarket/新闻/基金公告，不得写死月份
- **NASA ETF 溢价/折价跟踪**：必须实时查 IOPV / NAV / 基金官网；溢价 > 3% 警告抢筹过热
- **避免重复加仓**：若已持 RKLB/SATS/ASTS，再买 NASA 可能等于变相加仓；重复暴露比例必须实时按 ETF 最新持仓计算

#### C. 输出时的优先级规则
1. **Holdings 区段**永远显示在 Watchlist 之前
2. 持仓部分必须给"今日盈亏温度计"（红 / 黄 / 绿）和"是否触发硬规则止损 / 移动止盈"
3. Watchlist 部分跑 DirectionScore Top N（N = 用户问题中提及的，默认 5）
4. **Mag7 / 半导体板块表**自动从持仓 + 观察池中抽取交集（NVDA / GOOGL / MSFT / META / AMD / AVGO / TSM / MU / MRVL / INTC / ARM / TSEM / VRT / LITE / AAPL）

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
### 5.A 持仓全扫（Holdings — 13 只）
- 表格：Ticker / 现价 / 当日 % / vs MA20 / vs MA50 / RSI14 / DirectionScore / 是否触发止损 / 操作建议
### 5.B 观察池打分（Watchlist — 18 只，DirectionScore 排序）
- Top Bullish 3 + Top Bearish 3
- 对最强多/空票自动跑期权策略
### 5.C 特殊标的快照
- **VIX 期权三件套**：① VIX 现货 ② VX 期货 1-5 月期限结构（contango/backwardation） ③ VVIX（VIX 期权 IV）；附用户头寸 Δ/Vega/Theta
- **MSTR mNAV**（市值/BTC 持仓市值）+ BTC 现价
- **TLT vs 10Y/30Y 收益率**（联动检查）
- **OXY vs WTI**（原油 Beta 检查）
- **太空板块**：NASA ETF / ASTS / RKLB / SATS / PL / LUNR / FLY（含 SpaceX IPO 倒计时）
- **MAG7 + 半导体交集表**：NVDA / GOOGL / MSFT / META / AMD / AVGO / TSM / MU / MRVL / INTC / ARM / TSEM / VRT / LITE / AAPL

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
- 维护者：用户 + Claude（每月一次校准 macro 日历窗口）
