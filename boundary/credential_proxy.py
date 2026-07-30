"""Credential-scoping proxy orchestration (nono proxy wrapper).

No imports from boundary.envelope or boundary.agent — this module is a leaf.

Runtime shapes (nono 0.70.0) are documented in docs/spikes/nono-proxy-runtime.md;
every parser/flag here cites a sample from that spike.
"""
from __future__ import annotations

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
