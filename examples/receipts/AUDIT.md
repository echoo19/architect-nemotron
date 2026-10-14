# Receipt label audit (W5 hand-check)

Written by hand on 2026-10-03 against `receipts-v1` (rules sha256 `6c5825cd…`), dataset revision `7f0115a4…`. The data script does not generate or check this file. No model was called, and no derived file or script was changed. Case ids below drop the `cord-` prefix, except in the exclusion list.

## Status: applied as receipts-v2

On 2026-10-03 `scripts/fetch_bench_data.py` applied section 5 as `receipts-v2` (rules sha256 `b552ab60…`). RULES.md describes the result. It departs from this audit in these ways:

- **Rules.** The 5.1 draft, with the obvious total names dropped (TL and PAID stay) and two additions that CORD's labels support: the amount on an ITEMS line is the subtotal, and a line that belongs to an item stays part of it even when it prints its own quantity and price (57 of the v1 cases annotate such lines as part of an item).
- **Row split.** Clustering by centre y, as 5.3 proposed, paired amounts with the wrong labels on tilted photos: test-0072 read "cash: 17,500", and train-0795 split GRAND from TOTAL. The script instead gives each largest set of stacked words one line per word, top to bottom, and compares positions along the photo's tilt. After the split train-0400, 0516, 0596 and 0759 read correctly and are back in the sets, and train-0795 joins train-0004 through the digits key. train-0293, 0425 and 0568 stay out as `garbled_text`: their amounts sit in the row_id of another line, which no split within a row can fix.
- **Near duplicates.** A second review found 7 more copies of one purchase: train-0212 / 0227, train-0197 / 0215, test-0096 / validation-0004, test-0044 / train-0662, test-0084 / validation-0014, train-0443 / 0675, and train-0530 / validation-0077, whose tax labels differ. train-0541 / validation-0029 is not merged, because the cash tendered differs (30000 and 50000), so they are two purchases; validation-0029 is out anyway (E2). The review read every pair of v1 cases whose letter and digit runs, as multisets, have a Jaccard similarity of 0.7 or more, the pairs from 0.6 to 0.7 that share half their amounts or their total, and every pair with the same total and either the same payment amounts or the same two or more item prices.
- **Copies.** A listed receipt takes its duplicate group with it: train-0694 (a copy of train-0490) and train-0084 (a copy of validation-0053) are out too. train-0593 is recorded as a label conflict with train-0159, with its E5 note.
- **Sets.** Rehearsal is 100 receipts (50 practice, 50 exam), by the owner's decision. The sets are pilot 150, rehearsal 100, evidence 200 and delivery 348 (166 train, 86 validation, 96 test), and 202 of CORD's 1,000 receipts are left out.
- **Fresh check.** A hand check of 40 v2 receipts (`random.Random(20261005)`, strata as in section 1, 1/4/10/10/10/5) found no label that the v2 rules contradict. Screens of every v2 case found none either: null totals, null subtotals or taxes beside a matching line, extras and bags counted the other way, and the suspect flags.
- Section 7 reproduces the v1 sample only at commit 3d6f304.

## Verdict

**The plan's trigger (more than 5% wrong in the hand-check) is met. Act before the pilot on 10/9:** the pilot sets the bar, and the rule text is quoted in every plain prompt and recorded in the pre-registration.

- **Wrong labels are rare in the random sample.** Only 1 of the 50 receipts has a plainly wrong label: 2%, with a 95% interval of 0.4–10.5%. Weighted back to all 854 cases, that is about 0.5%.
- **Receipts without one clear answer are common.** 13 of the 50 have at least one label that a careful bookkeeper following RULES.md could not be sure to give from the text: 26% (interval 16–40%), or about 18% of all 854 once weighted. They split into 8 rule gaps, 4 ambiguous texts (3 of them garbled by the rebuild) and 1 wrong label. Exact-match scoring counts a correct reader's answer on these as a miss.
- **More wrong labels outside the sample.** 9 of the 19 receipts the data build flagged as suspect have a wrong label, and keyword and arithmetic screens found 5 more. 8 of these 14 are in the delivery set.
- **The derivation code is right.** An independent re-implementation of RULES.md reproduces all 854 labels and texts exactly. The faults are in CORD's annotations, and in gaps between the prompt rules and CORD's habits.
- **Why this is more than noise.** Forge sees the tuning labels, so it can learn CORD habits that the prompt never states: bags count as items, "ITEMS TOTAL" or "HARGA JUAL" is the subtotal. The plain-prompt arms cannot learn them. Left as they are, these cases tilt H1 toward A7 for reasons unrelated to reading receipts.

**Before the pilot, in `scripts/fetch_bench_data.py`:**

