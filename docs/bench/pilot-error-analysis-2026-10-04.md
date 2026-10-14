# Pilot error analysis, 2026-10-04

This is a calibration note. It reads every receipt that plain Ultra or plain Lightning got wrong in the receipt pilot (`docs/bench/pilot-2026-10-04.md`) and gives each wrong field a cause. Its numbers are not evidence. The evaluation plan uses the pilot for calibration only and keeps its numbers out of the results (`docs/bench/prereg-nebius.md`, "Data" and "The bar").

## Outcome

- Nearly every miss is the model's own. Of Ultra's 91 wrong fields, 89 break a rule the prompt states, on text that supports the label. For Lightning it is 143 of 145.
- One label is wrong (train-0146, item count, already listed in AUDIT-v2) and one receipt allows two readings under the rules (train-0185, item count). No miss came from reply parsing, and none clearly from garbled text, though the item counts of three receipts sit near damaged or flattened lines (see the borderline verdicts).
- One rule decides most of the score. On the 51 pilot receipts whose subtotal label is null, Ultra answered null once and Lightning never. That one field is the only miss on 46 of Ultra's 76 failed receipts.
- A reader that applies the rules perfectly would score about 0.97 under the current labels, somewhere between 0.95 and 0.99. Label noise is not what holds Ultra at 0.493.
- No data or rule change is recommended. The bar of 0.40 and the headroom of 0.187 stand.
- CORD habits that the rules do not state explain 1 miss per model (at most 3 for Ultra and 2 for Lightning, counting borderline verdicts). The practice half of the evidence floor, the only labels Forge sees in the main run, holds no receipt that shows either habit.

## What ran

The pilot kept no replies. So plain Ultra (A1-pilot's configuration) and plain Lightning (A3-pilot's) answered the 150 pilot receipts again on 2026-10-04 between 18:53 and 18:57 UTC, at commit `909a556`, and every reply was kept.

- The calls took the bench's own path. The spec is the receipts spec of suite `v2` with `--plain`: the extractor preset with `verifier_depth` 0 and `retries` 0, and reply parsing `first`. The model settings came from the pilot's profile: temperature 0, thinking off and `max_tokens` 512, on Token Factory's default tier. The prompt quotes the receipts-v2 rules word for word. A wrapper around the provider adapter saved each request body, raw reply and parsed output. No bench row was written.
- Every call went through the local spend relay under the pilot's relay code. All 300 calls returned 200 and were settled from reported usage, with no reasoning tokens.
- A5-pilot ran the same configuration as A3-pilot and passed and failed the same receipts, so the Lightning findings apply to it at the receipt level. Super (A2-pilot) was not run again.
- The captured replies stay outside the repository. This page names receipts by case id and quotes only short field values.

The re-run matched the pilot closely:

| Model | Right in the pilot | Right in the re-run | Same verdict | Same prompt tokens | Same reply length | Field accuracy, re-run |
|---|---|---|---|---|---|---|
| Ultra | 74 | 74 | 146 of 150 | 150 of 150 | 144 of 150 | 0.848 |
| Lightning | 46 | 46 | 150 of 150 | 150 of 150 | 149 of 150 | 0.758 |

- Prompt token counts equal the pilot's on every receipt, so both runs sent the same prompts.
- At temperature 0 Ultra still changed its verdict on 4 receipts. Two that were right in the pilot are wrong now (train-0039 and train-0659), and two went the other way (train-0077 and train-0664). The analysis reads the re-run's replies, so its Ultra misses differ from the pilot's on those 4 receipts.
- No reply was invalid. Every reply was a bare JSON object with the four keys, every call ended normally, and no reply needed the fallback parse.

## How each miss was judged

For every wrong field I read the rebuilt text, the label, both replies and the receipt's CORD annotation (`gt_parse`, and `valid_line` with word positions where the layout mattered). Each wrong field then got one cause:

| Class | Meaning |
|---|---|
| (a) model misread | the text and the rules support the label, and the model answered otherwise |
| (b) rule ambiguity | the rules allow more than one reasonable answer for this text, and the model gave one of them |
| (c) label wrong | the CORD annotation contradicts a clear text |
| (d) garbled text | the rebuild damaged the lines the field depends on |
| (e) format or parse | the reply could not be read, or a value had the wrong type |

