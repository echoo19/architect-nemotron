# Visual spec: bar view, exam chart, gate scatter, receipt card, hash line

This spec covers five visuals that the desktop app and the evidence site share. The desktop app (React, `desktop/src`) ports them, and the static evidence site (`site/`) uses the builders as they are.

| File | What it is |
|---|---|
| `components.js` | Builders and geometry. Plain data in, markup out. No DOM, no framework, so it also runs in Node for a static build. |
| `viz.css` | Role tokens mapped onto Lectern's tokens, light and dark, plus every mark's style. |
| `proto.html` | Every visual with placeholder data, light theme first, with a dark toggle and two motion replays. |
| `shots/` | Renders at 1440x900, 1x and 2x, light and dark, plus two phone captures (390 wide, 2x). |

Every number in `proto.html` and in the shots is placeholder data. None of it is a result.

To view the prototype, serve the repository root over http, because ES modules do not load from `file://`. For example, run `python -m http.server 8000` in the repository root and open `http://localhost:8000/site/design/proto.html`. Add `?theme=dark` to start in dark.

## One grammar for all five

- The bar is a pine rule, 2px wide with round caps, and nothing else in these visuals is drawn that way. It appears in the bar view, the exam chart and the scatter, and the plan card prints the same value.
- Solid means passed and outlined means failed. A solid unit is a correct exam case and a solid ring or point is licensed; an outlined unit is a miss and an outlined ring or point is refused. A dotted unit is an invalid output.
- A closed lock means licensed and an open lock means refused. The lock is a Tabler outline glyph from the set the desktop already ships.
- Grey marks references and context: the best preset's practice score, and harnesses outside a claim.
- The screen uses five words: practice (what the builder saw), exam (what it never saw), bar, licensed and refused. "Floor", "tuning" and "held-out" stay in code and logs.

A mark's position is always its real score. A ring sits at its exam score even when that puts a refused ring right of the rule (a budget refusal) or a licensed ring on it (a score equal to the bar). The lock and the fill carry the decision.

## Tokens

Every colour is a token from `desktop/src/styles/tokens.css`, which copies Lectern's verbatim. `viz.css` gives each job a role name and picks the step, and the steps come from measured contrast on the card surface. Pine 700 is 12.7:1 on white but 1.3:1 on the dark card, so every pine role steps up to 300 in dark.

| Role (`--viz-*`) | Light token | Hex | On card | Dark token | Hex | On dark card |
|---|---|---|---|---|---|---|
| `rule`: the bar | accent-700 | #0E3932 | 12.7:1 | accent-300 | #68C4B4 | 8.1:1 |
| `mark`: rings, licensed fills, points, the practice score that sat the exam | accent-700 | #0E3932 | 12.7:1 | accent-300 | #68C4B4 | 8.1:1 |
| `unit`: exam units, a step lighter so the rule reads through them | accent-500 | #1F6B5E | 6.3:1 | accent-400 | #359C8B | 5.0:1 |
| `trail-old`: earlier practice scores | accent-300 | #68C4B4 | 2.1:1 | accent-500 | #1F6B5E | 2.7:1 |
| `trail-new`: later practice scores | accent-500 | #1F6B5E | 6.3:1 | accent-400 | #359C8B | 5.0:1 |
| `ref`: best preset tick and its stem | text-faint | #6E6E77 | 5.1:1 | text-faint | #94949E | 5.6:1 |
| `context`: points outside the claim, when lit | text-ghost | #A0A0A7 | 2.6:1 | text-ghost | #61616B | 2.8:1 |
| `track`, `frame` | divider | #E9E9E7 | 1.2:1 | divider | #2C2C30 | 1.2:1 |
| `track-quiet`: rungs not run | hairline | #F1F1EF | 1.1:1 | hairline | #222225 | 1.1:1 |
| `diagonal`: practice equals exam | border | #DADAD8 | 1.4:1 | border | #3D3D43 | 1.6:1 |
| `quad`: the caught quadrant, lit | accent-100 | #DBF0EC | area | accent-700 | #0E3932 | area |
| `on-mark`: the lock on a licensed ring | on-accent | #FFFFFF | 12.7:1 on the fill | bg | #131316 | 9.0:1 on the fill |
| `surface` | card | #FFFFFF | | card | #1D1D20 | |

