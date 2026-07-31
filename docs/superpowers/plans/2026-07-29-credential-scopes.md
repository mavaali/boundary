# Credential Scopes Implementation Plan

> **⚠️ IMPLEMENTED WITH A REVISED ARCHITECTURE (2026-07-30).** This plan targets srt + a
> standalone `nono proxy` composed via env injection. During execution that approach was
> found not to hold (srt owns `HTTP(S)_PROXY` and clobbers the injected proxy; the jail env
> leaks the credential). It was replaced by a **nono sandbox driver** (`_run_nono`): each bash
> command runs under `nono run` with the scope flags, which does the fs jail + egress +
> credential scoping natively (phantom injection, no leak). Tasks 1–3/5/9/10 landed as written;
> Tasks 4/6/8 were superseded by the driver; Task 7's precondition flips srt→nono; Task 11's
> probe passes. See `docs/spikes/nono-proxy-runtime.md` (Architecture pivot) and the Daftari
> vault `projects/boundary-credential-scopes.md` for the as-built design.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax.

**Goal:** Add a first-class `credential_scopes` envelope leg that bounds WHICH credential an agent wields and WHICH HTTP method+path patterns it may use it against. Enforcement: srt jails the caller with loopback-only egress pointed at a standalone `nono proxy` that does phantom-token credential injection + L7 endpoint scoping (403 out-of-scope). The real credential never enters the jail. boundary owns the field, fail-closed preconditions, and a Third Umpire `credential_scope_held` grade bound into the receipt. v1 = the `run` path only.

**Architecture:** Three actors: (1) **nono proxy** (host-side, standalone) holds real creds via `keyring://`, listens loopback, swaps phantom→real, enforces endpoint rules (403 out-of-scope); (2) **srt jail** runs the agent with OS egress allowlist = loopback only, proxy env + CA injected into the caller env; (3) **boundary** orchestrates proxy lifecycle around the agent loop, refuses fail-closed when preconditions are unmet, emits `credential_scope_violation` events from the proxy audit, and grades `credential_scope_held` in the Third Umpire, bound into the receipt via spec-hash.

**Tech Stack:** Python 3 (stdlib `subprocess`, `dataclasses`), pytest (`python -m pytest`), `nono` (external binary, version confirmed in the Task-1 spike; gated with `shutil.which`), `srt` (external binary, gated with `shutil.which`).

---

## File Structure

| File | One responsibility |
|---|---|
| `boundary/credential_proxy.py` (new) | `CredentialScope` dataclass + parse validation; pure `compile_nono_flags()`; `ProxyHandle` process wrapper (`start_credential_proxy`, `proxy_env()`, `audit()`, `close()`). No envelope/agent imports. |
| `boundary/envelope.py` (modify) | Envelope field `credential_scopes` (@~234), `spec_dict()` inclusion (@350-386), runner precondition gate + proxy lifecycle + start/end log fields (@1003-1417). |
| `boundary/agent.py` (modify) | Store `credential_scopes` + mutable `proxy_env` attr; bash tool reads egress/proxy_env live at call-time. |
| `boundary/tools/sandbox.py` (modify) | Thread `proxy_env` param through `run_sandboxed()` (@106-114) → `_run_srt` (@202) → merged into `_jail_env` (@69-84). |
| `boundary/tools/shell.py` (modify) | `_bash` passes live `agent.egress_allowlist` / `agent.proxy_env` into `run_sandboxed`. |
| `boundary/third_umpire.py` (modify) | New `credential_scope_held` CheckResult mirroring `egress_uncontained` (@510-526). |
| `boundary/cli.py` (modify) | Repeatable `--credential-scope` flag (@265-341) + parser helper + Envelope wiring (@1154-1170). |
| `boundary/receipt.py` | No change — already binds spec + verdict (@34-104). |
| `tests/test_credential_proxy.py` (new) | Scope validation, flag compilation, proxy lifecycle (nono-gated). |
| `tests/test_envelope_spec.py` (extend) | Field, spec_dict, spec_hash sensitivity, precondition gate. |
| `tests/test_third_umpire_credential_scope.py` (new) | `credential_scope_held` grading. |
| `tests/test_credential_scope_e2e.py` (new) | Load-bearing security probe (nono+srt gated). |
| `docs/spikes/nono-proxy-runtime.md` (new) | Captured live output shapes from spike task. |

---

## Task 1: Spike — capture nono proxy runtime shapes (UNKNOWNS)

The exact startup connection-info format (URL/token/CA path) and audit shape of `nono proxy` are runtime unknowns. Capture them live before building the parser and `audit()`. **This task is manual/exploratory — no TDD, but the artifact is required (Experiment and Publish).** If `nono` is not installed on the dev machine, STOP and ask the human before proceeding past Task 4 (Tasks 2–4 are pure and can proceed).

**Files:**
- Create: `docs/spikes/nono-proxy-runtime.md`

**Steps:**

- [ ] Check the binary is present:
  ```bash
  which nono && nono --version
  ```
- [ ] Start a proxy with a throwaway scope and capture EVERYTHING it prints on startup (stdout and stderr separately):
  ```bash
  nono proxy --port 0 --credential github --allow-endpoint "github:GET:/repos/*/pulls" > /tmp/nono-stdout.txt 2> /tmp/nono-stderr.txt &
  sleep 2; cat /tmp/nono-stdout.txt; echo ---; cat /tmp/nono-stderr.txt
  ```
- [ ] Record in the spike doc: exact line(s) containing the proxy URL, port, session token, and CA cert path; whether output is JSON or free text; which stream it appears on; how long startup takes.
- [ ] Check for a machine-readable startup option (e.g. `nono proxy --help` for `--json`, `--connection-file`, or similar) and record it. Prefer a machine-readable option over text parsing if one exists.
- [ ] Drive one in-scope and one out-of-scope request through the proxy (using the captured token/CA), then capture the audit shape:
  ```bash
  nono proxy --help | grep -i -A2 "log-file\|audit"
  # then whichever applies:
  nono audit show <session-id> --json
  # and/or restart with --log-file /tmp/nono-audit.log and cat it
  ```
- [ ] Record in the spike doc: the per-request audit record shape (fields for service, method, path, allowed/denied, status), and which source (`--log-file` vs `nono audit show --json`) is usable for a proxy-only session.
- [ ] Kill the proxy; verify the port is released.
- [ ] Write `docs/spikes/nono-proxy-runtime.md` with all captured shapes, labeled `[DATA]`, including exact sample output blocks. Every regex/parser written in Task 4 must cite a sample from this doc.
- [ ] Commit:
  ```bash
  git add docs/spikes/nono-proxy-runtime.md && git commit -m "spike: capture nono proxy startup and audit shapes"
  ```

---

## Task 2: `CredentialScope` dataclass with parse-time validation

**Files:**
- Create: `boundary/credential_proxy.py`
- Test: `tests/test_credential_proxy.py`

**Steps:**

