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