The brand pine is #0E3A33; the token renders #0E3932, and the token wins because it is what the desktop paints. Text on the lit quadrant keeps its contrast: in light, text is 14.7:1 and text-mute 5.1:1 on accent-100. In dark, text is 10.7:1 and text-mute 5.5:1 on accent-700. Marks below 3:1 (the oldest practice dots, the greyed points) carry nothing that is not also in the table twin and the tooltip.

There are no gradients and no decorative shadows. Cards are flat on a 1px card-line. The tooltip is ink on the page colour with no shadow.

Type uses Lectern's three registers and no others.

| Register | Face | Sizes | Used for |
|---|---|---|---|
| Text | Plus Jakarta Sans 500 and 600 | 12.5 to 14 | rung names, model names, card names, the quadrant note |
| Caption | Space Grotesk 500, normal case, tracking 0.01em | 11 | "bar", axis titles, column heads, legends, roles, hash labels |
| Numerals | JetBrains Mono, tabular figures | 11 to 24 | every score, count, axis step, amount, duration, date and hash |

Words never use the mono face and numbers never use the sans. Labels stay in normal case.

## Bar view

Each model rung (Lightning, then Super, then Ultra) gets one row. The scale runs across. The bar is a vertical pine rule through every row. On a rung's row, the best preset's practice score is a grey tick, and Forge's practice scores are pine dots in order, with earlier ones a step lighter. A dot is a score on every practice case, the same cases the tick and the bar are measured on; a quick measure on a few of them is not a dot. The exam score is a ring with a lock. A licensed ring is solid pine with a white closed lock, and a refused ring is outlined with an open lock. A 1.5px stem joins the practice score that sat the exam to its ring, so the drop from practice to exam is a visible line. The exam score is repeated as a number in a mono column on the right, so no number sits inside the plot.

| Part | 1x (CSS px) | 2x (device px) |
|---|---|---|
| Width | fills its container; 578 in the desktop agent card | 1156 |
| Name column, where the plot starts | 86 (76 below 440 wide) | 172 (152) |
| Value column, right-aligned, and its gap to the plot | 48 and 12 (40 and 12 narrow) | 96 and 24 |
| Top band for the rule label | 34 | 68 |
| Row pitch | 44 | 88 |
| Axis band | 28 | 56 |
| Height with three rungs | 194 | 388 |
| Track | 1, centred on a half pixel so it is crisp at 1x | 2 |
| Bar rule | 2, from 8 above the first row to 2 below the last | 4 |
| Best preset tick | 2 by 18 | 4 by 36 |
| Practice dot | 6 (r 3) with a 1.5 surface ring | 12 |
| Practice dot that sat the exam | 9 (r 4.5) with a 2 surface ring | 18 |
| Practice to exam stem | 1.5 | 3 |
| Exam ring | 22 (r 11), stroke 1.5, surface halo to r 13.75 | 44, stroke 3 |
| Lock in the ring | 14 (Tabler grid, stroke 2.1 units, 1.2px) | 28 |
| Ring hit and focus area | 34 (r 17) | 68 |
| Working mark, value column | 14 tall | 28 |
| Rung name | Jakarta 13/500; the rung that ships 13/600 ink; rungs not run text-faint | 26 |
| Exam value | Mono 13/600 ink; refused 13/500 text-mute | 26 |
| Rule label | "bar" Grotesk 11 text-faint, value Mono 12/600 ink | 22 and 24 |
| Axis steps | Mono 11 text-faint, every 0.1 | 22 |