- [ ] Write the failing test:
  ```python
  # tests/test_credential_proxy.py
  import pytest

  from boundary.credential_proxy import CredentialScope


  class TestCredentialScope:
      def test_constructs_with_all_fields(self):
          scope = CredentialScope(
              service="github",
              credential_key="keyring://gh:github.com",
              allow_endpoints=["GET:/repos/*/pulls", "GET:/repos/*/issues"],
          )
          assert scope.service == "github"
          assert scope.credential_key == "keyring://gh:github.com"
          assert scope.allow_endpoints == ["GET:/repos/*/pulls", "GET:/repos/*/issues"]

      def test_empty_allow_endpoints_rejected(self):
          with pytest.raises(ValueError, match="allow_endpoints"):
              CredentialScope(
                  service="github",
                  credential_key="keyring://gh:github.com",
                  allow_endpoints=[],
              )

      def test_as_spec_dict_round_trips_fields(self):
          scope = CredentialScope(
              service="github",
              credential_key="keyring://gh:github.com",
              allow_endpoints=["GET:/repos/*/pulls"],
          )
          assert scope.as_spec_dict() == {
              "service": "github",
              "credential_key": "keyring://gh:github.com",
              "allow_endpoints": ["GET:/repos/*/pulls"],
          }

      def test_from_dict_parses(self):
          scope = CredentialScope.from_dict(
              {
                  "service": "github",
                  "credential_key": "keyring://gh:github.com",
                  "allow_endpoints": ["GET:/repos/*/pulls"],
              }
          )
          assert scope.service == "github"

      def test_from_dict_empty_endpoints_rejected(self):
          with pytest.raises(ValueError, match="allow_endpoints"):
              CredentialScope.from_dict(
                  {
                      "service": "github",
                      "credential_key": "keyring://gh:github.com",
                      "allow_endpoints": [],
                  }
              )
  ```
- [ ] Run it, expect FAIL (module does not exist):
  ```bash
  python -m pytest tests/test_credential_proxy.py -x -q
  ```
- [ ] Minimal implementation:
  ```python
  # boundary/credential_proxy.py
  """Credential-scoping proxy orchestration (nono proxy wrapper).

  No imports from boundary.envelope or boundary.agent — this module is a leaf.
  """
  from __future__ import annotations

  from dataclasses import dataclass, field


  @dataclass
  class CredentialScope:
      service: str
      credential_key: str
      allow_endpoints: list[str] = field(default_factory=list)

      def __post_init__(self) -> None:
          if not self.allow_endpoints:
              raise ValueError(
                  f"credential scope for service {self.service!r} has empty "
                  "allow_endpoints; deny-all scopes are rejected (a credential "
                  "the agent can never use is a footgun, not a policy)"
              )

      def as_spec_dict(self) -> dict:
          return {
              "service": self.service,
              "credential_key": self.credential_key,
              "allow_endpoints": list(self.allow_endpoints),
          }

      @classmethod
      def from_dict(cls, data: dict) -> "CredentialScope":
          return cls(
              service=data["service"],
              credential_key=data["credential_key"],
              allow_endpoints=list(data.get("allow_endpoints", [])),
          )
  ```
- [ ] Run, expect PASS:
  ```bash
  python -m pytest tests/test_credential_proxy.py -x -q
  ```
- [ ] Commit:
  ```bash
  git add boundary/credential_proxy.py tests/test_credential_proxy.py && git commit -m "feat: CredentialScope dataclass, empty allow_endpoints rejected at parse"
  ```

---

## Task 3: Pure `compile_nono_flags()`

**Files:**
- Modify: `boundary/credential_proxy.py`
- Test: `tests/test_credential_proxy.py`

**Steps:**

- [ ] Write the failing test (append to `tests/test_credential_proxy.py`):
  ```python
  from boundary.credential_proxy import compile_nono_flags


  class TestCompileNonoFlags:
      def test_single_scope_single_endpoint(self):
          scopes = [
              CredentialScope(
                  service="github",
                  credential_key="keyring://gh:github.com",
                  allow_endpoints=["GET:/repos/*/pulls"],
              )
          ]
          assert compile_nono_flags(scopes) == [
              "--credential", "github",
              "--allow-endpoint", "github:GET:/repos/*/pulls",
          ]

      def test_multiple_endpoints_and_scopes_preserve_order(self):
          scopes = [
              CredentialScope(
                  service="github",
                  credential_key="keyring://gh:github.com",
                  allow_endpoints=["GET:/repos/*/pulls", "GET:/repos/*/issues"],
              ),
              CredentialScope(
                  service="slack",
                  credential_key="keyring://slack:token",
                  allow_endpoints=["POST:/api/chat.postMessage"],
              ),
          ]
          assert compile_nono_flags(scopes) == [
              "--credential", "github",
              "--allow-endpoint", "github:GET:/repos/*/pulls",
              "--allow-endpoint", "github:GET:/repos/*/issues",
              "--credential", "slack",
              "--allow-endpoint", "slack:POST:/api/chat.postMessage",
          ]

      def test_empty_scopes_yields_empty_flags(self):
          assert compile_nono_flags([]) == []
  ```
- [ ] Run it, expect FAIL:
  ```bash
  python -m pytest tests/test_credential_proxy.py::TestCompileNonoFlags -x -q
  ```
- [ ] Minimal implementation (append to `boundary/credential_proxy.py`):
  ```python
  def compile_nono_flags(scopes: list[CredentialScope]) -> list[str]:
      """Compile scopes into nono proxy CLI flags. Pure function."""
      flags: list[str] = []
      for scope in scopes:
          flags.extend(["--credential", scope.service])
          for endpoint in scope.allow_endpoints:
              flags.extend(["--allow-endpoint", f"{scope.service}:{endpoint}"])
      return flags
  ```
- [ ] Run, expect PASS:
  ```bash
  python -m pytest tests/test_credential_proxy.py -x -q
  ```
- [ ] Commit:
  ```bash
  git add boundary/credential_proxy.py tests/test_credential_proxy.py && git commit -m "feat: pure compile_nono_flags for nono proxy CLI"
  ```

---

## Task 4: `ProxyHandle` + `start_credential_proxy()` (process wrapper)

Depends on Task 1's captured shapes. The connection-info parser and `audit()` below contain `# SPIKE:` markers — replace each with the real format from `docs/spikes/nono-proxy-runtime.md` before running the gated test.

**Files:**
- Modify: `boundary/credential_proxy.py`
- Test: `tests/test_credential_proxy.py`

**Steps:**

- [ ] Write the failing unit tests for the parseable parts (connection-info parsing and `proxy_env()`), using sample text captured in the spike doc:
  ```python
  import shutil

  from boundary.credential_proxy import (
      ProxyHandle,
      parse_connection_info,
      start_credential_proxy,
  )


  class TestParseConnectionInfo:
      def test_parses_url_token_ca_from_startup_output(self):
          # SPIKE: replace this sample with a verbatim block from
          # docs/spikes/nono-proxy-runtime.md
          sample = (
              "nono proxy listening on http://127.0.0.1:54321\n"
              "session token: abc123def456\n"
              "ca certificate: /tmp/nono-ca/session-ca.pem\n"
          )
          info = parse_connection_info(sample)
          assert info == {
              "url": "http://127.0.0.1:54321",
              "port": 54321,
              "token": "abc123def456",
              "ca_path": "/tmp/nono-ca/session-ca.pem",
          }

      def test_incomplete_output_raises(self):
          with pytest.raises(RuntimeError, match="connection info"):
              parse_connection_info("nothing useful here\n")


  class TestProxyEnv:
      def test_proxy_env_sets_proxy_and_ca_vars(self):
          handle = ProxyHandle(
              process=None,
              url="http://127.0.0.1:54321",
              port=54321,
              token="abc123",
              ca_path="/tmp/ca.pem",
              audit_path="/tmp/audit.log",
          )
          env = handle.proxy_env()
          assert env == {
              "HTTP_PROXY": "http://abc123@127.0.0.1:54321",
              "HTTPS_PROXY": "http://abc123@127.0.0.1:54321",
              "http_proxy": "http://abc123@127.0.0.1:54321",
              "https_proxy": "http://abc123@127.0.0.1:54321",
              "NODE_EXTRA_CA_CERTS": "/tmp/ca.pem",
              "SSL_CERT_FILE": "/tmp/ca.pem",
              "CURL_CA_BUNDLE": "/tmp/ca.pem",
              "GIT_SSL_CAINFO": "/tmp/ca.pem",
          }
  ```
