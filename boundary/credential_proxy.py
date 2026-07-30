"""Credential-scoping proxy orchestration (nono proxy wrapper).

No imports from boundary.envelope or boundary.agent — this module is a leaf.

Runtime shapes (nono 0.70.0) are documented in docs/spikes/nono-proxy-runtime.md;
every parser/flag here cites a sample from that spike.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field


@dataclass
class CredentialScope:
    """One credential the agent may wield, confined to specific endpoints.

    - service: nono credential route name (e.g. "github").
    - host: the route's host (e.g. "api.github.com"), used to emit --allow-domain
      for hard blocking. nono owns the service->host table; we carry it explicitly
      rather than shadowing that table (see spike doc, section 3).
    - credential_key: nono credential reference — "env://VAR" or a macOS-keychain
      account name (NOT "keyring://..."; that scheme does not exist in nono).
    - allow_endpoints: "METHOD:/path" globs ("*" one segment, "**" zero-or-more).
    """

    service: str
    host: str
    credential_key: str
    allow_endpoints: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.allow_endpoints:
            raise ValueError(
                f"credential scope for service {self.service!r} has empty "
                "allow_endpoints; deny-all scopes are rejected (a credential "
                "the agent can never use is a footgun, not a policy)"
            )
        if not self.host:
            raise ValueError(
                f"credential scope for service {self.service!r} has empty host; "
                "a host is required to emit the --allow-domain hard block"
            )
        for endpoint in self.allow_endpoints:
            method, _, path = endpoint.partition(":")
            if not method or not path.startswith("/"):
                raise ValueError(
                    f"credential scope for service {self.service!r} has malformed "
                    f"endpoint {endpoint!r}; expected 'METHOD:/path' "
                    "(e.g. 'GET:/repos/*/pulls')"
                )

    def as_spec_dict(self) -> dict:
        return {
            "service": self.service,
            "host": self.host,
            "credential_key": self.credential_key,
            "allow_endpoints": list(self.allow_endpoints),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CredentialScope":
        return cls(
            service=data["service"],
            host=data["host"],
            credential_key=data["credential_key"],
            allow_endpoints=list(data.get("allow_endpoints", [])),
        )


def compile_nono_flags(scopes: list[CredentialScope]) -> list[str]:
    """Compile scopes into nono proxy CLI flags. Pure function.

    Per scope, emits three flag families (see docs/spikes/nono-proxy-runtime.md):
      --credential <service>                 credential injection route
      --allow-endpoint <service>:METHOD:/p   confine which endpoints get the cred
      --allow-domain https://<host>/p        hard block (403) outside the paths

    --allow-domain is method-agnostic, so paths are de-duplicated across methods.
    """
    flags: list[str] = []
    for scope in scopes:
        flags.extend(["--credential", scope.service])
        domains: list[str] = []
        for endpoint in scope.allow_endpoints:
            flags.extend(["--allow-endpoint", f"{scope.service}:{endpoint}"])
            path = endpoint.partition(":")[2]
            domain = f"https://{scope.host}{path}"
            if domain not in domains:
                domains.append(domain)
        for domain in domains:
            flags.extend(["--allow-domain", domain])
    return flags


PROXY_READY_TIMEOUT = 10.0  # seconds

# Startup banner anchors (stdout, free text) — see spike doc section 1.
_PORT_RE = re.compile(r"nono proxy listening on 127\.0\.0\.1:(\d+)")
_URL_RE = re.compile(r"proxy URL:\s+(\S+)")
_TOKEN_RE = re.compile(r"^\s*token:\s+([a-f0-9]{8,})", re.MULTILINE)
_CA_RE = re.compile(r"TLS interception trust bundle:\s+(\S+intercept-ca\.pem)")

# -vv --log-file decision anchors — see spike doc section 4.
_AUDIT_ALLOW_RE = re.compile(
    r'l7 endpoint policy decision.*?method="(\w+)" path="([^"]+)" decision=Allow'
)
_AUDIT_DENY_RE = re.compile(r"endpoint rules denied (\w+) (\S+?):")


def parse_connection_info(output: str) -> dict:
    """Parse nono proxy's free-text startup banner into structured fields."""
    port_m = _PORT_RE.search(output)
    url_m = _URL_RE.search(output)
    token_m = _TOKEN_RE.search(output)
    ca_m = _CA_RE.search(output)
    if not (port_m and url_m and token_m and ca_m):
        raise RuntimeError(
            f"could not parse nono proxy connection info from output:\n{output}"
        )
    return {
        "url": url_m.group(1),
        "port": int(port_m.group(1)),
        "token": token_m.group(1),
        "ca_path": ca_m.group(1),
    }


@dataclass
class ProxyHandle:
    process: "subprocess.Popen | None"
    url: str
    port: int
    token: str
    ca_path: str
    audit_path: str

    def proxy_env(self) -> dict[str, str]:
        """Env vars to inject into the jailed caller so all HTTP(S) egress is
        forced through the proxy and the proxy's CA is trusted. The nono URL
        already embeds Basic auth (nono:<token>@host:port), so it is used verbatim.
        """
        return {
            "HTTP_PROXY": self.url,
            "HTTPS_PROXY": self.url,
            "http_proxy": self.url,
            "https_proxy": self.url,
            "NODE_EXTRA_CA_CERTS": self.ca_path,
            "SSL_CERT_FILE": self.ca_path,
            "CURL_CA_BUNDLE": self.ca_path,
            "GIT_SSL_CAINFO": self.ca_path,
        }

    def audit(self) -> list[dict]:
        """Per-request decisions from the -vv log: {method, path, allowed}.

        Duplicate decision lines (nono logs one per matched rule) are collapsed.
        """
        records: list[dict] = []
        seen: set[tuple[str, str, bool]] = set()
        try:
            with open(self.audit_path) as f:
                lines = f.readlines()
        except FileNotFoundError:
            return records
        for line in lines:
            allow_m = _AUDIT_ALLOW_RE.search(line)
            if allow_m:
                rec = (allow_m.group(1), allow_m.group(2), True)
            else:
                deny_m = _AUDIT_DENY_RE.search(line)
                if not deny_m:
                    continue
                rec = (deny_m.group(1), deny_m.group(2), False)
            if rec not in seen:
                seen.add(rec)
                records.append(
                    {"method": rec[0], "path": rec[1], "allowed": rec[2]}
                )
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
    """Spawn a standalone `nono proxy` for the given scopes and wait until ready.

    The proxy inherits this process's environment so nono can resolve each
    service's credential (env://VAR or macOS keychain); boundary never reads the
    secret itself. Verbose logging goes to a --log-file scraped by audit().
    """
    if shutil.which("nono") is None:
        raise RuntimeError("nono is not installed; cannot start credential proxy")
    audit_path = f"{ca_dir}/nono-proxy.log"
    cmd = [
        "nono", "proxy", "--port", "0", "-vv", "--log-file", audit_path,
        *compile_nono_flags(scopes),
    ]
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=ca_dir,
    )
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
        except RuntimeError:
            continue
        return ProxyHandle(
            process=process,
            url=info["url"],
            port=info["port"],
            token=info["token"],
            ca_path=info["ca_path"],
            audit_path=audit_path,
        )
    process.kill()
    raise RuntimeError(
        f"nono proxy did not become ready within {PROXY_READY_TIMEOUT}s:\n{output}"
    )
