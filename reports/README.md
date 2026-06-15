# reports/ — 分析报告产物目录

存放针对单只标的的分析报告，按 ticker 分子目录，文件名带日期前缀，便于版本留痕与 GitHub 网页端查看。

## 目录约定

```
reports/
└── <TICKER>/
    ├── <YYYY-MM-DD>_<TICKER>_<type>.md   # 分析报告正文（Markdown）
    └── assets/                            # 图表等附件（可选）
```

示例：

```
reports/
└── NVDA/
    ├── 2026-05-31_NVDA_deep-dive.md
    └── assets/
        └── 2026-05-31_price.png
```

## 命名规范

- 日期用 `YYYY-MM-DD`，排序友好。
- `<type>` 取值：`deep-dive` / `daily-brief` / `option-decision` / `holdings`。
- 报告用 Markdown 编写，GitHub 网页端可直接渲染。

> 注意：本目录是「分析产物」，与源码 `src/stock_quant/reports/`（报告**生成逻辑**）是两回事。