- [ ] Run, expect FAIL:
  ```bash
  python -m pytest tests/test_credential_proxy.py::TestParseConnectionInfo tests/test_credential_proxy.py::TestProxyEnv -x -q
  ```
- [ ] Minimal implementation (append to `boundary/credential_proxy.py`):
  ```python
  import json
  import re
  import shutil
  import subprocess
  import time
  from dataclasses import dataclass as _dataclass


  PROXY_READY_TIMEOUT = 10.0  # seconds

  # SPIKE: replace these patterns with ones matching the verbatim samples in
  # docs/spikes/nono-proxy-runtime.md
  _URL_RE = re.compile(r"listening on (http://127\.0\.0\.1:(\d+))")
  _TOKEN_RE = re.compile(r"session token: (\S+)")
  _CA_RE = re.compile(r"ca certificate: (\S+)")


  def parse_connection_info(output: str) -> dict:
      url_m = _URL_RE.search(output)
      token_m = _TOKEN_RE.search(output)
      ca_m = _CA_RE.search(output)
      if not (url_m and token_m and ca_m):
          raise RuntimeError(
              f"could not parse nono proxy connection info from output:\n{output}"
          )
      return {
          "url": url_m.group(1),
          "port": int(url_m.group(2)),
          "token": token_m.group(1),
          "ca_path": ca_m.group(1),
      }


  @_dataclass
  class ProxyHandle:
      process: "subprocess.Popen | None"
      url: str
      port: int
      token: str
      ca_path: str
      audit_path: str

      def proxy_env(self) -> dict[str, str]:
          proxy_url = self.url.replace("http://", f"http://{self.token}@")
          return {
              "HTTP_PROXY": proxy_url,
              "HTTPS_PROXY": proxy_url,
              "http_proxy": proxy_url,
              "https_proxy": proxy_url,
              "NODE_EXTRA_CA_CERTS": self.ca_path,
              "SSL_CERT_FILE": self.ca_path,
              "CURL_CA_BUNDLE": self.ca_path,
              "GIT_SSL_CAINFO": self.ca_path,
          }

      def audit(self) -> list[dict]:
          """Return per-request audit records: dicts with at least
          service, method, path, allowed (bool)."""
          # SPIKE: implement against the shape captured in
          # docs/spikes/nono-proxy-runtime.md (--log-file JSONL assumed here).
          records: list[dict] = []
          try:
              with open(self.audit_path) as f:
                  for line in f:
                      line = line.strip()
                      if line:
                          records.append(json.loads(line))
          except FileNotFoundError:
              pass
          return records

      def close(self) -> None:
          if self.process is not None and self.process.poll() is None:
              self.process.terminate()
              try:
                  self.process.wait(timeout=5)
              except subprocess.TimeoutExpired:
                  self.process.kill()
                  self.process.wait()


  def start_credential_proxy(
      scopes: list[CredentialScope], *, ca_dir: str
  ) -> ProxyHandle:
      if shutil.which("nono") is None:
          raise RuntimeError("nono is not installed; cannot start credential proxy")
      audit_path = f"{ca_dir}/nono-audit.jsonl"
      cmd = [
          "nono", "proxy", "--port", "0",
          "--log-file", audit_path,
          *compile_nono_flags(scopes),
      ]
      process = subprocess.Popen(
          cmd,
          stdout=subprocess.PIPE,
          stderr=subprocess.STDOUT,
          text=True,
          cwd=ca_dir,
      )
      # SPIKE: adjust which stream carries connection info per the spike doc.
      output = ""
      deadline = time.monotonic() + PROXY_READY_TIMEOUT
      while time.monotonic() < deadline:
          if process.poll() is not None:
              output += process.stdout.read() or ""
              raise RuntimeError(
                  f"nono proxy exited during startup (code {process.returncode}):\n{output}"
              )
          line = process.stdout.readline()
          if line:
              output += line
          try:
              info = parse_connection_info(output)
              return ProxyHandle(
                  process=process,
                  url=info["url"],
                  port=info["port"],
                  token=info["token"],
                  ca_path=info["ca_path"],
                  audit_path=audit_path,
              )
          except RuntimeError:
              continue
      process.kill()
      raise RuntimeError(
          f"nono proxy did not become ready within {PROXY_READY_TIMEOUT}s:\n{output}"
      )
  ```
- [ ] Run unit tests, expect PASS:
  ```bash
  python -m pytest tests/test_credential_proxy.py -x -q
  ```
- [ ] Write the nono-gated integration test (append to `tests/test_credential_proxy.py`):
  ```python
  requires_nono = pytest.mark.skipif(
      shutil.which("nono") is None, reason="nono binary not installed"
  )


  @requires_nono
  class TestProxyLifecycle:
      def test_start_ready_env_close(self, tmp_path):
          scopes = [
              CredentialScope(
                  service="github",
                  credential_key="keyring://gh:github.com",
                  allow_endpoints=["GET:/repos/*/pulls"],
              )
          ]
          handle = start_credential_proxy(scopes, ca_dir=str(tmp_path))
          try:
              assert handle.port > 0
              assert handle.url.startswith("http://127.0.0.1:")
              assert handle.token
              env = handle.proxy_env()
              assert env["HTTPS_PROXY"].endswith(f"127.0.0.1:{handle.port}")
              assert env["SSL_CERT_FILE"] == handle.ca_path
          finally:
              handle.close()
          assert handle.process.poll() is not None
  ```
- [ ] Run the gated test (PASS with nono installed; SKIP otherwise — a SKIP is acceptable only on a machine without nono):
  ```bash
  python -m pytest tests/test_credential_proxy.py::TestProxyLifecycle -x -q
  ```
- [ ] If the gated test ran: remove all `# SPIKE:` markers whose code was confirmed against live output; update `docs/spikes/nono-proxy-runtime.md` with anything that surprised you.
- [ ] Commit:
  ```bash
  git add boundary/credential_proxy.py tests/test_credential_proxy.py docs/spikes/nono-proxy-runtime.md && git commit -m "feat: ProxyHandle + start_credential_proxy nono process wrapper"
  ```

---

## Task 5: Envelope field + `spec_dict()` inclusion

**Files:**
- Modify: `boundary/envelope.py` (field after line ~234; `spec_dict()` @350-386)
- Test: `tests/test_envelope_spec.py`

**Steps:**

