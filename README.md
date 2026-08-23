# 邮箱换绑分站

## GitHub 自动发布规则

本项目固定发布到私有仓库
`https://github.com/bxqan2-hub/email-rebind-console` 的
`codex/email-rebind-console` 分支，不使用主站仓库。

当前工作副本运行一次 `setup-git-hooks.bat` 后，每次完成修改并提交时，
`.githooks/post-commit` 会自动推送；`.githooks/pre-push` 会阻止误推到主站、
误推其他分支、改写目标或删除远程分支。完整交付规则见 `AGENTS.md`。

> **项目身份（请勿与主站混淆）**：本目录是独立的“邮箱换绑分站”Git 项目，
> 不是 `turb-gpt-free-register` 注册与账号管理主站，也不是主站仓库中的子目录。

分站独立监听 `127.0.0.1:5092`，运行数据位于本目录 `data/`，不会读写主站账号 JSON。
主站只负责导出原账号文本、接收换绑结果并更新账号分组；分站只负责 Roxy 换绑、
替换邮箱 API 接码、新邮箱重登录和新 AT 获取。

## 与主站的边界

| 边界 | 主站 `turb-gpt-free-register` | 本分站 `email-rebind-console` |
| --- | --- | --- |
| Git | 主站自己的 GitHub 仓库与提交历史 | 独立 `.git` 与独立提交历史，不向主站仓库提交分站源码 |
| 进程 | 主站 WebUI 进程 | 独立的 `127.0.0.1:5092` 进程 |
| 数据 | 主站账号、分组、注册任务 | 本目录 `data/` 中的换绑任务与替换邮箱号池 |
| 联动 | 导出原账号登录资料；导入换绑结果 | 导入原账号登录资料；按原邮箱类型导出换绑结果 |

分站不会修改密码和 2FA，也不会直接清理主站分组。主站导入换绑结果后，
才会清除旧邮箱的原分组记录，并在用户当前选择的分组显示新邮箱。

## 联动格式

- 原邮箱入口：`原邮箱----API取码地址`，或 `原邮箱----OpenAI密码----MFA Secret`
- 替换邮箱号池：`新邮箱----API取码地址`
- 分站导出（密码原邮箱）：`原邮箱----新邮箱----原OpenAI密码----原MFA Secret----新accessToken`
- 分站导出（URL 原邮箱）：`原邮箱----新邮箱----替换邮箱API取码地址----新accessToken`

首页两个入口严格分开：“导入原邮箱账号”中的邮箱+URL 仍然属于换绑前的原邮箱，
只保存到原账号列表；“导入替换邮箱”中的邮箱+URL 才会进入替换邮箱号池。
两类记录不会再根据相同的邮箱+URL 外形互相分流。
待换绑和失败的原邮箱账号可在“一对一配对”表格中逐条删除；活动任务、成功结果和
待核验结果会保持锁定，避免删除正在执行或需要保留的身份记录。
替换邮箱号池和换绑代理池均提供逐条删除；活动任务正在占用的记录会拒绝删除，
已完成任务历史及完成账号结果不会随号池记录一起删除。

## 换绑代理池

在分站“导入换绑代理”中手动粘贴，每行一条。支持：

```text
http://user:pass@host:port
socks5h://user:pass@host:port
host:port
host:port:user:pass
host:port----用户名----密码
```

未显式写协议的认证格式（`host:port:user:pass`、`host:port----用户名----密码`）
默认按 `socks5h://` 导入；HTTP 代理请明确写成 `http://user:pass@host:port`。

每个原账号在任务开始时随机取得一条代理，并在同一账号的替换邮箱轮换过程中保持使用。
创建 Roxy 环境前必须先取得代理出口 IP；检测失败的代理会记录失败原因、退出随机池，
同一账号立即随机选择下一条，替换邮箱不会因此被判坏。确认代理修复后，可在“换绑代理池”
中点击“重新启用”。代理认证信息只保存在分站 `data/换绑代理.json`，页面只显示掩码。

主站导入密码账号结果时使用“密码 + MFA Secret”识别原账号；导入 API 原邮箱结果时
使用导出的原邮箱识别原账号。识别后会把账号邮箱和 AT 更新为新值，从全部旧分组移除旧邮箱，
再加入用户当前选择的分组。

## 启动

双击 `start.bat`，或运行：

```powershell
C:\Users\Administrator\Desktop\turb-gpt-free-register\.venv\Scripts\python.exe app.py
```

浏览器打开 <http://127.0.0.1:5092/>。

换绑遵循 ChatGPT 网页流程：登录 → Settings → Account → 点击当前邮箱 → 新邮箱验证 → 退出 → 用新邮箱重新登录并读取 `/api/auth/session` 的新 AT。

## HAR 验证的换绑契约