1. Replace the prompt rules with the receipts-v2 draft in §5.1. This bumps `RULE_VERSION` and changes the rules sha256.
2. Freeze the 42-receipt exclusion list in §5.2. Apply it like `label_conflict`: before the split assignment, with its reason recorded in `manifests/excluded.json`.
3. Split row_ids that span several printed lines (§5.3); this changes 16 texts. If the 8 garbled receipts (E4) then read correctly, take them off the exclusion list; otherwise keep them excluded.
4. Merge the 11 near-duplicate pairs that both duplicate keys missed (§5.4).
5. Hand-check the rebuilt sets again with a fresh seed before anything is frozen.

**Effect on set sizes.** Exclusions applied before the split assignment leave pilot, rehearsal and evidence at 150, 150 and 200, and cut delivery from 354 to 312 (854 − 42 − 500). The merges take about 9 more, so delivery ends near 303, or about 311 if E4 comes off the list after §5.3. Shrinking rehearsal to 100 would add 50 back. This bears on the open decision between 354 and the planned 500.

## 1. Method

**Population.** The 854 derived cases: pilot 150, rehearsal 150, evidence 200 and delivery 354.

**Strata.** Each receipt goes to the first stratum that fits, in this order:

| Stratum | Definition | Cases | Sampled |
|---|---|---|---|
| V | has a void line | 1 | 1 |
| T0 | total is null | 24 | 6 |
| S1 | subtotal or tax null, with a quantity above 1 or a discount line | 151 | 15 |
| S2 | subtotal or tax null, with neither | 303 | 13 |
| S3 | subtotal and tax printed, with a quantity above 1 or a discount line | 137 | 10 |
| S4 | none of the above | 238 | 5 |

- A quantity above 1 is the build's `quantity_over_1` flag. A discount line is an annotation in `menu.discountprice` or `sub_total.discount_price`.
- Receipts with a null field are 70% of the sample against 56% of all cases. Receipts with a quantity above 1, a void line or a discount line are 60% against 35%.
- By set, the sample holds 17 delivery, 16 evidence, 10 pilot and 7 rehearsal receipts.
- The draw used `random.Random(20261003).sample` over each stratum's sorted case ids, in the order of the table. §7 has the code.

**Reading.** For each receipt I first read the rebuilt text and wrote down what a careful bookkeeper applying the prompt rules would answer. Then I compared that answer with the four labels and the raw `gt_parse` and `valid_line` annotations.

**Verdicts.** A receipt takes its worst verdict; the Field column names the label concerned.

- **ok:** all four labels are what a careful reader following the rules would give.
- **label-wrong:** the text and the rules give a different value, so the annotation is wrong.
- **rule-gap:** the text is clear, but the rules do not settle the point, or settle it against the label. CORD's own labels show which convention it used.
- **text-ambiguous:** the rebuilt text does not let a careful reader tell which value is meant.

**Mechanical check.** I wrote fresh code from the RULES.md description, reading labels from `gt_parse` and text from `valid_line`, without reusing the script's functions. It reproduced all 854 cases exactly.

## 2. Results

| Verdict | Receipts | Labels (of 200) |
|---|---|---|
| ok | 37 | 183 |
| label-wrong | 1 | 1 |
| rule-gap | 8 | 12 |
| text-ambiguous | 4 | 4 |

| Measure | In the sample | Weighted to all 854 |
|---|---|---|
| Receipts with a wrong label | 1 of 50: 2% (0.4–10.5%) | 0.5% (0–1.3%) |
| Receipts with any label that is not the unique careful answer | 13 of 50: 26% (16–40%) | 18% (8–28%) |
| Labels that are not the unique careful answer | 17 of 200: 8.5% | – |

- The sample intervals are Wilson 95%. The weighted figures use stratum weights, with an approximate 95% interval from the stratified variance.
- Receipts that are not ok, by stratum: V 0/1, T0 5/6, S1 1/15, S2 3/13, S3 4/10, S4 0/5.

**Reading the trigger.**

- The strict rate (2%) is under 5%, but its interval reaches 10.5%.
- The broad rate is far above 5% on any reading.
- The flagged receipts and the screens add 14 definite errors (§4), 13 of them outside the sample.
- The trigger exists to protect exact-match scoring, so it is treated as met.

## 3. Per-case verdicts

Labels are total · subtotal · tax · items. In the Fix column, E1–E5 refer to the exclusion list in §5.2 and "v2" means the receipts-v2 rule text resolves the case.

