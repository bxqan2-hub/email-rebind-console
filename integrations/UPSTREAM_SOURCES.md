# Upstream sources

## MK GCash Link

- Source: https://github.com/mika50000/MK-GCash-Link-OpenSource
- Vendored commit: `2607d879ce2005ef9a9c6cdfa1ec747c6f26d4d5`
- Local path: `integrations/mk_gcash_link`
- License: MIT; the upstream `LICENSE`, `README.md`, and `SECURITY.md` are retained.

The vendored project runs as a loopback-only companion HTTP server in the email
rebind process. `gcash_service.py` is the local lifecycle adapter and the
left-side **GCash提链** view embeds the upstream web workspace. The sole local
source adaptation replaces upstream's `frame-ancestors 'none'` / `X-Frame-Options:
DENY` response policy with a configurable `MK_EMBED_ORIGIN` allow-list entry so
the loopback rebind console can render the workspace. All chain, proxy, queue,
payment-monitor, and API behavior remains at the locked upstream revision. The
upstream web client also accepts one origin-checked `postMessage` from its parent
rebind console to populate the AT field after the user clicks “推送AT到GCash提链”;
the message is not persisted and does not start a payment task automatically.

Before updating, fetch the source repository, compare it with the vendored tree,
update `upstream-lock.json`, run its unit tests, then run the complete rebind
console test suite.

## ChatGPT Rebind Standalone

- Source: https://github.com/MervLis/chatgpt-rebind-standalone
- Vendored commit: `e27b3217dbfddab19e83dc57ab225173877e4663`
- Local path: `integrations/chatgpt_rebind_standalone`
- Upstream contains no declared license file at the locked revision; its README
  and source attribution are retained.

This is the sole runtime implementation for the core email-rebind protocol:
password + TOTP login, eligibility, begin, replacement-mail OTP, verify, and
replacement-email login/AT export. `protocol_flow.py` is the local adapter. It
uses the vendored primitives without writing login bundles, cookies, access
tokens, or traces into the source tree. Local namespace-only import changes keep
the vendored `registration_core` isolated from the main-site Python packages.

Roxy is not part of the core rebind path. When the user enables **完成后开
Roxy**, `worker.py` first finishes the protocol transaction, then selects a
second proxy and calls the existing Roxy replacement-email login extension.
Failure of that optional extension does not repeat or roll back an already
confirmed email change.
