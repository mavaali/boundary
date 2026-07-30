# Credential Scopes — an L7 credential-scoping leg for the envelope

**Status:** design (approved 2026-07-29)
**Author:** brainstormed with Mihir via Mavaali
**Depends on:** srt (OS egress jail), nono ≥ 0.70 (standalone credential proxy)

## Problem

Boundary's egress leg today is domain-granular: under `--sandbox-driver srt` it
can say *"the agent may reach `api.github.com`."* It cannot say *"the agent may
wield a GitHub token, but only against `GET /repos/*/pulls`."* Once an agent has
a credential and a reachable host, it can call any method on any path that
credential authorizes — the "external communication" leg of the lethal trifecta
is bounded by destination, not by **which credential** and **which operation**.

This leaves a real exfil/abuse channel open: a prompt-injected agent with a live
`gh` token and reach to `github.com` can open issues, push, read private repos —
all "in policy" as far as a domain allowlist is concerned.

## Goal

Add a first-class envelope leg — **credential scopes** — that bounds *which
credential the agent wields and exactly which HTTP method+path patterns it may
use it against*, enforced at the network boundary and graded by the Third
Umpire. The agent never holds the real credential.

This is capability **A** from the competitive brainstorm: the one differentiator
that clears the 7th-grader bar — *"Boundary already says where the agent can go;
now it also controls what key the agent holds and exactly what it's allowed to
do with it."*

## Non-goals (v1)