| # | Case | Set | Stratum | Labels | Verdict | Field | What the text shows | Fix |
|---|---|---|---|---|---|---|---|---|
| 1 | train-0279 | evidence | V | 15000 · null · null · 2 | ok | | "SOP AYM BNG -7,000" cancels the soup above it. The rule keeps the cancelled item, so 2. | |
| 2 | test-0009 | delivery | T0 | null · 36000 · null · 1 | rule-gap | total, subtotal | Only "Sub Total 36000", then "Discount (0%)", "Tunai" and "Kembalian". CORD labels this layout both ways (§4.3). | E2 |
| 3 | train-0035 | rehearsal | T0 | null · 289000 · null · 1 | rule-gap | total, subtotal | Same layout as test-0009. | E2 |
| 4 | train-0468 | evidence | T0 | null · null · null · 5 | ok | | No total line. "CASH 167.000" is a tender: it equals the item sum, but the rules forbid computing. | |
| 5 | train-0490 | rehearsal | T0 | null · null · null · 1 | label-wrong | total | The fuel receipt prints "Total Rp. 250.000", which CORD annotated as the item price. Total is 250000. | E1 |
| 6 | train-0555 | delivery | T0 | null · null · null · 3 | rule-gap | items | A combo plus "1 UP Drink 16 0 0" and "1 UP Orange 16 2.000 2.000" upgrades. They count as items here but as part of the combo in train-0271. | E3 |
| 7 | train-0683 | evidence | T0 | null · 102000 · null · 3 | rule-gap | total, subtotal | Same "Sub Total" layout. | E2 |
| 8 | test-0022 | delivery | S1 | 40000 · null · null · 2 | ok | | | |
| 9 | test-0063 | delivery | S1 | 57000 · 57000 · null · 2 | ok | | "Ice" and "Kopi Susu Kampung Less Ice 0" are options of the first coffee. | |
| 10 | test-0069 | delivery | S1 | 74000 · null · null · 3 | ok | | | |
| 11 | train-0062 | evidence | S1 | 3600000 · null · null · 1 | ok | | "Total (Qty= 60 3.600.000": the quantity sits inside the total line. | |
| 12 | train-0078 | evidence | S1 | 56000 · 56000 · null · 1 | ok | | | |
| 13 | train-0081 | pilot | S1 | 36000 · null · null · 1 | ok | | | |
| 14 | train-0261 | evidence | S1 | 134000 · 134000 · null · 5 | ok | | "Pajak -" has no digit, so tax is null. | |
| 15 | train-0392 | pilot | S1 | 30000 · 30000 · null · 2 | ok | | "PAID 30.000" is the amount due. The free shopping bag counts, and the receipt prints "4 PCS". | |
| 16 | train-0517 | delivery | S1 | 87000 · null · null · 3 | ok | | | |
| 17 | train-0531 | pilot | S1 | 60000 · null · null · 1 | ok | | | |
| 18 | train-0716 | pilot | S1 | 10000000 · 10000000 · null · 2 | rule-gap | items | "1 TAKE AWAY CHARGE 1,000.00" counts as an item, although the rules say "purchased items" and no count is printed. The ".00" amounts follow the stated rule. | v2 |
| 19 | train-0758 | delivery | S1 | 22500 · 24000 · null · 1 | ok | | "TOTAL SALES 22,500". "T-CASH DISC 4,500" and "T-CASH 18,000" are payment lines. | |
| 20 | train-0786 | delivery | S1 | 32000 · null · null · 1 | ok | | | |
| 21 | train-0790 | delivery | S1 | 30000 · null · null · 1 | ok | | | |
| 22 | validation-0052 | delivery | S1 | 56000 · 56000 · null · 3 | ok | | | |
| 23 | test-0054 | delivery | S2 | 10000 · 10000 · null · 1 | ok | | | |
| 24 | test-0062 | delivery | S2 | 48000 · null · null · 2 | ok | | | |
| 25 | train-0128 | evidence | S2 | 33000 · 33000 · null · 1 | ok | | Each label and its amount sit on separate lines ("GRAND TOTAL" / "33,000"), but the pairing is still clear. | |
| 26 | train-0186 | evidence | S2 | 13500 · null · null · 1 | ok | | | |
| 27 | train-0265 | evidence | S2 | 75000 · null · null · 3 | ok | | | |
| 28 | train-0305 | rehearsal | S2 | 42000 · 42000 · null · 2 | ok | | | |
| 29 | train-0390 | pilot | S2 | 42000 · 42000 · null · 1 | text-ambiguous | subtotal | The subtotal is a bare "42,000" with no label. The total's amount shares a line with "ITEMS: 1", and "Total." comes a line later. | v2 |
| 30 | train-0412 | pilot | S2 | 13000 · null · null · 1 | ok | | | |
| 31 | train-0432 | rehearsal | S2 | 55000 · null · null · 2 | ok | | | |
| 32 | train-0440 | delivery | S2 | 24000 · null · null · 2 | ok | | The plastic bag at 0 counts, and the receipt prints "Total Item: 2". | |
| 33 | train-0631 | pilot | S2 | 58500 · null · null · 6 | ok | | "SUPER CHEESE" on two lines counts twice, and the bag counts. The receipt prints "6.00 ITEMS". | |
| 34 | train-0717 | evidence | S2 | 55000 · null · null · 3 | rule-gap | items | "1 Extra Jelly Lychee 5,000" counts as its own item. In test-0078, train-0328 and train-0380 the same line is part of the drink. | E3 |
| 35 | train-0759 | rehearsal | S2 | 64000 · null · null · 6 | text-ambiguous | total | "TOTAL 64000" and "CASH 100000" share a row_id, so the text reads "6 CASH TOTAL 64000 100000". | E4 |
| 36 | test-0043 | delivery | S3 | 16500000 · 16500000 · 1500000 · 1 | ok | | The ".00" amounts follow the stated rule. "Total Tax Included", above "Total:", is the tax line. | |
| 37 | train-0075 | evidence | S3 | 337230 · 291975 · 30657 · 8 | ok | | The package contents under the first PAKET line are part of it. | |
| 38 | train-0190 | delivery | S3 | 351000 · 300000 · 30000 · 3 | ok | | | |
| 39 | train-0266 | evidence | S3 | 1241790 · 1065000 · 112890 · 12 | ok | | The total line reads "DU 1,241,790", which equals SUBTOTAL + SERVICE CHARGE + Tax. The FOOD, BEVERAGES and OTHERS group sums come before SUBTOTAL. | |
| 40 | train-0302 | pilot | S3 | 353320 · 365000 · 32120 · 2 | ok | | The subtotal is printed before the 20% discount. | |
| 41 | train-0400 | delivery | S3 | 110000 · 100003 · 10000 · 4 | text-ambiguous | items | Three item lines share one row_id: "1 1 1 NASI (TW) MIE (TW)NASI GORENG GORENG GORENG AYAM 31,819 27,273 27,273". | E4 |
| 42 | train-0425 | pilot | S3 | 135000 · 122730 · 12273 · 4 | text-ambiguous | tax | The row_ids are shifted: "12,273" stands alone, then "VAT Before 10% Rounding 135,003 -3". The VAT amount can be found only by arithmetic. | E4 |
| 43 | train-0472 | evidence | S3 | 3100 · 3100 · 282 · 2 | rule-gap | subtotal, items | The subtotal is "HARGA JUAL 3,100", which includes the PPN of 282, against "the amount before tax". The bag line "PLASTIK SDG 1 1 1" counts, with no printed count. | v2 |
| 44 | train-0556 | evidence | S3 | 9900 · 13801 · 1236 · 2 | rule-gap | subtotal | The subtotal is the amount on "Total Item 2 13,801": a gross figure that includes the PPN and comes before the discounts. It is not labelled a subtotal. | v2 |
| 45 | train-0751 | delivery | S3 | 160000 · 145458 · 14546 · 5 | ok | | The subtotal is the pre-VAT amount on the "ITEMS :6" line. | |
| 46 | train-0037 | rehearsal | S4 | 13500 · 12273 · 1227 · 1 | ok | | "TL 13,500" is the total. | |
| 47 | train-0202 | evidence | S4 | 51000 · 46364 · 4636 · 1 | ok | | | |
| 48 | train-0307 | pilot | S4 | 11500 · 10455 · 1046 · 1 | ok | | The price is printed on the line above the item name. | |
| 49 | train-0431 | rehearsal | S4 | 44000 · 40000 · 4000 · 1 | ok | | In "PB 1 10% 4,000" the amount is 4,000; the rate is not part of it. | |
| 50 | train-0616 | evidence | S4 | 56800 · 51636 · 5164 · 2 | ok | | The subtotal is "DPP 51,636", the tax base. | |

