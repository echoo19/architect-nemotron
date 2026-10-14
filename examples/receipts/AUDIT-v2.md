# Receipt data v2 hand-check

Written by hand on 2026-10-03 against `receipts-v2` at commit 067edad (rules sha256 `b552ab6033626d0e5f1792a560932b764b749a7dc17f7f2cbe928653a8f52480`, dataset revision `7f0115a4b758a71d6473b8d085751692da2fef98`). It is the second hand-check of the receipt data, done independently of AUDIT.md and of the 40-receipt check recorded there. No model was called, and no derived file or script was changed. Case ids below drop the `cord-` prefix.

Re-checked on 2026-10-04 against the same commit. All 61 receipts were read again, with the same verdicts. Fresh code reproduced the sample, the comparison with v1, the labels and texts, the pairing screen (101 placements in 90 receipts), the set and group checks, and the set shifts in section 6. Two statements were wrong and are corrected here: train-0297 has seven wrapped names where this file said five, and section 5 undercounted the repeat purchases and missed a family that links a tuning case with delivery cases.

## Verdict

**The trigger (more than 5% of hand-checked receipts not ok) is not met.** All 61 receipts I read are ok: each label is what a careful bookkeeper would take from the text under the v2 rules.

| Group | Receipts not ok | Wilson 95% interval |
|---|---|---|
| Random sample, oversampling hard receipts | 0 of 50 | 0 to 7.1% |
| Every receipt whose text changed from v1 | 0 of 11 | 0 to 25.9% |
| Both | 0 of 61 | 0 to 5.9% |
| Labels in both | 0 of 244 | 0 to 1.6% |