The checks ran in the order (e), (c), (d), (b), (a). A verdict that could reasonably go another way keeps its first reading and is listed under borderline verdicts below. Two screens backed the reading. Every missed amount whose label is printed has its annotation on a single rebuilt line, and only one failed receipt carries the `row_split` flag (train-0400, which reads correctly).

## Misses by class

| Model | Field | Wrong | (a) | (b) | (c) | (d) | (e) |
|---|---|---|---|---|---|---|---|
| Ultra | total | 8 | 8 | 0 | 0 | 0 | 0 |
| Ultra | subtotal | 54 | 54 | 0 | 0 | 0 | 0 |
| Ultra | tax | 4 | 4 | 0 | 0 | 0 | 0 |
| Ultra | item_count | 25 | 23 | 1 | 1 | 0 | 0 |
| Ultra | all fields | 91 | 89 | 1 | 1 | 0 | 0 |
| Lightning | total | 14 | 14 | 0 | 0 | 0 | 0 |
| Lightning | subtotal | 59 | 59 | 0 | 0 | 0 | 0 |
| Lightning | tax | 10 | 10 | 0 | 0 | 0 | 0 |
| Lightning | item_count | 62 | 60 | 1 | 1 | 0 | 0 |
| Lightning | all fields | 145 | 143 | 1 | 1 | 0 | 0 |

By receipt, Ultra failed 76: 74 hold only (a) misses, one is train-0185 (b) and one is train-0146 (c). Lightning failed 104, split 102, 1 and 1. Lightning failed every receipt Ultra failed, plus 28 more.

The two receipts outside class (a):

- train-0146, item count, class (c). The label is 6 and both models said 5. "BIBIMBAB" is the second line of a wrapped dish name, with no quantity or price, and CORD made it a menu entry of its own. AUDIT-v2 (section 4.2) lists the receipt as `label_wrong`. It is still in the data.
- train-0185, item count, class (b). The label is 1 and both models said 7. Six lines such as "1 PEACH 0" follow one frozen yogurt. Each has its own quantity, a price of 0, the yogurt's indent on the photo and no marker. The rules keep toppings inside their item, but nothing in the text says these lines are toppings rather than free items. AUDIT-v2 rated the receipt ok, with an edge note.

Five more field verdicts could reasonably go another way. Each keeps its (a) here:

| Receipt | Field | Label | Replies | Other reading |
|---|---|---|---|---|
| train-0396 | item_count | 1 | both 2 | (b): the photo indents "Bag-S" and "PS Carrier" under the tarts as box contents, but the sentence on bags could pull them into the count, which gives 3. Neither reading gives 2. |
| train-0757 | item_count | 2 | Ultra 1 | (b): "Cuci 1.0 0" is a free service line printed above the priced one, and Ultra may have read it as part of the treatment. |
| train-0255 | item_count | 3 | both 5 | (d): two set contents without prices follow "PAKET HAPPY HOUR". The photo indents them and the rebuild drops the indent. |
| train-0718 | item_count | 3 | both 5 | (d): CORD's row_ids move two amounts one line away, next to the two "DINE FOR 2" sets. |
| train-0295 | item_count | 3 | Lightning 4 | (d): the word "Total" sits on the rounding line, so the total line reads "Take Away 20,000". |

Read the other way, Ultra would have 3 (b) and 2 (d) misses, and Lightning 2 (b) and 3 (d). No conclusion on this page changes.

## What the model misses are

The (a) misses fall into a few patterns, and each breaks a sentence of the rules that the prompt quotes. In the examples the label comes first, then the replies.

