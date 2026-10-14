"""W7 test helpers: throwaway Ed25519 keys and hosts (workspaces) that hold them.

Every key here is generated at run time inside a pytest temp directory. No private key
is ever stored in the repository, and the shipped key file is never written.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from forge_modes_support import make_profile, services_for, spec

from architect import trust
from architect.registry import Registry, RegistryEntry
from architect.spec import Lifecycle

# The fingerprint of the release key whose public half ships in
# architect/keys/architect-ed25519.pub. Only its public half is ever read here.
RELEASE_KEY = "39762f2071b307da0db6ada328bc0743b228351fe2b78d890b50d09bf6cc30f8"


def use_host(monkeypatch: pytest.MonkeyPatch, root: Path) -> Path:
    """Make ``root`` this process's workspace: its HMAC key, Ed25519 key and trust list."""
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ARCHITECT_SIGNING_KEY_FILE", str(root / "signing.key"))
    monkeypatch.delenv("ARCHITECT_ED25519_KEY_FILE", raising=False)
    monkeypatch.delenv("ARCHITECT_TRUST_FILE", raising=False)
    return root


def host_key(root: Path, name: str = "bench") -> Ed25519PublicKey:
    """An Ed25519 key for the host at ``root``, trusted in that host's own list."""
    public = trust.generate_key(root / trust.ED25519_KEY_NAME)
    trust.add_trusted_key(public, name, root / trust.TRUST_FILE_NAME)
    return public


def trust_on(root: Path, public: Ed25519PublicKey, name: str = "bench") -> None:
    root.mkdir(parents=True, exist_ok=True)
    trust.add_trusted_key(public, name, root / trust.TRUST_FILE_NAME)


def write_pub(path: Path, public: Ed25519PublicKey) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(trust.public_key_pem(public), encoding="ascii", newline="\n")
    return path


async def _compile_install(root: Path) -> tuple[RegistryEntry, Registry, Path]:
    services = services_for(make_profile(root))
    try:
        compiled = await services.compiler.compile(
            spec(lifecycle=Lifecycle.INSTALL), approved=True
        )
        assert compiled.artifact is not None and compiled.artifact.licensed
        services.registry.install("feature-ok", compiled.artifact)
        return services.registry.status("feature-ok"), services.registry, services.workspace
    finally:
        services.close()


def compile_install(root: Path) -> tuple[RegistryEntry, Registry, Path]:
    """A licensed INSTALL artifact compiled offline (fake provider) and installed under
    ``root/work``, signed by whatever keys the current host holds."""
    root.mkdir(parents=True, exist_ok=True)
    return asyncio.run(_compile_install(root))


def flip(path: Path, offset: int) -> None:
    """Change exactly one byte of ``path`` (one bit of it, keeping ASCII text ASCII)."""
    data = bytearray(path.read_bytes())
    data[offset] ^= 0x01
    path.write_bytes(bytes(data))