- [ ] Read `boundary/envelope.py:161-290` and `boundary/envelope.py:350-386` to confirm current line positions before editing.
- [ ] Write the failing test (append to `tests/test_envelope_spec.py`):
  ```python
  from boundary.credential_proxy import CredentialScope
  from boundary.envelope import Envelope


  class TestCredentialScopesField:
      def test_default_is_empty_list(self):
          env = Envelope()
          assert env.credential_scopes == []

      def test_spec_dict_includes_credential_scopes(self):
          env = Envelope(
              credential_scopes=[
                  CredentialScope(
                      service="github",
                      credential_key="keyring://gh:github.com",
                      allow_endpoints=["GET:/repos/*/pulls"],
                  )
              ]
          )
          spec = env.spec_dict()
          assert spec["credential_scopes"] == [
              {
                  "service": "github",
                  "credential_key": "keyring://gh:github.com",
                  "allow_endpoints": ["GET:/repos/*/pulls"],
              }
          ]

      def test_spec_hash_changes_with_credential_scopes(self):
          bare = Envelope()
          scoped = Envelope(
              credential_scopes=[
                  CredentialScope(
                      service="github",
                      credential_key="keyring://gh:github.com",
                      allow_endpoints=["GET:/repos/*/pulls"],
                  )
              ]
          )
          assert bare.spec_hash() != scoped.spec_hash()
  ```
  (If `Envelope()` requires positional args in this repo, mirror the construction pattern already used in `tests/test_envelope_spec.py`.)
- [ ] Run it, expect FAIL:
  ```bash
  python -m pytest tests/test_envelope_spec.py::TestCredentialScopesField -x -q
  ```
- [ ] Minimal implementation in `boundary/envelope.py`:
  - Add import near the top:
    ```python
    from boundary.credential_proxy import CredentialScope
    ```
  - Add the field after `commit_allowlist` (~line 234), matching the existing list-field pattern:
    ```python
    credential_scopes: list[CredentialScope] = field(default_factory=list)
    ```
  - In `spec_dict()` (@350-386), add alongside the other list fields:
    ```python
    "credential_scopes": [s.as_spec_dict() for s in self.credential_scopes],
    ```
  - `spec_hash()`/`canonical_spec_hash()` (@149-158) recurse with `sort_keys` — no change needed.
- [ ] Run, expect PASS:
  ```bash
  python -m pytest tests/test_envelope_spec.py -x -q
  ```
- [ ] Run the full suite to catch spec-hash golden tests that may need updating:
  ```bash
  python -m pytest -x -q
  ```
- [ ] Commit:
  ```bash
  git add boundary/envelope.py tests/test_envelope_spec.py && git commit -m "feat: credential_scopes envelope field, included in spec_dict/spec_hash"
  ```

---

## Task 6: Agent stores scopes + live-read bash wiring

The bash tool must read `egress_allowlist`/`proxy_env` from the agent **at call-time**, because the runner sets `proxy_env` after the proxy starts (which is after tool registration).

**Files:**
- Modify: `boundary/agent.py` (@19-87; attrs @51-53)
- Modify: `boundary/tools/shell.py` (`register_shell_tools` → `_bash`)
- Modify: `boundary/tools/sandbox.py` (`run_sandboxed` @106-114; `_run_srt` @202; `_jail_env` @69-84)
- Test: `tests/test_credential_proxy.py` (sandbox env wiring), existing agent tests

**Steps:**