| Code | What the model did | Rule it breaks | Ultra | Lightning | Examples |
|---|---|---|---|---|---|
| P1 | gave a subtotal where the rules give none, mostly by copying the total | a subtotal is an amount "apart from the total", and an amount the receipt does not print is null | 50 | 51 | train-0008: null; both 36000. train-0541, which prints only a lone "Sub Total": null; both 30000. train-0788: null; Ultra 107271, the item prices added up |
| P2 | dropped decimals that the digits rule keeps | "226,500.00" is 22650000 | 12 | 18 | train-0137 total: 8800000; both 88000. train-0481 total: 6200000; both 62000 |
| P3 | counted quantities, or copied a printed count | count each listed item once, whatever its quantity | 11 | 16 | train-0365: 6; both 9, as in its printed "ITEMS 9". train-0716: 2; Ultra 7 |
| P4 | counted options or set contents as items | lines that belong to an item are part of it | 1 | 4 | train-0608: 1; both 2, counting a "+" add-on. train-0258: 1; Lightning 2, counting an option at 0 |
| P5 | took a tender line, or the amount after rounding, as the total | a tender line is never the total | 3 | 5 | train-0113: null; both 22000, the amount on its CASH line. train-0147: 17001; both 17000, printed only on the e-money line |
| P6 | took a service charge or a discount as the tax | the tax is not a service charge | 1 | 7 | train-0040: 11394; Lightning 5940, the service charge. train-0180: null; both 16400, a 10% discount |
| P7 | picked the wrong printed subtotal | the subtotal is the first amount that sums the items | 0 | 2 | train-0147: 18182; Lightning 15455, the second subtotal, after a discount |
| P8 | miscounted item lines in other ways: wrapped names, long lists, discount or payment lines | count each listed item once | 11 | 40 | train-0277: 11; Ultra 14, Lightning 13, counting wrapped dish names. train-0748: 12; both 11, though two equal lines count twice |
| | all (a) misses | | 89 | 143 | |

P1 decides most of the score. On the 51 pilot receipts whose subtotal label is null, the receipt prints no subtotal the rules accept: most print only a total, two print a lone "Sub Total" that the rules turn into the total, and one prints separate sums for food and beer. Ultra answered null on one of them (train-0113) and Lightning on none. On the 99 receipts with a printed subtotal, Ultra read it right 95 times and Lightning 91. P1 is the only miss on 46 of Ultra's 76 failed receipts and on 32 of Lightning's 104. Had both models applied that one rule, Ultra would have scored 120 of 150 (0.800) and Lightning 78 (0.520). The rule is in the prompt and the bench measures whether an arm follows it, so this is a finding about the models and calls for no change to the data.

## Ceiling for a perfect reader

A perfect reader here applies the receipts-v2 rules exactly to the rebuilt text. It loses train-0146, whose label is wrong. Six more receipts could go either way for it:

- train-0185, item count: the (b) case above.
- train-0757, subtotal: "Total Perawatan 92,000" is a sum for one group, which the rules exclude, and also a total line that a later Grand Total follows, which they include. AUDIT-v2 lists it as a rule conflict. Both models gave the label's 92000.
- train-0396 and train-0257, item count: bag lines inside a boxed set, which the sentence on bags could pull into the count.
- train-0219, subtotal: if the bare "83,716" after "SUBTOTAL: 83,716" is a payment rather than the total, the rules make the subtotal the total and leave subtotal null. AUDIT-v2 calls it borderline.
- train-0295, total: the garbled total line. AUDIT-v2 calls it borderline.

So a perfect reader would get between 143 and 149 of the 150 receipts right (0.953 to 0.993), and about 146 (0.973) if each of the six goes either way with even odds. Ultra's 0.493 is about 48 points under that, and Lightning's 0.307 about 67.

## Recommendations

None. Classes (b) to (d) explain 2 of Ultra's 76 failed receipts and 2 of Lightning's 104. Every pilot arm missed both of those receipts (train-0146 and train-0185), so they move no paired count, and dropping them would change the headroom only through the smaller denominator. The rule text and the data can be frozen as they are. A label found wrong after publication would be logged and still scored as labeled, as the pre-registration draft of 2026-10-04 required. AUDIT-v2 calls three receipts borderline, and train-0185 belongs with them. The evaluation plan (`docs/bench/prereg-nebius.md`) lists no borderline receipts.

For reference, this is what dropping the two receipts would do, re-scored on the existing pilot rows and not applied:

| Change | Pilot receipts | Ultra (A1-pilot) | Lightning (A3-pilot and A5-pilot) | Bar | Headroom |
|---|---|---|---|---|---|
| None, as run | 150 | 74 (0.493) | 46 (0.307) | 0.40 | 0.187 |
| Drop train-0146 | 149 | 74 (0.497) | 46 (0.309) | 0.40 | 0.188 |
| Drop train-0146 and train-0185 | 148 | 74 (0.500) | 46 (0.311) | 0.45 | 0.189 |

The bar moves in the last row only because 74 of 148 is exactly 0.500. A real exclusion would go through the data script. Each dropped pilot receipt pulls the next receipt group into the pilot and shifts the later sets and floor halves (AUDIT-v2, section 6), so an official bar would need a new pilot run on the new set.

