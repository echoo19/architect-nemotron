# Receipt reader results

These are the results of the receipt evaluation of 2026-10-05 and of the ladder builds with
rule checks of 2026-10-06 and 2026-10-07, signed with Architect's release key. `SIGNED.json`
lists the sha256 of every other file in this folder and carries an Ed25519 signature over
that list.

To check them on your computer, offline:

    architect verify --published receipts

It prints `verified` when every file is exactly what the release key signed, and
`refused: artifact_changed` when a file was edited, added or removed. The release key's
fingerprint is `39762f2071b307da0db6ada328bc0743b228351fe2b78d890b50d09bf6cc30f8`. Its
public half ships with Architect in `architect/keys/architect-ed25519.pub`.

## The results

On 348 receipts that no build step saw, plain Nemotron 3 Ultra got 184 right, Nemotron 3
Super 180 and Nemotron 3.5 Lightning 94, at $0.92, $0.28 and $0.058 per 1,000 receipts.
These are the models alone, with no rule checks.

In the ladder build of 2026-10-05, with a bar of 0.40, the exam refused Lightning (27 of 100
right) and licensed Super (51 of 100) and Ultra (49 of 100). Super ships because it is the
cheaper of the two licensed models.

On 2026-10-06 the spec gained two rule checks, code that applies two of the prompt's rules to
every reply, and one more ladder build sat the same exam. It refused Lightning (39 of 100)
and licensed Super (65) and Ultra (71), and Super shipped again, with 14 more right than
without the checks.

The same spec was built three more times in the desktop app for the demo video, twice on
2026-10-06 and once on 2026-10-07, and the same three agents sat the same exam each time.
At 14:27 UTC on 2026-10-06 Lightning got 39 and was refused, Super 63 and Ultra 70, and
Super shipped. At 20:20 UTC Lightning got 42 and was licensed, Super 65 and Ultra 72, and
Lightning shipped as the cheapest licensed model. At 09:19 UTC on 2026-10-07 Lightning got
39 and was refused, Super 64 and Ultra 72, and Super shipped. Which model ships depends on
how Lightning does on the exam, since it lands within two receipts of the bar.

## What is here

| File | What it holds |
|---|---|
| `plan.md` | The evaluation plan, a copy of `docs/bench/prereg-nebius.md`. It was committed and published as a public Gist before the first model call. Its "Changes after publication" section records the rule checks, their predictions and their results, the video builds included. |
| `A1/`, `A2/`, `A3/` | The bench rows of the plain models: A1 is Ultra, A2 Super and A3 Lightning. |
| `ladder/outcome.json` | The 2026-10-05 ladder build: each rung's practice score, exam counts and result, Forge's two submissions and the spend. |
| `ladder/spec.json`, `ladder/profile.json` | The spec and the profile that build ran with. |
| `ladder/exam-lightning.json`, `ladder/exam-super.json`, `ladder/exam-ultra.json` | Each rung's exam report, as Stage 4 wrote it. |
| `ladder-2026-10-06/outcome.json` | The ladder build with the rule checks: each rung's practice score, exam counts and result, and the spend. |
| `ladder-2026-10-06/spec.json`, `ladder-2026-10-06/profile.json` | The spec that declares the two checks, and the profile that build ran with. |
| `ladder-2026-10-06/exam-lightning.json`, `ladder-2026-10-06/exam-super.json`, `ladder-2026-10-06/exam-ultra.json` | Each rung's exam report, as Stage 4 wrote it. |
| `ladder-2026-10-06/frozen_checks.py.txt` | The two check functions as the plan froze them before the first model call, byte for byte `docs/bench/eval-2026-10-06/frozen_checks.py`. It ends in `.txt` because this folder ships inside Architect's Python package. |
| `ladder-2026-10-06-video-1/outcome.json` | The first build of the same spec for the demo video, on 2026-10-06 at 14:27 UTC, through the relay: each rung's practice score, exam counts and result, and the spend. |
| `ladder-2026-10-06-video-1/exam-lightning.json`, `ladder-2026-10-06-video-1/exam-super.json`, `ladder-2026-10-06-video-1/exam-ultra.json` | Each rung's exam report, as Stage 4 wrote it. |
| `ladder-2026-10-06-video-2/outcome.json` | The second build for the demo video, on 2026-10-06 at 20:20 UTC, which called Token Factory directly while the relay was down: each rung's practice score, exam counts and result, and the spend. |
| `ladder-2026-10-06-video-2/exam-lightning.json`, `ladder-2026-10-06-video-2/exam-super.json`, `ladder-2026-10-06-video-2/exam-ultra.json` | Each rung's exam report, as Stage 4 wrote it. |
| `ladder-2026-10-06-video-3/outcome.json` | The third build for the demo video, on 2026-10-07 at 09:19 UTC, which also called Token Factory directly: each rung's practice score, exam counts and result, and the spend. |
| `ladder-2026-10-06-video-3/exam-lightning.json`, `ladder-2026-10-06-video-3/exam-super.json`, `ladder-2026-10-06-video-3/exam-ultra.json` | Each rung's exam report, as Stage 4 wrote it. |

Run in this folder, `architect bench report A1 A2 A3 --md` rebuilds the table of the plain
models from the rows.

The 2026-10-05 ladder build ran before the release key existed. Stage 4 signed its exam
reports with the build host's HMAC key, which only that host can check, so `architect
verify` on one of those reports alone answers `refused: license_unverifiable`. This
folder's signature covers their bytes. The reports of the builds with rule checks also carry
an Ed25519 seal by the release key, so `architect verify` on one of them alone answers
`verified`.

A pass means the files are the ones the release key's holder signed. It does not prove that
the runs behind them were honest. That still rests on the host that ran them.