- **A sample this size cannot rule out a rate a little above 5%.** The screens over all 798 cases carry more weight here. They found 6 receipts that are clearly not ok and 3 borderline ones (section 4.2), none of them in the sample. That is 0.8% to 1.1% of the data. At a true rate near 1%, a sample of 61 comes back clean about half the time (0.99 to the power 61 is 0.54).
- **Every label of the 11 changed receipts can be read from the text.** The row split repaired train-0400, train-0516, train-0596 and train-0759, four of the receipts the first audit listed as garbled (E4). Eight of the 11 still put a word on a neighbouring line (a payment amount, an option's price, the item count or part of a name), and no label depends on those words.
- **No case and no copy of one purchase crosses sets or floor halves** (section 5). Repeat purchases do: receipts of the same order at the same prices, with the same labels and a different payment. RULES.md keeps those apart on purpose. One such pair sits on both sides of the evidence floor.
- **Before the pilot freeze on 10/9** I would add the 6 clear cases to the exclusion list and decide on the 3 borderline ones (section 6). Excluding them moves a few receipts between sets and flips many evidence halves, so it should happen once, before any build or pilot run reads the data.

## 1. Method

**Population.** The 798 v2 cases: pilot 150, rehearsal 100, evidence 200 and delivery 348.

**What changed from v1.** Against the v1 case files at commit 3d6f304, 11 cases have a new text and none has a new label. All 11 carry the `row_split` flag. No case was added. 56 v1 cases were dropped, 69 moved to another set and 87 changed half within their floor; those moves leave the text and labels as they were.

**Strata.** Each receipt goes to the first stratum that fits.

| Stratum | Definition | Cases | Read |
|---|---|---|---|
| C | the text changed from v1 (`row_split`) | 11 | 11 |
| V | has a void line | 1 | 1 |
| T0 | total is null | 11 | 6 |
| NH | subtotal or tax null, with a quantity above 1, a sub-item or a discount line | 188 | 18 |
| N | subtotal or tax null, with none of those | 232 | 10 |
| H | subtotal and tax printed, with a quantity above 1, a sub-item or a discount line | 174 | 11 |
| R | none of the above | 181 | 4 |

- A quantity above 1, a sub-item and a void line are the build's `quantity_over_1`, `sub_items` and `void_menu` flags. A discount line is an annotation in `menu.discountprice` or `sub_total.discount_price`.
- The draw used `random.Random(20261006).sample` over each stratum's sorted case ids, in the order V, T0, NH, N, H, R. The first audit used seeds 20261003 and 20261004, and the v2 build's own check used 20261005. Section 7 has the code.
- The 50 drawn receipts oversample the hard ones:

  | Receipts with | Drawn 50 | All 798 |
  |---|---|---|
  | a null field | 70% | 55% |
  | a quantity above 1, a sub-item, a void or a discount line | 64% | 47% |
  | a quantity above 1 | 40% | 28% |
  | sub-items | 20% | 16% |
  | a discount line | 14% | 12% |
  | a void line | 1 | 1 |

- By set the 50 are 24 delivery, 13 pilot, 11 evidence and 2 rehearsal receipts. The 11 changed ones are 8 delivery receipts and one each from the other sets. Two drawn receipts, train-0075 and train-0279, were also in the first audit's sample.

**Reading.** For each receipt I read the rebuilt text first and wrote down what a careful bookkeeper applying RULES.md v2 would answer. Then I compared that answer with the four labels and with the raw `gt_parse` and `valid_line` annotations. For the 11 changed receipts I also placed every word at its quad position in a character plot and compared the plot with the text.

**Verdicts,** as in AUDIT.md. A receipt takes its worst verdict.

- **ok:** all four labels are what a careful reader following the rules would give.
- **label-wrong:** the text and the rules give a different value, so the annotation is wrong.
- **rule-gap:** the text is clear, but the rules do not settle the point, or settle it against the label.
- **text-ambiguous:** the rebuilt text does not let a careful reader tell which value is meant.

A note that starts with "Edge" marks an ok receipt where the reader needs a judgment that the rules support.

**Mechanical checks.** Labels derived again from `gt_parse` by fresh code written from RULES.md match all 798 cases. A plain rebuild (one line per `row_id`, lines ordered by mean centre y, words by centre x) matches all 787 texts without a split row.

## 2. Results

| Verdict | Random sample | Changed | Labels (of 244) |
|---|---|---|---|
| ok | 50 | 11 | 244 |
| label-wrong | 0 | 0 | 0 |
| rule-gap | 0 | 0 | 0 |
| text-ambiguous | 0 | 0 | 0 |

- The intervals are Wilson 95%. Every stratum came back clean, so the estimate weighted back to all 798 cases is 0 as well.
- 10 of the 61 receipts carry an Edge note.

**Reading the trigger.** The rate of receipts that are not ok is 0%, so the trigger is not met. The upper bounds, 7.1% for the random sample and 5.9% with the changed receipts, pass 5% only because the samples are small. The screens in section 4 put the rate near 1%. For comparison, the first audit found 13 of 50 not ok under the v1 rules, and the v2 rule text and exclusion list resolve all 13 (AUDIT.md, section 3).

## 3. Per-case verdicts

All 61 are ok. Labels are total · subtotal · tax · items.

| # | Case | Set | Stratum | Labels | What the text shows |
|---|---|---|---|---|---|
| 1 | train-0279 | evidence, held-out | V | 15000 · null · null · 2 | "SOP AYM BNG -7,000" voids the soup above it; the voided item still counts. Also in the first audit's sample. |
| 2 | test-0057 | delivery | T0 | null · null · null · 2 | Two items, then "CASH 42,000". A tender line is never the total. |
| 3 | train-0113 | pilot | T0 | null · null · null · 1 | One item, then "CASH 22,000". |
| 4 | train-0122 | rehearsal, tuning | T0 | null · 24800 · null · 2 | Edge: "SPECIAL DISC -300" follows "SUBTOTAL 24,800", so the subtotal does not stand in for the total. "TOTAL QTY 2 ITEM" is a count. |
| 5 | train-0524 | delivery | T0 | null · null · null · 1 | The price line prints above the item name, then "CASH 28.000". |
| 6 | train-0695 | evidence, held-out | T0 | null · null · null · 1 | "ICED TT 20,000", then "CASH 20,000". |
| 7 | train-0699 | pilot | T0 | null · null · null · 3 | Items named only by PLU codes, then "CASH 85000". |
| 8 | test-0004 | delivery | NH | 174600 · 194000 · null · 3 | "SUB_TOTAL 194,000", "DISCOUNT 19,400", "TOTAL 174,600". |
| 9 | test-0006 | delivery | NH | 46000 · null · null · 3 | |
| 10 | test-0035 | delivery | NH | 54000 · null · null · 4 | |
| 11 | test-0082 | delivery | NH | 385000 · 385000 · null · 3 | "VISA MASTER 385,000" is the tender. |
| 12 | train-0010 | pilot | NH | 25000 · 25000 · null · 1 | "+Hot" and "+M" are options. |
| 13 | train-0027 | delivery | NH | 30000 · null · null · 1 | "TL Rp 30,000". "CUP MEGA" and "BBQ" belong to the item. |
| 14 | train-0156 | pilot | NH | 27300 · 27300 · null · 2 | The bag at 0 counts; "4.00xITEMs" is the sum of quantities. |
| 15 | train-0169 | delivery | NH | 23000 · 23000 · null · 1 | "*CARAMEL DIP*" with "1 0 0" is an option. |
| 16 | train-0170 | delivery | NH | 34000 · 34000 · null · 1 | |
| 17 | train-0184 | delivery | NH | 42000 · null · null · 3 | |
| 18 | train-0361 | pilot | NH | 282000 · 243000 · null · 5 | Edge: "Others 25.637" is 10% of subtotal plus service, but no line calls it a tax. Two item names wrap onto a second line. |
| 19 | train-0443 | delivery | NH | 45000 · 45000 · null · 1 | The subtotal is the amount on "ITEMS: 1"; "1 Tidak Pedas 0" is an option. |
| 20 | train-0493 | delivery | NH | 41000 · null · null · 3 | "1 *PlastiK Kcl 0" counts; the "5" in "5 Total 41.000" is the quantity sum. |
| 21 | train-0567 | evidence, held-out | NH | 100100 · 100100 · null · 12 | Edge: five lines at 0 (two food containers, a spoon, a fork and a bag) are listed as items, so they count. |
| 22 | train-0590 | evidence, held-out | NH | 55000 · null · null · 2 | "1 BREAD BOX M 4,000" is a charge listed as an item. |
| 23 | train-0634 | pilot | NH | 42000 · 42000 · null · 1 | |
| 24 | train-0748 | pilot | NH | 127678 · 127678 · null · 12 | Edge: "S/QUEEN MONTES 50" on two lines counts twice, so 12, although the receipt prints "ITEMS PURCHASED: 11". "Total Hero" is the total. |
| 25 | validation-0079 | delivery | NH | 72000 · 72000 · null · 7 | |
| 26 | test-0077 | delivery | N | 17000 · 17000 · null · 1 | |
| 27 | train-0025 | pilot | N | 14000 · null · null · 1 | "TL 14,000". |
| 28 | train-0034 | evidence, tuning | N | 30000 · null · null · 1 | "Total Belanja 30.000". |
| 29 | train-0060 | pilot | N | 161000 · null · null · 5 | |
| 30 | train-0061 | evidence, tuning | N | 17000 · 17000 · null · 1 | A repeat purchase of train-0225 (section 5). |
| 31 | train-0092 | pilot | N | 25000 · null · null · 2 | |
| 32 | train-0393 | evidence, held-out | N | 25000 · null · null · 2 | Edge: "25.000" prints on the line above "Total.". Read as the total's amount, or as a bare subtotal with no total printed, the labels come out the same. |
| 33 | train-0423 | pilot | N | 22000 · null · null · 2 | |
| 34 | train-0557 | delivery | N | 190000 · null · null · 1 | |
| 35 | train-0705 | pilot | N | 12000 · 12000 · null · 1 | |
| 36 | test-0029 | delivery | H | 145900 · 132636 · 13264 · 6 | Edge: "GRAND TOTAL" has its amount on the next line, "~ BCA EDC [145,900] 145,900", and both numbers agree. The two udon lines at 0 under "BCA PROMO" are its contents. |
| 37 | test-0053 | delivery | H | 123585 · 107000 · 11235 · 3 | "Pajak Rest 10% 11.235". |
| 38 | test-0059 | delivery | H | 680500 · 618490 · 61849 · 9 | "Total Item 9" agrees. |
| 39 | train-0011 | evidence, tuning | H | 250107 · 214500 · 22737 · 5 | |
| 40 | train-0075 | evidence, held-out | H | 337230 · 291975 · 30657 · 8 | Also in the first audit's sample. |
| 41 | train-0185 | pilot | H | 58000 · 52727 · 5273 · 1 | Edge: six toppings at 0, each with a quantity, belong to the frozen yogurt. |
| 42 | train-0297 | evidence, tuning | H | 300300 · 273000 · 0 · 11 | "PAJAK PPN 10 % 0" prints a tax of 0. Seven names wrap; "Total Menu 13" is the quantity sum. |
| 43 | train-0444 | delivery | H | 52800 · 48000 · 4800 · 1 | |
| 44 | train-0551 | delivery | H | 293370 · 254000 · 26670 · 3 | |
| 45 | train-0653 | delivery | H | 516780 · 435000 · 46980 · 5 | The BEVERAGES and PASTRY subtotals print after "SUBTOTAL 435,000"; "Total item 5" agrees. |
| 46 | validation-0088 | delivery | H | 170000 · 154545 · 15455 · 1 | Ten package lines without prices belong to "PAKET BER4 UP3". |
| 47 | test-0066 | delivery | R | 40000 · 36364 · 3636 · 2 | "PD1 10% 10.000Z" holds rates; the tax is "AMOUNT 3,636". |
| 48 | train-0098 | evidence, tuning | R | 25900 · 25900 · 2355 · 3 | "HARGA JUAL" is the subtotal. |
| 49 | train-0187 | rehearsal, held-out | R | 15500 · 14091 · 1409 · 1 | |
| 50 | train-0363 | delivery | R | 58000 · 52727 · 5273 · 1 | The same "GRAND TOTAL" layout as test-0029. |
| 51 | test-0011 | delivery | C | 230000 · null · null · 1 | The split puts "TOTAL 230,000" and "CASH 230,000" on lines of their own. |
| 52 | test-0045 | delivery | C | 88000 · null · null · 4 | The change amount "0" stands alone above "CHANGED". |
| 53 | test-0072 | delivery | C | 17500 · 15909 · 1591 · 1 | "PB1:" and "1,591" fall on two lines. The payment lines still pair wrongly: "cash:", then "Change: 20,000", then "2,500". |
| 54 | test-0096 | delivery | C | 41000 · 41000 · null · 1 | Edge: "ITEMS: 1 41,000" carries the subtotal, then "41,000" prints above "Total.". The option's "0" stands alone. |
| 55 | train-0382 | delivery | C | 30000 · null · null · 1 | "70.000" stands above "CHANGED". |
| 56 | train-0400 | pilot | C | 110000 · 100003 · 10000 · 4 | Reads correctly now: four item lines, then "ITEMS : 5 100,003". |
| 57 | train-0516 | evidence, held-out | C | 156090 · 132000 · 14190 · 4 | Edge: the item lines read correctly now. The count ":4" stays in the total line, "TOTAL :4 156,090". |
| 58 | train-0596 | delivery | C | 42000 · 42000 · null · 1 | Reads correctly now; "Pay" ends up on a line of its own. |
| 59 | train-0614 | rehearsal, held-out | C | 201900 · 174000 · null · 4 | Edge: "Hubert chicken capers" prints above "1 fettucine 65.000", and "GRAND" below "TOTAL 201.900". "Others 18.357" as in train-0361. |
| 60 | train-0759 | delivery | C | 64000 · null · null · 6 | Reads correctly now: "TOTAL 64000" and "6 CASH 100000" are separate lines. |
| 61 | validation-0047 | delivery | C | 42000 · 42000 · null · 1 | The option's "0" stands alone. |

## 4. Beyond the sample

### 4.1 Screens over all 798 cases

- **Pairing.** For every total, subtotal and tax annotation I located its amount in the rebuilt text, relative to its own label words and to the other annotated lines. 101 placements, in 90 receipts, fall outside the label's own line, and I read each of them.
- **Fields left null.** Lines that name a tax, subtotal or total beside a digit while that label is null.
- **Odd values.** Subtotal annotations that name a group of items, taxes that equal a rate or exceed the subtotal, and totals below the subtotal.
- **Item counts.** Printed item counts that match neither the label nor the sum of quantities; top-level entries at price 0 or without a price that are not bags or charges; top-level entries that look like options; sub-items that look like bags; sub-items with their own quantity and a price.
- **Total lines.** The label words of every total annotation. 13 have no word that names a total, and I read them all.
- **Flags and names.** The 9 cases with a suspect flag, and every line that says "Other" or "Others".

### 4.2 Receipts that are not ok

| Case | Set | Labels | Verdict | Field | What the text shows | Reason if excluded |
|---|---|---|---|---|---|---|
| train-0082 | evidence, tuning | 23000 · 20909 · 2091 · 1 | text-ambiguous | subtotal, tax | Each amount sits in the row_id of the line above its label, so the text reads "20.909" alone, then "Subtotal 2,091", then "PAIRK 10%" with no amount. The same fault as the excluded train-0425. | `garbled_text` |
| validation-0023 | delivery | 20000 · 18182 · 1818 · 1 | text-ambiguous | subtotal, tax, total | Each amount sits in the row_id of the line below its label: "Subtotal" alone, then "PAJAK 10% 18,182", then "1 Total 1,818". | `garbled_text` |
| train-0124 | evidence, held-out | 1092542 · 937000 · 99322 · 11 | rule-gap | items | "2 CRYSANTNUM" under "2 CHINESE TE 14,000 28,000" names the kind of tea and has no price, yet it is a menu entry of its own. The v2 rule makes it part of the tea, so 10. The same store's "2 KWAN'IM" under "2 CHINESE TE" stays inside the tea in train-0527 and train-0266. | `rule_conflict` |
| train-0777 | delivery | 1303112 · 1082000 · 141192 · 11 | rule-gap | items | Two "3 CRYSANTNUM" lines, as in train-0124, so 9 under the v2 rule. | `rule_conflict` |
| train-0146 | pilot | 564425 · 482000 · 48685 · 6 | label-wrong | items | "GOCHUJANG 1 85,000", then "BIBIMBAB" with no quantity or price: one dish whose name wraps, annotated as two entries. The five priced lines add up to "Sub Total 482,000". Items is 5. | `label_wrong` |
| train-0757 | pilot | 92000 · 92000 · null · 2 | rule-gap | subtotal | The subtotal label is "Total Perawatan 92,000", the total for treatments, printed beside "Total Produk 0" and before "Grand Total 92,000". The rules say a sum for one group of items is not the subtotal, and also that a TOTAL line followed by a GRAND TOTAL is. | `rule_conflict` |
| train-0295 | pilot | 20000 · 18181 · 1818 · 3 | text-ambiguous, borderline | total | The word "Total" sits in the row_id of the rounding line, so the text reads "Reund Total 1", then "Take Away 20,000". A careful reader would most likely still answer 20000. | `garbled_text`, or keep |
| train-0219 | pilot | 83716 · 83716 · null · 7 | text-ambiguous, borderline | subtotal | "SUBTOTAL: 83,716" is followed by a bare "83,716"; the total's label word is not in the text. Read as a total, the labels hold. Read as a payment, no total is printed and the rules make the subtotal null. | keep, or `rule_conflict` |
| test-0079 | delivery | 22000 · null · null · 2 | text-ambiguous, borderline | subtotal | "Total Item: 2" is followed by two bare "22.000" lines, CORD's total and cash, with no Total or Cash word. The labels follow the strict reading; reading the second amount as the total gives a subtotal of 22000. | keep |

- **Counts.** 6 clear cases (0.8% of 798, Wilson 0.3% to 1.6%) and 3 borderline ones (1.1% with them). These are what the screens found, so they are a floor on the true count.
- **By set.** The clear cases are 2 pilot, 2 evidence (one tuning, one held-out) and 2 delivery receipts. The borderline ones are 2 pilot and 1 delivery receipt. None is in the rehearsal set.
- **Garbled pairings.** train-0082 and validation-0023 have the fault the build already excludes as `garbled_text`: CORD put each amount in the row_id of a neighbouring line, and a split within one row cannot move words between rows. The pairing screen above finds them mechanically.

### 4.3 Edges left as ok

- **Printed counts that the rules overrule.** train-0748 prints "ITEMS PURCHASED: 11" where the rule gives 12. train-0350 and train-0356 print "ITEMS: 3" for a value meal whose two contents are priced at 0, where the rule gives 1. train-0179 lists three items that add up to the subtotal and then prints "Total item 4"; the rule counts the 3 listed.
- **Bags inside a boxed set.** train-0396 lists "Bag-S" and "PS Carrier" under the tarts with no quantity or price, and train-0550 lists "1 PL. Bag Box 6 ---" among the contents of a box of six. Both read as package contents, so 1, although the bag clause could pull a reader to count them.
- **Options out of place.** In train-0161 "1 MUSHROOM SAUCE 0" follows the iced tea though it belongs to the steak. In train-0563 the price sits on the variant line ("1 Ice Tea 0", then "1 Ice Tea Lemon 29,091"), and the printed "Total Item 7" agrees with the label.
- **Totals under other names.** train-0439 ("Payment 1126510", with the card line after it), train-0465 ("HOLD 56,760"), train-0558 (the amount after tax printed as a second "SUBTOTAL"), and three receipts whose total is a bare amount printed after the tax (train-0282, train-0388 and train-0730).
- **"Others" as tax.** In one restaurant's layout (train-0361, train-0614 and validation-0094) "Others" is 10% of subtotal plus service, but nothing calls it a tax, and all three have a null tax.
- **One udon chain's layout.** 14 receipts print "GRAND TOTAL" with its amount on the next line, beside the tender ("~ CASH [25,000] 25,000"). Both numbers agree, and all 14 label the total the same way.

## 5. Splits and near-duplicates

**Cases.**

- Each of the 798 case ids is in exactly one set: 150, 100, 200 and 348. Delivery holds 166 train, 86 validation and 96 test receipts. With the 202 excluded receipts that makes all 1,000 source rows, with no overlap.
- Validation and test receipts appear only in delivery.
- `floors/rehearsal.json` and `floors/evidence.json` hold exactly the cases of their case files. Architect's rule (sorted case ids, even positions tune) gives the partitions the manifests record: 50 and 50, 100 and 100.

**Copies of one purchase.**

- I rebuilt the duplicate groups over all 1,000 receipts with both content keys, computed on the committed text and again on a plain row_id rebuild, and joined the 16 hand-listed pairs. That gives 844 groups. None holds two kept cases, no case outside delivery shares a group with a validation or test receipt, and each of the 140 `duplicate` entries in `manifests/excluded.json` points to the kept case of its own group.
- A fresh near-duplicate screen, with measures the data build does not use, compared all 318,003 pairs of kept cases. It kept the pairs whose word and number tokens overlap by at least 0.5 (Jaccard), whose amounts of three or more digits overlap by at least 0.7, or whose item names overlap by at least 0.8. Of those it flagged the pairs with a difflib ratio of at least 0.75 on the lowercased texts, a token overlap of 0.7, an amount overlap of 0.8 with a token overlap of 0.4, or an item-name overlap of 0.8 with an amount overlap of 0.5. That flagged 322 pairs, 224 of them across sets or floor halves. Grouping kept cases by the full multiset of their amounts with four or more digits found 19 more groups to read.
- I read the texts of 100 flagged pairs: those where one copy's amounts are all among the other's, where the difflib ratio reaches 0.9, where the differing amounts are one digit apart, or where the payment amounts agree. I also read all 19 groups. Every pair and group differs in its items or in its payment, so none is a copy of one purchase.
- A second screen on 2026-10-04 used other measures. Among the pairs of kept cases with the same total, or whose amounts of four or more digits overlap by at least 0.4 (Jaccard), it flagged those whose sequences of printed numbers, or whose texts without punctuation, have a difflib ratio of at least 0.75, and those whose amounts overlap by 0.8 and item-name words by 0.5. That flagged 197 pairs, 142 of them across sets or floor halves. Of the 197, 95 list different items, and every other pair differs in an item, a quantity or the payment, so this screen found no copy of one purchase either.

**Repeat purchases.** 22 families (58 receipts) print the same item names, quantities and prices and carry the same labels. They differ only in the payment (the cash tendered, the change, or a card or e-wallet) or in an option. RULES.md keeps these apart on purpose. 18 families span sets or floor halves, and 4 of them link a tuning case with an exam or delivery case. One more pair, whose item names differ by a word, does too:

- "TRIPPLE CHEESE" at 17,000: train-0061 (evidence, tuning), train-0225 (evidence, held-out), train-0228 (rehearsal, tuning), test-0077 and train-0485 (delivery), and train-0125 (pilot);
- one cheese tart at 29,000: train-0447 (rehearsal, tuning), train-0480 (delivery) and train-0771 (evidence, held-out);
- "ALMOND CHOCO CREAM CHEESE" at 28,000: train-0415 (evidence, tuning), train-0042 and train-0090 (delivery);
- train-0249 (rehearsal, tuning) and train-0007 (evidence, held-out), "PKT AYAM 33,000";
- the pair: train-0046 (rehearsal, tuning) and train-0004 (evidence, held-out), a meatball noodle dish at 43,636 ("BASO TAHU BIHUN" on one, "BASO BIHUN" on the other).

Only the first family crosses inside the evidence floor. I would leave these receipts as they are and mention repeat purchases in the pre-registration, since a deployed reader meets repeat orders too.

## 6. What to do before the pilot

1. Add the 6 clear cases to `EXCLUDED` in `scripts/fetch_bench_data.py`: train-0082 and validation-0023 as `garbled_text`, train-0124, train-0777 and train-0757 as `rule_conflict`, and train-0146 as `label_wrong`.
2. Decide on the 3 borderline cases. I lean toward excluding train-0295 with the other garbled texts and keeping train-0219 and test-0079, whose labels match the most natural reading.
3. Expect the sets to shift. Computed in memory with the script's own derivation, excluding the 6 leaves pilot 150, rehearsal 100, evidence 200 and delivery 342. It moves 2 receipts from rehearsal to pilot, 2 from evidence to rehearsal and 4 from delivery to evidence, and it flips the half of 87 evidence and 12 rehearsal receipts, because halves follow sorted ids. Excluding all 9 leaves delivery at 339 and flips 89 evidence and 68 rehearsal halves.
4. Consider a build flag for a label amount that the text prints beside another label (the pairing screen in section 4.1), so that a garbled text shows up in `stats.json`.
5. Mention repeat purchases in the pre-registration's notes on the splits.

## 7. Reproducing the sample

Run this from the repository root with the CORD cache in place. It reads the committed cases and manifests and the cached raw annotations, and it changes nothing. Set `PYTHONDONTWRITEBYTECODE=1` to avoid writing bytecode next to the script.

```python
import importlib.util, json, random, sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("fbd", "scripts/fetch_bench_data.py")
fbd = importlib.util.module_from_spec(spec); sys.modules["fbd"] = fbd; spec.loader.exec_module(fbd)
rows, _ = fbd.read_cache(fbd.default_cache())
raw = {f"cord-{s}-{r.index:04d}": json.loads(r.ground_truth) for s, rs in rows.items() for r in rs}
cases, flags = {}, {}
for name in ("pilot", "rehearsal", "evidence", "delivery"):
    for line in Path(f"examples/receipts/cases/{name}.jsonl").read_text(encoding="utf-8").splitlines():
        case = json.loads(line)
        cases[case["case_id"]] = case
    manifest = json.loads(Path(f"examples/receipts/manifests/{name}.json").read_text(encoding="utf-8"))
    flags.update({entry["case_id"]: set(entry["flags"]) for entry in manifest["cases"]})

def stratum(cid):
    e, f = cases[cid]["expected"], flags[cid]
    cats = {line["category"] for line in raw[cid]["valid_line"]}
    hard = bool(f & {"quantity_over_1", "sub_items", "void_menu"}) or bool(
        cats & {"menu.discountprice", "sub_total.discount_price"})
    if "row_split" in f: return "C"
    if "void_menu" in f: return "V"
    if e["total"] is None: return "T0"
    null = e["subtotal"] is None or e["tax"] is None
    return "NH" if null and hard else "N" if null else "H" if hard else "R"

strata = {}
for cid in sorted(cases):
    strata.setdefault(stratum(cid), []).append(cid)
rng = random.Random(20261006)
sample = [cid for s, k in (("V", 1), ("T0", 6), ("NH", 18), ("N", 10), ("H", 11), ("R", 4))
          for cid in sorted(rng.sample(strata[s], k))]
changed = strata["C"]
```

This gives the 50 receipts of section 3 in the same order (rows 1 to 50) and the 11 changed ones (rows 51 to 61).