The bar sits one receipt below a step. With 150 receipts, 68 to 74 right gives a bar of 0.40 and 75 to 82 gives 0.45. Ultra got 74 in both runs while changing its verdict on 4 receipts, and Super got 75. Under the pre-registration the pilot's count stands, since no cell is rerun to improve a number, so the bar is 0.40. One more receipt right would have made it 0.45.

## CORD habits a harness could learn

The first audit (`examples/receipts/AUDIT.md`, verdict) warned that Forge sees practice labels and could learn CORD habits the prompt never states, which would help A7 (an arm of the pre-registration draft, in which Lightning runs a harness Forge wrote) for reasons unrelated to reading receipts. Among the pilot misses only class (b) depends on such a habit:

- train-0185, both models. CORD keeps zero-priced lines that look like items inside the item above them when they are toppings.
- Counting the borderline verdicts, train-0396 for both models (CORD keeps the bag lines of a boxed set inside the item) and train-0757 for Ultra (CORD counts a zero-priced service line as an item).

That is 1 of Ultra's 91 wrong fields, or at most 3, and 1 of Lightning's 145, or at most 2. Apart from the wrong label of train-0146, every other miss breaks a rule the prompt states. A harness that enforces those rules, for example by leaving subtotal null when no subtotal line is printed, gains for a reason every arm was given, so that part of any A7 gain is not the confound the audit described.

In the main run Forge sees only the 100 practice receipts of the evidence floor. A screen of CORD's annotations over all 798 cases counts the receipts that show each convention:

| Set | Zero-priced lines that look like items, kept inside an item (the train-0185 shape) | Bag lines kept inside an item | Zero-priced lines counted as items of their own |
|---|---|---|---|
| Evidence floor, practice half | 0 | 0 | 1 |
| Evidence floor, exam half | 0 | 1 | 1 |
| Rehearsal, practice half | 2 | 0 | 1 |
| Rehearsal, exam half | 1 | 0 | 2 |
| Pilot | 1 | 2 | 4 |
| Delivery | 8 | 2 | 6 |

The first column counts receipts with a sub-line that has its own quantity and a price of 0, names no bag or charge, starts with no marker such as "-" or "+", and is not indented on the photo. Some of these read as options by their wording. The second counts sub-lines that name a bag or carrier. The last counts top-level lines with a quantity and a price of 0 that name no bag or charge, the opposite convention to the first column.

The practice half of the evidence floor shows neither habit, and its one zero-priced line (train-0527) is counted as an item of its own. A harness built there cannot learn either habit from the labels, so the habits give A7 no edge in the main run. The delivery receipts that show them (8 and 2) meet every arm alike. The rehearsal floor's practice half holds two receipts of the train-0185 shape, so development builds on it could pick the habit up. Those builds are never evidence: the plan keeps the rehearsal set for development and the Forge sessions (`docs/bench/prereg-nebius.md`, "Data").

## Spend

The re-run made 300 calls under the `pilot` relay code and spent $0.14763318 at the list prices of 2026-10-03:

| Model | Calls | Input tokens | Output tokens | Ledger $ |
|---|---|---|---|---|
| Ultra | 150 | 120,033 | 6,289 | $0.1389 |
| Lightning | 150 | 120,033 | 6,380 | $0.00873318 |

Two of the Lightning calls were a check of the capture before the full run. The relay's file ledger shows the same total: the `pilot` code went from $0.42452778 to $0.57216096, and the relay's global total is $0.57245784 of its $26 cap. The cap for this work was $0.30. The relay was stopped after the run.

These lines apply to every table on this page:

```
Prices: Nebius Token Factory list prices of 2026-10-03 (the re-run produced no reasoning tokens).
No Forge session ran. The re-run made no build: plain Ultra and plain Lightning answered the 150 pilot receipts directly.
Regions: Lightning is served from eu-north1 and Ultra from us-central1.
The models may have seen CORD v2 in pretraining.
The input is text rebuilt from CORD's word annotations. No OCR ran.
Every call ran at temperature 0. NVIDIA recommends 1.0 for these models.
Every call ran on the default tier.
These are calibration numbers, never evidence.
```

## Every receipt with a miss

Labels are total · subtotal · tax · items. A cell lists only the wrong fields, each with the reply's value and its code: P1 to P8 are the patterns above, and b and c are the classes. A second code after a slash is the borderline reading. "right" means all four fields matched.