- `launch` (involuntary containment), schedule, and pipeline inheritance — later specs.
- Non-`srt` sandbox drivers (they don't OS-force egress, so the guarantee is void).
- Non-keyring credential sources.
- nono's per-tool child-sandbox brokering (a deeper nono feature).
- Windows.

## Validated feasibility (2026-07-29, hands-on, nono 0.70.0)

- `nono proxy` runs as a **standalone loopback server** — independent of nono's
  kernel jail. (This contradicts nono's marketing page, which claims no
  standalone deployment; the CLI proves otherwise.)
- It does phantom-token **credential injection** (`--credential <service>`,
  resolving real creds from the OS keyring) plus **L7 endpoint scoping**
  (`--allow-endpoint "github:GET:/repos/*/issues"`; `*` = one path segment,
  `**` = zero or more), with loopback + session-token auth.
- Out-of-scope requests are **refused with 403 at the boundary** — enforcement is
  hard and does not depend on log-parsing.
- Audit is available via `nono audit show <id> --json` (hash-chained) and the
  proxy's `--log-file`.

## Architecture

Three actors, each doing its best thing:

| Actor | Responsibility |
|---|---|
| **nono proxy** (host-side, standalone) | Holds real creds via `keyring://`; listens loopback; swaps phantom→real; enforces endpoint rules (403 out-of-scope). |
| **srt jail** | Runs the agent; OS egress allowlist = **only** the proxy's loopback port; fs write-jail as today; proxy CA injected into the caller env. |
| **boundary** | Orchestrates lifecycle; owns staging/floor/ceiling/taint/receipts; grades via the Third Umpire. |

The phantom-token guarantee holds because **srt forces all egress through the
proxy** (the agent cannot bypass it) and the **real credential never enters the
srt jail** — it lives only in the host-side proxy process.

### Lifecycle inside `boundary run`

1. Parse envelope. If `credential_scopes` is non-empty → run **preconditions** (below).
2. `start_credential_proxy(scopes)` spawns `nono proxy` on an ephemeral loopback
   port with a per-session CA and compiled `--credential` / `--allow-endpoint`
   flags. Returns `{proxy_url, port, session_token, ca_path}`.
3. The runner configures the srt caller:
   - egress allowlist = `[127.0.0.1]` limited to the proxy port (nothing else);
   - `HTTPS_PROXY` / `HTTP_PROXY = http://<session_token>@127.0.0.1:<port>`;
   - CA env vars (`NODE_EXTRA_CA_CERTS`, `SSL_CERT_FILE`, `CURL_CA_BUNDLE`,
     `GIT_SSL_CAINFO`) = the per-session proxy CA (same pattern srt already uses).
4. The agent runs. Every outbound call is forced through the proxy; the proxy
   injects the real credential and enforces method+path rules; out-of-scope → 403.
5. On run end: boundary reads the proxy audit → computes the Umpire verdict +
   receipt entry, then tears down the proxy and removes the session CA/scratch.

## The `credential_scopes` field

Schedule/pipeline/envelope YAML:

```yaml
envelope:
  credential_scopes:
    - service: github                              # logical name → nono --credential
      credential_key: "keyring://gh:github.com"    # where the real cred lives; nono resolves it
      allow_endpoints:                             # METHOD:/path globs
        - "GET:/repos/*/pulls"
        - "GET:/repos/*/issues"
```

CLI mirror (repeatable): `--credential-scope service=github,key=keyring://…,endpoint=GET:/repos/*/pulls`.

Compilation → `nono proxy --credential github --allow-endpoint "github:GET:/repos/*/pulls" --allow-endpoint "github:GET:/repos/*/issues"`.

**Validation:**
- A scope with an **empty `allow_endpoints` is rejected at parse** (deny-all is a
  useless footgun — it would inject a credential the agent can never use).
- `service` is a free label (no registry lookup).
- boundary never reads the raw credential — it passes the `keyring://` reference
  through and lets nono resolve it host-side.

## Fail-closed preconditions

When `credential_scopes` is non-empty, **refuse the run** (fail closed, exit 2,
loud reason) if any of:
- `nono` is not installed (`shutil.which("nono")` is None);
- the resolved sandbox driver is not `srt` (seatbelt / none / auto-fallback do
  not OS-force egress, so the agent could sidestep the proxy and the scoping
  guarantee is void);
- the proxy does not come up on its loopback port within a timeout.

This mirrors the launcher's srt fail-closed and `require_srt_for_bash`. The
secure path is the default, not opt-in.

## Third Umpire grading + receipt

Enforcement is hard (the proxy 403s out-of-scope regardless); the grade is the
**report** on top of it.

- New verdict property **`credential_scope_held`**: true iff the proxy audit
  shows **zero out-of-scope (boundary-403) attempts** and egress was not
  uncontained (the existing `egress_uncontained` check already covers a tainted
  run without OS-forced egress).
- Per-call **`credential_request`** envelope events (service, method, path,
  allowed) are emitted into the engine transcript so gateway/scheduled sessions
  are gradeable in the same shape the Umpire already consumes.
- The **receipt** (`boundary.receipt/v1`) binds the `credential_scopes` spec-hash
  → verdict, so the policy that was in force is provable after the fact.
- An out-of-scope **attempt** is a Third Umpire **FAIL line** (like a taint
  violation), not a runtime block — the proxy already blocked it; the Umpire
  records that the agent *tried*.

## Components / interfaces

- `boundary/credential_proxy.py` (new) — the thin orchestration module:
  - `compile_nono_flags(scopes) -> list[str]` (pure, unit-testable).
  - `start_credential_proxy(scopes, *, ca_dir) -> ProxyHandle` (spawns `nono
    proxy`, waits ready, returns url/port/token/ca_path).
  - `ProxyHandle.audit() -> list[CredentialRequest]` and `.close()`.
- `boundary/envelope.py` — add the `credential_scopes` field + parse-time
  validation + spec-hash inclusion.
- The `run` path (`EnvelopeRunner`) — precondition gate, srt egress+env wiring,
  transcript events, teardown in a `finally`.
- `boundary/third_umpire.py` — the `credential_scope_held` property.
- `boundary/receipt.py` — bind the credential-scopes spec-hash.

Each unit is independently testable: flag compilation is pure; the proxy handle
is a process wrapper; the runner wiring is env construction; the Umpire property
is a function of the transcript.

## Testing

- **Unit:** scope→nono-flags compilation; parse rejects empty `allow_endpoints`;
  precondition fail-closed (no nono; wrong driver; proxy fails to start); caller
  env wiring sets `HTTPS_PROXY` + CA vars; teardown removes the session CA/scratch.
- **Integration** (nono-gated, skips when absent — mirrors the srt e2e /
  `importorskip` pattern): start the proxy, drive a real in-scope request
  (injected, 200) and an out-of-scope one (403), assert the audit → verdict
  mapping.
- **Load-bearing probe** (F26-style, run under a live nono + srt): an
  out-of-scope method+path is actually **refused**, *and* the phantom token
  inside the jail never exposes the real credential (grep the caller-visible env
  and the injected request for the real secret; assert absent inside, present
  only upstream of the proxy).

## Open risks / to verify during implementation

- Exact machine-readable shape of the standalone proxy's per-request audit
  (`--log-file` format vs `nono audit show --json` for a proxy-only session) —
  the *enforcement* does not depend on this, only the grade.
- Per-session CA trust inside the srt jail across HTTP clients (curl/git/node/
  python) — reuse srt's existing CA-env pattern; verify git in particular.
- Startup latency / port-race of `nono proxy` under the ready-check timeout.

## Why this clears the bar

7th-grader sentence: *"Boundary already says where the agent can go; now it also
controls what key the agent holds and exactly what it's allowed to do with it."*
It is the composed "stand on Nono" thesis — srt forces egress, nono scopes the
credential, boundary owns the envelope and grades whether the scope held — and no
neighbor (predicate-secure, Cupcake, nah, the MCP gateways) has a
credential-operation-scoping leg bound to post-run grading.