A rung is `done`, `running`, `pending` or `skipped`. While it runs, Lectern's working mark waits in the value column where the score will land. Pending and skipped rungs share a faint name and a quiet track; skipped means a cheaper rung was already licensed. A `done` rung whose exam was not scored shows `unscored` in the value column, with its counts in the tooltip.

Start every bar view of a build on one domain, set when the plan is made: `barDomain(bar)` is half a unit on 0.1 steps that starts 0.2 below the bar, kept inside [0, 1]. The receipts task's bar of 0.40 gives `[0.2, 0.7]`; a bar of 0.80 gives `[0.5, 1]`. The desktop's plan card carries it as `domain`. A mark beyond it moves that end out to the next 0.1 step, never past [0, 1] (`widenDomain` in the desktop's `lib/viz.ts`), so every mark sits at its value and the bar keeps its place until a score lands outside the domain: a practice score of 0.78 turns `[0.2, 0.7]` into `[0.2, 0.8]`. `scoreDomain()` (from the lowest value, never less than 0.3 wide) is for charts with no bar to fix them.

The legend appears once per screen, under the first bar view. Each entry is the mark itself followed by one word: best preset, practice, licensed, refused.

The builder takes:

```js
barView({
  bar: 0.80, domain: barDomain(0.80), // [0.5, 1]
  rungs: [{
    name: 'Lightning', model: 'Nemotron 3.5 Lightning', state: 'done',
    preset: 0.74,                      // grey tick
    practice: [0.58, 0.63, /* ... */], // Forge's practice scores, oldest first
    examined: 0.88,                    // optional: what sat the exam (default: best dot, else the preset)
    exam: { correct: 86, cases: 100, invalid: 1 },
    licensed: true, ships: true,
    reason: 'over budget',             // optional: why a rung at or above the bar was refused
  }],
}, { width, legend: true, enter: ['Lightning:ring'] })
```

## Exam chart

This is a unit chart of a signed report's counts, ten to a row. Correct cases come first as solid units, then misses as outlined units, then invalid outputs as dotted units. The bar is drawn as a stepped pine rule after the cell the bar needs, `ceil(bar x n)`. To clear the bar, the solid units must reach the rule; a report that falls short leaves outlined units above it. Beside the grid sit the lock, the score and a legend that doubles as the counts. The chart knows only counts, because no per-case exam data leaves `verify()`, so units are grouped by kind and never imply which case was which.

| Part | 1x (CSS px) | 2x (device px) |
|---|---|---|
| Unit | 14 (r 7) | 28 |
| Gap and pitch | 6 and 20 | 12 and 40 |
| Grid, 100 cases | 194 by 194 | 388 by 388 |
| Grid, 20 cases (the judge build) | 194 by 34 | 388 by 68 |
| Label column ("bar 0.80", left end of the rule) | 70 | 140 |
| Bar rule | 2, overhangs 7 each side, runs through the gaps on whole pixels | 4 |
| Missed unit stroke | 1.5 | 3 |
| Invalid unit | 1.9 round dots about 4.4 apart | 3.8, about 8.8 apart |
| Lock | 18 | 36 |
| Score | Mono 24/500 ink, "/n" at 62% in text-faint | 48 |
| Legend rows | 12 glyph, Mono 12.5 count, Jakarta 12.5 word, 7 apart | doubled |
| Side block | 36 right of the grid, wraps below on a narrow card | 72 |

For a larger site version, pass `{ cell: 18, gap: 8 }`. When n is not a multiple of ten, the last row is short and the rule still spans the full grid. An unscored report (invalid outputs over the ceiling, or a budget failure) draws invalid and over-budget units dotted and the rest as quiet outlines, and reads "not scored".

```js
examChart({ cases: 100, correct: 86, invalid: 1, overBudget: 0, bar: 0.80, licensed: true }, { cell: 14, gap: 6 })
```

## Gate scatter

Each harness is one point, with its practice score across and its exam score up. Both axes share one domain, so the plot is square and a faint line marks practice equal to exam. The bar is a pine rule on both axes. The bottom-right quadrant holds the harnesses that passed practice and failed the exam. When the chart is lit, that quadrant is tinted and names its count, for example "9 of 31, passed practice, failed exam", where 31 is every harness that met the bar in practice. Harnesses that passed practice stay pine, and the rest turn grey. Solid points were licensed and outlined points were refused.

| Part | 1x (CSS px) | 2x (device px) |
|---|---|---|
| Width | fills its container; 560 default; small layout below 460 | doubled |
| Pads: left, right, top, bottom | 42, 14, 30, 44 (small: 36, 10, 30, 40) | doubled |
| Plot | square: width minus the side pads | doubled |
| Frame and diagonal | 1 | 2 |
| Bar rules | 2; the vertical rises 6 above the plot to its label, the horizontal runs 6 past the right edge | 4 |
| Point | 9 (r 4.5), small 8 (r 4); outlined stroke 1.5 | 18 (16), stroke 3 |
| Point hit area | 24 (r 12) | 48 |
| Quadrant note | count Mono 17/600 ink, "of n" Mono 13 text-mute, line Jakarta 12.5/500, 10 in from the emptiest corner | doubled |
| Axis titles | "exam" above the y steps, "practice" under the x steps at the right, Grotesk 11 | 22 |

The note goes in whichever corner of the quadrant no point covers. When the quadrant is narrower than 214px the line breaks in two, and when the quadrant cannot hold it at all, as on a phone, the note moves under the chart as a line with a 12px swatch of the tint. Either way the count stays on screen.

```js
gateScatter({ bar: 0.80, points: [{ id: 14, label: 'Build 5, submission 2', practice: 0.84, exam: 0.76 }] }, { width, lit: true })
```

## Receipt card

This is the signed run summary, laid out as a receipt. The head carries the agent's name and date, then the verdict: the lock, the exam score, and a line beneath them that reads "exam · invalid 1 · bar 0.80". The body is a table with one row per role (role and model on one line, then calls, tokens, cost and time) and a total. The foot is the proof: the Forge pack hash, the spec hash, and the signing key's fingerprint with the browser's check.

| Part | 1x (CSS px) | 2x (device px) |
|---|---|---|
| Card | up to 480 wide; padding 18, 20, 14; radius 12; 1px card-line; no shadow | doubled |
| Name, date | Jakarta 14/600 ink; Mono 11.5 text-faint | 28; 23 |
| Lock and score | 18; Mono 22/500 ink, "/n" at 62% | 36; 44 |
| Meta, column heads, roles, "total" | Grotesk 11 text-faint | 22 |
| Model | Jakarta 13/500 | 26 |
| Numbers | Mono 12.5/500, tabular, right-aligned; total row 600 ink | 25 |
| Rows | 8 above and below, hairline between, divider above the total | 16 |
| Narrow card (400 or less, a container query) | role 52 wide, numbers 12, the model's family hidden from sight but read aloud ("Lightning") | doubled |

Values print as `$1.37`, `$0.009`, `$0.00018` (two significant figures under ten cents, so the cheapest model's cost per run still reads), `1.41M`, `23.5K`, `18m 24s` and `2026-10-18 15:42 UTC`. Exact token counts are in each cell's title.

```js
receiptCard({
  name: 'receipt-reader', date: '2026-10-18T15:42:07Z',
  roles: [{ role: 'Build', model: 'Nemotron 3 Ultra', calls: 58, tokens: 1412880, cost: 1.372, seconds: 1104 }],
  exam: { cases: 100, correct: 86, invalid: 1, bar: 0.80 }, licensed: true,
  pack: '<sha256>', spec: '<sha256>', prereg: 'prereg-2026-10-11', packMatch: true, specMatch: true,
  signature: { alg: 'Ed25519', key: '<fingerprint>', verified: true },
})
```

## Hash line

A hash is set the way people compare them: mono, lowercase, eight groups of eight in two lines of four, with the published match and a copy button beside it. Selecting the text copies the hash unbroken, because the groups are spans with no characters between them. The plan card shows the spec hash and the Forge pack hash this way. The receipt reuses it for both hashes and the signing key.

| Part | 1x (CSS px) | 2x (device px) |
|---|---|---|
| Columns | label 34, value exactly four groups wide (about 252 at 12px mono), state 24, action 24, gaps 10 | doubled |
| Hash | Mono 12/500, groups 0.6em apart, line height 22 | 24, 44 |
| Label | Grotesk 11 text-faint: spec, pack, key | 22 |
| Match state | circle-check 15 in pine with the tip "Matches prereg-2026-10-11"; alert-triangle 15 in warn when it differs; nothing when unknown | 30 |
| Copy | 24 button, 14 icon, tip "Copy" then "Copied" | 48, 28 |
| Note | Jakarta 12 text-faint under the hash, for example "frozen, receipts-v1.json" | 24 |
| Narrow container (380 or less) | the label stacks above; a column narrower than four groups wraps two groups to a line, so no group is left alone | doubled |

```js
hashLines([
  { label: 'spec', hash, match: true, matchName: 'prereg-2026-10-11', note: 'frozen, receipts-v1.json' },
  { label: 'pack', hash: packHash, match: true, matchName: 'prereg-2026-10-11' },
])
```

## Motion

Motion happens once, when a value arrives, and it uses Lectern's curves.

1. A static render is complete. Every visual draws at its final values with no animation, and page loads, replayed builds and the site all draw this way.
2. A live value appears at its value. The new mark (a practice dot, or a ring with its stem and number) enters with opacity 0 to 1 and scale 0.6 to 1 over 180ms on `cubic-bezier(0.16, 1, 0.3, 1)`, and lines and text fade in over 220ms. Nothing slides along the axis or grows from zero. When a mark lands beyond the domain, the domain widens (see Bar view) and the marks already drawn take their new places at once. When a better practice dot arrives, the emphasis moves to it without animation.
3. Each arrival plays its own entrance. Forge's measures come at Forge's pace, minutes apart. A demo or film replay spaces them at least 120ms apart; the prototype uses 320ms.
4. The exam chart arrives as one signed report and fades in as one piece over 200ms after `compile()` returns. It never counts up or fills cell by cell, because the report holds no order.
5. Lectern's working mark is the only loop. It sits in a rung's value column while that rung is being worked on and unmounts when the score lands.
6. The scatter's reveal (the film's cold open, the site's replay button) tints the quadrant from 0 to full over 240ms on `cubic-bezier(0.4, 0, 0.2, 1)`. The points outside the claim turn grey in the same 240ms, and the note fades in 120ms later. Toggle `is-lit` on the figure to play it.
7. With reduced motion there is no scale: entrances become 120ms fades and the reveal is immediate.

## Accessibility

- Colour is never the only signal. In the bar view, the side of the rule, the lock's shape, the fill and the printed value all agree. In the exam chart, fill, outline and dots carry the kind, and the counts are printed beside it. In the scatter, position and fill carry it, with the quadrant note. The receipt and hash lines use glyphs that have names and tooltips.
- Numbers are always mono with tabular figures, so columns align and digits hold still as values update.
- Each chart has a one-sentence accessible name with its values. The bar view and the scatter also carry a visually hidden table with every value. The exam legend and the receipt are real text, and the receipt is a real table with row and column headers.
- Rings are in the tab order with their full readout as a label. Hover and focus show the same tooltip, and tooltips set text only, never markup.
- Hit areas are at least 24px: rings 34, points 24, buttons 24. Focus shows an accent-500 ring at 60%.
- Every mark meets 3:1 on its surface except the two listed under Tokens, whose values are also in the table and the tooltip.

## Using it on the site

- Copy `tokens.css`, `fonts.css` (with its three woff2 files) and `motion.css` verbatim from `desktop/src/styles`, then load `viz.css`. The prototype links them in place so the two cannot drift.
- Render at build time in Node, `const html = gateScatter(data, { width: 560 })`, and write the strings into the page. The charts need no script to display.
- In the browser, `mount(el, (w) => gateScatter(data, { width: w }))` re-renders at the container's width, so text stays at its true size instead of scaling with the drawing. `attachTips(document)` adds hover and focus readouts and the copy buttons.

## Porting to the desktop

- `layoutBarView`, `layoutExam`, `layoutScatter`, `barDomain`, `scoreDomain`, `examScore` and the `fmt*` helpers have no DOM and no framework. Import them, or copy them into `desktop/src/lib/viz.ts` with types. The markup functions are the reference for element order, classes and attributes, and JSX mirrors them.
- Copy `viz.css` to `desktop/src/styles/viz.css` unchanged. Its dark block keys on `html[data-theme="dark"]`, which the desktop already sets.
- Icons: `<IconLock size={14} stroke={2.1} />` and `<IconLockOpen />` in rings, `IconCopy`, `IconCircleCheck`, `IconCircleX` and `IconAlertTriangle` in hash lines. Use `<WorkingMark className="working-sm" />` in the value column while a rung is in flight.
- Entrances: keep the set of mark keys from the last render and add `viz-enter` to keys that are new, such as `Lightning:dot:7` and `Lightning:ring`.
- Suggested placement: the bar view in the agent card under the task line, live during the build. The exam chart goes under it after `compile()` returns, from the signed report's counts only. The hash lines go at the foot of the plan card, where "spec" and its hash sit in the footer today.
- Suggested sources: rungs from the ladder's stage feed; practice dots from Forge's results that ran every practice case (`measurement.score` where `measurement.sample_count` equals the plan's practice count, which in practice means its submissions), in order; the tick from Stage 2's best preset; the ring from the signed report (`held_out_counts` and `licensed`). An exam that was not scored has no ring: its signed score is a 0.0 placeholder, so the row reads `unscored`.
- The plan card's hash lines carry no match state until the app can compare them with the pre-registration manifest; a check that meant anything else would be read as that match.
- For the film's thumbnail (the pine rule and one ring per rung, no words), render the bar view with `legend: false` and hide its text.

## Decisions beyond the brief

These are choices made here that the brief did not ask for. Each can be dropped without touching the rest.

1. The exam chart repeats the bar as a stepped rule after cell `ceil(bar x n)`, so clearing the bar reads as the solid units reaching a line.
2. A stem joins the practice score that sat the exam to its ring, so the drop from practice to exam is drawn as a line.
3. Exam scores are repeated as numbers in a right-hand column, and the working mark waits in that column while a rung is in flight.
4. Earlier practice dots are a step lighter, so a still frame keeps their order.
5. The scatter has a practice equals exam line, and when lit, harnesses outside the claim turn grey.
6. The receipt shows the signing key's fingerprint and the browser's check.
7. The legend appears once per screen.

## Screenshots

Shots are in `shots/`. Each board was captured as a 1440x900 viewport at 1x and at 2x (2880x1800), in light and dark: `bar-*`, `exam-*` and `site-*`, named `<board>-<theme>-<scale>.png`. `mobile-site-light.png` and `mobile-bar-light.png` are 390 wide at 2x. They were rendered in headless Chrome with puppeteer-core, one browser at a time. The same run checked that the console was clean, that no text left its drawing, that the page did not scroll sideways, and that both replays ended in the right state.

These shots date from 2026-10-03, before the mark became one traced outline, so their header still draws it as three overlapping parts. `proto.html` now draws the outline.

## Licences

- The lock, lock-open, copy, circle-check, circle-x, alert-triangle, play, sun and moon paths come from Tabler Icons (MIT), the same set as `@tabler/icons-react` 3.48.0 in the desktop.
- The working mark and every token are Lectern's, which the owner licenses to Architect under MIT. The grant and the font notices (SIL OFL) belong in `THIRD_PARTY_NOTICES.md`.
