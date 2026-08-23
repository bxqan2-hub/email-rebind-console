# 换绑分站仓库规则

## 独立仓库红线

- 本目录只属于换绑分站，仓库地址固定为
  `https://github.com/bxqan2-hub/email-rebind-console.git`。
- 发布远程名固定为 `rebind-origin`，发布分支固定为
  `codex/email-rebind-console`。
- 主站仓库 `https://github.com/bxqan2-hub/-.git` 与本目录完全分离；不得把它添加为
  本仓库远程，不得向它推送，不得修改主站工作树或主站远程配置。
- 禁止强制推送、改写远程历史或删除远程分支。

## 每次修改后的自动发布

- “每次修改”按一次完成的改动任务计算：先运行相关测试，再提交全部预期源码改动。
- 本仓库启用 `.githooks/post-commit`；每次成功提交后，它会自动把当前 HEAD 推送到
  `rebind-origin/codex/email-rebind-console`。
- 如果克隆了新副本或 hooks 未启用，先运行 `setup-git-hooks.bat`，或执行：
  `git config core.hooksPath .githooks`。
- 交付前必须验证工作树为 clean，并确认下面两个提交完全一致：
  `git rev-parse HEAD` 与
  `git ls-remote rebind-origin refs/heads/codex/email-rebind-console`。
- 自动推送失败时，修复原因并重新推送同一提交；不得把代码改推到主站仓库。

## 数据与凭据

- `data/`、`logs/`、`.env`、缓存、Cookie、验证码、账号文件、AT 和 API 凭据不得提交。
- 新增运行时数据文件前先补充 `.gitignore`，再提交源码。
