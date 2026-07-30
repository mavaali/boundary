# Spike: nono proxy runtime shapes (Task 1)

Captured live against **nono 0.70.0** / **srt 1.0.0** on macOS (Mac Mini), 2026-07-30.
Every block below is `[DATA]` — real captured output. Task 4's parser/audit MUST cite samples here.

## 1. Startup connection info — `[DATA]`

`nono proxy --port 0 --credential github --allow-endpoint "github:GET:/repos/*/pulls"` prints to **stdout** (stderr empty). **Free text, not JSON.** Ready in < 3s. Sample:

```
  nono proxy listening on 127.0.0.1:61928
  proxy URL: http://nono:b7cf3e2b…734fa9@127.0.0.1:61928
  token:     b7cf3e2b…734fa9
  export HTTPS_PROXY=http://nono:b7cf3e2b…734fa9@127.0.0.1:61928
  export HTTP_PROXY=http://nono:b7cf3e2b…734fa9@127.0.0.1:61928
  routes:
    https://api.github.com | creds: env://GITHUB_TOKEN ✓ | intercept: on | endpoint_rules: 1
  TLS interception trust bundle: /Users/…/.local/state/nono/sessions/intercept-<pid>-<rand>/intercept-ca.pem
  Press Ctrl-C to stop.
```

Parse anchors (stdout, per-line):
- Port: `^\s*nono proxy listening on 127\.0\.0\.1:(\d+)`
- Token: `^\s*token:\s+([a-f0-9]{16,})` (64 hex)
- CA path: `TLS interception trust bundle:\s+(\S+intercept-ca\.pem)`
- Proxy URL (basic-auth `nono:<token>@host:port`): `proxy URL:\s+(http://\S+)`
- Route health: `creds: env://GITHUB_TOKEN ✓` (found) vs `✗ (credential_not_found)` (missing)

**Machine-readable option:** none. `nono proxy --help` exposes no `--json`/`--connection-file`. Text parsing is required. `-s/--silent` suppresses this output, so do NOT use it.

## 2. Credential resolution scheme — `[DATA]` (contradicts spec)

`--credential github` resolves the secret from, in order: env var `GITHUB_TOKEN`, else macOS keychain (`security add-generic-password -s "nono" -a "GITHUB_TOKEN" -w`). The route reports it as `env://GITHUB_TOKEN`. Startup WARN when absent:

```
WARN Credential not found for route 'github' … Looked for env var 'GITHUB_TOKEN' (not set).
To add to the macOS keychain: security add-generic-password -s "nono" -a "GITHUB_TOKEN" -w
— and set credential_key to bare 'GITHUB_TOKEN' (no env:// prefix).
```

**Implication:** the spec's `credential_key: "keyring://gh:github.com"` scheme does NOT exist in nono. Real schemes are `env://<VAR>` or a bare keychain account name. The `service` (e.g. `github`) maps to a built-in route host (`api.github.com`); nono owns the service→host table.

## 3. Enforcement semantics — `[DATA]` (contradicts spec: withhold ≠ block)

