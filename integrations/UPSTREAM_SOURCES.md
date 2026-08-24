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
