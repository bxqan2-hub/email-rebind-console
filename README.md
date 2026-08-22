# 邮箱换绑分站

独立监听 `127.0.0.1:5091`，运行数据位于本目录 `data/`，不会读写主站账号 JSON。

## 联动格式

- 主站 → 分站：`原邮箱----OpenAI密码----MFA Secret`
- 替换邮箱号池：`新邮箱----API取码地址`
- 分站 → 主站：`新邮箱----原OpenAI密码----原MFA Secret----新accessToken`

主站导入时使用“密码 + MFA Secret”识别原账号，因此不需要在导出结果中暴露原邮箱；识别后会把账号邮箱和 AT 更新为新值，从全部旧分组移除旧邮箱，再加入用户当前选择的分组。

## 启动

双击 `start.bat`，或运行：

```powershell
C:\Users\Administrator\Desktop\turb-gpt-free-register\.venv\Scripts\python.exe app.py
```

浏览器打开 <http://127.0.0.1:5091/>。

换绑遵循 ChatGPT 网页流程：登录 → Settings → Account → 点击当前邮箱 → 新邮箱验证 → 退出 → 用新邮箱重新登录并读取 `/api/auth/session` 的新 AT。

邮箱 API 默认校验 HTTPS 证书；只有内部自签名接口需要在启动前设置
`EMAIL_REBIND_MAIL_VERIFY_TLS=0`。也可用 `EMAIL_REBIND_PORT`、
`EMAIL_REBIND_WORKERS`、`EMAIL_REBIND_OTP_MAX_WAIT` 调整端口、并发和取码超时。

## 失败处理状态机

| 失败位置 | 替换邮箱 | 原账号 | 后续动作 |
| --- | --- | --- | --- |
| API 无新验证码、取码超时、新邮箱已占用 | 标记 `failed`，保存原因和失败次数 | 保持原身份 | 原子占用下一个可用邮箱并自动重试 |
| Roxy、原账号登录、账号资格在换绑完成前失败 | 释放回 `available` | 标记失败并保存原因 | 人工重试原账号，不浪费邮箱 |
| 验证码已提交但结果未知，或已换绑后新邮箱登录/AT 刷新失败 | 标记 `review` | 记录可能的新邮箱并标记 `review` | 冻结双方，避免再次换绑造成身份错位 |
| 换绑和新 AT 校验成功 | 标记 `used` | 标记成功 | 进入四段格式导出 |
| 号池耗尽或达到自动轮换上限 | 保留全部失败标记 | 标记失败并写明原因 | 补充号池后重新选择账号运行 |

失败邮箱不会因重复导入而自动恢复。确认邮箱或 API 已修好后，在“替换邮箱号池”
点击“重新启用”。自动轮换默认最多 5 次，可用
`EMAIL_REBIND_MAX_REPLACEMENT_ATTEMPTS` 调整。
