"""Private stage-4 capability, not a hostile-Python sandbox.

Only architect.compiler.verification may import the identity token in production.
Other floor modules call the validator without importing the token. Trusted
callers must not forward verification cases to tuning or rewrite code. Stage 3
must run in a sandbox that cannot import this package; Python reflection is not
an isolation boundary.
"""

_VERIFIER_CAPABILITY = object()


def is_verifier(capability: object) -> bool:
    """Use identity only: truthy values and equality spoofing grant nothing."""
    return capability is _VERIFIER_CAPABILITY
