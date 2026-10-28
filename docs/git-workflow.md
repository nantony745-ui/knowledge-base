# 本地修改、提交与推送

当前操作对象是 `comingsoon` 中的独立仓库，远端为 `https://github.com/nantony745-ui/knowledge-base.git`，远端别名是 `origin`。外层开发仓库与它拥有独立的分支、暂存区和历史。在外层执行 `git add` 不会提交这里的修改。

| 阶段 | 命令 | 含义 |
| --- | --- | --- |
| 工作区 | 编辑文件、`git diff` | 修改尚未进入提交 |
| 暂存区 | `git add` | 选择下一次提交包含哪些修改 |
| 本地历史 | `git commit` | 保存为本地提交，尚未上传 |
| 远端分支 | `git push` | 上传本地提交，不会自动合入 main |

## 1. 确认仓库与分支

在外层项目目录进入：

```bash
cd comingsoon
git status --short
git branch --show-current
git remote -v
```

本次分支应为 `feat/model-settings`。以后开发新的功能时，先保持工作区干净，再从更新后的 main 创建新分支：

```bash
git switch main
git pull --ff-only origin main
git switch -c feat/新功能名
```

本次已创建分支，不需要重复创建。

## 2. 验证并检查修改

按 README 的开发与验证命令运行测试，再检查：

```bash
git diff --check
git diff --stat
git diff
```

`.env`、`.env.langfuse`、`.env.models`、用户数据和运行产物由 `.gitignore` 忽略。可以用 `git check-ignore -v .env.models` 核对规则。

## 3. 选择文件并形成提交

本次只修改了以下代码、测试与文档目录：

```bash
git add app frontend tests README.md docs
git diff --cached --stat
git diff --cached
git commit -m "feat: add model settings and connection checks"
git status --short
```

`git diff` 查看未暂存改动，`git diff --cached` 查看下一次提交的内容。提交后工作区应干净。以后按实际修改选择文件，不固定套用本次的暂存目录。

## 4. 获取远端状态并推送功能分支

```bash
git fetch origin
git log --oneline HEAD..origin/main
```

第二条命令没有输出，说明已包含当前远端 main 的全部提交，可以推送：

```bash
git push -u origin feat/model-settings
git branch -vv
git status -sb
```

`-u` 将本地分支关联到 `origin/feat/model-settings`；之后在该分支新增提交，可直接运行 `git push`。如果提示登录或权限不足，先完成 GitHub 认证。

如果 `HEAD..origin/main` 有输出，说明远端主线有本分支尚未包含的提交。先在功能分支执行 `git merge origin/main`，处理冲突并重新测试，再提交合并结果、推送。普通合并成功时会自动生成合并提交；有冲突时先修正文中冲突，再 `git add` 相关文件并 `git commit`。

## 5. 发起审查，确认后再合并

推送只更新功能分支，main 不变。打开：

<https://github.com/nantony745-ui/knowledge-base/compare/main...feat/model-settings>

创建 Pull Request，选择 `base: main`、`compare: feat/model-settings`，说明问题、功能变化与实际测试结果。当前模型设置功能先保留在分支，等人工验收确认后再合并。

采用 GitHub 的 **Create a merge commit** 合并方式，可以保留功能分支原始提交。远端合并完成后，再同步本地：

```bash
git switch main
git pull --ff-only origin main
git branch -d feat/model-settings
```

本地删除分支不会删除远端分支；远端分支可在已合并的 PR 页面点击 Delete branch。保留 main 作为可用版本，下一项功能再创建新的分支。
