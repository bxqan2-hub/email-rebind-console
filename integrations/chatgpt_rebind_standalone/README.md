# ChatGPT 换绑邮箱纯协议（独立包）

纯协议流程：

```text
旧邮箱 + 密码 + TOTP
  → 登录（含 MFA）
  → change_email begin / 收信验证码 / verify
  → 新邮箱 + 旧密码 + 旧 TOTP 重登
  → 导出 session + AT（login_bundle）
```

## 环境

- Python 3.10+
- Windows / macOS / Linux

```bash
pip install -r requirements.txt
```

## 运行

交互：

```bash
python run_rebind_email.py
```

参数：

```bash
python run_rebind_email.py --yes   --old-email old@example.com   --password "your-password"   --totp-secret "BASE32SECRET"   --new-email new@example.com   --mail-api "https://your-mail-api/latest?token=..."   --proxy ""   --out ./outputs/session_export
```

环境变量（可选）：`REBIND_OLD_EMAIL` / `REBIND_PASSWORD` / `REBIND_TOTP_SECRET` / `REBIND_NEW_EMAIL` / `REBIND_MAIL_API` / `REBIND_PROXY` / `REBIND_OUT`

## 收信 API

`begin` 后脚本会轮询 `--mail-api`，从返回 JSON/文本中提取 6 位验证码。

## 产出

默认写入 `./outputs/session_export/`：

- `login_bundle_*.json` / `login_bundle_latest.json`
- `access_token_latest.txt`
- `session_cookie_latest.txt`
- `session_cookie_header_latest.txt`（分片 cookie 可注入头）

## 说明

- 本包已内嵌最小 `registration_core`（纯协议登录 / Sentinel / TOTP），**不依赖**你机器上的其它本地项目路径。
- 不含浏览器抓包工具、不含账号样本、不含代理密钥。
- 请自行遵守平台条款与当地法律；仅在你有权操作的账号上使用。

## 免责声明

本项目仅供技术研究与个人账号自动化测试。请确保你对所操作账号、邮箱、代理拥有合法权限，并遵守 OpenAI 服务条款与当地法律法规。禁止用于未授权访问、批量滥用或其他违法用途。作者不对使用后果负责。
