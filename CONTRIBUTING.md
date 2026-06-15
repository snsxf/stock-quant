# 开发规范

本文件约定 stock-quant 项目的协作与提交规范。

## Git 提交规范

### 核心约定：每个完整任务合成一个 commit 再推送

- **按「任务」提交，而不是按「改动次数」提交。** 一个完整任务（如「新增 NVDA 分析报告功能」「修复期权 Greeks 计算」）完成后，合成**一个语义清晰的 commit** 再 push。
- 中途探索性、半成品的改动可以在本地多次提交作为存档点，但**不单独推送到远端**。
- 目的：远端 `main` 保持一条干净的「功能时间线」，避免零碎、无意义的 commit 污染历史。

### 推送前整理零碎 commit

若本地积累了多个零碎 commit（如 `wip`、`fix typo`），在 push 前压成一个：

```bash
git reset --soft HEAD~N      # N = 要合并的 commit 数，保留改动
git commit -m "<语义化提交说明>"
git push
```

对刚提交、**尚未 push** 的小补丁，可并入上一个 commit：

```bash
git add .
git commit --amend --no-edit
git push
```

> ⚠️ 已经 push 到远端的 commit 不得擅自 `amend` 或 `reset` 重写历史。如确需重写，先确认无他人依赖该分支。

### Commit message 风格

采用语义化前缀：

- `feat:` 新功能
- `fix:` 修复 bug
- `docs:` 文档
- `refactor:` 重构（不改变行为）
- `chore:` 杂项 / 工程配置
- `test:` 测试

## 敏感信息

- **绝不提交**个人持仓、成本、账户、API Key 等敏感信息。
- 项目记忆文件（`~/.trae-cn/memory/`）含投资策略与会话历史，**不纳入版本控制**。
- `.env` 已在 `.gitignore` 中忽略；新增密钥一律走环境变量，不写入仓库。

## 分析报告产物

- 单标的分析报告存入 `reports/<TICKER>/<YYYY-MM-DD>_<TICKER>_<type>.md`，详见 [reports/README.md](reports/README.md)。
- 报告用 Markdown 编写，提交后可在 GitHub 网页端直接查看。
