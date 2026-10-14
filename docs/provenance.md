# Provenance

Architect was built in a private repository and published here as one squashed
commit. This page summarizes that private history without personal details. The
sha256 below fixes that exact history, so a copy of it shown later can be checked
against this page.

| | |
|---|---|
| Commits | 599 (58 merges) |
| First commit | 2026-09-24, `f4de300` |
| Last commit in this snapshot | 2026-10-14, `5344a3a` |
| Line changes across all commits | 3,471 file changes: +354,036 / -29,171 lines; 162 binary file changes |
| Tree at the last commit | 1,041 files, 324,775 lines, 41 of the files binary |
| sha256 of the full log | `ac3aa9f524679e18a39828870067d537ceda1eb710e9a5d5c4f2cbca9a64a9e6` |

The hash is of the exact output of this command, run in a clone of the private
repository:

```sh
git -c core.quotepath=true -c log.showRoot=true -c i18n.logOutputEncoding=UTF-8 log --no-color --no-decorate --no-show-signature --no-mailmap --no-renames --date=iso-strict --format=fuller --numstat 5344a3acd8b0fba59c15dfc7a7a18d743fda0f01 | sha256sum
```

## Authors

No names or addresses are given here. Commits by identity:

| Identity | Commits | Dates (UTC) |
|---|---|---|
| a local development-agent identity | 577 | 2026-09-24 to 2026-10-14 |
| the owner's personal identity | 22 | 2026-09-25 |

## Phases

| Phase | Dates (UTC) | Commits | First commit | What it added |
|---|---|---|---|---|
| 1 | 2026-09-24 | 1 | `f4de300` | Schemas, model adapters, the budgeted runtime and content-free telemetry |
| 2 | 2026-09-24 | 1 | `16cf91a` | Runnable presets and isolated, calibrated quality floors |
| 3 | 2026-09-24 | 1 | `9d886ee` | Inference, tuning and verification of budgeted harnesses |
| 4 | 2026-09-24 | 1 | `f315592` | Sandboxed rewrites, a verified registry and harness reuse |
| 5 | 2026-09-24 | 1 | `12f79b5` | Guarded Pi factories, orchestration and the token policy |
| 6 | 2026-09-25 | 27 | `8b446d2` | Game factories, the official ARC-AGI-3 adapter, the HTTP service, the TypeScript client and an independent audit |
| 7 | 2026-09-25 to 2026-09-26 | 13 | `3554732` | The interactive session as the operator's entry point |
| 8 | 2026-09-26 | 14 | `a7ac578` | Image inputs, synthetic exam pages and the bench-and-iterate loop |
| 9 | 2026-09-27 to 2026-09-28 | 11 | `d8d9fde` | Forge, a harness-engineering agent on Pi, and sandbox language v2 |
| 10 | 2026-09-28 to 2026-09-29 | 5 | `5d12479` | Routing among verified versions; Forge run and integrate |
| 11 | 2026-09-29 to 2026-09-30 | 8 | `1de7b1a` | Resilience: resumable tests and bench runs, live-model protocol fixes |
| 12 | 2026-09-30 | 13 | `c1096c4` | The agent CLI: conductor loop, coder subagent and build mode |
| 13 | 2026-10-01 | 12 | `baee7c6` | The complete CLI: providers, MCP, Forge modes and parity |
| 14 | 2026-10-02 to 2026-10-14 | 491 | `e3a3985` | The desktop app (Electron and React) |

## Commits per day

| Date (UTC) | Commits |
|---|---|
| 2026-09-24 | 5 |
| 2026-09-25 | 32 |
| 2026-09-26 | 22 |
| 2026-09-27 | 1 |
| 2026-09-28 | 11 |
| 2026-09-29 | 8 |
| 2026-09-30 | 17 |
| 2026-10-01 | 12 |
| 2026-10-02 | 8 |
| 2026-10-03 | 33 |
| 2026-10-04 | 80 |
| 2026-10-05 | 154 |
| 2026-10-06 | 63 |
| 2026-10-07 | 43 |
| 2026-10-08 | 61 |
| 2026-10-09 | 3 |
| 2026-10-12 | 18 |
| 2026-10-13 | 27 |
| 2026-10-14 | 1 |

## Every commit

Subjects as written, with anything personal replaced by [removed].