After the fixes, all 13 receipts that are not ok are resolved: 9 by the exclusion list and 4 by the v2 rule text.

## 4. Beyond the sample

### 4.1 The 19 flagged receipts

The data build flagged these 19 as suspect. All were read in full.

| Case | Set | Flag | Label | Verdict | Why |
|---|---|---|---|---|---|
| test-0099 | delivery | multi_value_disagree:tax | tax 6818 | ok | "PAJAK PPN 10 % 6,818" comes before "PAJAK 0% 0", and the first wins. |
| train-0053 | delivery | menu_entry_without_name | items 2 | ok | An unnamed "3 12.000 36.000" item and a shopping bag. "PAID RP 36.000" is the total. |
| train-0135 | rehearsal | multi_value_disagree:total | total 55834 | ok | "TOTAL: 55,834" comes before "BALANCE DUE: 55,800", and the first wins. |
| train-0191 | evidence | multi_value_disagree:tax, multi_number:tax | tax 30273200 | label-wrong | The first tax annotation joins the two DPP (tax base) amounts "30.273" and "200". The tax printed is "PPN= 3.027", so 3027. |
| train-0333 | delivery | menu_entry_without_name | subtotal 54000 | label-wrong | The item line "TMP KpK 54,000" (3 × 18,000) is annotated as the subtotal. No subtotal is printed. |
| train-0421 | pilot | multi_number:tax | tax 104818 | label-wrong | In "Pb1 10% 4,818" the rate was annotated as part of the amount. Tax is 4818. |
| train-0435 | delivery | multi_number:tax | tax 1107000 | label-wrong | "Pb1 10% 7,000", so 7000. |
| train-0490 | rehearsal | total_missing_total_word | total null | label-wrong | "Total Rp. 250.000" is annotated as the item price. Total is 250000. |
| train-0495 | pilot | multi_value_disagree:tax | tax 10000 | label-wrong | The first tax annotation is the rate "10.000%". The amount is "AMOUNT 546". |
| train-0500 | delivery | total_missing_total_word | total null | label-wrong | "GRAND TOTAL 70,000" is annotated as `total_etc`. Total is 70000. |
| train-0603 | rehearsal | multi_value_disagree:subtotal | subtotal 73638 | ok | A bare 73,638 on the "ITEMS :4" line comes before "Before Rounding 81,002", and the first wins. |
| train-0670 | delivery | total_missing_total_word | total null | label-wrong | "Total Rp. 200.000" is annotated as the item price. Total is 200000. |
| train-0676 | delivery | menu_entry_without_name | subtotal 22000 | label-wrong | "GT 22,000" under "1 22,000" is the item line, the same layout as train-0333, but is annotated as a subtotal. |
| train-0692 | evidence | multi_value_disagree:subtotal | subtotal 18182 | ok | Two "Subtotal" lines around a discount, and the first wins. It is the same receipt as pilot train-0147 (§5.4). |
| train-0699 | pilot | menu_entry_without_name | items 3, total null | ok | The items are named only by PLU codes, and only "CASH 85000" is printed. |
| train-0714 | rehearsal | menu_entry_without_name | items 1 | ok | The item is named only by the code "-YJP11037". |
| train-0744 | delivery | multi_value_disagree:tax | tax 98178 | ok by rule | The digits rule turns "PAJAK PPN 10 % 9,817.8" into 98178, the same digits as the subtotal. A model may well "correct" it. |
| train-0778 | rehearsal | multi_value_disagree:tax | tax 49541 | ok by rule | Same pattern: "4,954.1" becomes 49541. |
| validation-0078 | delivery | multi_number:subtotal, multi_number:tax | 154000 · 16170 | ok | Spaces separate the thousands ("154 000", "16 170"). |

