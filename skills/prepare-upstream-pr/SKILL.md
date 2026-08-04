---
name: prepare-upstream-pr
description: 从指定功能点、文件或提交中提炼最小改动，基于 base/main 新建分支并发起正式上游 PR。用于用户要求将某个功能或 commit 贡献到上游、避免混入 fork 改动，或需要将草稿 PR 转为正式 PR 的场景。
---

# 基于 base/main 发正式上游 PR

将一个明确功能点或提交中的最小必要改动贡献到上游。默认约定：`base` 是上游远程仓库，`origin` 是可推送的 fork；PR 的目标为 `base/main`。

## 先确定范围

- 要求用户给出功能点、提交 SHA、文件或现有分支之一。范围仍不明确时，先询问，不要猜选其他改动。
- 读取项目的 `AGENTS.md` 和提交规则，检查 `git status -sb`。工作区含有无关改动时，不要执行 `git add -A` 或覆盖它们。
- 检查远程与基线：

```bash
git remote -v
git fetch base main
git fetch origin main
git log -1 --oneline base/main
```

- 查询同类开放 PR，避免重复提交：

```bash
gh pr list --repo <upstream-owner>/<repo> --base main --state open
```

## 提取最小补丁

提交不是天然的功能边界。先审查候选提交，再决定是否可直接复用：

```bash
git show --stat <commit>
git diff <commit>^ <commit> -- <候选路径>
```

- 提交只含目标功能时，可以采用等效补丁或谨慎 cherry-pick。
- 提交混有订单、页面、发布、配置或其他功能时，只手工补入目标代码和相应测试；不要整体 cherry-pick。
- 每一处变更都必须能追溯到用户选定的功能点。保留上游已有代码风格和行为。

## 从上游基线创建分支

从 `base/main` 创建新的主题分支，不得从 `origin/main`、发布分支或既有 PR 分支切出：

```bash
git switch -c agent/<short-feature-name> base/main
git merge-base --is-ancestor base/main HEAD
```

若当前工作区不能安全切换，使用独立 worktree；不要清理、还原或暂存用户的无关改动。

## 实现与验证

- 添加针对根因的最小回归测试，优先复用项目现有测试框架。
- 运行与改动相称的测试、语法检查或构建，并执行 `git diff --check`。
- 检查完整上游差异，而非只看最后一次提交：

```bash
git diff --stat base/main...HEAD
git diff --check base/main...HEAD
```

- 遵从项目提交规则：如要求版本和中文 `CHANGELOG.md`，只递增所属组件版本；如要求标签，确认标签未占用后推送到 fork。不要因为创建上游 PR 擅自部署生产环境。

## 提交、推送与正式 PR

- 仅暂存已核对的路径，使用简短且准确的提交信息。
- 将新分支推送到 `origin`，不要推送到 `base`：

```bash
git push -u origin agent/<short-feature-name>
```

- 创建跨 fork PR 时，明确指定上游仓库、目标分支与 fork 头分支。创建正式 PR，**不得添加 `--draft`**：

```bash
gh pr create \
  --repo <upstream-owner>/<repo> \
  --base main \
  --head <fork-owner>:agent/<short-feature-name> \
  --title '<类型>: <简短说明>' \
  --body-file <pr-body.md>
```

- PR 描述至少包括：变更内容、根因、用户影响、明确不涉及的范围、验证命令与结果。若断连或重启可能丢消息，写明可恢复的后续消息与不可恢复的断连窗口。
- 若 PR 已存在且为草稿，执行 `gh pr ready <number> --repo <upstream-owner>/<repo>`，不要重建 PR。

## 交付核验

回读 PR，确认它是打开的正式上游 PR：

```bash
gh pr view <number> --repo <upstream-owner>/<repo> \
  --json url,state,isDraft,baseRefName,headRefName,commits,changedFiles
```

完成条件：`state` 为 `OPEN`、`isDraft` 为 `false`、`baseRefName` 为 `main`，且变更与 `base/main...HEAD` 的审查结果一致。报告 PR 链接、分支、提交、验证结果和未验证边界。