| Receipt | Labels | Ultra | Lightning |
|---|---|---|---|
| train-0008 | 36000 · null · null · 2 | subtotal 36000 (P1) | subtotal 36000 (P1); items 3 (P8) |
| train-0009 | 40000 · 40000 · null · 1 | right | items 2 (P3) |
| train-0014 | 25000 · null · null · 2 | subtotal 25000 (P1) | subtotal 25000 (P1) |
| train-0021 | 44000 · null · null · 3 | subtotal 44000 (P1) | subtotal 44000 (P1) |
| train-0022 | 22000 · null · null · 1 | subtotal 22000 (P1) | subtotal 22000 (P1) |
| train-0025 | 14000 · null · null · 1 | subtotal 14000 (P1) | subtotal 14000 (P1) |
| train-0029 | 22650000 · 22650000 · null · 3 | right | items 4 (P8) |
| train-0038 | 24000 · null · null · 1 | subtotal 24000 (P1) | subtotal 24000 (P1) |
| train-0039 | 70000 · null · null · 1 | subtotal 70000 (P1) | subtotal 70000 (P1); items 3 (P8) |
| train-0040 | 125334 · 108000 · 11394 · 2 | right | tax 5940 (P6) |
| train-0045 | 22000 · null · null · 2 | subtotal 22000 (P1); items 3 (P3) | subtotal 22000 (P1); items 4 (P8) |
| train-0060 | 161000 · null · null · 5 | subtotal 161000 (P1) | subtotal 161000 (P1); items 6 (P8) |
| train-0069 | 23000 · null · null · 2 | subtotal 23000 (P1) | subtotal 23000 (P1) |
| train-0077 | 118100 · 118101 · 10736 · 4 | right | total 120000 (P5); subtotal 118100 (P7) |
| train-0081 | 36000 · null · null · 1 | subtotal 36000 (P1) | subtotal 36000 (P1) |
| train-0092 | 25000 · null · null · 2 | subtotal 25000 (P1) | subtotal 25000 (P1); items 3 (P8) |
| train-0102 | 29999 · 27272 · 2727 · 1 | right | items 2 (P8) |
| train-0113 | null · null · null · 1 | total 22000 (P5) | total 22000 (P5); subtotal 22000 (P1) |
| train-0131 | 22000 · null · null · 2 | subtotal 22000 (P1) | subtotal 22000 (P1) |
| train-0137 | 8800000 · 8000000 · 800000 · 2 | total 88000 (P2); subtotal 80000 (P2); tax 8000 (P2) | total 88000 (P2); subtotal 80000 (P2); tax 8000 (P2) |
| train-0140 | 69000 · 69000 · null · 2 | right | items 3 (P4) |
| train-0141 | 13000000 · null · null · 1 | subtotal 13000000 (P1) | total 130000 (P2); subtotal 130000 (P1) |
| train-0146 | 564425 · 482000 · 48685 · 6 | items 5 (c) | items 5 (c) |
| train-0147 | 17001 · 18182 · 1546 · 1 | total 17000 (P5) | total 17000 (P5); subtotal 15455 (P7) |
| train-0156 | 27300 · 27300 · null · 2 | items 4 (P3) | items 5 (P8) |
| train-0162 | 41580 · 37800 · 3780 · 3 | right | items 4 (P8) |
| train-0176 | 38000 · null · null · 3 | subtotal 38000 (P1) | subtotal 38000 (P1); items 4 (P8) |
| train-0179 | 128764 · 109400 · 11706 · 3 | right | items 4 (P8) |
| train-0180 | 147600 · 164000 · null · 6 | tax 16400 (P6); items 8 (P8) | tax 16400 (P6); items 11 (P8) |
| train-0185 | 58000 · 52727 · 5273 · 1 | items 7 (b) | items 7 (b) |
| train-0199 | 31410000 · 31410000 · null · 3 | right | items 4 (P8) |
| train-0212 | 950000 · null · null · 1 | subtotal 950000 (P1) | total 9500 (P2); subtotal 9500 (P1) |
| train-0219 | 83716 · 83716 · null · 7 | right | items 10 (P8) |
| train-0239 | 177500 · null · null · 10 | subtotal 177500 (P1); items 13 (P3) | subtotal 177500 (P1); items 12 (P8) |
| train-0254 | 259298 · 224500 · 23573 · 7 | items 9 (P3) | items 10 (P8) |
| train-0255 | 104000 · 94546 · 9454 · 3 | items 5 (P8/d) | items 5 (P8/d) |
| train-0257 | 16500000 · 16500000 · 1500000 · 1 | total 165000 (P2); subtotal 165000 (P2); tax 15000 (P2) | total 165000 (P2); subtotal 165000 (P2); tax 15000 (P2); items 6 (P3) |
| train-0258 | 41000 · 41000 · null · 1 | right | items 2 (P4) |
| train-0262 | 67000 · null · null · 5 | subtotal 67000 (P1) | subtotal 67000 (P1) |
| train-0277 | 2307021 · 1963000 · 206611 · 11 | items 14 (P8) | items 13 (P8) |
| train-0287 | 28000 · null · null · 1 | subtotal 28000 (P1) | subtotal 28000 (P1) |
| train-0294 | 78000 · 78000 · null · 6 | items 7 (P8) | items 10 (P8) |
| train-0295 | 20000 · 18181 · 1818 · 3 | right | items 4 (P8/d) |
| train-0296 | 56000 · 56000 · null · 1 | right | items 2 (P3) |
| train-0302 | 353320 · 365000 · 32120 · 2 | right | items 3 (P3) |
| train-0303 | 836000 · 760000 · 76000 · 21 | items 22 (P8) | items 20 (P8) |
| train-0311 | 22000 · 20000 · 2000 · 1 | right | items 2 (P8) |
| train-0312 | 10000 · null · null · 1 | subtotal 10000 (P1) | subtotal 10000 (P1) |
| train-0316 | 505428 · 437600 · 45948 · 6 | items 7 (P8) | tax 21880 (P6); items 10 (P8) |
| train-0332 | 60523 · 60523 · null · 5 | right | items 6 (P8) |
| train-0336 | 35000 · null · null · 2 | subtotal 35000 (P1) | subtotal 35000 (P1) |
| train-0346 | 45000 · null · null · 2 | subtotal 45000 (P1) | subtotal 28500 (P1) |
| train-0347 | 347655 · 294000 · 31605 · 6 | right | tax 22050 (P6); items 7 (P8) |
| train-0359 | 239165 · 204000 · 20885 · 3 | right | items 4 (P8) |
| train-0361 | 282000 · 243000 · null · 5 | right | tax 13365 (P6); items 6 (P8) |
| train-0365 | 441782 · 373600 · 40162 · 6 | items 9 (P3) | tax 28020 (P6); items 9 (P3) |
| train-0392 | 30000 · 30000 · null · 2 | items 4 (P3) | items 4 (P3) |
| train-0394 | 22000 · null · null · 2 | subtotal 22000 (P1) | subtotal 22000 (P1) |
| train-0396 | 5800000 · 5800000 · null · 1 | total 58000 (P2); subtotal 58000 (P2); items 2 (P8/b) | total 58000 (P2); subtotal 58000 (P2); items 2 (P8/b) |
| train-0400 | 110000 · 100003 · 10000 · 4 | items 5 (P3) | items 5 (P3) |
| train-0410 | 125000 · null · null · 1 | subtotal 125000 (P1) | subtotal 125000 (P1) |
| train-0412 | 13000 · null · null · 1 | subtotal 13000 (P1) | subtotal 13000 (P1) |
| train-0419 | 369600 · 320000 · 33600 · 5 | right | items 8 (P8) |
| train-0423 | 22000 · null · null · 2 | subtotal 22000 (P1) | subtotal 22000 (P1) |
| train-0438 | 97500 · 88636 · 8865 · 6 | right | items 7 (P8) |
| train-0439 | 1126510 · null · 102410 · 7 | subtotal 1060000 (P1) | subtotal 645000 (P1); items 10 (P8) |
| train-0441 | 25000 · null · null · 1 | subtotal 25000 (P1) | subtotal 25000 (P1) |
| train-0445 | 42500 · null · 3864 · 2 | subtotal 42500 (P1) | subtotal 42500 (P1) |
| train-0446 | 17500 · null · null · 1 | subtotal 17500 (P1) | subtotal 17500 (P1) |
| train-0471 | 30000 · null · null · 1 | subtotal 30000 (P1) | subtotal 30000 (P1); items 3 (P8) |
| train-0476 | 7550000 · 7550000 · null · 3 | right | total 75500 (P2); subtotal 75500 (P2) |
| train-0481 | 6200000 · null · null · 4 | total 62000 (P2); subtotal 62000 (P1); items 6 (P3) | total 62000 (P2); subtotal 62000 (P1); items 6 (P3) |
| train-0523 | 50000 · null · null · 1 | subtotal 50000 (P1) | subtotal 50000 (P1) |
| train-0528 | 70000 · null · null · 3 | subtotal 70000 (P1) | subtotal 70000 (P1); items 4 (P8) |
| train-0529 | 80000 · null · null · 1 | subtotal 80000 (P1) | subtotal 80000 (P1) |
| train-0531 | 60000 · null · null · 1 | subtotal 60000 (P1) | subtotal 60000 (P1); items 2 (P3) |
| train-0541 | 30000 · null · null · 1 | subtotal 30000 (P1) | subtotal 30000 (P1) |
| train-0558 | 1178100 · 1071000 · 107100 · 2 | right | items 6 (P3) |
| train-0565 | 28000 · null · null · 2 | subtotal 28000 (P1) | subtotal 28000 (P1) |
| train-0581 | 25000 · null · null · 2 | subtotal 25000 (P1) | subtotal 25000 (P1) |
| train-0584 | 7975000 · 7250000 · 725000 · 7 | total 79750 (P2); subtotal 72500 (P2); tax 7250 (P2); items 8 (P8) | total 79500 (P5); subtotal 72500 (P2); tax 7250 (P2); items 10 (P8) |
| train-0601 | 63910 · 116200 · 5810 · 6 | items 8 (P3) | items 8 (P3) |
| train-0608 | 75000 · 75000 · null · 1 | items 2 (P4) | items 2 (P4) |
| train-0631 | 58500 · null · null · 6 | subtotal 58500 (P1) | subtotal 58500 (P1) |
| train-0632 | 22000 · 20000 · 2000 · 1 | right | items 2 (P3) |
| train-0634 | 42000 · 42000 · null · 1 | right | items 2 (P4) |
| train-0645 | 69000 · 62727 · 6272 · 2 | right | items 3 (P3) |
| train-0647 | 11800000 · null · null · 3 | subtotal 11800000 (P1) | total 118000 (P2); subtotal 118000 (P1) |
| train-0659 | 293370 · 254000 · 26670 · 4 | items 7 (P3) | tax 12700 (P6); items 7 (P3) |
| train-0664 | 18000 · 18000 · null · 3 | right | items 4 (P8) |
| train-0677 | 21000 · null · null · 2 | subtotal 21000 (P1) | subtotal 21000 (P1) |
| train-0699 | null · null · null · 3 | total 85000 (P5); subtotal 85000 (P1) | total 85000 (P5); subtotal 85000 (P1); items 4 (P3) |
| train-0716 | 10000000 · 10000000 · null · 2 | items 7 (P3) | total 100000 (P2); subtotal 100000 (P2) |
| train-0718 | 364716 · 307000 · 33156 · 3 | items 5 (P8/d) | items 5 (P8/d) |
| train-0725 | 72500 · null · null · 4 | subtotal 72500 (P1) | subtotal 72500 (P1); items 5 (P3) |
| train-0736 | 24000 · 21818 · 2182 · 1 | right | items 2 (P8) |
| train-0748 | 127678 · 127678 · null · 12 | items 11 (P8) | items 11 (P8) |
| train-0757 | 92000 · 92000 · null · 2 | items 1 (P8/b) | items 4 (P8) |
| train-0761 | 65000 · null · null · 2 | subtotal 65000 (P1) | subtotal 65000 (P1) |
| train-0770 | 67000 · null · null · 1 | subtotal 67000 (P1) | subtotal 67000 (P1) |
| train-0780 | 105000 · null · null · 1 | subtotal 105000 (P1) | subtotal 105000 (P1) |
| train-0788 | 117998 · null · 10727 · 3 | subtotal 107271 (P1) | subtotal 117998 (P1); items 4 (P8) |
| train-0793 | 20000 · null · null · 1 | subtotal 20000 (P1) | subtotal 20000 (P1) |
| train-0796 | 28000 · null · null · 2 | subtotal 28000 (P1) | subtotal 28000 (P1) |
