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
主站只负责导出原账号文本、接收换绑结果并更新账号分组；分站使用纯协议完成换绑、
替换邮箱 API 接码、新邮箱重登录和新 AT 获取，Roxy 只作为可选的成功后扩展。

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

每个原账号在纯协议任务开始时随机取得一条代理，并在同一账号的替换邮箱轮换过程中使用。
勾选“完成后开 Roxy”时，协议换绑成功后会排除当前协议代理，再从代理池额外选择第二条
代理创建 Roxy 环境；Roxy 代理检测失败会记录原因并切换下一条。确认代理修复后，可在“换绑代理池”
中点击“重新启用”。代理认证信息只保存在分站 `data/换绑代理.json`，页面只显示掩码。

主站导入密码账号结果时使用“密码 + MFA Secret”识别原账号；导入 API 原邮箱结果时
使用导出的原邮箱识别原账号。识别后会把账号邮箱和 AT 更新为新值，从全部旧分组移除旧邮箱，
再加入用户当前选择的分组。

## 启动

双击 `start.bat`，或运行：

```powershell
$env:EMAIL_REBIND_HOST='127.0.0.3'; $env:EMAIL_REBIND_PORT='5092'; & 'C:\Users\Administrator\Desktop\turb-gpt-free-register\.venv\Scripts\python.exe' app.py
```

浏览器打开 <http://127.0.0.3:5092/>。双击 `start.bat` 会先强制停止 5092 端口上的现有分站进程，再启动并打开该地址。

## GCash提链

左侧导航的“GCash提链”内嵌
[MK GCash Link OpenSource](https://github.com/mika50000/MK-GCash-Link-OpenSource)，
锁定版本为 `2607d879ce2005ef9a9c6cdfa1ec747c6f26d4d5`。源码保存在
`integrations/mk_gcash_link`，MIT 许可证和上游说明均随源码保留。

GCash 工作台由分站进程自动启动并仅监听本机 `127.0.0.3:8931`；
`start.bat` 会同时检查 5092 分站和 8931 GCash 服务，`stop.bat` 会同时停止两者。
它使用用户在工作台中提供的 AT 与 PH 住宅代理，任务数据只保存在当前 Python
进程内，重启后清空。完成账号支持多选后点击“推送GCash”，批量 AT 会直接进入提链账号确认页。
GCash 工作台可设置本批提链并发；多账号任务要求每个 AT 配置一条独立首选代理，后端按所选并发同时执行。
依赖安装命令为：

```powershell
& 'C:\Users\Administrator\Desktop\turb-gpt-free-register\.venv\Scripts\python.exe' -m pip install -r requirements.txt
& 'C:\Users\Administrator\Desktop\turb-gpt-free-register\.venv\Scripts\python.exe' -m playwright install chromium
```

换绑核心来自 [chatgpt-rebind-standalone](https://github.com/MervLis/chatgpt-rebind-standalone)，
锁定提交 `e27b3217dbfddab19e83dc57ab225173877e4663`，源码位于
`integrations/chatgpt_rebind_standalone`。流程为：纯协议登录原邮箱（密码 + TOTP）→
eligibility → begin → 新邮箱验证码 → verify → 纯协议使用新邮箱重登并获取 AT。
分站直接调用上游原生 `rebind_core.pipeline.run_rebind_email`；上游目录的 27 个文件与
锁定提交逐字节一致，不保留分站自有换绑实现。默认全程不创建 Roxy，也不执行 Settings/DOM 流程。

> 该上游锁定版本只实现密码 + TOTP 登录，因此纯协议核心只接受带 OpenAI 密码和
> 2FA Secret 的原账号。原邮箱 API-only 记录仍可保留/导入，但启动纯协议任务时会明确失败。

## 上游对齐方式

- `integrations/chatgpt_rebind_standalone` 是锁定提交的完整原样副本，包含上游两个
  `outputs/**/.gitkeep`，不含本地命名空间补丁。
- `protocol_flow.py` 只负责调用上游 `run_rebind_email`、读取其 `login_bundle` 并把结果接入分站状态机；
  登录、MFA、eligibility、begin、收码、verify、重登和导出均由上游实现。
- 原 Roxy/HAR 换绑入口及对应辅助实现已经删除；`roxy_flow.py` 只保留换绑成功后的新邮箱登录扩展、
  窗口保留、CDP 端口、刷新 AT 和关闭环境功能。
- 上游生成的 bundle、Cookie、AT 和 trace 位于其已忽略的 `outputs/`，不会加入 Git。

## 可选 Roxy 扩展与 AT 刷新

- 默认模式：纯协议换绑完成并保存 AT，不创建 Roxy Profile，完成账号可直接清理。
- “完成后开 Roxy”：要求至少两条可用代理；协议完成后额外选择第二条代理，新建窗口并登录替换邮箱。
- Roxy 扩展失败：邮箱换绑和纯协议 AT 结果仍记为成功，窗口显示“打开失败”及原因，不重复提交换绑。
- “检查并更新 AT”：先检查成功账号是否有 Roxy 窗口；有窗口时复用同一窗口读取最新 `/api/auth/session`，没有窗口时使用新邮箱、密码和 2FA 走一次纯协议登录并更新 AT。
- “关闭并删除”：先关闭 Roxy 窗口，再删除对应 Profile；删除后再次检查 AT 时会自动走纯协议登录，不会重新创建 Roxy 环境。
- “复制AT”：只复制该完成账号保存的单独 AT，不拼接邮箱、密码或取码 URL。
- “清理账号”：成功账号的 Roxy Profile 删除后可清理；保存的 AT 和该账号导出结果随账号一起清理。
- “清理记录”：成功、失败和待核验任务都支持单条或批量清理，活动任务不会被删除。

### 临时故障自动重试

- 纯协议登录超时、代理/网络临时错误、换绑 API 不可用，以及 begin 未取得响应时，系统会保持同一原邮箱和替换邮箱，切换代理后重试；邮箱换绑始终只走纯协议。
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
| 上游纯协议登录、账号资格在换绑完成前失败 | 释放回 `available` | 标记失败并保存原因 | 人工重试原账号，不浪费邮箱 |
| 验证码已提交但结果未知，或已换绑后新邮箱登录/AT 刷新失败 | 标记 `review` | 记录可能的新邮箱并标记 `review` | 冻结双方，避免再次换绑造成身份错位 |
| 换绑和新 AT 校验成功 | 标记 `used` | 标记成功 | 按原邮箱类型进入对应格式导出 |
| 号池耗尽或达到自动轮换上限 | 保留全部失败标记 | 标记失败并写明原因 | 补充号池后重新选择账号运行 |

代理失败独立于替换邮箱状态机：纯协议请求中的代理网络失败只隔离代理并自动换下一条；可选 Roxy 登录使用另一条代理；
代理池耗尽或达到代理切换上限时释放当前替换邮箱，并把原账号保留为可重试失败状态。

失败邮箱不会因重复导入而自动恢复。确认邮箱或 API 已修好后，在“替换邮箱号池”
点击“重新启用”。自动轮换默认最多 5 次，可用
`EMAIL_REBIND_MAX_REPLACEMENT_ATTEMPTS` 调整。