That is 9 label-wrong out of 19: 2 in pilot, 1 in rehearsal, 1 in evidence and 5 in delivery.

### 4.2 Wrong labels found by screens

I screened all 854 receipts three ways: lines naming a total, subtotal or tax that are annotated under another category; checks of subtotal + tax + service − discount against the total; and printed item counts against the menu entries.

| Case | Set | Label | Why |
|---|---|---|---|
| train-0290 | rehearsal | tax null | "Pajak Resto 9,818" is annotated as a discount. Tax is 9818. |
| train-0592 | evidence | tax null | "PB 1 4,955" is annotated as a discount. Tax is 4955, and 41.818 + 7.727 + 4.955 equals the total of 54.500. |
| train-0271 | delivery | subtotal 36000 | "3 Total 36.000" is annotated as the subtotal and "CASH 36.000" as the total. No subtotal is printed apart from the total. |
| train-0503 | delivery | items 3 | "MDSE ST RP36362", a merchandise subtotal, is annotated as a menu item. Items is 2. |
| validation-0021 | delivery | items 4 | "1 Discount BCA 15% -Rp 34,363" is annotated as a menu item. Items is 3. |

The other hits have an explanation in the receipt: tax-inclusive prices, discounts that close the arithmetic, a tax printed as 0, or a "TOTAL" before tax followed by a "GRAND TOTAL". The few unclear ones are in E3 and E5 (§5.2).

### 4.3 Conventions CORD labels both ways

**The lone "Sub Total".** In 15 receipts the only line resembling a total is a "Sub Total". Thirteen of them share one layout, in which "Sub Total" and its amount are followed only by "Discount (0%)", "Tunai" (cash) and "Kembalian" (change). The other two are train-0749 ("Sub total 100,000", then "Tunai") and train-0583, whose text ends at "SUB-TOTAL 72,500.00". CORD labels them both ways:

- 8 receipts label it total = X and subtotal = null: test-0071, train-0117, 0250, 0541, 0553, 0583, 0749 and 0770.
- 7 receipts label it total = null and subtotal = X: test-0009, train-0035, 0052, 0460, 0572, 0683 and validation-0029.
- train-0541 (rehearsal) and validation-0029 (delivery) are the same product at the same price, and their labels are swapped.
- No single rule can match both groups. The v2 draft picks total = X, the reading an expense app needs, and the exclusion list drops the other 7.

**Extras and upgrades.**

- "1 Extra Jelly Lychee 5.000" is printed the same way on several receipts. It is an item in train-0211, 0308, 0717 and 0739, but part of the drink in test-0078, train-0328 and train-0380.
- The "UP" upgrade lines are items in train-0555 but part of the combo in train-0271.
- "ADD CHICKEN BOX 909" is part of the item in test-0007 and validation-0091, but "CHANGE TO CHICKEN BOX 909" is an item in train-0398.
- "Topping Milo Sparkle" is an item in test-0060.
- Priced add-ons printed without their own quantity, such as PEARL, Bubbles, GRASS JELLY or ADD CHICKEN BOX, are part of the item in all 14 receipts that have them.

