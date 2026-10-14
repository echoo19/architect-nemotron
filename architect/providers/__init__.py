"""Providers and sign-in (Phase 13, Section 3).

``catalog`` lists every provider Architect can talk to, ``credentials`` stores API
keys (environment first, else ``<workspace>/credentials.json``), ``discovery`` lists
a provider's models, ``status`` answers "can I use it now" with cheap checks only,
``signin`` runs the guided sign-in flows, and ``onboarding`` turns a choice into a
profile entry (``onboarding_choices``, ``register_model``, ``onboard``).

Nothing here is imported by the package itself, so ``architect.profiles`` can use
``credentials`` without pulling in HTTP discovery or the CLI.
"""