2026-08-23 的完整成功抓包验证了下面的服务端时序：登录邮箱验证码
`POST /api/accounts/email-otp/validate` →
`GET /backend-api/accounts/change_email/eligibility` →
`POST /backend-api/accounts/change_email/begin` →
`POST /backend-api/accounts/change_email/verify` → `GET /auth/logout`。
其中 `verify` 的 `200`/`success=true` 是服务端已经换绑的提交点；`/auth/logout` 是网页成功回调触发的后续退出动作，不能再只等待 `/auth/login`。

同版本前端资源确认 `begin` 使用 `{email}`，`verify` 使用 `{email, code}`；
`social_password` 类型的两个请求还带 `remove_social_subs=true`。DOM 兜底使用版本化稳定标识：

```text
[data-testid="account-info-email"]
[data-testid="modal-edit-email"] input[name="email"]
[data-testid="modal-add-email-otp"] input[name="verification_code"]
```

实现会优先在当前 Roxy 登录页面内执行同源请求；接口缺失或需要页面重新认证时才退回 DOM 流程。
抓包原文件、Cookie、验证码、邮箱、AT 和请求头值均不写入仓库或日志。

## 成功窗口与 AT 刷新

- 换绑失败：关闭并删除本轮临时 Roxy 环境，释放未判坏的替换邮箱。
- 换绑成功：获取新 AT 后不退出、不关闭窗口，保持新邮箱登录态和 Roxy 环境。
- “重新获取 AT”：在窗口仍保持登录时复用同一成功窗口读取最新 `/api/auth/session`，适合充值 Plus 后刷新 AT。
- “关闭并删除”：先关闭 Roxy 窗口，再删除对应 Profile；删除后不能重新获取 AT，避免环境持续堆积。
- “复制AT”：只复制该完成账号保存的单独 AT，不拼接邮箱、密码或取码 URL。
- “清理账号”：成功账号的 Roxy Profile 删除后可清理；保存的 AT 和该账号导出结果随账号一起清理。
- “清理记录”：成功、失败和待核验任务都支持单条或批量清理，活动任务不会被删除。

### 临时故障自动重试

- 登录页超时、Roxy/浏览器临时错误，以及换绑开始请求未取得响应时，系统会保持同一原邮箱和替换邮箱，自动重新创建环境并切换到下一条代理重试。
- 默认自动重试 2 次；验证码已经提交、服务端已确认但最终状态不明确的任务仍进入待核验，不会自动换下一个邮箱。
- 可用 `EMAIL_REBIND_MAX_TRANSIENT_RETRIES` 调整次数（0~10），用 `EMAIL_REBIND_TRANSIENT_RETRY_DELAY` 调整每次重试前等待秒数（0~30）。
- 自动重试耗尽后，任务和账号保留为失败状态，替换邮箱释放回可用池，仍可使用“失败重试”手动再次执行。

完成账号表会显示 Profile ID、窗口状态、AT 更新时间和失败原因。重新获取成功后，
页面会自动刷新并明确显示“AT 已更新”或“AT 未变化”；获取到的 AT 会原子保存并回读校验，
“复制结果”和“复制AT”都会立即使用已保存的 AT，页面不再提供 TXT 下载按钮。

邮箱 API 默认校验 HTTPS 证书；只有内部自签名接口需要在启动前设置
`EMAIL_REBIND_MAIL_VERIFY_TLS=0`。也可用 `EMAIL_REBIND_PORT`、
`EMAIL_REBIND_WORKERS`、`EMAIL_REBIND_OTP_MAX_WAIT` 调整端口、并发和取码超时。
`EMAIL_REBIND_MAX_PROXY_ATTEMPTS` 控制单账号最多自动检测多少条代理，默认 5。

## 失败处理状态机

| 失败位置 | 替换邮箱 | 原账号 | 后续动作 |
| --- | --- | --- | --- |
| API 无新验证码、取码超时、新邮箱已占用 | 标记 `failed`，保存原因和失败次数 | 保持原身份 | 原子占用下一个可用邮箱并自动重试 |
| Roxy、原账号登录、账号资格在换绑完成前失败 | 释放回 `available` | 标记失败并保存原因 | 人工重试原账号，不浪费邮箱 |
| 验证码已提交但结果未知，或已换绑后新邮箱登录/AT 刷新失败 | 标记 `review` | 记录可能的新邮箱并标记 `review` | 冻结双方，避免再次换绑造成身份错位 |
| 换绑和新 AT 校验成功 | 标记 `used` | 标记成功 | 按原邮箱类型进入对应格式导出 |
| 号池耗尽或达到自动轮换上限 | 保留全部失败标记 | 标记失败并写明原因 | 补充号池后重新选择账号运行 |

代理失败独立于替换邮箱状态机：Roxy 创建前出口检测失败只隔离代理并自动换下一条；
代理池耗尽或达到代理切换上限时释放当前替换邮箱，并把原账号保留为可重试失败状态。

失败邮箱不会因重复导入而自动恢复。确认邮箱或 API 已修好后，在“替换邮箱号池”
点击“重新启用”。自动轮换默认最多 5 次，可用
`EMAIL_REBIND_MAX_REPLACEMENT_ATTEMPTS` 调整。
