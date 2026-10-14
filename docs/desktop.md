# The desktop app from source

The desktop app runs everything the terminal agent does in a window: conversations side by
side, tool use folded under each answer and approvals in line. Building an agent is a guided
page with six steps across its top bar (Describe, Plan, Build, Results, Try it and Add to
project), with the build's conversation in a panel you can open beside it and the full
technical detail under Edit plan and Full report. It runs Architect's own core, one Python
process per conversation, so every command, mode, provider and approval behaves as it does
in the terminal. The design and parity notes are in [plan.md](plan.md), and the research
behind them in [discovery.md](discovery.md).

Most people should install a release instead (see the [README](../README.md#try-it)). This
page is for working on the app.

## Set up

Once, from a checkout, with Node 20.6 or later and uv:

```sh
uv sync                     # Architect's Python environment (.venv), if not done yet
npm ci --prefix architect/backends/pi-node   # Pi, for the coder and Forge
cd desktop
npm install                 # if npm reports blocked install scripts: npm approve-scripts electron esbuild
```

## Run

```sh
cd desktop
npm run app                 # build and open the desktop app
npm run dev                 # or the same app in a browser at http://127.0.0.1:5173
```

The app uses the same workspace as the CLI (`~/.architect`, or `ARCHITECT_WORKSPACE`, or
Settings > Advanced), so conversations, sign-ins and agents are shared with `architect` in
a terminal. Its own small files, such as window preferences and drafts, live in
`~/.architect/desktop`. `ARCHITECT_PYTHON` picks another Python. By default the app uses
this checkout's `.venv`.

## Tests

`cd desktop && npm test` runs the page-to-core bridge end to end with a fake model, the
conversation model and the main flows. `uv run pytest tests/test_desktop_bridge*.py` covers
the Python side, and `npm run typecheck` checks the TypeScript.

## On a Mac

Everything above works the same on a Mac. Shortcuts use ⌘, the menu bar has the app's
commands, and terminals open your login shell (zsh by default). Apps opened from the Finder
get a bare PATH, so the app reads your login shell's PATH at startup to find `uv`, `git`,
`gh`, `node` and editor commands from Homebrew.

## Installers

```sh
cd desktop
npm run dist:mac            # on a Mac: Architect-<version>-<arch>.dmg and .zip, signed and notarized
npm run dist:win            # on Windows: Architect-Setup-<version>.exe
npm run dist:dir            # an unpacked app for a quick check, any platform
```

An installed app needs nothing else on the machine: no uv, Python or Node. It carries
Architect's Python core and `uv.lock` read-only in its resources, a pinned copy of uv and
Pi's dependencies, and Pi runs on the app's own Electron binary as Node. On first launch,
and after an update changes `uv.lock`, the bundled uv downloads a Python 3.12 build and
installs the locked packages into the app's data folder (`%LOCALAPPDATA%\Architect` on
Windows, `~/Library/Application Support/Architect` on a Mac), so the signed bundle is never
modified. That needs an internet connection once. The window shows each step, and
`setup.log` in that folder keeps uv's output.

The build fetches uv for each platform from its GitHub release and refuses it unless the
sha256 matches the pin in `desktop/scripts/fetch-uv.mjs`. It installs Pi's dependencies
from their lockfile (`desktop/scripts/stage-pi.mjs`). Both go to `desktop/vendor/`, which
git ignores. To check a build the way a clean machine runs it, with no uv, Node or Python
on PATH ($0, fake model):

```sh
cd desktop
ELECTRON_RUN_AS_NODE=1 release/win-unpacked/Architect.exe scripts/smoke-packaged.mjs --app release/win-unpacked/Architect.exe
```

On a Mac, use `Architect.app/Contents/MacOS/Architect` in both places.

## Signing for macOS

`dist:mac` signs with a Developer ID Application certificate and notarizes with Apple.
Locally, the certificate in your login keychain is found on its own. Set the notarization
credentials, either an App Store Connect API key (`APPLE_API_KEY` = path to the `.p8`,
`APPLE_API_KEY_ID`, `APPLE_API_ISSUER`) or an Apple ID (`APPLE_ID`,
`APPLE_APP_SPECIFIC_PASSWORD`, `APPLE_TEAM_ID`). The hardened runtime entitlements are in
`desktop/build/entitlements.mac.plist`. Build each architecture on a machine of that
architecture (`-- --arm64` or `-- --x64`) so node-pty's native module matches.

## CI

`.github/workflows/desktop.yml` tests on macOS, Windows and Linux on every push that
changes the app (`desktop/`, `architect/desktop/`, the bridge tests or the workflow itself).
On a `v*` tag, or when run by hand from the Actions tab, it builds Apple silicon and Intel
Mac apps and a Windows installer, installs each one on its runner and runs that smoke test.
A tag then drafts a release. Mac builds are signed, notarized and verified when these
repository secrets are set, and ad-hoc signed test copies otherwise:

| Secret | What it is |
| --- | --- |
| `MAC_CERT_P12_BASE64` | Your Developer ID Application certificate and key, exported as .p12, base64-encoded |
| `MAC_CERT_PASSWORD` | The .p12 export password |
| `APPLE_API_KEY_P8` | The App Store Connect API key file's contents (Users and Access > Integrations > Keys) |
| `APPLE_API_KEY_ID` | That key's ID |
| `APPLE_API_ISSUER` | The issuer ID shown above the keys list |

The Windows installer is not code-signed.