| Flags | in-scope `GET /repos/*/pulls` | out-of-scope path `/issues` | out-of-scope domain `example.com` |
|---|---|---|---|
| `--credential` + `--allow-endpoint` only | 401 (dummy cred injected, reached github) | **404** — passes upstream **uncredentialed** (github's 404), NOT blocked | (all domains reachable; network filter is opt-in) |
| **+ `--allow-domain "https://api.github.com/repos/*/pulls"`** | 401 (injected) | **403 `{"error":"Forbidden"}`** — proxy-blocked, never reached github | **000** — blocked entirely |

**Key finding:** `--allow-endpoint` only *confines which endpoints receive the injected credential* — out-of-scope requests still flow, just without the secret. To get a real **hard block (403) + domain restriction**, you also need `--allow-domain "https://<host>/<path-glob>"`. The spec assumed `--allow-endpoint` alone yields 403; it does not.

So a genuine boundary = **both flags per scope**: `--credential`+`--allow-endpoint` (credential confinement) AND `--allow-domain` (hard block + auditable deny). Emitting `--allow-domain` requires the scope's **host/base-URL**, which the spec's `{service, credential_key, allow_endpoints}` does not carry.

## 4. Audit / grading source — `[DATA]`

Default `--log-file` is EMPTY for a proxy session. Per-request decisions require **`-vv`**. With `nono proxy -vv --log-file <path>` the file holds timestamped **text** lines (not JSON):

```
INFO l7 endpoint policy decision mode=connect_intercept target="api.github.com" method="GET" path="/repos/foo/bar/pulls" decision=Allow endpoint_policy_action="allow" endpoint_policy_rule="endpoint_policy.allow[GET /repos/*/pulls]"
INFO l7 proxy response mode=connect_intercept target="api.github.com" method="GET" path="/repos/foo/bar/pulls" status=401
WARN tls_intercept: endpoint rules denied GET /repos/foo/bar/issues: no rule matched on api.github.com:443
INFO proxy request denied mode=connect_intercept host="api.github.com" port=443 decision="deny" reason="endpoint rules denied GET /repos/foo/bar/issues: no rule matched on api.github.com:443"
```

Grading anchors for the Third Umpire `credential_scope_held` check:
- Allowed request: `l7 endpoint policy decision .* decision=Allow`
- Denied request (violation attempt): `proxy request denied .* decision="deny"` and/or `endpoint rules denied (\w+) (\S+)`
- Upstream status per request: `l7 proxy response .* path="(\S+)" status=(\d+)`

`nono audit`/`nono logs` cover only `nono run` sessions, NOT a standalone `proxy` — so the `-vv --log-file` scrape is the grading source.

## 5. Lifecycle — `[DATA]`
- Ephemeral port assigned with `--port 0`; released immediately on `kill` (confirmed via `lsof -iTCP:<port>`).
- CA path embeds the proxy PID: `…/sessions/intercept-<pid>-<rand>/intercept-ca.pem`. Per-session by default; `--proxy-ca-cert/--proxy-ca-key` reuse a fixed CA across runs.
- Auth: session token via `Proxy-Authorization` (Basic `nono:<token>` as in the printed URL, or Bearer). Overridable via `--pass`/`NONO_PROXY_PASS`.
- `--allow-endpoint` alias is annotated `remove_by="v1.0.0"` — watch for interface drift past nono 1.0.

## Architecture pivot: nono-as-DRIVER (2026-07-30) — `[DATA]`

The srt + standalone-`nono proxy` composition was abandoned: **srt runs its own egress MITM proxy and owns `HTTP(S)_PROXY`**, clobbering any injected nono-proxy URL, and `_jail_env`'s `os.environ.copy()` leaks the real credential into the srt jail. Both verified live.

`nono run` solves it natively (verified live):
- `GITHUB_TOKEN=<real> nono run --allow <ws> --allow-cwd -s --credential github --allow-endpoint "github:GET:/repos/*/pulls" -- env` → child's `GITHUB_TOKEN` is a **phantom** proxy token; the real secret is **absent** from the child env.
- One invocation = fs write-jail (`--allow`, deny-by-default hides secrets) + egress (`--allow-domain`/`--block-net`) + credential scoping (`--credential`/`--allow-endpoint`, `compile_nono_flags` reused verbatim).
- Headless: needs `--allow-cwd`; errors cleanly instead of prompting in non-interactive mode.
- Out-of-scope L7 denials are **not** surfaced in `--log-file` or `--diagnostics-json` (fs/seatbelt denials only). So `credential_scope_held` grades on *enforcement presence* (declared + `credential_scopes_enforced`), not per-call attempts. Enforcement itself is hard/automatic (403).

### Load-bearing probe result (`tests/test_credential_scope_e2e.py`) — `[DATA]`
Under the live nono driver, all three pass:
1. `test_real_credential_absent_inside_jail` — PASS (phantom holds; real secret never in child env)
2. `test_out_of_scope_endpoint_refused_403` — PASS
3. `test_external_host_sealed` — PASS

## Design deltas this spike forces (before Task 2)
1. **Data model:** `CredentialScope` must carry the scope's **host/base-URL** (to emit `--allow-domain`). Reconsider `credential_key` to use nono's real scheme (`env://VAR` or keychain account), not `keyring://`.
2. **compile_nono_flags (Task 3):** emit `--credential` + `--allow-endpoint` + **`--allow-domain "https://<host>/<path-glob>"`** per scope; run the proxy with `-vv --log-file`.
3. **Umpire grade (Task 9):** key `credential_scope_held` on the `-vv` log's `decision="deny"` / `endpoint rules denied` lines, not on a transcript "403 event."
