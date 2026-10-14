"""The desktop app's bridge to the agent core (see docs/plan.md).

The desktop shell (Electron, ``desktop/``) starts ``python -m architect.desktop.bridge`` once
per open conversation and talks to it in JSON lines over stdio. The bridge is one more
``Frontend`` for the CLI's own ``Repl``: every turn, slash command, approval and question
runs through the same code the terminal uses.
"""
