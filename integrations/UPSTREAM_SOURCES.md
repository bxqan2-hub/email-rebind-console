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

Additional local extension points are limited to batch handoff and scheduling:
the same origin-checked message may contain multiple selected AT/email pairs,
which are parsed into the existing account confirmation step; each submitted AT
must have one distinct initial proxy. A per-job `concurrency` value is bounded by
the upstream global/session limits and controls how many accounts from that job
run at once. The underlying checkout, payment, retry, and monitor chain is unchanged.
Authenticated SOCKS5 is a local compatibility extension: Chromium receives a
loopback HTTP CONNECT endpoint while the API/Sentinel path connects to the same
SOCKS5 host with its credentials. The bridge is memory-only, loopback-bound, and
never writes proxy credentials to logs or task payloads.
On Windows, the local Sentinel Node bridge is launched with `CREATE_NO_WINDOW`;
this is a process-display adapter only and leaves the upstream Sentinel payload
and protocol unchanged.

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
imports and calls the upstream `rebind_core.pipeline.run_rebind_email` entrypoint
instead of reimplementing its steps. The 27-file vendored tree is locked to the
commit above, with one local bug fix in `rebind_core/mail_inbox.py`: HTML
mailbox responses are ordered newest-first, so the adapter now selects the
first explicit OTP card (and avoids CSS/URL numbers) instead of the final
six-digit match. Timestamped cards older than the current `begin` window are
ignored while polling, so delivery latency cannot cause immediate submission
of the previous run's code. The adapted combined tree SHA-256 is
`aed98c25c6cb70204889a19a6e4a0eb2350556f87a7e2f129a6298994f927ce7`.
On Windows, `registration_core/sentinel_quickjs.py` passes
`CREATE_NO_WINDOW` when spawning the Node Sentinel helper, preventing a
console window flash without changing the Sentinel payload or protocol.
Upstream runtime bundles, cookies, access tokens, and traces remain under its
ignored `outputs/` paths and are not committed.

Roxy is not part of the core rebind path. When the user enables **完成后开
Roxy**, `worker.py` first finishes the protocol transaction, then selects a
second proxy and calls the existing Roxy replacement-email login extension.
Failure of that optional extension does not repeat or roll back an already
confirmed email change. The previous Roxy/HAR email-change entrypoint and its
email-change helpers are removed; Roxy contains only this post-success extension.