**Bags.** "*Plastik Kcl 0" is an item in train-0493 and 0568 but part of a pastry in validation-0053.

### 4.4 Conventions CORD keeps but the rules do not state

Forge can learn these from the tuning labels; the plain prompts never see them.

- **Bags and charges count as items.** A plastic bag, take-away charge or similar line is an item in 78 receipts. Only 6 keep one inside an item: four are the carrier of a boxed set ("Bag-S", "PL. Bag Box 6"), one is a cake's bag (train-0722), and one is validation-0053. The v1 rule says "purchased items".
- **The subtotal goes by other names.** It is the first amount that sums the items, whatever the receipt calls it:
  - "ITEMS TOTAL" is the subtotal even when a discount and a line named "SUBTOTAL" follow (train-0284, 0680 and 0750);
  - "HARGA JUAL" is the subtotal although it includes the PPN (4 receipts);
  - so are "DPP", "Net Total", the bare amount on an "ITEMS : n" line, and a "TOTAL" followed by a "GRAND TOTAL".

  The v1 parenthetical "(the amount before tax and service)" points a careful reader away from some of these.
- **The tax is the amount, not the rate or the base.** CORD keeps to this except in the four receipts where the rate was annotated as part of the value (§4.1).

## 5. Rule fixes

### 5.1 Draft prompt rules (receipts-v2)

This draft makes CORD's consistent habits explicit, so every arm sees them. It picks one side of each convention CORD splits. It changes no label: receipts whose labels contradict it are in the exclusion list.

```text
Return one JSON object with exactly these four keys: total, subtotal, tax, item_count.
- total: the total amount printed on the receipt, including any tax and service, on a line such as TOTAL, GRAND TOTAL, TL, AMOUNT DUE, TOTAL SALES, TOTAL BELANJA or PAID. A line that names a tender (cash, card, e-wallet, voucher), a payment discount or change is never the total. If no line shows a total and nothing after the subtotal changes the amount (only tender and change lines, or a 0% discount), the subtotal is the total and subtotal is null.
- subtotal: the first amount printed after the items that sums them, apart from the total, whatever the receipt calls it: SUBTOTAL, SUB TOTAL, ITEMS TOTAL, HARGA JUAL, DPP, NET TOTAL, a TOTAL line that a later GRAND TOTAL follows, or a bare amount under the items. A sum for one group of items (FOOD, BEVERAGE) is not the subtotal.
- tax: the tax amount printed on the receipt (TAX, VAT, PPN, PB1, PAJAK), not its rate, not its base (DPP) and not a service charge.
- item_count: the number of items listed on the receipt. Count each listed item once, whatever its quantity; the same item on two separate item lines counts twice. Bags and charges listed as items (plastic bag, take-away charge) count, even at price 0. Lines that belong to an item (options, toppings, extras, notes, package contents) are part of that item. Discount lines are not items. A void line that cancels an item is not an item; the cancelled item still counts.
- Amounts are integers: take the amount itself, without its label or rate, and delete every character that is not a digit 0-9. For example "75,000" is 75000, "Rp 12.500" is 12500, "-2.727" is 2727, "226,500.00" is 22650000 and "PB1 10% 4,818" is 4818.
- An amount the receipt does not print is null; never compute it from other amounts. A printed value without any digit, such as "-", is null.
- If the receipt prints the same field more than once, use the first (topmost) one.
```

- **What it settles.**
  - The lone "Sub Total" layout now has one answer.
  - Bags and charges, and subtotals printed under other names, are stated outright.
  - The rate is kept out of the tax amount.
  - Tender and voucher lines are kept out of the total.
  - Extras follow the "part of the item" side, which matches CORD for priced toppings without their own quantity.
- **Cost.** It is 873 characters longer than v1, about 220 more input tokens per plain call.
- **Re-check these receipts by hand after the change.** train-0439, whose total line reads "Payment 1126510"; train-0558, whose post-tax amount is printed as a second "SUBTOTAL"; and train-0135, where "TOTAL" comes before "BALANCE DUE". All three agree with the draft as I read it, but they sit on its edges.

### 5.2 Exclusion list (42 receipts)