| Date (UTC) | Commit | Subject |
|---|---|---|
| 2026-09-24 | `f4de300` | Phase 1: verified schemas, adapters, budget runtime and private telemetry |
| 2026-09-24 | `16cf91a` | Phase 2: runnable presets and isolated calibrated quality floors |
| 2026-09-24 | `9d886ee` | Phase 3: infer tune and verify budgeted agent harnesses |
| 2026-09-24 | `f315592` | Phase 4: sandbox rewrites and verify registry and harness reuse |
| 2026-09-24 | `12f79b5` | Complete guarded Pi factories, orchestration and token-metrics policy |
| 2026-09-25 | `8b446d2` | Phase 6: game factories, official ARC adapter, HTTP service, TS client and independent audit |
| 2026-09-25 | `6993cfa` | Record GitHub remote and push run note |
| 2026-09-25 | `a0a2077` | Note local-evidence branch |
| 2026-09-25 | `a0e996c` | Make strict mypy pass on every platform |
| 2026-09-25 | `0432793` | Run the live TypeScript e2e in CI |
| 2026-09-25 | `ddd3564` | Document the Windows registry path limit and mark Phase 6 interfaces implemented |
| 2026-09-25 | `a37b539` | Report wallet exhaustion after Stage 0 as game budget exceeded |
| 2026-09-25 | `561c9a2` | Manufacture a fresh ARC-AGI-3 harness on every CI run |
| 2026-09-25 | `927e598` | Reject offline best-of-runs and missing-baseline scores |
| 2026-09-25 | `1385980` | Fix POSIX test failures in Pi cleanup, eval timing and registry test |
| 2026-09-25 | `883d71f` | Keep server compiles and requests available under damaged state and lock contention |
| 2026-09-25 | `3268b17` | Isolate public-game fetching in the ARC harness script |
| 2026-09-25 | `8264727` | Reap the Pi bridge session even after the bridge has exited |
| 2026-09-25 | `56eace8` | Report a write to an exited Pi bridge as a fixed Pi error |
| 2026-09-25 | `1076598` | Refuse mismatched offline baselines and report compile wallet refusals as budget |
| 2026-09-25 | `2f0bacb` | Heal lost install catalog writes and keep telemetry waits off the event loop |
| 2026-09-25 | `f02727d` | Fetch public ARC games over HTTP without running their code |
| 2026-09-25 | `08d082a` | Tolerate zombie-only Pi groups, persist cancelled telemetry, heal overlapping approvals |
| 2026-09-25 | `72a3a3e` | Add Claude Code and Codex CLI model adapters |
| 2026-09-25 | `a7f25b9` | Unwrap a CLI reply that is entirely one markdown fence |
| 2026-09-25 | `1a00388` | Add a live Claude Code example and harness adapter option |
| 2026-09-25 | `3444585` | Cache stable prompt prefixes and price cached tokens |
| 2026-09-25 | `f77b2ed` | Allow an operator opt-in for cloud game models on offline previews |
| 2026-09-25 | `b12708a` | Show game models the exact decision shape and give inference two minutes |
| 2026-09-25 | `9a1bb25` | Record Phase 6 audit fixes, verification rounds and sign-off numbers |
| 2026-09-25 | `7ffad7c` | Make two server snapshot tests pass on Windows |
| 2026-09-25 | `ed3aab2` | Run the suite, types and TypeScript e2e on Windows in CI |
| 2026-09-25 | `3554732` | Publish the Phase 7 interactive session contract |
| 2026-09-25 | `d56cbb2` | Surface child game failures and stop the RESET trap behind the live ARC Stage 4 failure |
| 2026-09-25 | `30ecbd6` | Keep game presets' shared retries grid; guarantee one decision repair in the game backend |
| 2026-09-25 | `b7b5b5f` | Let a compile observer see stage entries |
| 2026-09-25 | `3deff8e` | Force LF checkouts so hashed fixtures survive Windows CI |
| 2026-09-26 | `6b4d7f1` | Give the Windows CI job a short pytest temp root |
| 2026-09-26 | `2a17d3d` | Phase 7: interactive session as the operator entry point |
| 2026-09-26 | `b491ee4` | Cover the session handlers, console and editor by behaviour |
| 2026-09-26 | `eaded40` | Record the first live-licensed ARC-AGI-3 preview harness |
| 2026-09-26 | `bde5ea5` | Remove first-user friction found in a live walkthrough of the session |
| 2026-09-26 | `13260d2` | Point the top-level help at the session and hint the next step after install |
| 2026-09-26 | `b5ad2d6` | Make two registry-read session tests independent of path wrapping |
| 2026-09-26 | `c42f505` | Keep the game probe within the inference context on animated public games |
| 2026-09-26 | `a7ac578` | Publish the Phase 8 contract: image inputs, synthetic exam-page floor, bench-and-iterate loop |
| 2026-09-26 | `5262518` | Phase 8 WP1: image inputs on the HTTP and fake adapters |
| 2026-09-26 | `8ac1146` | Reserve the cache-write rate for cached images without a prefix; no phantom fake calls |
| 2026-09-26 | `61605a5` | Phase 8 WP2: thread images through runtime, sandbox, presets, pipeline and floors |
| 2026-09-26 | `eb6df62` | Make local Ollama models usable: chat template, fixed context, fail-closed truncation |
| 2026-09-26 | `ef53b50` | Under exact overrides, read only the proposal fields the host does not pin |
| 2026-09-26 | `22af984` | Phase 8 WP3: bench data, synthetic exam pages, oracles and paired statistics |
| 2026-09-26 | `6c3046e` | Fix three harness failures found by logging a local ARC run |
| 2026-09-26 | `e7d3212` | Record the first licensed ARC-AGI-3 harness on a local model |
| 2026-09-26 | `ae4f80f` | Phase 8 WP4: bench runner, architect bench commands, session verb, CI smoke job |
| 2026-09-26 | `05362a6` | ARC strategy v2: tell the model when it loops, what changed, and what it tried |
| 2026-09-26 | `b03a421` | Commit the bench smoke baseline from the Linux CI runner |
| 2026-09-26 | `9082ca7` | Record Phase 8 progress, local-model gotchas and the run note |
| 2026-09-26 | `fdd7c41` | Drop a runner-speed-dependent assertion from the client deadline test |
| 2026-09-27 | `d8d9fde` | Publish the Phase 9 Forge contract |
| 2026-09-28 | `3a8ebaa` | Phase 9: Forge, a harness-engineering agent on Pi, with sandbox language v2 |
| 2026-09-28 | `747ff5b` | Document Forge and add its arms and Stage A checks to CI |
| 2026-09-28 | `7bcc9dc` | Fix three Phase 9 CI failures: LF golden digest, Pillow guard, Linux timeout |
| 2026-09-28 | `46f309f` | Record Phase 9 progress, gotchas and the run note |
| 2026-09-28 | `c3e83a2` | Scale sandbox fuel wall-clock bounds under coverage; give CI test jobs 60 minutes |
| 2026-09-28 | `989e758` | Refresh the bench smoke baseline with the forge and template arms |
| 2026-09-28 | `76764e8` | Let the TypeScript e2e script survive the Windows temp-cleanup race |
| 2026-09-28 | `67f4b31` | Note the two known CI timing flakes in the Phase 9 run note |
| 2026-09-28 | `b3a7fa7` | Stage 0 under exact overrides ignores pinned repeats and invented top-level keys |
| 2026-09-28 | `29f9dcd` | Run Forge on the real public ARC path's child game compiles |
| 2026-09-28 | `5d12479` | Publish the Phase 10 router contract |
| 2026-09-29 | `71daebb` | Phase 10: route tasks and Architect's own roles among verified versions; Forge run and integrate |
| 2026-09-29 | `b629bae` | Refresh the bench smoke baseline with the routed arms |
| 2026-09-29 | `7e293d6` | Record Phase 10 progress and the run note |
| 2026-09-29 | `68471a9` | Note the router and the orphan-process gotcha in project memory |
| 2026-09-29 | `1de7b1a` | Phase 11 spec and a resumable test runner |
| 2026-09-29 | `2844bb5` | Live-model protocol fixes: Pi tool envelopes and structured decoding |
| 2026-09-29 | `1e2af2e` | Resumable bench runs: bench run --resume and session bench resume |
| 2026-09-29 | `86f3be2` | Harden the Pi parent prompt for live models; Forge re-asks stray prose |
| 2026-09-30 | `8fc6ec5` | Record Phase 11 progress; the test runner pauses instead of forcing a batch |
| 2026-09-30 | `a4b8e71` | Mask step bars from ARC play signals; model-free ARC probe |
| 2026-09-30 | `6767985` | Explorer template crops clock bands before keying states and detecting no-ops |
| 2026-09-30 | `2192c9d` | Record the ARC probe findings and the Forge rank recommendation |
| 2026-09-30 | `c1096c4` | Phase 12 spec: Architect as a terminal agent that builds agents |
| 2026-09-30 | `1d1a2af` | Phase 12 foundation: the conductor agent loop, its events and tools |
| 2026-09-30 | `c99a73a` | Lint the conductor tests |
| 2026-09-30 | `3256e84` | Forge gets a design playbook; built agents report their design |
| 2026-09-30 | `a8f0660` | Coder subagent with per-action approvals and live Pi tool events |
| 2026-09-30 | `b10dde2` | Build mode: interview, inferred floor and budget, Forge-engineered compile |
| 2026-09-30 | `771e81b` | Terminal UI: `architect` opens the conversational agent CLI |
| 2026-09-30 | `832791b` | Integrate Phase 12: session-capped subagents, honest floor origin, /forge arc --run |
| 2026-09-30 | `12147f4` | Register every detected model; --model and /model add one on demand |
| 2026-09-30 | `3769f80` | Build mode lifts per-run seconds to fit two calls; correct next-step syntax |
| 2026-09-30 | `110bcdc` | Stream Forge's tool calls to the CLI during a build |
| 2026-09-30 | `dbb2be9` | /run takes plain text, rejects broken JSON, and names run failures |
| 2026-09-30 | `708b97f` | Image-input agents need real floor cases; Phase 12 progress and live evidence |
| 2026-10-01 | `baee7c6` | Phase 13 spec: the complete CLI; per-package slash-command modules |
| 2026-10-01 | `37660e6` | events: TextDelta for streamed answers (Phase 13) |
| 2026-10-01 | `1afe8ba` | MCP client: stdio and streamable HTTP servers as conductor tools, /mcp |
| 2026-10-01 | `65d7076` | Providers, sign-in and native streaming (Phase 13 packages A and B) |
| 2026-10-01 | `09e141d` | Conductor.set_tools for MCP tool changes |
| 2026-10-01 | `e3a1309` | Forge mode: run it here or put it into software (Phase 13 package E) |
| 2026-10-01 | `33d73db` | architect serve runs the HTTP service; boundary test admits the CLI export |
| 2026-10-01 | `13dede4` | CLI parity: streaming, context meter, checkpoints, plan mode, config, doctor |
| 2026-10-01 | `d560fbc` | Integrate Phase 13: images reach capable models, /serve stops with the app |
| 2026-10-01 | `a275871` | Docs: the complete Architect CLI (providers, modes, Forge destinations, commands) |
| 2026-10-01 | `93bdb95` | Test that App.close stops the /serve services it started |
| 2026-10-01 | `b5f8f02` | Drop unverified bundled prices; Phase 13 progress and live evidence |
| 2026-10-02 | `e3a3985` | Desktop app: Architect and Forge in a window, at parity with Claude Code and Codex desktop |
| 2026-10-02 | `d2856f0` | Desktop: Lectern's type registers and no decorative tiles, pills or badges |
| 2026-10-02 | `663fe5d` | Desktop CI: build an unsigned Mac package when the signing secrets are missing |
| 2026-10-02 | `523e33b` | Track desktop/build: the app icon and Mac entitlements were caught by the root build/ ignore |
| 2026-10-02 | `714407e` | Desktop CI: sign with the certificate alone; notarize only when the App Store Connect key is set |
| 2026-10-02 | `041e425` | Desktop: the mark at Lectern's 20px lockup size; no icon tiles left in empty states and dialogs |
| 2026-10-02 | `3b4ce5c` | Desktop CI: run on branch pushes too, not only tags |
| 2026-10-02 | `50ed944` | Desktop: no label repeats what an icon, switch or dot already shows |
| 2026-10-03 | `adac7d1` | Nebius Token Factory provider: Nemotron roles, thinking toggle, retries, reasoning-cut guard |
| 2026-10-03 | `3d6f304` | Receipt task data from CORD v2: text rebuilt from annotations, exact labels, pinned splits |
| 2026-10-03 | `ac62a66` | Spend relay, public-cut tooling, receipt label audit, feedback log, Forge probe notes |
| 2026-10-03 | `bc2c315` | Phase 14 interface: gate integrity and public evidence bench |
| 2026-10-03 | `da40900` | Phase 14 WB-fair: Day 0 Spec fields from contract section 3.1 |
| 2026-10-03 | `42da450` | Example expense app for the coder demo |
| 2026-10-03 | `924be0b` | Forge and coder use native tool calls when the model supports them |
| 2026-10-03 | `fd04a56` | Merge branch 'hackathon/nebius' into hackathon/w3-native-forge |
| 2026-10-03 | `db4deb5` | Phase 14 WB-fair: input limit, reply modes, plain arm, model aliases, model id rule |
| 2026-10-03 | `067edad` | Receipt data v2: rules that match CORD's labels, exclusions, row splits, near-duplicate merges |
| 2026-10-03 | `5b25b51` | Relay: close the review's spend and availability gaps |
| 2026-10-03 | `951bb61` | Pre-registration draft for the Nemotron receipt evidence |
| 2026-10-03 | `3a1994c` | Visual spec for the bar, rungs, exam chart, gate scatter and receipt |
| 2026-10-03 | `5cc3ea3` | Relay re-attack: pin a still-open R1 hold gap, lock R1/R2 shapes that hold |
| 2026-10-03 | `ded46c4` | Receipt data v2 hand-check |
| 2026-10-03 | `568cb52` | Phase 14 WC-bench: suite v2 receipts, delivery metrics, paired arms, report, audit replay, analysis skeleton |
| 2026-10-03 | `3f8eae5` | Phase 14 WC-bench: read split sizes from the manifests in tests |
| 2026-10-03 | `e21dbcb` | Pi parent: cut replies re-asked without echo, one nudge before a required tool, read refusals that say what is there, parent-side context cap |
| 2026-10-03 | `09abcb5` | Phase 14 WA-gates: count_as_miss with signed counts, public dataset floors, receipt_v1, frozen spec builds, Forge audit |
| 2026-10-03 | `a6ff5da` | Merge Phase 14 WA-gates: count_as_miss, public dataset floors, receipt_v1, frozen specs, Forge audit |
| 2026-10-03 | `d178447` | Merge Phase 14 WB-fair: input limit, reply modes, plain arm, model aliases, model id rule |
| 2026-10-03 | `d08306b` | Merge Phase 14 WC-bench: suite v2 receipts, delivery metrics, paired arms, report, audit replay, analysis skeleton |
| 2026-10-03 | `68c51dd` | Usage carries reasoning tokens and Nebius cache hits |
| 2026-10-03 | `75acc9c` | Forge pack and session cost controls, per-turn rows and the pack hash |
| 2026-10-03 | `5273a70` | Ladder controls: Stage 2 candidates per rung and Forge on the first rungs only |
| 2026-10-03 | `129a3f3` | Show the Forge pack hash: architect forge pack, the plan card and forge report |
| 2026-10-03 | `4cfd6ec` | Forge turn log: close the file after each row instead of syncing it |
| 2026-10-03 | `ba1749e` | Merge branch 'hackathon/nebius' into hackathon/w3-native-forge |
| 2026-10-03 | `c613ae2` | Forge reply cap: a profile's lower max_tokens ceiling leaves the request alone |
| 2026-10-03 | `eb92076` | Phase 14 integration: the audit replay reads what the Forge audit writes |
| 2026-10-03 | `701e6fa` | Read refusals examine at most 512 entries of a directory |
| 2026-10-03 | `570b65c` | Profile schema test: the Forge agent's reply cap is a per-call cap, not a budget |
| 2026-10-03 | `799cf0e` | Merge branch 'hackathon/nebius' into hackathon/w3-native-forge |
| 2026-10-04 | `5325d18` | Receipt data v2 hand-check |
| 2026-10-04 | `a316c54` | Evidence site scaffold |
| 2026-10-04 | `dd1a249` | Relay re-attack 2: pin the R1, R2 and R4 gaps still open, lock what held |
| 2026-10-04 | `a2dd806` | Pi parent: re-ask cut and early final replies; a cancelled stream keeps its quote |
| 2026-10-04 | `df7d68e` | Context cap: edit texts count, and the extension thins the wire by the parent's rule |
| 2026-10-04 | `c682dd9` | Forge pack hash covers the bridge and scaffolding; TUNING.md fits one read |
| 2026-10-04 | `9a67c2d` | W3 review tests: the strict xfails pass now and stay as regression tests |
| 2026-10-04 | `75705a7` | Desktop: run Pi on the app's own Electron binary as Node |
| 2026-10-04 | `b2130fb` | Desktop: first start sets up Python with the bundled uv, and says so |
| 2026-10-04 | `86727d7` | W7a: Ed25519 trust list and signed Stage 4 reports |
| 2026-10-04 | `cc665db` | W7a: signed export manifests; packages run where the key is trusted |
| 2026-10-04 | `6ecfb8e` | W7a: architect verify and architect keys |
| 2026-10-04 | `0bec34e` | Phase 14 review fixes |
| 2026-10-04 | `b9824b4` | Merge branch 'hackathon/nebius' into hackathon/w3-native-forge |
| 2026-10-04 | `59c9bdf` | Desktop installers: ship a pinned uv and Pi's dependencies |
| 2026-10-04 | `64f093d` | Desktop: one tested place for the packaged app's environment |
| 2026-10-04 | `3b1a306` | Desktop CI: install each build and smoke-test it with no uv or Node |
| 2026-10-04 | `16e28c6` | README: installed apps need no uv, Python or Node |
| 2026-10-04 | `3714fad` | Merge hackathon/site: evidence site scaffold |
| 2026-10-04 | `3a9a916` | Merge hackathon/expense-app: example expense app for the coder demo |
| 2026-10-04 | `0d918d6` | Merge hackathon/relay-fix: spend and availability fixes, two re-attacks |
| 2026-10-04 | `6de9e3e` | Merge hackathon/data-v2: receipt data v2 (rules receipts-v2, exclusions, row splits, near-duplicate merges) |
| 2026-10-04 | `6c0b6a8` | Merge branch 'hackathon/nebius' into hackathon/w3-native-forge |
| 2026-10-04 | `8b2b81e` | W7 attack review: trust path regression tests and open findings |
| 2026-10-04 | `dc3f8c6` | Receipt data v2 through the bench, the pre-registration and the expense app |
| 2026-10-04 | `1e972db` | Phase 14 review fixes: no child report in the Forge audit, zero-call runs |
| 2026-10-04 | `265d6d1` | Relay: close the R1 hold gap for characters repr() escapes in tools |
| 2026-10-04 | `bba7938` | Merge branch 'hackathon/nebius' into hackathon/w3-native-forge |
| 2026-10-04 | `bd7e77b` | Relay G0 on the local host: live checks and one Architect call |
| 2026-10-04 | `89a3bc6` | Merge branch 'hackathon/nebius' into hackathon/w3-native-forge |
| 2026-10-04 | `909a556` | Receipt pilot: bar 0.40, headroom 0.187, CORD stays the hero |
| 2026-10-04 | `4718ff8` | W7 fix: small-order Ed25519 keys are key_invalid (W7-3) |
| 2026-10-04 | `a4479d2` | W7 fix: plain manifest names, .gitattributes in packages, evidence label from the check (W7-6) |
| 2026-10-04 | `a2a7c81` | W7 fix: checks count configured keys only, --require-key, no vouching from inside (W7-1, W7-2) |
| 2026-10-04 | `5a80f8a` | W7 fix: path guards compare without case and refuse 8.3 names (W7-4, W7-5) |
| 2026-10-04 | `cf40e2f` | W7 fix: the package's own tests fail on key_untrusted; CI runs verify --require-key (W7-7) |
| 2026-10-04 | `66ab3da` | Streams keep their quote until the provider's final usage |
| 2026-10-04 | `ec5aa4a` | Merge branch 'hackathon/nebius' into hackathon/w3-native-forge |
| 2026-10-04 | `3a3c06b` | Bench: charge billed replies the adapter refused and calls cut at the deadline |
| 2026-10-04 | `0a4136d` | Gate audit: decide "more than 5 points" from exact fractions |
| 2026-10-04 | `2c2056c` | Test-subset pairs count invalid and over-budget runs over the subset |
| 2026-10-04 | `85b5798` | Analysis: a missing cell fails only the hypotheses that need it |
| 2026-10-04 | `fc2e5f7` | Pilot error analysis: the misses are the models', not the labels |
| 2026-10-04 | `3bedf2c` | Prereg: pilot results, the bar, headroom and power near 50% |
| 2026-10-04 | `bc4a349` | Bench meter: a cancelled call adds its reservation's dollars, not tokens |
| 2026-10-04 | `e91a428` | Merge branch 'hackathon/nebius' into hackathon/w3-native-forge |
| 2026-10-04 | `1b9bfeb` | W7 re-attack: UNC and extended-length aliases cannot slip a key past trust_in_target |
| 2026-10-04 | `6593525` | Desktop plumbing for the bar view, the exam chart and Try it |
| 2026-10-04 | `cd35723` | Desktop: the bar view, the exam chart and Forge's harness versions |
| 2026-10-04 | `919738f` | G1 on native tool calls: clean transport, no Ultra submission |
| 2026-10-04 | `5a64d2e` | Desktop: no flash of every command on a bare / |
| 2026-10-04 | `c0075e4` | Desktop: Try it folds into one line; replays keep real lengths |
| 2026-10-04 | `afaa9de` | Desktop: build notices and lines in the app's words |
| 2026-10-04 | `34f3bdb` | Desktop: layout fixes from the replayed-build captures |
| 2026-10-04 | `2016454` | Desktop: the narrow chat beside a build keeps its controls |
| 2026-10-04 | `7439331` | Desktop: card rows drop the terminal's code marks |
| 2026-10-04 | `8a19e67` | Merge hackathon/w3-native-forge: native tool calls for Forge and the coder, Forge cost controls, pack hash, ladder controls, live G1 notes |
| 2026-10-04 | `515732e` | Merge hackathon/w7-ed25519: Ed25519 trust path, architect verify and keys, attack fixes |
| 2026-10-04 | `8ea6c7c` | Merge hackathon/installers: bundled uv, Pi on the app's Electron binary, packaged smoke tests |
| 2026-10-04 | `3f49c73` | Merge hackathon/analysis-fixes: four prereg analysis fixes, billed refusals in the bench meter, prereg pilot results |
| 2026-10-04 | `8fc62bd` | W6 review: pin the held-out and bar view defects found in review |
| 2026-10-04 | `aa90985` | Pi parent: a cut native reply runs nothing; Forge dry runs and spawns count toward the context cap |
| 2026-10-04 | `a41a668` | Forge pack: cover the dry run's example values, leave max_rungs out, record the hash the session started with |
| 2026-10-04 | `1446786` | TUNING.md: clip the first case harder when it alone would not fit |
| 2026-10-04 | `92ae1b4` | Session meter: an ObservedAdapter around a streaming model streams too |
| 2026-10-04 | `1392281` | W3 review tests: the last eight strict xfails pass and stay as regression tests |
| 2026-10-04 | `db39766` | Docs: Pi drives a streaming model with native tool calls |
| 2026-10-04 | `71cb66e` | ObservedAdapter: copy and pickle still work now that __new__ picks the class |
| 2026-10-04 | `254414d` | Limitations: the Nemotron Forge sessions were rehearsal runs, and Forge evidence is still offline |
| 2026-10-04 | `46f2ddb` | ObservedChat: hand the output limit on without naming it in the session package |
| 2026-10-04 | `77431b0` | Phase 11: say exactly what a native re-ask sends back |
| 2026-10-04 | `a9701e9` | Sandbox language v2: accept annotations, defaults and method spellings |
| 2026-10-04 | `a3aaf59` | Forge tools: every refusal names its rule, line and allowed form |
| 2026-10-04 | `53c5532` | Forge prompt: the language card rides in the system prompt |
| 2026-10-04 | `f56e25a` | Language card: say that a default is copied for each call |
| 2026-10-04 | `93815d3` | Report tool: leave the verification id out |
| 2026-10-04 | `e09a500` | Desktop: W6 review fixes for the bar view, the build's words and the dock |
| 2026-10-04 | `943ff1f` | DECISIONS: record the Forge pack v2 language and prompt decisions |
| 2026-10-04 | `003a021` | Forge tools: drop an unused constant; test a method on an awaited reply |
| 2026-10-04 | `4c70dd7` | G1 retry on Forge pack v2: no sandbox_helper refusals, still no Ultra submission |
| 2026-10-05 | `4e1f101` | Merge branch 'hackathon/nebius' into hackathon/forge-pack-v2 |
| 2026-10-05 | `f6f0b7c` | Merge hackathon/w6-desktop: the bar view, the exam chart, the Harness diff dock, hash lines, the app's vocabulary, Try it costs, slash-menu fixes |
| 2026-10-05 | `7ac268e` | Relay: the limiter allows 90% and counts only uncached prompt tokens |
| 2026-10-05 | `f3bec4a` | Sandbox language v2: comprehensions, one check for every refused line |
| 2026-10-05 | `d02b75f` | Docs: Forge pack v3 in the phase 9 contract, decisions and the G1 re-check |
| 2026-10-05 | `2d98a85` | G1 retry 2 on Forge pack v3: no submission from Ultra or Super |
| 2026-10-05 | `a8cb92f` | Forge engineer bake-off on pack v3: no accepted submission from Super, DeepSeek or Kimi |
| 2026-10-05 | `d6d47f9` | Pi parent: a trimmed write reads as a note, and a pasted note is refused |
| 2026-10-05 | `a41fe88` | Sandbox language v2: typed excepts, three loop names and the string builtins |
| 2026-10-05 | `7d5cbfb` | DESIGN.md: code's exact reading of a field is final |
| 2026-10-05 | `9cdc3fa` | Docs: Forge pack v4 in the phase 9 contract and decisions, and its re-check |
| 2026-10-05 | `af8da38` | Last Forge round on pack v4: no accepted submission from Super, so O5 |
| 2026-10-05 | `c1ea7cc` | Merge hackathon/forge-pack-v2: Forge pack v4, the relay limiter at 90% on uncached prompt tokens |
| 2026-10-05 | `19e04b2` | Merge hackathon/forge-pack-v2: Forge pack v4 |
| 2026-10-05 | `a8c0d3f` | Nebius role pack: Super engineers the harness, thinking off at temperature 0 |
| 2026-10-05 | `94b1e29` | Phase 9 contract: name the Lectern checkout without its local path |
| 2026-10-05 | `95a89b8` | Relay tests: run the races and the Vercel entry on a live Upstash database |
| 2026-10-05 | `5295b8e` | Tests: the Ed25519 seal never shows in the report tool or the agent page |
| 2026-10-05 | `339b31c` | README: a short public front page; the long reference moves to docs |
| 2026-10-05 | `050385c` | Relay README: runbook for carrying the local ledger's spend onto Upstash |
| 2026-10-05 | `6554ae3` | Relay: ready for a Vercel Hobby deploy, with the judge caps planned |
| 2026-10-05 | `7bdf7b1` | Judge codes: architect judge <code>, and a code works at the Nebius key prompt |
| 2026-10-05 | `d89635d` | Site: trim the page to the video, trying it and the results |
| 2026-10-05 | `7afeaa7` | Site: screenshots of the trimmed page |
| 2026-10-05 | `b799106` | Docs: say when a compile runs Forge, and name the evidence run |
| 2026-10-05 | `c9edebf` | Docs: draft testing instructions for the judges |
| 2026-10-05 | `17b792d` | Judge codes: refuse a malformed relay URL instead of raising; README fixes |
| 2026-10-05 | `dd00f02` | Evaluation plan: plain Ultra, Super and Lightning on the receipts, and one ladder build |
| 2026-10-05 | `91048c0` | Evaluation plan: record its public Gist |
| 2026-10-05 | `ca3b263` | Receipt results: plain Ultra, Super and Lightning on 348 receipts, and the ladder build |
| 2026-10-05 | `25b4edb` | Merge hackathon/presentation: the short README, the reference and desktop docs, the trimmed site |
| 2026-10-05 | `eb1d228` | Site: draw the results table from the evaluation run's report |
| 2026-10-05 | `2dbde44` | Merge hackathon/relay-judge: judge codes, the judges' draft, the relay's Vercel deploy prep |
| 2026-10-05 | `5f45106` | Tests: the judge code tests pass mypy --strict |
| 2026-10-05 | `4713350` | README: the receipt results, what the build licensed and the Forge record |
| 2026-10-05 | `8e19597` | Site: tell the receipt results plainly and show what sat each exam |
| 2026-10-05 | `d00f8f7` | Limitations: the Forge record on Nemotron, and what ships instead |
| 2026-10-05 | `a4d8474` | Release scan: allow the reviewed PEM markers and the relay test's Bearer key |
| 2026-10-05 | `6e30ea8` | Judge relay: live on Vercel, and RELAY_URL points at it |
| 2026-10-05 | `3e913a3` | Feedback: say what each Nemotron model did, from the evaluation |
| 2026-10-05 | `3496885` | Bench examples: point at the committed pilot and evaluation rows |
| 2026-10-05 | `e68fcdb` | Site data: plainer wording for the examined field |
| 2026-10-05 | `e77bbea` | Trust: the release key ships, and the receipt results are published signed |
| 2026-10-05 | `23fb1ab` | Build: on Token Factory the tuned preset is the default engineer |
| 2026-10-05 | `1b17010` | First run: start on Nemotron 3 Super |
| 2026-10-05 | `87e08de` | Relay refusals: say which limit, in one sentence |
| 2026-10-05 | `da824d1` | Receipt reader for judges: the evaluation's spec and setup in the expense app |
| 2026-10-05 | `1d8ad33` | Merge hackathon/w2-trust: the release key ships, and the receipt results are published signed |
| 2026-10-05 | `90523a5` | Merge hackathon/w2-judge-ux: the preset engineer on Token Factory, Super first, plain relay refusals, the receipt reader for judges |
| 2026-10-05 | `5c67648` | Merge hackathon/w2-deploy: the judges' relay is live on Vercel |
| 2026-10-05 | `9af2c24` | Merge hackathon/w2-content: the receipt results in the README and site, the Forge record, the release scan allowances |
| 2026-10-05 | `e1b024f` | Release scan: allow the shipped release public key's PEM marker |
| 2026-10-05 | `6d7ae62` | Judges' draft: drop the note that the signed results have not shipped |
| 2026-10-05 | `b89a7a4` | Tests: the relay refusal tests and the modules they import pass mypy --strict |
| 2026-10-05 | `0dd0a16` | Reference: twelve Forge sessions, the bench extra, the full command list |
| 2026-10-05 | `2db34aa` | Docs: American spelling in the public docs, as the app and CLI use |
| 2026-10-05 | `cc33029` | README: what the intervals mean, the practice half of a license, the pilot re-run |
| 2026-10-05 | `36832c9` | Limitations: Windows CI, plain list items, what the competition entry means |
| 2026-10-05 | `8d70b43` | Trust: the key's offline backup, and what Stage 4 signs today |
| 2026-10-05 | `f68e0a6` | Desktop doc: Pi's packages and Node 20.6 for a checkout, the CI path filter |
| 2026-10-05 | `9f769f2` | Usage: say that architect judge refuses to replace a stored key |
| 2026-10-05 | `e8687bb` | Results: drop the relay cap that no record in the repository shows |
| 2026-10-05 | `d8230ad` | Feedback docs: ranges in words and a straight apostrophe |
| 2026-10-05 | `db952e8` | Relay README: plain list items, and no internal labels |
| 2026-10-05 | `6a171da` | Site: say what Forge and the intervals mean, and call Forge's dots submissions |
| 2026-10-05 | `84ed540` | Judges' page: Pi for the terminal, the Engineer choice, a terminal Try it |
| 2026-10-05 | `bf8ce3a` | Judge code and relay messages: whole sentences, the full code format |
| 2026-10-05 | `a80a5d5` | Sign-in: say what a Nemotron build tunes, and drop 'line UI' |
| 2026-10-05 | `eb20cff` | Export card: say where a host-attested license can be checked |
| 2026-10-05 | `29ad307` | Help: /make builds an agent; Forge runs only when chosen |
| 2026-10-05 | `713bc5e` | App words: the export card's floor reads as exam and bar |
| 2026-10-05 | `8437d91` | Result card: no design row for a preset harness |
| 2026-10-05 | `655c3bd` | Drafted cases: drop any the spec's own schemas refuse |
| 2026-10-05 | `de56710` | Build progress: each stage line names its model, and repeats while it runs |
| 2026-10-05 | `8b75ec2` | Result card: a ladder row with each rung's exam |
| 2026-10-05 | `8400a25` | Build next steps: a practice-only refusal, and a frozen plan's footer |
| 2026-10-05 | `e40d5cd` | Published folders: skip the files the operating system writes |
| 2026-10-05 | `4bb5967` | Ignore .env files, which can hold keys |
| 2026-10-05 | `fc4bf8c` | Release scan: relay codes, Token Factory keys and private key bodies |
| 2026-10-05 | `ec75fb6` | Relay: serve an empty static folder, so only /v1 and /healthz answer |
| 2026-10-05 | `75a9183` | Exported packages: skip the files the operating system writes |
| 2026-10-05 | `25d8b19` | Relay firewall: 300 requests a minute from one address |
| 2026-10-05 | `99f72c7` | Feedback: fill the onboarding and billing answers from our records |
| 2026-10-05 | `16f475d` | Site README and pilot analysis: drop stale lines and old section numbers |
| 2026-10-05 | `0a222d3` | Build mode: drafted cases relax a made-up string limit instead of being dropped |
| 2026-10-05 | `6226eab` | Nebius: a fresh profile builds within $1.50 and 30 minutes |
| 2026-10-05 | `761502f` | Build mode: a configured or supplied floor relaxes made-up string limits too |
| 2026-10-05 | `46cde7f` | Receipt example: its profile works in the workspace the CLI and the app use |
| 2026-10-05 | `28cbe48` | Expense app: read receipts over plain HTTP to Token Factory |
| 2026-10-05 | `42c4081` | Expense app README: using the exported receipt agent in extract() |
| 2026-10-05 | `b9179be` | Judges: how to switch to a spare code |
| 2026-10-05 | `4ed1315` | Plain messages: an address that does not answer, and a folder that needs a sign-in |
| 2026-10-05 | `2861b56` | Desktop: Try it grows with a long input, and a preset's exam stem starts at the preset |
| 2026-10-05 | `c40a03e` | Floors: say that relaxing fits a floor's cases, drafted or not |
| 2026-10-05 | `a1961fb` | Docs: the plan card's relax note and the Nebius build budget |
| 2026-10-05 | `e68b43d` | Merge hackathon/w3-relay: the relay takes 300 requests a minute from one address |
| 2026-10-05 | `4af5457` | Merge hackathon/w3-core: drafted cases fit the spec, a smaller Nebius budget, plain messages, Try it for long inputs |
| 2026-10-05 | `2e18998` | Merge hackathon/w3-app: the expense app reads receipts over plain HTTP, and judges can switch to a spare code |
| 2026-10-05 | `68d2ec0` | Merge hackathon/w3-docs: the feedback log's onboarding and billing answers, and stale lines out of the site README and pilot analysis |
| 2026-10-05 | `523b31b` | Release scan: allow the made-up credentials in the unreachable-address test |
| 2026-10-05 | `6052f38` | Desktop: American spelling in a bar view comment |
| 2026-10-05 | `e730bc3` | Expense app: say HTTPS, and where the receipt agent goes |
| 2026-10-05 | `baac6b4` | Expense app README: the package reads its stored key from ~/.architect |
| 2026-10-05 | `20399be` | Usage: which schema limits build mode drops to keep the floor's cases |
| 2026-10-05 | `34ee94c` | Pilot analysis: the published plan lists no borderline receipts |
| 2026-10-05 | `a07923a` | Feedback log: state the trial's lag instead of calling it no cap |
| 2026-10-05 | `d00b28d` | Build interview: never ask the model's guess about limits |
| 2026-10-05 | `d50dc46` | Result card: say why a rung whose exam cleared the bar was refused |
| 2026-10-05 | `b4348c5` | Run line: show a sub-cent cost with two significant digits |
| 2026-10-05 | `f2cd31c` | Judge: a next step that works from examples/expense-app |
| 2026-10-05 | `87a3c20` | Build progress: mark the stage line's counts as the build's so far |
| 2026-10-05 | `6c723be` | Terminal cards: wrap long values instead of cutting them short |
| 2026-10-05 | `d6aab28` | Install notice: start the engineer's summary with a capital |
| 2026-10-05 | `921c733` | Plan card: say the raised per-run budget once |
| 2026-10-05 | `29f325e` | Judge install: npm ci without the audit and funding notes |
| 2026-10-05 | `5327a7b` | Sign-in: call a stored judge code a code |
| 2026-10-05 | `d5530eb` | Expense app README: name the steps whose variables the app needs |
| 2026-10-05 | `9af643f` | Your agents: say why Try it could not run, as the agent view does |
| 2026-10-05 | `82e12af` | Sign-in screens: call a stored judge code a judge code |
| 2026-10-05 | `69193ad` | Exported agents: run in the workspace the CLI and the app use |
| 2026-10-05 | `c6e81c3` | Rebuilds: say when the new build was refused and which version stays |
| 2026-10-05 | `5c89fea` | Sign-in: keep a model entry's own settings |
| 2026-10-05 | `7c54a64` | Refusals: say lower the bar, as the reason beside it does |
| 2026-10-05 | `2023ef6` | Pi bridge: install the patched basic-ftp under Pi 0.73.1 |
| 2026-10-05 | `41dd7c7` | Desktop: lock the patched http-cache-semantics |
| 2026-10-05 | `05d7ae8` | Your agents: the cost per run and test report in the app's words |
| 2026-10-05 | `66ef54a` | Merge hackathon/w4-runtime: exported agents use the CLI's workspace, a refused rebuild names the build that stays, sign-in keeps a model's settings, two patched npm packages |
| 2026-10-05 | `e67766d` | Merge hackathon/w4-desktop: Try it on the Agents screen says why a run failed, a stored judge code is called one, the agent page's cost and test report in the app's words |
| 2026-10-05 | `d8e40de` | Floor drafts: send the job under its own key |
| 2026-10-05 | `a9eeeb4` | Build mode: ask once more when a drafted floor does not parse |
| 2026-10-05 | `8f7c36e` | Export: never call a floor the user's data when this profile does not list it |
| 2026-10-05 | `ac546b2` | Build mode: give the conductor an input of the right shape |
| 2026-10-05 | `85941dd` | Build results: a refused build installed and exported nothing |
| 2026-10-05 | `0697159` | Build results: say a declined budget increase in words |
| 2026-10-05 | `88f9629` | Try it: say where a failed run's reason is |
| 2026-10-05 | `2ad14a8` | Docs: say which credential an exported agent's run uses |
| 2026-10-05 | `2908052` | Docs: split the workspace aside in Profiles and drop "as before" |
| 2026-10-05 | `b539494` | Agent card: say the task in its first line, and key only marks the chart draws |
| 2026-10-05 | `8cfeb8b` | Plan card: start the Engineer value with a capital, in the terminal and the app |
| 2026-10-05 | `7f3443b` | Build results: give the out-of-budget step's time in minutes |
| 2026-10-05 | `15b7a90` | Desktop: the spend meter follows a build as it runs |
| 2026-10-05 | `922a560` | README: show the receipt reader's agent page from the desktop app |
| 2026-10-05 | `3d4a8ef` | Frozen builds: a refusal's next step names no /floor, /budget or /model |
| 2026-10-05 | `0617b3d` | Build mode: a drafted floor's bar is one that a single miss still clears |
| 2026-10-05 | `4e48eca` | Build progress: a repeated stage line says how long the stage has run |
| 2026-10-05 | `ea35607` | Build interview: show the model's questions whole, and cut text at a word |
| 2026-10-05 | `fa764e8` | Result card: a build's cost says when failed calls are left out of it |
| 2026-10-05 | `537991f` | Judge codes: a stored code makes Nemotron 3 Super the conversation model |
| 2026-10-05 | `c98c02b` | README: give the picture's scores and say the Results exams are another build |
| 2026-10-05 | `2cc1664` | Desktop: a card's sentence may open with a hyphenated word and still get a capital |
| 2026-10-05 | `9867de6` | Terminal cards: start each sentence row with a capital, as the app does |
| 2026-10-05 | `2e853fa` | README picture: end the frame inside the card, above the next divider |
| 2026-10-05 | `46b5cda` | Docs: say when build mode lowers a drafted floor's limit |
| 2026-10-05 | `6237a42` | Judges: say what to do when the receipt reader is over budget on the exam |
| 2026-10-06 | `c5e2186` | Plan: record the receipt rule checks, predictions and stop rules before any call |
| 2026-10-06 | `0bc30af` | Settings: open Keyboard shortcuts when no shortcut was changed |
| 2026-10-06 | `ffefc0c` | Build mode: call it Build in the app, the CLI and the docs |
| 2026-10-06 | `ae4d30b` | Checks: run the rule checks a spec declares on each reply, before the schema |
| 2026-10-06 | `b96be95` | Receipt checks: the frozen reference and the checked ladder's spec and profile |
| 2026-10-06 | `1b766e0` | Plan card: a Checks row after the engineer with each declared rule |
| 2026-10-06 | `2a3d432` | Receipt reader: declare the two checks; the new hash and flag in the docs |
| 2026-10-06 | `591df68` | Tests: the declared checks answer as the frozen functions |
| 2026-10-06 | `d6a2bcd` | Tests: say exactly why the copy check first loses cord-train-0447 |
| 2026-10-06 | `d2918b6` | Receipt reader: say what the copy check does in its rule sentence |
| 2026-10-06 | `d99810f` | Checks: refuse a declaration that names a field its spec does not have |
| 2026-10-06 | `8a1aef3` | Stage 0: keep checks only from the user's own spec, never from a draft |
| 2026-10-06 | `359feea` | Build mode: keep Forge off for a spec with checks; the plan says when they do not fit |
| 2026-10-06 | `0aa8244` | Tests: an exported package whose declaration was taken out is refused |
| 2026-10-06 | `b808613` | Docs: take /forge off the receipt reader's path; checks and Forge in the limits |
| 2026-10-06 | `bc84b2b` | Merge hackathon/checks: the receipt reader's rule checks, run in code and licensed |
| 2026-10-06 | `f263d02` | Docs: a warm start from a Forge harness can skip a spec's checks |
| 2026-10-06 | `1074a80` | Merge hackathon/checks: a warm start from a Forge harness can skip the checks |
| 2026-10-06 | `e1c5334` | Receipt checks: the checked ladder's one exam on the evidence floor |
| 2026-10-06 | `89d154f` | Plan: record the checked ladder's Gate 1 and exam results |
| 2026-10-06 | `18e5be5` | Published receipts: the checked ladder's signed reports and frozen checks |
| 2026-10-06 | `ec326a6` | Results: the ladder build with the rule checks beside the 2026-10-05 one |
| 2026-10-06 | `e648303` | README: the rule checks step, the checked ladder in Results, and the limit |
| 2026-10-06 | `920fb48` | Judges: the receipt reader's spec is the checked build's; its 18 minutes |
| 2026-10-06 | `2d07d06` | Results: keep the heading "Plain models on 348 unseen receipts" |
| 2026-10-06 | `0a4cb1c` | Results: name the Forge section's build; exam cost equal to the cent |
| 2026-10-06 | `03d527b` | README: the caption's 27 is the exam column; whose spec declares checks |
| 2026-10-06 | `1a20483` | Judges: the receipt spec's hash is the checked build's agent id |
| 2026-10-06 | `feaa94f` | Limitations: where a spec declares checks, and when they run |
| 2026-10-06 | `eedd8d9` | Merge hackathon/build-label: the build mode is called Build; Keyboard shortcuts opens |
| 2026-10-06 | `6cf60bf` | Build mode: the first description goes to make, and one build per turn |
| 2026-10-06 | `49ea336` | Desktop: Send stays in view, and words after a tool call start a new block |
| 2026-10-06 | `cf57cd5` | Site: the results show the build with the rule checks |
| 2026-10-06 | `3a7d68a` | Merge hackathon/site-checks: the site results show the build with the rule checks |
| 2026-10-06 | `9e2fc73` | Relay: one string per ledger record, so a call costs 6 Upstash commands |
| 2026-10-06 | `b5bc510` | Relay: admin.mjs migrate moves an Upstash ledger to the new layout |
| 2026-10-06 | `87a1a37` | Relay README: what a call, a judge build and a month cost the database |
| 2026-10-06 | `e819d31` | Build mode: Not now ends the turn in one sentence |
| 2026-10-06 | `df5f573` | Desktop: a build step says what it did |
| 2026-10-06 | `84e20f3` | Merge hackathon/relay-lean: a model call costs 6 Upstash commands, not 52 |
| 2026-10-06 | `505394b` | Merge hackathon/notnow: Not now ends the turn in one sentence |
| 2026-10-06 | `0df1464` | Relay: day records are kept, with no EXPIRE the next MSET wiped |
| 2026-10-06 | `80e7eaf` | Relay: a host whose clock runs ahead no longer wipes the limiter windows |
| 2026-10-06 | `84c7090` | Relay: a refused call sweeps overdue holds, so a code cannot lock itself out |
| 2026-10-06 | `c386767` | Relay: one bad usage report cannot book more than a call can cost |
| 2026-10-06 | `077a383` | Relay: migrate --force refuses a new ledger that has served a call |
| 2026-10-06 | `f93d3a8` | Relay: admin.mjs mint --existing puts a published code back |
| 2026-10-06 | `5e32461` | Relay: build codes get a 50% share, and the cost test runs a real build |
| 2026-10-06 | `491cfe5` | Relay README: test the new scripts on Upstash before the copy, and check one call after |
| 2026-10-06 | `efb95ce` | Relay README: plainer wording in the cost and migration sections |
| 2026-10-06 | `8d1a5fa` | Relay: retry the ledger outage, then name it and stop the build |
| 2026-10-06 | `8b66742` | Expense app README: step 3 shows every import and the whole extract() |
| 2026-10-06 | `0f3ad76` | Receipt checks: the demo video's two builds of the checked spec |
| 2026-10-06 | `837ba4a` | Published receipts: the two video builds, and the plan reports them; re-signed |
| 2026-10-06 | `b0e13c0` | Docs: report all three sits of the checked design |
| 2026-10-06 | `fc193c8` | Site: one line under the checked table on the two later sits |
| 2026-10-06 | `e3b2867` | Desktop: a reopened conversation shows everything it showed |
| 2026-10-06 | `5504748` | Desktop: the bar view's scale widens for a mark beyond it |
| 2026-10-06 | `1b03afa` | Desktop: a plan set aside says so in the agent panel |
| 2026-10-06 | `551314b` | Desktop: a reopened conversation keeps the turns its record never saw |
| 2026-10-06 | `c057161` | Merge hackathon/sits: report all three sits of the checked design |
| 2026-10-06 | `6d05187` | Merge hackathon/takefixes: restored conversations, bar view scale, set-aside plans |
| 2026-10-06 | `347e6e4` | Tests: the evaluation timeout check allows for one clock tick |
| 2026-10-07 | `88d3e6c` | Docs: the checked spec's three builds, in plainer words |
| 2026-10-07 | `7defb86` | Feedback: Lightning's exam in the checked builds, and which build ships it |
| 2026-10-07 | `dd69fa2` | Published receipts: why there were two video builds, and plainer words; re-signed |
| 2026-10-07 | `18f890b` | Relay README: about 9,050 calls used up the free month, at about 55 commands each |
| 2026-10-07 | `0df2bcf` | Relay: /healthz answers 503 for 30 s after a call's ledger command failed |
| 2026-10-07 | `7e8797b` | Mark: the still mark is one traced outline, no seams at the joints |
| 2026-10-07 | `434114b` | Loader: the middle is one column drawn last; the bars retract 20 units |
| 2026-10-07 | `220911f` | App icon: the mark as one outline, re-rendered |
| 2026-10-07 | `76dc0aa` | Site: the mark as one outline; the bar view's loader draws the column last |
| 2026-10-07 | `f03d2c7` | Test: the mark's outline is its parts' union; the loader draws the column last |
| 2026-10-07 | `7c67c26` | Discovery notes: Lectern's mark as one outline, and the loader's column |
| 2026-10-07 | `7c8823b` | Loader: the foot holds its first frame through its delay |
| 2026-10-07 | `1b5b349` | Loader: the bars hide while they hold retracted |
| 2026-10-07 | `4c08c9e` | Loader: reduced motion holds the one outline, not three parts |
| 2026-10-07 | `d0c57d1` | Tray: resize the icon with 'better' quality |
| 2026-10-07 | `c536ee6` | Working.tsx: name the outline without Lectern's internal identifier |
| 2026-10-07 | `5505795` | Test: the site's hand copies of the mark's geometry |
| 2026-10-07 | `fc08c31` | Site shots: retaken with the mark as one outline |
| 2026-10-07 | `61ea6fe` | Design spec: the design shots predate the one-outline mark |
| 2026-10-07 | `f440e59` | Discovery notes: where Architect's loader now differs from Lectern's |
| 2026-10-07 | `0ccfe58` | Merge hackathon/mark: Lectern's mark as one outline, and the loader's column |
| 2026-10-07 | `36b5c30` | Judges: the public repository and releases, and codes only on Devpost |
| 2026-10-07 | `4f93693` | Release scan: fail on a release placeholder left in Markdown |
| 2026-10-07 | `5d855a2` | Tests: the build-mode ARC test skips without the arc extra, runs in arc-extra |
| 2026-10-07 | `f3693da` | Feedback: Lightning's thinking controls, temperature 0, and a rule Ultra breaks |
| 2026-10-07 | `ab520ea` | CI: a run by hand makes the Linux smoke baseline |
| 2026-10-07 | `7df7f5d` | Installer: leave node-pty's debug symbols out |
| 2026-10-07 | `5e33574` | Release cut: the installer carries the notices and names the license holder |
| 2026-10-07 | `f1322ee` | Release cut: docs/research.md stops pointing at the agent memory |
| 2026-10-07 | `60c3da6` | First start: say it takes a few minutes |
| 2026-10-07 | `f6edc97` | Release README: the baseline check's failure means more than fingerprints changed |
| 2026-10-07 | `8902d45` | Release docs: how the new smoke baseline reaches the public repo; fingerprint wording; two more links to private notes cut |
| 2026-10-07 | `ae328c4` | Feedback: review fixes to FB-05, FB-26 and FB-27; Gate 1's per-check split goes public |
| 2026-10-07 | `356951a` | Gate 1: publish every reply, so anyone can recount the checks' effect |
| 2026-10-07 | `3d66037` | Receipt checks: the fourth build of the checked spec, for the demo video |
| 2026-10-07 | `5c7ae9b` | Published receipts: the fourth build, and the plan reports it; re-signed |
| 2026-10-07 | `c1e18d0` | Docs: the checked spec's fourth build, the one the video shows |
| 2026-10-07 | `ed809e8` | Site: the line under the checked table counts the fourth build |
| 2026-10-07 | `8a57763` | Feedback: FB-26 adds the fourth sit of the checked spec |
| 2026-10-07 | `8ffb55d` | Docs: the feedback README tells the video build as the README does |
| 2026-10-07 | `dde95e1` | README: the picture is the agent card from the build in the video |
| 2026-10-07 | `d1ba568` | README: the caption says only the exam column comes from the 2026-10-05 build |
| 2026-10-07 | `cf143a1` | Desktop: a reopened build counts its time from Build it |
| 2026-10-08 | `d8ebccc` | Desktop: the four catch-all stylesheets are split into owner files |
| 2026-10-08 | `cda6317` | Desktop: shared helpers move to lib and ui, the store holds the build sheets |
| 2026-10-08 | `a508bb8` | Desktop: the flows and Forge view tests are split by owner |
| 2026-10-08 | `6bcb2f9` | Desktop: Lectern v2 tokens, Inter, the type scale and the ui primitives |
| 2026-10-08 | `20684be` | Desktop: the primitives on one page at #/ui in a development build |
| 2026-10-08 | `fe48a96` | Desktop: the conversation on Lectern v2 surfaces, asks held where the card answers |
| 2026-10-08 | `c76e837` | Desktop: panes ask before a discard in a Dialog, panes.css on the scale |
| 2026-10-08 | `065ef5c` | Desktop: merge slice 4, chat surfaces and approvals |
| 2026-10-08 | `17470ed` | Desktop: the composer gains its build and Describe forms |
| 2026-10-08 | `54f28d3` | Desktop: the forge page, one card with the transcript as a rail |
| 2026-10-08 | `bd29e99` | Desktop: the rail, the empty window and the start errors on Lectern v2 |
| 2026-10-08 | `41c3f4b` | Desktop: the build composer sits right under the card |
| 2026-10-08 | `eabf41a` | Desktop: merge slice 3, the forge page, its sheets and the shell |
| 2026-10-08 | `935d8e0` | Desktop: the card's words, states and plan facts come from the build's own rows |
| 2026-10-08 | `bd0a3ce` | Desktop: the ladder draws a model once it starts, the exam grid keeps only its key, Run sits beside Try it's fields |
| 2026-10-08 | `240d385` | Desktop: the agent card names each step and holds its one action |
| 2026-10-08 | `ace5333` | Desktop: merge slice 2, the agent card |
| 2026-10-08 | `9e17e5b` | Desktop: first run is one centred column with the mark, the title and one card |
| 2026-10-08 | `96cc537` | Desktop: Your agents shows each agent as the build card does, its exam through ResultBody |
| 2026-10-08 | `f291695` | Desktop: Settings and Routines on the primitives, confirms in a Dialog, screens.css on the scale |
| 2026-10-08 | `2e52fab` | Desktop: merge slice 5, the screens |
| 2026-10-08 | `9775225` | Desktop: Confirm and QualityBar go, dead CSS goes, Inter is the only UI face |
| 2026-10-08 | `710a3d8` | Desktop: focus rings are outlines, the column follows the card's width, and the screens get their visual pass |
| 2026-10-08 | `0746af8` | Desktop: the dock gives way to the card column, the dark thumb reads as on, Settings nav fits its labels |
| 2026-10-08 | `b08b608` | Desktop: the design and code review pass on the redesign |
| 2026-10-08 | `36e1e35` | Desktop: the ladder tells ships, passed and did not pass apart, and the build card says what it is doing |
| 2026-10-08 | `94f96ec` | Desktop: the guided build's step comes from the build, and each model keeps its time and cost |
| 2026-10-08 | `d5db20b` | Desktop: the wizard page's props, the step screens' props and their placeholders |
| 2026-10-08 | `7001976` | Desktop: the demo build as a fixture, from the recorded tapes, and the wizard's tests |
| 2026-10-08 | `0f61e3e` | Desktop: the receipt job is the first Describe example |
| 2026-10-08 | `12fc4e4` | Desktop: each rung carries its exam cost, and the plan card its task, rules, fields, origin and list prices |
| 2026-10-08 | `3720c35` | Desktop: models name the ladder a build tries, and an agent page has a practice sample |
| 2026-10-08 | `47dcafa` | Desktop: every forge line says when it came, and Build with changes is pinned |
| 2026-10-08 | `c1d316c` | Desktop: the exam table shows each model's score against the pass mark, and the dot grid can draw the pass line |
| 2026-10-08 | `3452b68` | Desktop: Build lists the models with their time and no score before the result, and Results says which one passed and why |
| 2026-10-08 | `01f6e1a` | Desktop: the wizard's Build and Results tests replace the agent card's |
| 2026-10-08 | `580e6fe` | Desktop: merge the Build and Results steps (clarity S3) |
| 2026-10-08 | `f3c870c` | Desktop: Build with changes redraws a waiting plan with the saved settings, and a plan file keeps what it fixes |
| 2026-10-08 | `f7725db` | Desktop: the Plan step says what the agent does and how Architect picks its model, in the plan's own numbers |
| 2026-10-08 | `05ed42b` | Desktop: the Describe step asks for the job in one sentence, and the tests for steps 1 and 2 |
| 2026-10-08 | `ecba19f` | Merge branch 'hackathon/cl-describe-plan' into hackathon/clarity |
| 2026-10-08 | `bb0a272` | Desktop: the ask box is a one-line composer, and the Describe field is driven by its page |
| 2026-10-08 | `f027e1c` | Desktop: the build's top bar, its six steps and the step page's action bar |
| 2026-10-08 | `ac0c432` | Desktop: the build page is the six-step wizard, and the sidebar steps aside on it |
| 2026-10-08 | `a355b7a` | Desktop: the Describe field needs no handle; its page drives it through the draft event |
| 2026-10-08 | `8da0a7e` | Desktop: merge the shell, its top bar, steps and action bar (clarity S1) |
| 2026-10-08 | `a3af748` | Desktop: Try it lives in its own file, and the form lets a screen own its values and its Run |
| 2026-10-08 | `3cf6de9` | Desktop: Try it and Add to project as steps 5 and 6 |
| 2026-10-08 | `21c755f` | Desktop: merge Try it and Add to project (S4) |
| 2026-10-08 | `c33bb4a` | Desktop: a step drawn on its own still has its one-line ask |
| 2026-10-08 | `5b7742a` | Desktop: merge the shell's ask box for a step drawn on its own |
| 2026-10-08 | `d8da4fb` | Desktop: the Add progress line marks its part as current, not as the page's step |
| 2026-10-08 | `6c8e19a` | Desktop: the shell's test counts the current step in the Steps list only |
| 2026-10-08 | `4c77048` | Desktop: merge the Add progress line fix (S4) |
| 2026-10-08 | `f03a80d` | Desktop: merge the shell's step test fix |
| 2026-10-08 | `295b882` | Desktop: the top bar gives way in steps on a narrow window, and Describe alone shows a build loading |
| 2026-10-08 | `454463d` | Desktop: merge the shell's narrow top bar |
| 2026-10-08 | `ad6dbb3` | Desktop: merge the bridge data (SB): exam costs, plan facts, forge times, ladder and practice sample |
| 2026-10-08 | `ccd4811` | Desktop: the clarity screens' visual pass: the plan and Added fit the window, Try it's field fills its pane, results colour their word, and the why-not line points at the plan file |
| 2026-10-08 | `c69c143` | Desktop: the clarity retest fixes: the edit approval shows a true diff, the plan says where its pass mark changes, and every running turn can stop |
| 2026-10-08 | `0532fb8` | Desktop: the clarity retest's last fixes: a plan file's mark changes in the file, the dots say they are a count, the edit preview ends where Architect's does, and Added says why the agent starts off |
| 2026-10-09 | `dc00ca0` | README: the picture is the Results step of the six-step app, from the build in the video |
| 2026-10-09 | `b8dfdc5` | Docs: describe the six-step app in its own words: pass mark, passed, did not pass, Build agent, Try your agent, Add to project and Connect it |
| 2026-10-09 | `c4e0542` | Docs and site: the first-run screen's words, the fonts the app ships, and the landing page's pass mark, passed and did not pass |
| 2026-10-12 | `a3ec7f8` | Retake: floors hold extra exam cases for retakes, a report signs its attempt and its cases, and an exam on a published dataset refuses retakes by default |
| 2026-10-12 | `c5b3dea` | Retake: a demo exam of development receipts, with 150 more that only retakes use, and its plan for the example app |
| 2026-10-12 | `e4757a8` | Retake: a stopped agent says why in plain words, and architect retake, retake --practice and undo examine a change or put back the version that passed |
| 2026-10-12 | `89fef32` | Example app: extract() uses the agent in agents/receipt_reader, and its reply and terminal show the stop sentence when the agent's files changed |
| 2026-10-12 | `b9b7770` | Docs: retake, practice and undo, the two floor keys, and what a stop does not prove |
| 2026-10-12 | `9001713` | Desktop bridge: an agent's page is told where the agent stands in its project, and can retake, practise, cancel and undo |
| 2026-10-12 | `103f2fd` | Desktop: an agent's page says where the agent stands in its project, and offers Retake exam, Try on practice and Undo |
| 2026-10-12 | `c4e0783` | Desktop: an edit approval on an added agent's files says that saving it stops the agent |
| 2026-10-12 | `b3c6304` | Retake: the review's fixes: a retake checks it is the same exam, never claims unused cases it cannot account for, and never writes over a folder that changed meanwhile |
| 2026-10-12 | `90e6d38` | Example app: extract() asks Architect what can be done about a stop, finds the demo agent's folder, and keeps error text out of its replies |
| 2026-10-12 | `e8dda9a` | Desktop: the retake panel says one verdict per page, promises a retake only where one is allowed, and takes its attempt and cost words from Python |
| 2026-10-12 | `11e37ea` | Docs: the retake rule and its narrow claims in the README, a walk-through for judges, the trial's table, and what the fixes changed |
| 2026-10-12 | `839d6e5` | Retake: tests follow the drafted-exam key, the manifest's name stays in the export module, and a pass with its folder not written offers "Write the folder" |
| 2026-10-12 | `faeecf5` | Example app: a file with a mistake ends "Fix it or undo." once |
| 2026-10-12 | `8aca90b` | Docs: one live pass on the fixed code, in the trial's document |
| 2026-10-12 | `c3d6762` | Site: the page leads with the rule that an agent only runs if it passed its exam, then the three steps, the exam, the trial's counts and what the rule does not do |
| 2026-10-12 | `ce64c4b` | Site: the steps are labelled as the app's sentences with our run's numbers, a pass covers the checks, and the trial's by-hand run and the notes under the tables get their own paragraphs |
| 2026-10-12 | `9e427d8` | Docs: the four builds of the checked design are no longer described as the demo video's, which now shows the demo exam |
| 2026-10-13 | `1fe3b7e` | Build: a configured case set whose cases do not take the agent's input fields, or whose answers do not fit what it gives back, is no longer kept as the exam; the cases are drafted instead |
| 2026-10-13 | `55717fa` | Build: a case-drafting reply that is not valid JSON no longer ends the build; the second request says what was wrong with the first, the whole cases of a broken reply are kept as written when they fit the agent's fields, and a stop says what happened and what to do next |
| 2026-10-13 | `f761097` | Chat: after a build that ended without an agent no tool runs in that turn, the turn ends with what the build said, and its fold no longer reads Built the agent |
| 2026-10-13 | `09854ee` | Chat: the fixed answer after a refused build leads with the build's own outcome and no longer says twice that nothing was installed |
| 2026-10-13 | `6154240` | Build: a case set whose answers have the agent's answer fields stays the exam under another input name or looser types, and the plan says which exam is used and why; drafted cases are kept once and never with two answers, cases from a broken reply need 6, a first reply survives a failed second call, and a stop says what the cases file holds |
| 2026-10-13 | `b2dc3df` | Chat: a build that ends in an error also ends the turn without another tool, and the fixed answer no longer carries the failure code |
| 2026-10-13 | `038cb1e` | Desktop: the fourth Describe example asks for a label from a list of three, which an exact-match exam can grade, in place of the meeting transcript |
| 2026-10-13 | `ea6bc45` | Build: a case set the profile already has is the exam only when every one of its cases fits the agent; the agent is changed to fit a set (its input field, the types of answer fields that do not fit) only when the set has a manifest or its own checker, a list of values is never dropped for it, a field the receipt checker does not read is said not to be examined, and the plan note gives that reason; drafted cases need 6 however they came to be few, a reply is said to have broken off only when it did, and answers are compared as inputs are |
| 2026-10-13 | `151b22e` | Build: an agent whose answer has no free text is built on the extractor preset when the plan named the synthesizer, whose prompt asks for prose; live, the two cheaper Nemotron models answered the review labeler with a paragraph where one JSON object was wanted |
| 2026-10-13 | `d23cbb3` | Chat: an error raised before any build starts ends the turn with what the error was (which setting, which file, which model) and does not say a build ran, an error during a build carries what the build reported, and an error from forge arc is not a build that ended |
| 2026-10-13 | `d521efb` | Build: the note on why the prose preset is set aside counts the replayed replies as they were, 19 of 24 not JSON |
| 2026-10-13 | `b32b899` | Build: the same note gives Nemotron 3.5 Lightning's share of those replies as 12 of 12 |
| 2026-10-13 | `62eb757` | Build: an answer field written as a number or a string of digits gains the null its checked set's answers have and keeps both branches, so the receipt sentence keeps the 200 receipts when the plan words the amounts that way, as it did live |
| 2026-10-13 | `77a7989` | README: opens on the agent factory sentence and the four steps, says which editors the stop was recorded with and how the video was made, and speaks as I |
| 2026-10-13 | `107ee9c` | Judges' walk-through: the steps match the app's screens (the folder row, the plan file button, the two-press retake and undo, Save for a spare code), name the installer files and add a first-open step for a Mac |
| 2026-10-13 | `8ef5f05` | Feedback pages: first person, no unfilled blanks, and the submission form's three scores under one heading in place of eight empty ones |
| 2026-10-13 | `53c8e43` | Site: the hero carries the agent factory sentence and the tripwire drawback, the Watch section says how the video was made, and the terminal commands match the README |
| 2026-10-13 | `af68e4e` | Docs: stale video lines, the published exam's retake wording, first person in the example app, and comparisons to other coding tools taken out of the reference and the progress notes |
| 2026-10-13 | `d7b4642` | Release steps for the owner: tagging from a clone of the public repository, publishing the draft release, Mac signing and the site data |
| 2026-10-13 | `7b9a47b` | Judges' walk-through: the ten-minute invoice path comes first, and the receipt build moves under it as the longer path |
| 2026-10-13 | `a918fe6` | Retake trial: the demo build's three exam scores, the Claude Code run and the same stop on a one-minute job |
| 2026-10-13 | `f59622c` | README and site: download and walk-through links near the top, the trial's count in one sentence, and how the film's cards and Undo press were made |
| 2026-10-13 | `3147f1f` | Published exam refusal says Architect doesn't retake a published exam |
| 2026-10-13 | `e0d692f` | Docs: first person in the relay README and the bake-off probe, and tool names out of the interface notes |
| 2026-10-13 | `157b7ea` | Release cut: noqa on the long anchored string lines |
| 2026-10-13 | `1b971ee` | Plan logs the 2026-10-12 private experiment that used the exam; published copy re-signed |
| 2026-10-13 | `f997e6a` | Site data: the video's YouTube id and length |
| 2026-10-14 | `5344a3a` | Bench smoke baseline recorded by the public repo's CI on Linux |