- [ ] Read `boundary/agent.py:19-87`, `boundary/tools/shell.py`, and `boundary/tools/sandbox.py:69-137,190-210` to confirm current signatures.
- [ ] Write the failing test for `_jail_env` merging (append to `tests/test_credential_proxy.py`):
  ```python
  from boundary.tools.sandbox import _jail_env


  class TestProxyEnvMerging:
      def test_jail_env_merges_proxy_env(self, tmp_path):
          proxy_env = {
              "HTTPS_PROXY": "http://tok@127.0.0.1:5000",
              "SSL_CERT_FILE": "/tmp/ca.pem",
          }
          env = _jail_env(str(tmp_path), proxy_env=proxy_env)
          assert env["HTTPS_PROXY"] == "http://tok@127.0.0.1:5000"
          assert env["SSL_CERT_FILE"] == "/tmp/ca.pem"

      def test_jail_env_without_proxy_env_unchanged(self, tmp_path, monkeypatch):
          monkeypatch.delenv("HTTPS_PROXY", raising=False)
          env = _jail_env(str(tmp_path))
          assert "HTTPS_PROXY" not in env
  ```
  (Match `_jail_env`'s actual positional signature from @69-84 — adjust the first argument if it takes something other than a workspace path.)
- [ ] Run, expect FAIL:
  ```bash
  python -m pytest tests/test_credential_proxy.py::TestProxyEnvMerging -x -q
  ```
- [ ] Implement in `boundary/tools/sandbox.py`:
  - `_jail_env` (@69-84): add keyword param `proxy_env: dict | None = None`; after the existing HOME/TMPDIR patching, add:
    ```python
    if proxy_env:
        env.update(proxy_env)
    ```
  - `run_sandboxed()` (@106-114): add keyword param `proxy_env: dict | None = None`; pass it through the `_run_srt` dispatch (@136-137).
  - `_run_srt` (@202): accept `proxy_env: dict | None = None` and pass it into its `_jail_env(...)` call.
- [ ] Run, expect PASS:
  ```bash
  python -m pytest tests/test_credential_proxy.py::TestProxyEnvMerging -x -q
  ```
- [ ] Implement in `boundary/agent.py` (@51-53 area): store the new state alongside the existing attrs:
  ```python
  self.credential_scopes = credential_scopes or []
  self.proxy_env: dict | None = None  # set by the runner after proxy start
  ```
  with `credential_scopes: list | None = None` added to `__init__`'s signature.
- [ ] Implement in `boundary/tools/shell.py`: inside `_bash`, replace any closure-captured `egress_allowlist` with live reads at call-time, and thread `proxy_env`:
  ```python
  result = run_sandboxed(
      ...,
      egress_allowlist=agent.egress_allowlist,
      proxy_env=agent.proxy_env,
  )
  ```
  (Keep whichever existing kwargs `_bash` already passes — only the two live reads change/append.)
- [ ] Write a failing live-read test (append to `tests/test_credential_proxy.py`), monkeypatching `run_sandboxed` to capture kwargs:
  ```python
  class TestLiveReadWiring:
      def test_bash_reads_proxy_env_at_call_time(self, monkeypatch, tmp_path):
          import boundary.tools.shell as shell_mod

          captured = {}

          def fake_run_sandboxed(*args, **kwargs):
              captured.update(kwargs)
              class R:
                  returncode = 0
                  stdout = ""
                  stderr = ""
              return R()

          monkeypatch.setattr(shell_mod, "run_sandboxed", fake_run_sandboxed)

          # Build a minimal agent the same way existing shell tests do
          # (mirror the fixture/construction pattern in tests/test_sandbox_driver.py).
          from boundary.agent import Agent
          agent = Agent(sandbox_driver="none", egress_allowlist=[], workspace_root=str(tmp_path))
          tools = shell_mod.register_shell_tools(agent)
          bash = next(t for t in tools if t.__name__ == "_bash" or getattr(t, "name", "") == "bash")

          # Simulate the runner setting proxy_env AFTER registration:
          agent.proxy_env = {"HTTPS_PROXY": "http://tok@127.0.0.1:5000"}
          agent.egress_allowlist = ["127.0.0.1", "localhost"]

          bash("echo hi")
          assert captured["proxy_env"] == {"HTTPS_PROXY": "http://tok@127.0.0.1:5000"}
          assert captured["egress_allowlist"] == ["127.0.0.1", "localhost"]
  ```
  (Adjust `Agent(...)` construction and the tool lookup to the repo's actual shapes at `boundary/agent.py:19-87` / `register_shell_tools` — the assertion is the contract: values set on the agent *after* registration reach `run_sandboxed`.)
- [ ] Run, expect FAIL, then fix `_bash` until PASS:
  ```bash
  python -m pytest tests/test_credential_proxy.py::TestLiveReadWiring -x -q
  ```
- [ ] Run the full suite (no regression in existing sandbox/shell tests):
  ```bash
  python -m pytest -x -q
  ```
- [ ] Commit:
  ```bash
  git add boundary/agent.py boundary/tools/shell.py boundary/tools/sandbox.py tests/test_credential_proxy.py && git commit -m "feat: thread proxy_env through sandbox; bash reads egress/proxy_env live from agent"
  ```

---

## Task 7: Fail-closed precondition gate in `EnvelopeRunner.run()`

When `credential_scopes` is non-empty, refuse the run unless: `nono` installed, resolved driver == `srt`, and the proxy comes up. The gate lives after the enforced registry is built (~line 1042), BEFORE the agent loop (@1065).

**Files:**
- Modify: `boundary/envelope.py` (@~1042, before @1065)
- Test: `tests/test_envelope_spec.py`

**Steps:**

- [ ] Read `boundary/envelope.py:1003-1065` to confirm where the enforced registry finishes and how the runner reports fatal refusals (find the existing srt fail-closed / `require_srt_for_bash` refusal path and mirror its exception/exit style exactly).
- [ ] Write the failing tests (append to `tests/test_envelope_spec.py`):
  ```python
  import shutil

  import pytest

  from boundary.envelope import CredentialScopePreconditionError, check_credential_scope_preconditions


  SCOPES = [
      CredentialScope(
          service="github",
          credential_key="keyring://gh:github.com",
          allow_endpoints=["GET:/repos/*/pulls"],
      )
  ]


  class TestCredentialScopePreconditions:
      def test_no_scopes_no_check(self):
          check_credential_scope_preconditions([], resolved_driver="none")  # no raise

      def test_refuses_when_nono_missing(self, monkeypatch):
          monkeypatch.setattr(shutil, "which", lambda name: None)
          with pytest.raises(CredentialScopePreconditionError, match="nono"):
              check_credential_scope_preconditions(SCOPES, resolved_driver="srt")

      def test_refuses_when_driver_not_srt(self, monkeypatch):
          monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/nono")
          with pytest.raises(CredentialScopePreconditionError, match="srt"):
              check_credential_scope_preconditions(SCOPES, resolved_driver="seatbelt")

      def test_passes_with_nono_and_srt(self, monkeypatch):
          monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/nono")
          check_credential_scope_preconditions(SCOPES, resolved_driver="srt")  # no raise
  ```
- [ ] Run, expect FAIL:
  ```bash
  python -m pytest tests/test_envelope_spec.py::TestCredentialScopePreconditions -x -q
  ```
- [ ] Minimal implementation in `boundary/envelope.py` (module level, near the runner):
  ```python
  import shutil


  class CredentialScopePreconditionError(RuntimeError):
      """Fail-closed refusal: credential_scopes set but the enforcement stack is unavailable."""


  def check_credential_scope_preconditions(
      scopes: list[CredentialScope], *, resolved_driver: str
  ) -> None:
      if not scopes:
          return
      if shutil.which("nono") is None:
          raise CredentialScopePreconditionError(
              "credential_scopes set but nono is not installed; refusing to run "
              "(fail closed). Install nono or remove credential_scopes."
          )
      if resolved_driver != "srt":
          raise CredentialScopePreconditionError(
              f"credential_scopes set but sandbox driver resolved to "
              f"{resolved_driver!r}, not 'srt'; only srt OS-forces egress through "
              "the credential proxy. Refusing to run (fail closed)."
          )
  ```
  (The proxy-startup timeout is the third precondition; it fires naturally when `start_credential_proxy` raises in Task 8 — the runner treats that raise as the same refusal class.)
- [ ] Run, expect PASS:
  ```bash
  python -m pytest tests/test_envelope_spec.py::TestCredentialScopePreconditions -x -q
  ```
- [ ] Wire the gate into `EnvelopeRunner.run()` after the enforced registry is built (~1042), before the loop (@1065):
  ```python
  check_credential_scope_preconditions(
      self.envelope.credential_scopes, resolved_driver=resolved_driver
  )
  ```
  (Use the runner's actual resolved-driver variable name from @1003-1042; ensure the raised error surfaces as exit code 2 with the message — mirror how the existing srt fail-closed refusal exits.)
- [ ] Run the full suite:
  ```bash
  python -m pytest -x -q
  ```
- [ ] Commit:
  ```bash
  git add boundary/envelope.py tests/test_envelope_spec.py && git commit -m "feat: fail-closed preconditions for credential_scopes (nono + srt required)"
  ```

---

## Task 8: Runner proxy lifecycle — start, wire, teardown in `finally`

**Files:**
- Modify: `boundary/envelope.py` (`run()` @1003-1417: start log @1022-1039, gate ~1042, loop @1065-1342 wrapped in try/finally, end log @1365-1395)
- Test: `tests/test_envelope_spec.py`

**Steps:**

- [ ] Read `boundary/envelope.py:1003-1100` and @1342-1417 to confirm the loop boundaries and how `envelope_start`/`envelope_end` events are logged.
- [ ] Write the failing test with a fake proxy (append to `tests/test_envelope_spec.py`):
  ```python
  class FakeProxyHandle:
      def __init__(self):
          self.closed = False

      def proxy_env(self):
          return {"HTTPS_PROXY": "http://tok@127.0.0.1:5000", "SSL_CERT_FILE": "/tmp/ca.pem"}

      def audit(self):
          return []

      def close(self):
          self.closed = True


  class TestRunnerProxyLifecycle:
      def _scoped_runner(self, monkeypatch, tmp_path, fake):
          import boundary.envelope as env_mod
          monkeypatch.setattr(shutil, "which", lambda name: f"/usr/local/bin/{name}")
          monkeypatch.setattr(env_mod, "start_credential_proxy", lambda scopes, *, ca_dir: fake)
          # Build the runner the same way existing EnvelopeRunner tests in this file do,
          # with envelope.credential_scopes = SCOPES and sandbox_driver = "srt".
          ...

      def test_proxy_started_env_set_and_closed_on_success(self, monkeypatch, tmp_path):
          fake = FakeProxyHandle()
          runner = self._scoped_runner(monkeypatch, tmp_path, fake)
          runner.run()
          assert fake.closed is True

      def test_proxy_closed_even_when_agent_loop_raises(self, monkeypatch, tmp_path):
          fake = FakeProxyHandle()
          runner = self._scoped_runner(monkeypatch, tmp_path, fake)
          # Make the agent loop raise (monkeypatch the agent step/loop entry point
          # used by run() — mirror how existing runner-failure tests do this).
          ...
          with pytest.raises(Exception):
              runner.run()
          assert fake.closed is True

      def test_agent_egress_forced_loopback_and_proxy_env_set(self, monkeypatch, tmp_path):
          fake = FakeProxyHandle()
          runner = self._scoped_runner(monkeypatch, tmp_path, fake)
          runner.run()
          agent = runner.agent  # or however run() exposes it — mirror existing tests
          assert agent.egress_allowlist == ["127.0.0.1", "localhost"]
          assert agent.proxy_env == fake.proxy_env()
  ```
  Fill each `...` by copying the construction/monkeypatch pattern from the existing `EnvelopeRunner` tests in `tests/test_envelope_spec.py` — the contracts under test are: (a) `close()` always called, including on exception; (b) egress overridden to loopback-only; (c) `agent.proxy_env` set from the handle.
- [ ] Run, expect FAIL:
  ```bash
  python -m pytest tests/test_envelope_spec.py::TestRunnerProxyLifecycle -x -q
  ```
- [ ] Implement in `EnvelopeRunner.run()`:
  - Import at module top: `from boundary.credential_proxy import start_credential_proxy` (import the name into `envelope`'s namespace so tests can monkeypatch `env_mod.start_credential_proxy`).
  - In the `envelope_start` log (@1022-1039), add:
    ```python
    "credential_scopes": [s.as_spec_dict() for s in self.envelope.credential_scopes],
    ```
  - After the precondition gate (~1042), before the loop:
    ```python
    proxy_handle = None
    if self.envelope.credential_scopes:
        import tempfile
        proxy_scratch = tempfile.mkdtemp(prefix="boundary-credproxy-")
        try:
            proxy_handle = start_credential_proxy(
                self.envelope.credential_scopes, ca_dir=proxy_scratch
            )
        except RuntimeError as exc:
            raise CredentialScopePreconditionError(
                f"credential proxy failed to start: {exc}"
            ) from exc
        agent.egress_allowlist = ["127.0.0.1", "localhost"]
        agent.proxy_env = proxy_handle.proxy_env()
    ```
    (Use the runner's actual agent variable name; the live-read wiring from Task 6 makes these post-registration assignments effective.)
  - Wrap the agent loop (@1065-1342) in try/finally:
    ```python
    try:
        # existing agent loop, unchanged
        ...
    finally:
        if proxy_handle is not None:
            audit_records = proxy_handle.audit()
            proxy_handle.close()
            import shutil as _sh
            _sh.rmtree(proxy_scratch, ignore_errors=True)
    ```
  - Emit violation events from the audit before the `envelope_end` log (@1365-1395): for each record with `allowed == False`, append to the runner's event list (same list that feeds `envelope_events`):
    ```python
    if proxy_handle is not None:
        for rec in audit_records:
            if not rec.get("allowed", True):
                events.append({
                    "event": "credential_scope_violation",
                    "service": rec.get("service"),
                    "method": rec.get("method"),
                    "path": rec.get("path"),
                })
    ```
    (Use the runner's actual events-collection mechanism from @1365-1395; field names per the spike doc.)
  - In the `envelope_end` log, add `"credential_scopes_enforced": proxy_handle is not None`.
- [ ] Run, expect PASS:
  ```bash
  python -m pytest tests/test_envelope_spec.py::TestRunnerProxyLifecycle -x -q
  ```
- [ ] Run the full suite:
  ```bash
  python -m pytest -x -q
  ```
- [ ] Commit:
  ```bash
  git add boundary/envelope.py tests/test_envelope_spec.py && git commit -m "feat: runner starts credential proxy, forces loopback egress, guaranteed teardown"
  ```

---

## Task 9: Third Umpire `credential_scope_held` check

**Files:**
- Modify: `boundary/third_umpire.py` (mirror `egress_uncontained` @510-526; transcript readers @151-155)
- Test: `tests/test_third_umpire_credential_scope.py`

**Steps:**

- [ ] Read `boundary/third_umpire.py:140-160` and @500-530 to confirm the `CheckResult` construction and how `egress_uncontained` reads `envelope_start`/`envelope_end`/`envelope_events`.
- [ ] Write the failing test:
  ```python
  # tests/test_third_umpire_credential_scope.py
  # Mirror the transcript-fixture pattern used by the egress_uncontained tests
  # (see how existing third_umpire tests build envelope_start/end/events).
  from boundary.third_umpire import ThirdUmpire  # adjust to actual entry point


  SCOPED_START = {
      "event": "envelope_start",
      "credential_scopes": [
          {
              "service": "github",
              "credential_key": "keyring://gh:github.com",
              "allow_endpoints": ["GET:/repos/*/pulls"],
          }
      ],
  }


  def _grade(events, start=SCOPED_START):
      # Build a transcript the same way tests for egress_uncontained do,
      # run the umpire, and return the credential_scope_held CheckResult.
      ...


  class TestCredentialScopeHeld:
      def test_no_scopes_check_is_info_not_applicable(self):
          result = _grade(events=[], start={"event": "envelope_start", "credential_scopes": []})
          assert result.passed is True
          assert "no credential scopes" in result.detail.lower()

      def test_scopes_and_no_violations_passes(self):
          result = _grade(events=[])
          assert result.passed is True

      def test_violation_event_fails(self):
          result = _grade(events=[
              {
                  "event": "credential_scope_violation",
                  "service": "github",
                  "method": "POST",
                  "path": "/repos/x/issues",
              }
          ])
          assert result.passed is False
          assert "POST" in result.detail and "/repos/x/issues" in result.detail
  ```
  Fill `_grade` by copying the fixture construction from the existing `egress_uncontained` tests.
- [ ] Run, expect FAIL:
  ```bash
  python -m pytest tests/test_third_umpire_credential_scope.py -x -q
  ```
- [ ] Minimal implementation in `boundary/third_umpire.py`, placed adjacent to `egress_uncontained` (@510-526) and following its exact CheckResult shape:
  ```python
  def _check_credential_scope_held(self) -> CheckResult:
      scopes = (self.envelope_start or {}).get("credential_scopes", [])
      if not scopes:
          return CheckResult(
              name="credential_scope_held",
              passed=True,
              detail="no credential scopes declared; check not applicable",
          )
      violations = [
          e for e in self.envelope_events
          if e.get("event") == "credential_scope_violation"
      ]
      if violations:
          lines = ", ".join(
              f"{v.get('service')}:{v.get('method')}:{v.get('path')}" for v in violations
          )
          return CheckResult(
              name="credential_scope_held",
              passed=False,
              detail=f"{len(violations)} out-of-scope credential attempt(s) "
                     f"(blocked at proxy, but the agent tried): {lines}",
          )
      return CheckResult(
          name="credential_scope_held",
          passed=True,
          detail=f"{len(scopes)} credential scope(s) enforced; zero out-of-scope attempts",
      )
  ```
  Register it in the same check-list/dispatch mechanism that runs `egress_uncontained`. (Adjust attribute names `envelope_start`/`envelope_events` to match @151-155.)
- [ ] Run, expect PASS:
  ```bash
  python -m pytest tests/test_third_umpire_credential_scope.py -x -q
  ```
- [ ] Verify the receipt binding needs no code: run the existing receipt tests and confirm `Receipt.build()` (@34-104) already carries `spec` (which now contains `credential_scopes` from Task 5) and `verdict` (which now contains `credential_scope_held`):
  ```bash
  python -m pytest tests/ -k receipt -q
  ```
- [ ] Commit:
  ```bash
  git add boundary/third_umpire.py tests/test_third_umpire_credential_scope.py && git commit -m "feat: Third Umpire credential_scope_held check graded from violation events"
  ```

---

## Task 10: CLI `--credential-scope` flag

**Files:**
- Modify: `boundary/cli.py` (run parser @265-341; Envelope construction @1154-1170)
- Test: `tests/test_envelope_spec.py` (or the repo's existing CLI test file if one exists — check first with `Glob tests/test_cli*`)

**Steps:**

- [ ] Read `boundary/cli.py:265-341` (existing repeatable-flag pattern: `action="append", default=[]`) and @1154-1170.
- [ ] Write the failing test for the parser helper:
  ```python
  from boundary.cli import parse_credential_scope_arg


  class TestParseCredentialScopeArg:
      def test_parses_single_endpoint(self):
          scope = parse_credential_scope_arg(
              "service=github,key=keyring://gh:github.com,endpoint=GET:/repos/*/pulls"
          )
          assert scope.service == "github"
          assert scope.credential_key == "keyring://gh:github.com"
          assert scope.allow_endpoints == ["GET:/repos/*/pulls"]

      def test_parses_repeated_endpoints(self):
          scope = parse_credential_scope_arg(
              "service=github,key=keyring://gh:github.com,"
              "endpoint=GET:/repos/*/pulls,endpoint=GET:/repos/*/issues"
          )
          assert scope.allow_endpoints == ["GET:/repos/*/pulls", "GET:/repos/*/issues"]

      def test_missing_endpoint_rejected(self):
          with pytest.raises(ValueError, match="allow_endpoints"):
              parse_credential_scope_arg("service=github,key=keyring://gh:github.com")

      def test_missing_service_rejected(self):
          with pytest.raises(ValueError, match="service"):
              parse_credential_scope_arg("key=keyring://x,endpoint=GET:/a")

      def test_unknown_key_rejected(self):
          with pytest.raises(ValueError, match="unknown"):
              parse_credential_scope_arg("service=x,key=keyring://x,endpoint=GET:/a,bogus=1")
  ```
- [ ] Run, expect FAIL:
  ```bash
  python -m pytest tests/test_envelope_spec.py::TestParseCredentialScopeArg -x -q
  ```
- [ ] Minimal implementation in `boundary/cli.py`:
  ```python
  from boundary.credential_proxy import CredentialScope


  def parse_credential_scope_arg(raw: str) -> CredentialScope:
      """Parse 'service=..,key=..,endpoint=..,endpoint=..' into a CredentialScope.

      Values (keyring refs, endpoint globs) contain ':' and '/' but never ','
      — split on ',' then on the first '='.
      """
      service = None
      key = None
      endpoints: list[str] = []
      for part in raw.split(","):
          if "=" not in part:
              raise ValueError(f"malformed credential-scope segment: {part!r}")
          k, v = part.split("=", 1)
          if k == "service":
              service = v
          elif k == "key":
              key = v
          elif k == "endpoint":
              endpoints.append(v)
          else:
              raise ValueError(f"unknown credential-scope key: {k!r}")
      if not service:
          raise ValueError("credential-scope missing required 'service='")
      if not key:
          raise ValueError("credential-scope missing required 'key='")
      return CredentialScope(
          service=service, credential_key=key, allow_endpoints=endpoints
      )
  ```
- [ ] Run, expect PASS:
  ```bash
  python -m pytest tests/test_envelope_spec.py::TestParseCredentialScopeArg -x -q
  ```
- [ ] Wire the flag: in the run parser (@265-341), following the existing repeatable-flag pattern:
  ```python
  run_parser.add_argument(
      "--credential-scope",
      action="append",
      default=[],
      dest="credential_scopes",
      metavar="service=NAME,key=keyring://...,endpoint=METHOD:/path[,endpoint=...]",
      help="Scope a credential to specific HTTP method+path patterns, enforced "
           "via nono proxy under srt. Repeatable.",
  )
  ```
  In the Envelope construction (@1154-1170):
  ```python
  credential_scopes=[parse_credential_scope_arg(s) for s in args.credential_scopes],
  ```
  Wrap the list comprehension so a `ValueError` becomes the CLI's standard argument-error exit (mirror how other parse failures are reported at @1154-1170).
- [ ] Write and run an end-to-end argparse test (mirror the repo's existing CLI-parse test pattern) asserting `boundary run --credential-scope service=github,key=keyring://gh:github.com,endpoint=GET:/repos/*/pulls ...` produces an Envelope with one scope; expect PASS:
  ```bash
  python -m pytest tests/ -k credential -q
  ```
- [ ] Commit:
  ```bash
  git add boundary/cli.py tests/test_envelope_spec.py && git commit -m "feat: repeatable --credential-scope CLI flag wired into Envelope"
  ```

---

## Task 11: Load-bearing security probe (live nono + srt)

The probe that makes the guarantee real: under a live stack, an out-of-scope method+path is **refused (403)**, and the **real credential never appears** in the jailed caller's environment — phantom-only inside, real only upstream of the proxy.

**Files:**
- Create: `tests/test_credential_scope_e2e.py`

**Steps:**

- [ ] Read `tests/test_sandbox_driver.py` to copy its exact `shutil.which` + `@pytest.mark.skipif` gating and srt-invocation pattern.
- [ ] Write the probe:
  ```python
  # tests/test_credential_scope_e2e.py
  """Load-bearing security probe: credential scoping under live nono + srt.

  Requires: nono and srt on PATH, and a keyring entry
  'boundary-e2e-test' holding the sentinel value REAL_SECRET (set up in-test).
  Skips otherwise.
  """
  import shutil
  import subprocess

  import pytest

  from boundary.credential_proxy import CredentialScope, start_credential_proxy
  from boundary.tools.sandbox import run_sandboxed

  requires_stack = pytest.mark.skipif(
      shutil.which("nono") is None or shutil.which("srt") is None,
      reason="nono and srt binaries required for credential-scope e2e probe",
  )

  REAL_SECRET = "boundary-e2e-real-secret-sentinel-2c7f"
  SERVICE = "boundary-e2e-test"


  @pytest.fixture
  def keyring_secret():
      # SPIKE: use the keyring-population mechanism nono documents for its
      # keyring:// refs (verify in docs/spikes/nono-proxy-runtime.md).
      subprocess.run(
          ["security", "add-generic-password", "-a", SERVICE, "-s", SERVICE,
           "-w", REAL_SECRET, "-U"],
          check=True,
      )
      yield
      subprocess.run(
          ["security", "delete-generic-password", "-a", SERVICE, "-s", SERVICE],
          check=False,
      )


  @requires_stack
  class TestCredentialScopeProbe:
      def _proxy(self, tmp_path):
          scopes = [
              CredentialScope(
                  service=SERVICE,
                  credential_key=f"keyring://{SERVICE}:{SERVICE}",
                  allow_endpoints=["GET:/get"],
              )
          ]
          return start_credential_proxy(scopes, ca_dir=str(tmp_path))

      def test_out_of_scope_refused_403(self, tmp_path, keyring_secret):
          handle = self._proxy(tmp_path)
          try:
              result = run_sandboxed(
                  'curl -s -o /dev/null -w "%{http_code}" -X POST https://httpbin.org/post',
                  workspace_root=str(tmp_path),
                  driver="srt",
                  egress_allowlist=["127.0.0.1", "localhost"],
                  proxy_env=handle.proxy_env(),
                  timeout=60,
              )
              assert result.stdout.strip() == "403", (
                  f"out-of-scope POST was not refused: {result.stdout!r} {result.stderr!r}"
              )
          finally:
              handle.close()

      def test_in_scope_allowed_and_injected(self, tmp_path, keyring_secret):
          handle = self._proxy(tmp_path)
          try:
              result = run_sandboxed(
                  "curl -s https://httpbin.org/get",
                  workspace_root=str(tmp_path),
                  driver="srt",
                  egress_allowlist=["127.0.0.1", "localhost"],
                  proxy_env=handle.proxy_env(),
                  timeout=60,
              )
              assert result.returncode == 0
              # httpbin echoes request headers: the REAL secret was injected
              # upstream of the jail by the proxy.
              assert REAL_SECRET in result.stdout, (
                  "real credential was not injected into the upstream request"
              )
          finally:
              handle.close()

      def test_real_credential_absent_inside_jail(self, tmp_path, keyring_secret):
          """The phantom-token guarantee: the jailed caller can dump its entire
          environment and never see the real secret."""
          handle = self._proxy(tmp_path)
          try:
              result = run_sandboxed(
                  "env",
                  workspace_root=str(tmp_path),
                  driver="srt",
                  egress_allowlist=["127.0.0.1", "localhost"],
                  proxy_env=handle.proxy_env(),
                  timeout=60,
              )
              assert REAL_SECRET not in result.stdout, (
                  "REAL CREDENTIAL LEAKED into the jailed caller's environment"
              )
              assert REAL_SECRET not in result.stderr
          finally:
              handle.close()

      def test_external_host_unreachable_without_proxy(self, tmp_path, keyring_secret):
          """Loopback-only egress: the agent cannot sidestep the proxy."""
          handle = self._proxy(tmp_path)
          try:
              result = run_sandboxed(
                  "curl -s --noproxy '*' --max-time 10 https://httpbin.org/get",
                  workspace_root=str(tmp_path),
                  driver="srt",
                  egress_allowlist=["127.0.0.1", "localhost"],
                  proxy_env=handle.proxy_env(),
                  timeout=60,
              )
              assert result.returncode != 0, (
                  "direct external egress succeeded — srt loopback jail is not holding"
              )
          finally:
              handle.close()
  ```
  (Adjust `run_sandboxed` kwargs to its actual signature @106-114; verify against the spike doc how nono presents the credential to httpbin — header name may need adjusting in the injection assertion; if the associated `--allow-endpoint` service prefix targets a specific upstream host, encode that per the spike findings.)
- [ ] Run on a machine with the full stack — all four probes must PASS (skips do not count as done for this task on the designated verification machine):
  ```bash
  python -m pytest tests/test_credential_scope_e2e.py -x -q
  ```
- [ ] Record the probe run output (pass/fail lines) as a `[DATA]` block appended to `docs/spikes/nono-proxy-runtime.md`.
- [ ] Commit:
  ```bash
  git add tests/test_credential_scope_e2e.py docs/spikes/nono-proxy-runtime.md && git commit -m "test: load-bearing probe — 403 out-of-scope, phantom-only in jail, loopback egress holds"
  ```

---

## Task 12: Full-suite verification + audit-shape reconciliation

**Files:**
- Modify: `boundary/credential_proxy.py` (only if Task 11 revealed audit-shape drift)
- Modify: `boundary/envelope.py` (only if violation-event field names drifted)

**Steps:**

- [ ] Reconcile: compare the audit records produced during Task 11's live run against the field names assumed in `ProxyHandle.audit()` and the runner's violation-event emission (Task 8: `allowed`/`service`/`method`/`path`). Fix any drift, updating the corresponding unit tests first (failing test → fix → pass).
- [ ] Confirm zero remaining `# SPIKE:` markers:
  ```bash
  grep -rn "SPIKE:" boundary/ tests/ && echo "MARKERS REMAIN — fix before done" || echo "clean"
  ```
- [ ] Run the entire test suite:
  ```bash
  python -m pytest -q
  ```
- [ ] Run one manual smoke of the full CLI path (with stack installed):
  ```bash
  python -m boundary run --sandbox-driver srt \
    --credential-scope "service=boundary-e2e-test,key=keyring://boundary-e2e-test:boundary-e2e-test,endpoint=GET:/get" \
    "fetch https://httpbin.org/get and summarize it"
  ```
  Verify: run completes, receipt contains `credential_scopes` in spec, verdict contains `credential_scope_held: pass`.
- [ ] Run the fail-closed smoke (driver deliberately wrong) and verify exit code 2 with a loud reason:
  ```bash
  python -m boundary run --sandbox-driver seatbelt \
    --credential-scope "service=x,key=keyring://x:x,endpoint=GET:/a" "noop"; echo "exit=$?"
  ```
- [ ] Commit any reconciliation changes:
  ```bash
  git add -A && git commit -m "fix: reconcile audit shape and violation events against live nono output"
  ```

---

## Definition of Done

- [ ] `CredentialScope` is structured (`service`, `credential_key`, `allow_endpoints`); empty `allow_endpoints` raises `ValueError` at parse — unit-tested.
- [ ] `compile_nono_flags()` is pure and unit-tested (single/multiple scopes, order preserved).
- [ ] `start_credential_proxy()` spawns `nono proxy --port 0`, parses connection info per the spike-captured format, returns a `ProxyHandle` with `proxy_env()` (HTTP(S)_PROXY + NODE_EXTRA_CA_CERTS/SSL_CERT_FILE/CURL_CA_BUNDLE/GIT_SSL_CAINFO), `audit()`, `close()` — nono-gated test passes live.
- [ ] `Envelope.credential_scopes` exists, appears in `spec_dict()`, and changes `spec_hash()` — receipt binds policy→verdict with zero receipt-code changes.
- [ ] Fail-closed: `credential_scopes` non-empty + (no nono | driver != srt | proxy startup timeout) → refusal with exit 2 and a loud reason — unit-tested for all three legs.
- [ ] Runner wraps the agent loop in try/finally; the proxy is always torn down and scratch removed, including on agent-loop exceptions — unit-tested with a fake handle.
- [ ] Bash tool reads `egress_allowlist`/`proxy_env` live from the agent at call-time; `proxy_env` threads run_sandboxed→_run_srt→_jail_env — unit-tested.
- [ ] Third Umpire `credential_scope_held`: info-pass with no scopes, pass with scopes + zero violations, FAIL listing each `credential_scope_violation` event — unit-tested.
- [ ] CLI `--credential-scope` is repeatable, parsed by a tested helper, rejects malformed/endpoint-less input, and wires into `Envelope(...)`.
- [ ] Load-bearing probe passes live under nono+srt: out-of-scope → 403; in-scope → injected upstream; real secret absent from the jailed env; direct external egress refused by srt.
- [ ] `docs/spikes/nono-proxy-runtime.md` exists with `[DATA]`-labeled samples backing every parser/audit assumption; zero `# SPIKE:` markers remain in code.
- [ ] `python -m pytest -q` is green (gated tests skip cleanly on machines without the binaries).