| Group | Reason | Pilot | Rehearsal | Evidence | Delivery |
|---|---|---|---|---|---|
| E1 | the label is wrong against the text (§4.1, §4.2, sample #5) | 2 | 2 | 2 | 8 |
| E2 | lone "Sub Total" labelled total = null (§4.3) | 1 | 2 | 1 | 3 |
| E3 | item count contradicts the v2 item rule: extras counted as items, a bag folded into an item, or a printed count that disagrees with an unclear label | 3 | 0 | 2 | 6 |
| E4 | text garbled by a row_id spanning several printed lines (§5.3, §6); drop these from the list once §5.3 lands and they read correctly | 2 | 2 | 1 | 3 |
| E5 | other: train-0507, a fuel receipt whose only amount line is "Jml Rupiah Rp. 217,685" while total is null; train-0593, where "TL 46,000" is labelled the subtotal above "TOTAL 46,000", though TL is the total elsewhere | 1 | 0 | 1 | 0 |
| | **Total** | 9 | 6 | 7 | 20 |

The 7 evidence receipts are 6 tuning cases and 1 held-out case. The 6 rehearsal receipts are 3 and 3.

```text
E1 cord-train-0191 cord-train-0271 cord-train-0290 cord-train-0333 cord-train-0421 cord-train-0435 cord-train-0490
   cord-train-0495 cord-train-0500 cord-train-0503 cord-train-0592 cord-train-0670 cord-train-0676 cord-validation-0021
E2 cord-test-0009 cord-train-0035 cord-train-0052 cord-train-0460 cord-train-0572 cord-train-0683 cord-validation-0029
E3 cord-test-0060 cord-train-0211 cord-train-0308 cord-train-0357 cord-train-0398 cord-train-0555 cord-train-0607
   cord-train-0717 cord-train-0722 cord-train-0739 cord-validation-0053
E4 cord-train-0293 cord-train-0400 cord-train-0425 cord-train-0516 cord-train-0568 cord-train-0596 cord-train-0759
   cord-train-0795
E5 cord-train-0507 cord-train-0593
```

A few E3 cases need a note:

- **train-0607:** "Milo 1 0" is labelled part of "Pepper Steak", but the receipt prints "ITEMS: 2".
- **train-0722:** the bag and knife of a cake box are labelled part of the cake, while free candles count as an item, and the lines are garbled.
- **train-0308:** it counts both "Extra Jelly Lychee" and "Extra Ice Cream" as items.

### 5.3 Text rebuild: split rows that span printed lines

The rebuild trusts `row_id`. Usually that is right, and it even absorbs tilted photos (§6). In 16 receipts, though, one `row_id` covers words that sit on top of each other.

- **The proposed rule.** Within one row_id, two words that overlap horizontally by more than 30% of the narrower word, and whose centres are more than half a word height apart vertically, sit on different printed lines. In that case, cluster the row's words by centre y, starting a new line whenever the next centre lies more than half a word height below the current line's mean. Then order lines and words as now.
- **Which receipts it changes.** test-0011, test-0045, test-0072, test-0096, train-0293, 0382, 0390, 0400, 0425, 0516, 0568, 0596, 0614, 0759, 0795 and validation-0047.
- **Which receipts it matters for.** In 8 of them it makes a line that carries a label readable again; those 8 are E4. In the other 8 the merge is harmless, either because equal amounts were merged or because an option's "0" sits on the item line.
- **What to do after.** Hand-check the 16 rebuilt texts, then drop E4 from the exclusion list.

### 5.4 Near-duplicates the two keys missed

The two copies differ by one line, a wrap, an item or option name, or the cash tendered. That changes both the word multiset and the digit sequence, so neither duplicate key fires. Every pair below was confirmed by reading both texts.

| Pair | Sets | What differs | Labels |
|---|---|---|---|
| train-0147 / train-0692 | pilot / evidence | one copy has the "*Gula Murni 100%" option line | same |
| train-0056 / validation-0065 | evidence / delivery | one copy has "DPP 49.636" | same |
| train-0066 / train-0691 | rehearsal / delivery | one copy has "DPP 52.636" | same |
| train-0036 / train-0362 | evidence / delivery | the wrap, and the card and change lines | same |
| train-0004 / train-0795 | evidence / delivery | the item name; same amounts, cash and change | same |
| train-0276 / train-0732 | pilot / evidence | the line breaks, and "TAKEOUT(1):" | same |
| train-0330 / train-0348 | evidence / pilot | one copy has "Sales included PB1" | same |
| train-0159 / train-0593 | evidence / pilot | one copy has "TL 46,000" | subtotal null vs 46000 |
| train-0474 / train-0753 | rehearsal / delivery | one copy has "Incl. 10% 2,955" | tax 2955 vs null |
| train-0390 / validation-0047 | pilot / delivery | the option ("Pedas" or "Sedang"); same amounts and payment | same |
| train-0541 / validation-0029 | rehearsal / delivery | cash tendered (30000 or 50000); same product and price | total and subtotal swapped |

- **What this means.** Six pairs link a builder-visible set to delivery, so "never overlap" is not yet true at the receipt level.
- **How they were found.** Nine pairs came from three tests together: token Jaccard of at least 0.7, Jaccard of the four-digit-or-longer amounts of at least 0.8, and identical tender and change amounts. The other two, train-0036 / train-0362 and train-0541 / validation-0029, came from looser screens.
- **What to add.** A third key built on these tests, with every new merge reviewed by hand. The three pairs whose labels differ should go to `label_conflict`.

## 6. Text rebuild ordering check (10 receipts)

**Method.** For each receipt I placed every word at its quad position, as a coarse character plot, and compared the plot with the rebuilt rows.

**Choice.** Four receipts are random (`random.Random(20261004)`). The other six had the strongest diagnostics among all 854: row_id order differing from vertical order, a strongly tilted photo, or words stacked inside one row_id.

| # | Case | Set | Why chosen | Result |
|---|---|---|---|---|
| 1 | test-0056 | delivery | random | Correct. |
| 2 | train-0090 | evidence | random | Correct. The item name prints on its own line above "1 28,000 28,000", as on the paper. |
| 3 | train-0245 | rehearsal | random | Correct. |
| 4 | validation-0010 | delivery | random | Correct. Slightly tilted lines are joined by row_id ("2 Serbu 1 40.000"). |
| 5 | train-0383 | delivery | most row_id inversions (4) | Correct. "1 Viet Milk Coffee 18,000" and "CASH 19.000" are joined although the photo is tilted and the row_ids run out of order. |
| 6 | train-0062 | evidence | row_id inversions (3) | Correct. The film-title row has the highest row_id but prints first, and "Total (Qty= 60 3.600.000" is joined right. |
| 7 | train-0487 | evidence | row_id inversions (2) | Correct. Prices printed a line below their names stay below them. |
| 8 | train-0308 | evidence | strongly tilted photo | Correct. Grouping by row_id absorbs the tilt. |
| 9 | train-0293 | pilot | words stacked in one row_id | **Wrong.** Four item lines become "1 1 1 1 Nasi Nila Ayam Ayam. Kremes Penyet Goreng Garang + Gila Asem Nasi 45.000 47.000 46.000". The subtotal, service and tax lines become "Pajak Servi Subtotal Rest Charge 10% 5% 404.000 20.200 42.420". |
| 10 | train-0516 | delivery | words stacked in one row_id | **Wrong.** Three item lines become "1 1 1 Ocha Ocha JapaneseBeef Hot Cold Hamburg 15,000 43,000 15,000", taking "Hamburg" from the line above. |

**Findings.**

- **Eight of ten are correct.** The data report's claim that the text "reads correctly even where row_id order is wrong" holds (cases 5–7).
- **Both failures share one cause:** a CORD row_id that spans several printed lines. The same fault garbles three sampled receipts (train-0400, 0425 and 0759). §5.3 lists all 16 affected receipts and a fix.
- **Split lines read correctly.** Where a printed line is split across two row_ids, a label and its amount land on consecutive lines, such as "GRAND TOTAL" then "33,000". Occasionally the amount comes first, as in "454,462" then "TOTAL" in train-0680. Such text still reads correctly.

## 7. Reproducing the sample

Run this from the repository root with the CORD cache in place. It reads the committed cases and the cached raw annotations and changes nothing. Set `PYTHONDONTWRITEBYTECODE=1` to avoid writing bytecode next to the script.

```python
import importlib.util, json, random, sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("fbd", "scripts/fetch_bench_data.py")
fbd = importlib.util.module_from_spec(spec); sys.modules["fbd"] = fbd; spec.loader.exec_module(fbd)
rows, _ = fbd.read_cache(fbd.default_cache())
raw = {f"cord-{s}-{r.index:04d}": json.loads(r.ground_truth) for s, rs in rows.items() for r in rs}
cases = {}
for name in ("pilot", "rehearsal", "evidence", "delivery"):
    for line in Path(f"examples/receipts/cases/{name}.jsonl").read_text(encoding="utf-8").splitlines():
        case = json.loads(line)
        cases[case["case_id"]] = case

def stratum(cid):
    e, gt = cases[cid]["expected"], raw[cid]
    flags = fbd.receipt_flags(gt, fbd.receipt_rows(gt["valid_line"]))
    cats = {line["category"] for line in gt["valid_line"]}
    hard = "quantity_over_1" in flags or bool(cats & {"menu.discountprice", "sub_total.discount_price"})
    if "void_menu" in flags: return "V"
    if e["total"] is None: return "T0"
    null = e["subtotal"] is None or e["tax"] is None
    return "S1" if null and hard else "S2" if null else "S3" if hard else "S4"

strata = {}
for cid in sorted(cases):
    strata.setdefault(stratum(cid), []).append(cid)
rng = random.Random(20261003)
sample = [cid for s, k in (("V", 1), ("T0", 6), ("S1", 15), ("S2", 13), ("S3", 10), ("S4", 5))
          for cid in sorted(rng.sample(strata[s], k))]
ordering_random = sorted(random.Random(20261004).sample(sorted(cases), 4))
```

This regenerates the 50 receipts of §3, in the same order, and the four random receipts of §6.
