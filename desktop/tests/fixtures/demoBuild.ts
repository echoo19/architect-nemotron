/**
 * The demo build, as the bridge reported it (clarity spec §6, S0): the receipt reader
 * built from `/make @receipt-reader.json` in examples/expense-app on 2026-10-07, which the
 * film records. Every message comes from a recorded tape and every number from the run:
 *
 * - The plan, the build and the export: videos/submission/capture/takes/t2/tape.json (the
 *   `/make @receipt-reader.json` turn and the `/export receipt-reader` turn), with `at`
 *   times from the tape's clock. The Build press is at 1791364797.89 (TAKES.md, ws13).
 * - Added the way the bridge now sends them (slice SB): the plan card's B2 fields (`task`,
 *   `checks`, `inputs`, `outputs` from examples/expense-app/receipt-reader.json, `origin`,
 *   and list `prices` from architect/models/pricing.py), each rung's B1 `mean_cost_usd`
 *   from architect/published/receipts/ladder-2026-10-06-video-3/exam-*.json, and B3's `at`
 *   on every stage, from the tape's arrival times. Lightning ran 12m 28s, Super 3m 13s,
 *   Ultra 3m 12s; the build 18m 54s; it cost $0.31.
 * - The connect turn: the coder's events from videos/submission/v4/capture/takes/r3a (the
 *   edit approval on app/extract.py, allowed, "+15 -3 lines"), sent here as Connect it
 *   sends it (quiet). Its approval detail is the bridge's own preview, as recorded.
 * - DEMO_NO_PASS is the one variant no tape holds: the same build at Architect's own
 *   suggested pass mark of 95 (its draft from the sentence, the T2 plan it set aside), so
 *   no model passes and every rung is refused for the pass mark alone.
 *
 * The message arrays are generated from the tapes; change the generator's sources, not
 * the arrays, if a shape changes.
 */
import type { ForgeFeed } from '../../src/lib/forge';
import { emptyFeed } from '../../src/lib/forge';
import type { Timeline } from '../../src/lib/timeline';
import { rebuild, type Conv } from '../../src/store/app';

export type Msg = Record<string, unknown>;

/** The project the demo build ran in. */
export const DEMO_CWD = 'C:\\rec\\expense-app';
/** The Build press, ms (TAKES.md: the settled Build question's `at`). */
export const DEMO_BUILD_PRESS = 1791364797.89 * 1000;
/** "Now" for DEMO_BUILDING: 15:02 after the Build press. */
export const DEMO_BUILDING_NOW = DEMO_BUILD_PRESS + 902_000;
export const DEMO_LADDER = ['nvidia/Nemotron-3_5-Lightning', 'nvidia/nemotron-3-super-120b-a12b', 'nvidia/Nemotron-3-Ultra-550b-a55b'];

/** The task the plan file gives (examples/expense-app/receipt-reader.json): seven rules, one per "- " line. */
export const DEMO_TASK = "Return one JSON object with exactly these four keys: total, subtotal, tax, item_count.\n- total: the total amount printed on the receipt, including any tax and service, on a line such as TOTAL, GRAND TOTAL, TL or PAID. A line that names a tender (cash, card, e-wallet, voucher), a payment discount or change is never the total. If no total is printed and nothing after the subtotal changes the amount (only tender and change lines, or a 0% discount), the subtotal is the total and subtotal is null.\n- subtotal: the first amount printed after the items that sums them, apart from the total, whatever the receipt calls it: SUBTOTAL, ITEMS TOTAL, HARGA JUAL, DPP, NET TOTAL, the amount on an ITEMS line, a TOTAL line that a later GRAND TOTAL follows, or a bare amount under the items. A sum for one group of items (FOOD, BEVERAGE) is not the subtotal.\n- tax: the tax amount printed on the receipt (TAX, VAT, PPN, PB1, PAJAK), not its rate, its base (DPP) or a service charge.\n- item_count: the number of items listed on the receipt. Count each listed item once, whatever its quantity; the same item on two separate item lines counts twice. Bags and charges listed as items (plastic bag, take-away charge) count, even at price 0. Lines that belong to an item (options, toppings, extras, notes, package contents) are part of that item, even with their own quantity and price. Discount lines are not items. A void line that cancels an item is not an item; the cancelled item still counts.\n- Amounts are integers: take the amount itself, without its label or rate, and delete every character that is not a digit 0-9. For example \"75,000\" is 75000, \"Rp 12.500\" is 12500, \"-2.727\" is 2727, \"226,500.00\" is 22650000 and \"PB1 10% 4,818\" is 4818.\n- An amount the receipt does not print is null; never compute it from other amounts. A printed value without any digit, such as \"-\", is null.\n- If the receipt prints the same field more than once, use the first (topmost) one.";

/** `/make @receipt-reader.json`: the plan card (with B2's fields) and its Build question. */
export const PLAN_MSGS: Msg[] = [
  {"type": "turn_started", "text": "/make @receipt-reader.json", "quiet": false, "at": 1791364787.975},
  {"type": "event", "event": {"title": "Plan: receipt-reader", "rows": [["task", "Return one JSON object with exactly these four keys: total, subtotal, tax, item_count.\n- total: the total amount printed on the receipt, including any tax and service, on a line such as TOTAL, GRAND..."], ["spec", "4d80f3d580cac16d3b384eb7f8ca7d84990bd48b8fca0482cca4afb00e0e48cb (frozen, receipt-reader.json)"], ["engineer", "Tuned preset only: extractor (Forge off)"], ["checks", "Amounts keep every printed digit, so \"226,500.00\" is 22650000. A subtotal equal to the total is null unless the receipt prints that amount as a subtotal."], ["inputs", "receipt"], ["outputs", "item_count, subtotal, tax, total"], ["floor", "held_out_tasks >= 0.40 on 'bench-receipts-v2-evidence'"], ["floor data", "CORD v2 · CC BY 4.0 · 200 receipts (100 tuning / 100 held-out)"], ["invalid output", "counts as a miss, at most 2 of 100 held-out runs"], ["budget", "$0.02 and 60 s per run; compile up to $0.60 and 60 min"], ["model", "cheapest that passes over nvidia/Nemotron-3_5-Lightning, nvidia/nemotron-3-super-120b-a12b, nvidia/Nemotron-3-Ultra-550b-a55b"], ["destination", "run it here: /run receipt-reader, `architect run`, HTTP through /serve"], ["install as", "receipt-reader (installed agent)"]], "footer": "spec 4d80f3d5. Enter compiles; the spec file is compiled as written.", "data": {"spec_hash": "4d80f3d580cac16d3b384eb7f8ca7d84990bd48b8fca0482cca4afb00e0e48cb", "bar": 0.4, "domain": [0.2, 0.7], "kind": "held_out_tasks", "ladder": ["nvidia/Nemotron-3_5-Lightning", "nvidia/nemotron-3-super-120b-a12b", "nvidia/Nemotron-3-Ultra-550b-a55b"], "practice": 100, "exam": 100, "engineer": "preset", "build_usd": 0.6, "build_s": 3600, "frozen": "receipt-reader.json", "task": "Return one JSON object with exactly these four keys: total, subtotal, tax, item_count.\n- total: the total amount printed on the receipt, including any tax and service, on a line such as TOTAL, GRAND TOTAL, TL or PAID. A line that names a tender (cash, card, e-wallet, voucher), a payment discount or change is never the total. If no total is printed and nothing after the subtotal changes the amount (only tender and change lines, or a 0% discount), the subtotal is the total and subtotal is null.\n- subtotal: the first amount printed after the items that sums them, apart from the total, whatever the receipt calls it: SUBTOTAL, ITEMS TOTAL, HARGA JUAL, DPP, NET TOTAL, the amount on an ITEMS line, a TOTAL line that a later GRAND TOTAL follows, or a bare amount under the items. A sum for one group of items (FOOD, BEVERAGE) is not the subtotal.\n- tax: the tax amount printed on the receipt (TAX, VAT, PPN, PB1, PAJAK), not its rate, its base (DPP) or a service charge.\n- item_count: the number of items listed on the receipt. Count each listed item once, whatever its quantity; the same item on two separate item lines counts twice. Bags and charges listed as items (plastic bag, take-away charge) count, even at price 0. Lines that belong to an item (options, toppings, extras, notes, package contents) are part of that item, even with their own quantity and price. Discount lines are not items. A void line that cancels an item is not an item; the cancelled item still counts.\n- Amounts are integers: take the amount itself, without its label or rate, and delete every character that is not a digit 0-9. For example \"75,000\" is 75000, \"Rp 12.500\" is 12500, \"-2.727\" is 2727, \"226,500.00\" is 22650000 and \"PB1 10% 4,818\" is 4818.\n- An amount the receipt does not print is null; never compute it from other amounts. A printed value without any digit, such as \"-\", is null.\n- If the receipt prints the same field more than once, use the first (topmost) one.", "checks": ["Amounts keep every printed digit, so \"226,500.00\" is 22650000.", "A subtotal equal to the total is null unless the receipt prints that amount as a subtotal."], "inputs": [{"name": "receipt", "type": "string", "nullable": false}], "outputs": [{"name": "total", "type": "integer", "nullable": true}, {"name": "subtotal", "type": "integer", "nullable": true}, {"name": "tax", "type": "integer", "nullable": true}, {"name": "item_count", "type": "integer", "nullable": false}], "origin": "public_dataset", "prices": {"nvidia/Nemotron-3_5-Lightning": [0.06, 0.24], "nvidia/nemotron-3-super-120b-a12b": [0.3, 0.9], "nvidia/Nemotron-3-Ultra-550b-a55b": [1.0, 3.0]}}, "kind": "plan_card"}},
  {"type": "ask", "ask": {"id": 3, "kind": "question", "question": "Compile this agent?", "default": "yes", "choices": []}},
];

/** The Build press to the result: every stage (with B3's `at`), the progress lines, the result card (with B1's costs). */
export const BUILD_MSGS: Msg[] = [
  {"type": "ask_closed", "id": 3, "answer": "yes", "at": 1791364797.89},
  {"type": "event", "event": {"agent": "compiler", "text": "compiling receipt-reader (spec 4d80f3d5)", "kind": "subagent"}},
  {"type": "forge", "data": {"kind": "stage", "stage": 1, "stages": [1], "depth": 0, "model": "nvidia/Nemotron-3_5-Lightning", "model_index": 1, "model_count": 3, "ladder": ["nvidia/Nemotron-3_5-Lightning", "nvidia/nemotron-3-super-120b-a12b", "nvidia/Nemotron-3-Ultra-550b-a55b"], "at": 1791364797.898}},
  {"type": "event", "event": {"text": "stage 1 select preset: 0 calls, $0.00 so far (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "forge", "data": {"kind": "stage", "stage": 2, "stages": [1, 2], "depth": 0, "model": "nvidia/Nemotron-3_5-Lightning", "model_index": 1, "model_count": 3, "ladder": ["nvidia/Nemotron-3_5-Lightning", "nvidia/nemotron-3-super-120b-a12b", "nvidia/Nemotron-3-Ultra-550b-a55b"], "at": 1791364797.903}},
  {"type": "event", "event": {"text": "stage 2 tune: 0 calls, $0.00 so far (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 41 calls, $0.0024 so far, 30 s in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 84 calls, $0.0050 so far, 60 s in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 125 calls, $0.0074 so far, 90 s in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 167 calls, $0.0098 so far, 2 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 212 calls, $0.01 so far, 2.5 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 257 calls, $0.02 so far, 3 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 302 calls, $0.02 so far, 3.5 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 346 calls, $0.02 so far, 4 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 394 calls, $0.02 so far, 4.5 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 441 calls, $0.03 so far, 5 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 482 calls, $0.03 so far, 5.5 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 525 calls, $0.03 so far, 6 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 570 calls, $0.03 so far, 6.5 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 617 calls, $0.04 so far, 7 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 661 calls, $0.04 so far, 7.5 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 709 calls, $0.04 so far, 8 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 756 calls, $0.04 so far, 8.5 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 804 calls, $0.05 so far, 9 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 845 calls, $0.05 so far, 9.5 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 892 calls, $0.05 so far, 10 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 933 calls, $0.06 so far, 10.5 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 976 calls, $0.06 so far, 11 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "forge", "data": {"kind": "stage", "stage": 4, "stages": [1, 2, 4], "depth": 0, "model": "nvidia/Nemotron-3_5-Lightning", "model_index": 1, "model_count": 3, "ladder": ["nvidia/Nemotron-3_5-Lightning", "nvidia/nemotron-3-super-120b-a12b", "nvidia/Nemotron-3-Ultra-550b-a55b"], "tuned": {"score": 0.48, "passed": true}, "examined": {"score": 0.48, "passed": true}, "at": 1791365474.106}},
  {"type": "event", "event": {"text": "stage 4 verify: 1000 calls, $0.06 so far (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 4 verify: 1017 calls, $0.06 so far, 14 s in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 4 verify: 1059 calls, $0.06 so far, 44 s in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "forge", "data": {"kind": "stage", "stage": 1, "stages": [1, 2, 4], "depth": 0, "model": "nvidia/nemotron-3-super-120b-a12b", "model_index": 2, "model_count": 3, "ladder": ["nvidia/Nemotron-3_5-Lightning", "nvidia/nemotron-3-super-120b-a12b", "nvidia/Nemotron-3-Ultra-550b-a55b"], "at": 1791365546.069}},
  {"type": "event", "event": {"text": "stage 1 select preset: 1100 calls, $0.06 so far (nvidia/nemotron-3-super-120b-a12b, rung 2 of 3)", "kind": "progress"}},
  {"type": "forge", "data": {"kind": "stage", "stage": 2, "stages": [1, 2, 4], "depth": 0, "model": "nvidia/nemotron-3-super-120b-a12b", "model_index": 2, "model_count": 3, "ladder": ["nvidia/Nemotron-3_5-Lightning", "nvidia/nemotron-3-super-120b-a12b", "nvidia/Nemotron-3-Ultra-550b-a55b"], "at": 1791365546.074}},
  {"type": "event", "event": {"text": "stage 2 tune: 1100 calls, $0.06 so far (nvidia/nemotron-3-super-120b-a12b, rung 2 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 1101 calls, $0.07 so far, 2 s in this stage (nvidia/nemotron-3-super-120b-a12b, rung 2 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 1134 calls, $0.07 so far, 32 s in this stage (nvidia/nemotron-3-super-120b-a12b, rung 2 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 1165 calls, $0.08 so far, 62 s in this stage (nvidia/nemotron-3-super-120b-a12b, rung 2 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 1194 calls, $0.09 so far, 92 s in this stage (nvidia/nemotron-3-super-120b-a12b, rung 2 of 3)", "kind": "progress"}},
  {"type": "forge", "data": {"kind": "stage", "stage": 4, "stages": [1, 2, 4], "depth": 0, "model": "nvidia/nemotron-3-super-120b-a12b", "model_index": 2, "model_count": 3, "ladder": ["nvidia/Nemotron-3_5-Lightning", "nvidia/nemotron-3-super-120b-a12b", "nvidia/Nemotron-3-Ultra-550b-a55b"], "tuned": {"score": 0.7, "passed": true}, "examined": {"score": 0.7, "passed": true}, "at": 1791365643.415}},
  {"type": "event", "event": {"text": "stage 4 verify: 1200 calls, $0.09 so far (nvidia/nemotron-3-super-120b-a12b, rung 2 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 4 verify: 1228 calls, $0.10 so far, 25 s in this stage (nvidia/nemotron-3-super-120b-a12b, rung 2 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 4 verify: 1257 calls, $0.11 so far, 55 s in this stage (nvidia/nemotron-3-super-120b-a12b, rung 2 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 4 verify: 1288 calls, $0.12 so far, 85 s in this stage (nvidia/nemotron-3-super-120b-a12b, rung 2 of 3)", "kind": "progress"}},
  {"type": "forge", "data": {"kind": "stage", "stage": 1, "stages": [1, 2, 4], "depth": 0, "model": "nvidia/Nemotron-3-Ultra-550b-a55b", "model_index": 3, "model_count": 3, "ladder": ["nvidia/Nemotron-3_5-Lightning", "nvidia/nemotron-3-super-120b-a12b", "nvidia/Nemotron-3-Ultra-550b-a55b"], "at": 1791365739.554}},
  {"type": "event", "event": {"text": "stage 1 select preset: 1300 calls, $0.12 so far (nvidia/Nemotron-3-Ultra-550b-a55b, rung 3 of 3)", "kind": "progress"}},
  {"type": "forge", "data": {"kind": "stage", "stage": 2, "stages": [1, 2, 4], "depth": 0, "model": "nvidia/Nemotron-3-Ultra-550b-a55b", "model_index": 3, "model_count": 3, "ladder": ["nvidia/Nemotron-3_5-Lightning", "nvidia/nemotron-3-super-120b-a12b", "nvidia/Nemotron-3-Ultra-550b-a55b"], "at": 1791365739.56}},
  {"type": "event", "event": {"text": "stage 2 tune: 1300 calls, $0.12 so far (nvidia/Nemotron-3-Ultra-550b-a55b, rung 3 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 1320 calls, $0.14 so far, 19 s in this stage (nvidia/Nemotron-3-Ultra-550b-a55b, rung 3 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 1350 calls, $0.17 so far, 49 s in this stage (nvidia/Nemotron-3-Ultra-550b-a55b, rung 3 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 1384 calls, $0.20 so far, 79 s in this stage (nvidia/Nemotron-3-Ultra-550b-a55b, rung 3 of 3)", "kind": "progress"}},
  {"type": "forge", "data": {"kind": "stage", "stage": 4, "stages": [1, 2, 4], "depth": 0, "model": "nvidia/Nemotron-3-Ultra-550b-a55b", "model_index": 3, "model_count": 3, "ladder": ["nvidia/Nemotron-3_5-Lightning", "nvidia/nemotron-3-super-120b-a12b", "nvidia/Nemotron-3-Ultra-550b-a55b"], "tuned": {"score": 0.78, "passed": true}, "examined": {"score": 0.78, "passed": true}, "at": 1791365833.332}},
  {"type": "event", "event": {"text": "stage 4 verify: 1400 calls, $0.22 so far (nvidia/Nemotron-3-Ultra-550b-a55b, rung 3 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 4 verify: 1414 calls, $0.23 so far, 15 s in this stage (nvidia/Nemotron-3-Ultra-550b-a55b, rung 3 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 4 verify: 1444 calls, $0.26 so far, 45 s in this stage (nvidia/Nemotron-3-Ultra-550b-a55b, rung 3 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 4 verify: 1475 calls, $0.28 so far, 75 s in this stage (nvidia/Nemotron-3-Ultra-550b-a55b, rung 3 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "installed receipt-reader v1: floor 0.64/0.40, $0.31 spent. A tuned extractor preset (Forge off)", "level": "info", "kind": "notice"}},
  {"type": "event", "event": {"title": "Result: receipt-reader v1", "rows": [["engineer", "a tuned extractor preset (Forge off)"], ["floor", "0.64/0.40 on 100 held-out cases (correct 64/100 · invalid 0)"], ["ladder", "Nemotron-3_5-Lightning 39/100 refused · nemotron-3-super-120b-a12b 64/100 licensed, ships · Nemotron-3-Ultra-550b-a55b 72/100 licensed"], ["cost", "$0.31 to build; up to $0.02 and 60 s per run"], ["destination", "run it here (installed)"], ["next", "/run receipt-reader <input>\nHTTP: /serve starts the local service (loopback, bearer token)\nshell: uv run architect run \"C:\\rec\\ws13\\registry\\versions\\e6253486fc275b1c1b6040788aab3ef9\\1\\artifacts\\4d80f3d580cac16d3b384eb7f8ca7d84990bd48b8fca0482cca4afb00e0e48cb\\5\" --profile \"C:\\rec\\ws13\\session-profile.json\" --input '<json>'"]], "footer": "", "data": {"spec_hash": "4d80f3d580cac16d3b384eb7f8ca7d84990bd48b8fca0482cca4afb00e0e48cb", "licensed": true, "bar": 0.4, "kind": "held_out_tasks", "model": "nvidia/nemotron-3-super-120b-a12b", "score": 0.64, "exam": {"cases": 100, "correct": 64, "invalid": 0, "over_budget": 0}, "rungs": [{"model": "nvidia/Nemotron-3_5-Lightning", "preset": "extractor", "licensed": false, "ships": false, "score": 0.39, "limit": 0.4, "cases": 100, "counts": {"cases": 100, "correct": 39, "invalid": 0, "over_budget": 0}, "codes": ["primary_floor_failed", "primary_evaluation_failed"], "mean_cost_usd": 5.83056e-05}, {"model": "nvidia/nemotron-3-super-120b-a12b", "preset": "extractor", "licensed": true, "ships": true, "score": 0.64, "limit": 0.4, "cases": 100, "counts": {"cases": 100, "correct": 64, "invalid": 0, "over_budget": 0}, "codes": [], "mean_cost_usd": 0.0002782949999999999}, {"model": "nvidia/Nemotron-3-Ultra-550b-a55b", "preset": "extractor", "licensed": true, "ships": false, "score": 0.72, "limit": 0.4, "cases": 100, "counts": {"cases": 100, "correct": 72, "invalid": 0, "over_budget": 0}, "codes": [], "mean_cost_usd": 0.0009273200000000001}]}, "kind": "plan_card"}},
  {"type": "turn_finished", "text": "/make @receipt-reader.json", "quiet": false, "seconds": 1143.83, "at": 1791365931.799},
];

/** The build at 15:02 after the Build press: Lightning done, Super taking the exam, Ultra waiting. */
export const BUILDING_MSGS: Msg[] = [
  {"type": "ask_closed", "id": 3, "answer": "yes", "at": 1791364797.89},
  {"type": "event", "event": {"agent": "compiler", "text": "compiling receipt-reader (spec 4d80f3d5)", "kind": "subagent"}},
  {"type": "forge", "data": {"kind": "stage", "stage": 1, "stages": [1], "depth": 0, "model": "nvidia/Nemotron-3_5-Lightning", "model_index": 1, "model_count": 3, "ladder": ["nvidia/Nemotron-3_5-Lightning", "nvidia/nemotron-3-super-120b-a12b", "nvidia/Nemotron-3-Ultra-550b-a55b"], "at": 1791364797.898}},
  {"type": "event", "event": {"text": "stage 1 select preset: 0 calls, $0.00 so far (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "forge", "data": {"kind": "stage", "stage": 2, "stages": [1, 2], "depth": 0, "model": "nvidia/Nemotron-3_5-Lightning", "model_index": 1, "model_count": 3, "ladder": ["nvidia/Nemotron-3_5-Lightning", "nvidia/nemotron-3-super-120b-a12b", "nvidia/Nemotron-3-Ultra-550b-a55b"], "at": 1791364797.903}},
  {"type": "event", "event": {"text": "stage 2 tune: 0 calls, $0.00 so far (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 41 calls, $0.0024 so far, 30 s in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 84 calls, $0.0050 so far, 60 s in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 125 calls, $0.0074 so far, 90 s in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 167 calls, $0.0098 so far, 2 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 212 calls, $0.01 so far, 2.5 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 257 calls, $0.02 so far, 3 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 302 calls, $0.02 so far, 3.5 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 346 calls, $0.02 so far, 4 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 394 calls, $0.02 so far, 4.5 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 441 calls, $0.03 so far, 5 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 482 calls, $0.03 so far, 5.5 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 525 calls, $0.03 so far, 6 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 570 calls, $0.03 so far, 6.5 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 617 calls, $0.04 so far, 7 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 661 calls, $0.04 so far, 7.5 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 709 calls, $0.04 so far, 8 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 756 calls, $0.04 so far, 8.5 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 804 calls, $0.05 so far, 9 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 845 calls, $0.05 so far, 9.5 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 892 calls, $0.05 so far, 10 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 933 calls, $0.06 so far, 10.5 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 976 calls, $0.06 so far, 11 min in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "forge", "data": {"kind": "stage", "stage": 4, "stages": [1, 2, 4], "depth": 0, "model": "nvidia/Nemotron-3_5-Lightning", "model_index": 1, "model_count": 3, "ladder": ["nvidia/Nemotron-3_5-Lightning", "nvidia/nemotron-3-super-120b-a12b", "nvidia/Nemotron-3-Ultra-550b-a55b"], "tuned": {"score": 0.48, "passed": true}, "examined": {"score": 0.48, "passed": true}, "at": 1791365474.106}},
  {"type": "event", "event": {"text": "stage 4 verify: 1000 calls, $0.06 so far (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 4 verify: 1017 calls, $0.06 so far, 14 s in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 4 verify: 1059 calls, $0.06 so far, 44 s in this stage (nvidia/Nemotron-3_5-Lightning, rung 1 of 3)", "kind": "progress"}},
  {"type": "forge", "data": {"kind": "stage", "stage": 1, "stages": [1, 2, 4], "depth": 0, "model": "nvidia/nemotron-3-super-120b-a12b", "model_index": 2, "model_count": 3, "ladder": ["nvidia/Nemotron-3_5-Lightning", "nvidia/nemotron-3-super-120b-a12b", "nvidia/Nemotron-3-Ultra-550b-a55b"], "at": 1791365546.069}},
  {"type": "event", "event": {"text": "stage 1 select preset: 1100 calls, $0.06 so far (nvidia/nemotron-3-super-120b-a12b, rung 2 of 3)", "kind": "progress"}},
  {"type": "forge", "data": {"kind": "stage", "stage": 2, "stages": [1, 2, 4], "depth": 0, "model": "nvidia/nemotron-3-super-120b-a12b", "model_index": 2, "model_count": 3, "ladder": ["nvidia/Nemotron-3_5-Lightning", "nvidia/nemotron-3-super-120b-a12b", "nvidia/Nemotron-3-Ultra-550b-a55b"], "at": 1791365546.074}},
  {"type": "event", "event": {"text": "stage 2 tune: 1100 calls, $0.06 so far (nvidia/nemotron-3-super-120b-a12b, rung 2 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 1101 calls, $0.07 so far, 2 s in this stage (nvidia/nemotron-3-super-120b-a12b, rung 2 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 1134 calls, $0.07 so far, 32 s in this stage (nvidia/nemotron-3-super-120b-a12b, rung 2 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 1165 calls, $0.08 so far, 62 s in this stage (nvidia/nemotron-3-super-120b-a12b, rung 2 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 2 tune: 1194 calls, $0.09 so far, 92 s in this stage (nvidia/nemotron-3-super-120b-a12b, rung 2 of 3)", "kind": "progress"}},
  {"type": "forge", "data": {"kind": "stage", "stage": 4, "stages": [1, 2, 4], "depth": 0, "model": "nvidia/nemotron-3-super-120b-a12b", "model_index": 2, "model_count": 3, "ladder": ["nvidia/Nemotron-3_5-Lightning", "nvidia/nemotron-3-super-120b-a12b", "nvidia/Nemotron-3-Ultra-550b-a55b"], "tuned": {"score": 0.7, "passed": true}, "examined": {"score": 0.7, "passed": true}, "at": 1791365643.415}},
  {"type": "event", "event": {"text": "stage 4 verify: 1200 calls, $0.09 so far (nvidia/nemotron-3-super-120b-a12b, rung 2 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 4 verify: 1228 calls, $0.10 so far, 25 s in this stage (nvidia/nemotron-3-super-120b-a12b, rung 2 of 3)", "kind": "progress"}},
  {"type": "event", "event": {"text": "stage 4 verify: 1257 calls, $0.11 so far, 55 s in this stage (nvidia/nemotron-3-super-120b-a12b, rung 2 of 3)", "kind": "progress"}},
];

/** Add to project: `/export receipt-reader`, the export plan and its approval (open). */
export const EXPORT_MSGS: Msg[] = [
  {"type": "turn_started", "text": "/export receipt-reader", "quiet": false, "at": 1791365950.504},
  {"type": "event", "event": {"text": "export: staging receipt_reader from receipt-reader v1 ...", "kind": "progress"}},
  {"type": "event", "event": {"title": "Export plan: agents/receipt_reader/", "rows": [["agent", "receipt-reader v1: licensed, floor 0.64/0.40, extractor on nvidia/nemotron-3-super-120b-a12b"], ["writes", "C:\\rec\\expense-app\\agents\\receipt_reader (one new directory; nothing existing is changed)"], ["files", ".gitattributes\nEVIDENCE.json\nEVIDENCE.md\nMANIFEST.json\nREADME.md\n__init__.py\nartifact/config.json\nartifact/harness.py\nartifact/report.json\nartifact/spec.json\nschemas/input.schema.json\nschemas/output.schema.json\ntests/test_receipt_reader.py"], ["size", "13 files, 22 KB"], ["feature flag", "ARCHITECT_FEATURE_4D80F3D5: off unless it is exactly 1"], ["review", "a human reviews the package before the flag is set; the license is Ed25519-signed with key 39762f2071b307da: it verifies wherever that key is trusted"]], "footer": "Nothing has been written into the repository yet.", "data": null, "kind": "plan_card"}},
  {"type": "ask", "ask": {"id": 4, "kind": "approval", "action": "export", "detail": "receipt_reader (13 files, 22 KB) into agents/receipt_reader/ (one new directory; feature flag OFF; human review required)"}},
];

/** The export approved: the Exported card and the turn's end. */
export const EXPORTED_MSGS: Msg[] = [
  {"type": "ask_closed", "id": 4, "answer": "yes"},
  {"type": "event", "event": {"title": "Exported: agents/receipt_reader/", "rows": [["verified", "files, pins, sub-harnesses and the Stage 4 license: ok"], ["python", "# Call the exported agent from Python (run from the repository root).\n# ARCHITECT_FEATURE_4D80F3D5 must be 1, set only after a human has reviewed the package.\nimport asyncio\nimport os\n\n# This host's signing key; the Ed25519-signed license also verifies wherever its key is trusted.\nos.environ.setdefault(\"ARCHITECT_SIGNING_KEY_FILE\", r\"C:\\rec\\ws13\\signing.key\")\n\nfrom agents.receipt_reader import FLAG, enabled, run, verify\n\ncheck = verify()  # ExportedHarness: file hashes, pins, sub-harnesses, license\nif check.codes:\n    raise SystemExit(f\"package check failed: {check.codes}\")\nif not enabled():\n    raise SystemExit(f\"{FLAG} is off; set it to 1 after review\")\n# run(): ExportedHarness.load re-verifies, then one licensed run within its budget\noutput = asyncio.run(\n    run(\n        {'receipt': '...'},\n        profile=r\"C:\\rec\\ws13\\session-profile.json\",\n        workspace=r\"C:\\rec\\ws13\",\n    )\n)\nprint(output)"], ["http", "# Other languages: the local Architect HTTP service (loopback, bearer token; POSIX\n# shell shown). 1. Start it, or type /serve in Architect, which prints its token:\nexport ARCHITECT_API_TOKEN=\"$(python -c 'import secrets; print(secrets.token_urlsafe(32))')\"\nuv run python -m architect.server --profile \"C:\\rec\\ws13\\session-profile.json\" --port 8765 &\n# 2. Once: the service runs only agents it licensed itself, so it licenses this\n#    agent's approved spec (a fresh Stage 4 on this host, up to the compile budget,\n#    $0.60); the answer carries the artifact_id.\nuv run python -c \"import json,sys; print(json.dumps({'spec': json.load(open(sys.argv[1], encoding='utf-8')), 'approved': True}))\" agents/receipt_reader/artifact/spec.json \\\n  | curl -s -X POST http://127.0.0.1:8765/compile -H \"Authorization: Bearer $ARCHITECT_API_TOKEN\" -H \"Content-Type: application/json\" --data-binary @-\n# 3. Call it (enabled and human_reviewed must both be true):\ncurl -s -X POST http://127.0.0.1:8765/run -H \"Authorization: Bearer $ARCHITECT_API_TOKEN\" -H \"Content-Type: application/json\" \\\n  -d '{\"artifact_id\": \"<artifact_id from step 2>\", \"input\": {\"receipt\": \"...\"}, \"enabled\": true, \"human_reviewed\": true}'\n# Status: curl -s http://127.0.0.1:8765/status/<artifact_id> -H \"Authorization: Bearer $ARCHITECT_API_TOKEN\""], ["tests", "uv run pytest agents/receipt_reader/tests -p no:cacheprovider  (license tests fail where key 39762f2071b307da is not trusted; in CI also run architect verify --require-key 39762f2071b307da0db6ada328bc0743b228351fe2b78d890b50d09bf6cc30f8 on the package)"], ["enable", "after review: ARCHITECT_FEATURE_4D80F3D5=1"]], "footer": "Human review required; the feature flag is OFF until set.", "data": null, "kind": "plan_card"}},
  {"type": "event", "event": {"text": "Exported receipt-reader to agents/receipt_reader/ (files, pins, sub-harnesses and the Stage 4 license: ok).", "level": "info", "kind": "notice"}},
  {"type": "event", "event": {"text": "Next: review agents/receipt_reader/ (README.md, EVIDENCE.md), run uv run pytest agents/receipt_reader/tests -p no:cacheprovider, then set ARCHITECT_FEATURE_4D80F3D5=1 where the software runs.", "level": "info", "kind": "notice"}},
  {"type": "turn_finished", "text": "/export receipt-reader", "quiet": false, "seconds": 3.36, "at": 1791365953.857},
];

/** Connect it (quiet): the coder reads, then asks to edit app/extract.py (R3a's events). */
export const CONNECT_MSGS: Msg[] = [
  {"type": "turn_started", "text": "Connect the agent in agents/receipt_reader/ to this project's code. Read agents/receipt_reader/README.md and the code that does this job today, then change that call so it uses the agent's run(). Keep the feature flag off and do not run the tests; I will do both.", "quiet": true, "at": 1791473264.71},
  {"type": "event", "event": {"call_id": "chatcmpl-tool-a42f08cfe2ee20de", "name": "read_file", "summary": "read_file(README.md)", "kind": "tool_started"}, "at": 1791473265.717},
  {"type": "event", "event": {"call_id": "chatcmpl-tool-a42f08cfe2ee20de", "name": "read_file", "ok": true, "summary": "README.md (48 lines)", "kind": "tool_finished"}, "at": 1791473265.719},
  {"type": "event", "event": {"call_id": "chatcmpl-tool-ac6db926688f648d", "name": "read_file", "summary": "read_file(app/extract.py)", "kind": "tool_started"}, "at": 1791473266.893},
  {"type": "event", "event": {"call_id": "chatcmpl-tool-ac6db926688f648d", "name": "read_file", "ok": true, "summary": "app/extract.py (137 lines)", "kind": "tool_finished"}, "at": 1791473266.895},
  {"type": "event", "event": {"call_id": "chatcmpl-tool-80df4b9d3e7eb203", "name": "code", "summary": "code(Modify app/extract.py: \n1. Add imports after the existing imports: import asyncio, from pathlib impo)", "kind": "tool_started"}, "at": 1791473268.956},
  {"type": "event", "event": {"agent": "coder", "text": "Modify app/extract.py:  1. Add imports after the existing imports: import asyncio, from pathlib import Path, from age...", "kind": "subagent"}, "at": 1791473268.991},
  {"type": "event", "event": {"agent": "coder", "text": "I'll help modify the app/extract.py file as requested. Let me first examine the current file to understand its structure.", "kind": "subagent"}, "at": 1791473275.98},
  {"type": "event", "event": {"agent": "coder", "text": "read C:/rec/expense-app/app/extract.py", "kind": "subagent"}, "at": 1791473275.981},
  {"type": "event", "event": {"agent": "coder", "text": "read C:/rec/expense-app/app/extract.py: 138 lines", "kind": "subagent"}, "at": 1791473275.986},
  {"type": "event", "event": {"agent": "coder", "text": "Now I'll make the requested modifications to app/extract.py: 1. Add the imports after existing imports 2. Change EXTRACTOR = \"general-model\" to EXTRACTOR = \"receipt-agent\" 3. Replace the entire body of the extract() function with the new implementation", "kind": "subagent"}, "at": 1791473279.24},
  {"type": "event", "event": {"agent": "coder", "text": "edit C:/rec/expense-app/app/extract.py", "kind": "subagent"}, "at": 1791473279.24},
  {"type": "ask", "ask": {"id": 5, "kind": "approval", "action": "edit", "detail": "C:/rec/expense-app/app/extract.py (2 edits)\n- from __future__ import annotations\n- \n- import http.client\n- import json\n- import os\n- import time\n- import urllib.error\n- import urllib.request\n- from typing import Any\n- \n- from pydantic import BaseModel, Field, ValidationError\n- \n+ from __future__ import annotations\n+ \n+ import asyncio\n+ import http.client\n+ import json\n+ import os\n+ import time\n+ import urllib.error\n+ import urllib.request\n+ from pathlib import Path\n+ from typing import Any\n+ \n- def extract(text: str) -> Receipt:\n-     \"\"\"Read one receipt. Architect's receipt agent will replace this call.\"\"\"\n-     return ask_general_model(text)\n+ def extract(text: str) -> Receipt:\n+     \"\"\"Read one receipt with Architect's licensed receipt agent.\"\"\"\n+     try:\n+         answer = asyncio.run(\n+             run(\n+                 {\"receipt\": text},\n+                 profile=Path(__file__).resolve().parents[1] / \"architect.json\",\n+             )\n+         )\n+         return Receipt.model_validate(answer)\n+     except Exception as exc:\n+         raise ExtractionError(f\"the receipt agent failed ({type(exc).__name__}: {exc})\") from exc"}},
];

/** The edit allowed: the coder's report (+15 -3), its answer and the turn's end. */
export const CONNECTED_MSGS: Msg[] = [
  {"type": "ask_closed", "id": 5, "at": 1791473281.355, "answer": "yes"},
  {"type": "event", "event": {"agent": "coder", "text": "edit C:/rec/expense-app/app/extract.py: +15 -3 lines", "kind": "subagent"}, "at": 1791473281.358},
  {"type": "event", "event": {"agent": "coder", "text": "Let me verify the changes were applied correctly by reading the modified file:", "kind": "subagent"}, "at": 1791473282.955},
  {"type": "event", "event": {"agent": "coder", "text": "read C:/rec/expense-app/app/extract.py", "kind": "subagent"}, "at": 1791473282.956},
  {"type": "event", "event": {"agent": "coder", "text": "read C:/rec/expense-app/app/extract.py: 150 lines", "kind": "subagent"}, "at": 1791473282.959},
  {"type": "event", "event": {"call_id": "chatcmpl-tool-80df4b9d3e7eb203", "name": "code", "ok": true, "summary": "{\"ok\": true, \"summary\": \"Perfect! All the requested changes have been successfully applied to app/ex...", "kind": "tool_finished"}, "at": 1791473284.849},
  {"type": "event", "event": {"text": "The changes have been successfully applied to app/extract.py as requested:\n\n1. Added the required imports: `import asyncio`, `from pathlib import Path`, and `from agents.receipt_reader import run`\n2. Set `EXTRACTOR = \"receipt-agent\"` \n3. Replaced the body of the `extract()` function with the code from README.md step 3\n4. Left all other parts of the file unchanged\n\nThe agent's flag remains off as requested, and I didn't run any tests. You can now turn on the flag and run tests when ready.", "kind": "answer"}, "at": 1791473286.148},
  {"type": "turn_finished", "text": "Connect the agent in agents/receipt_reader/ to this project's code. Read agents/receipt_reader/README.md and the code that does this job today, then change that call so it uses the agent's run(). Keep the feature flag off and do not run the tests; I will do both.", "quiet": true, "seconds": 21.44, "at": 1791473286.15},
];


/** The plan as the sentence alone drafted it: pass mark 95, Architect's suggestion (no plan file). */
const NO_PASS_REQUEST = "Read a receipt's text and give back the total, subtotal, tax and item count";
const NO_PASS_NEXT = 'add more real cases with /floor add <file>, or lower the bar with /floor <limit>';

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function noPassPlan(): Msg[] {
  return clone(PLAN_MSGS).map((m) => {
    if (m.type === 'turn_started') return { ...m, text: NO_PASS_REQUEST };
    const ev = m.event as Record<string, unknown> | undefined;
    if (ev?.kind === 'plan_card') {
      const rows = (ev.rows as Array<[string, string]>)
        .filter(([k]) => k !== 'spec')
        .map(([k, v]): [string, string] => [k, k === 'floor' ? v.replace('>= 0.40', '>= 0.95') : v]);
      const data: Record<string, unknown> = { ...(ev.data as Record<string, unknown>), bar: 0.95, domain: [0.5, 1] };
      delete data.frozen;
      return { ...m, event: { ...ev, rows, data, footer: 'spec 4d80f3d5. Enter compiles; answer no to edit with /floor, /budget or /model first.' } };
    }
    return m;
  });
}

function noPassBuild(): Msg[] {
  const out: Msg[] = [];
  for (const m of clone(BUILD_MSGS)) {
    const ev = m.event as Record<string, unknown> | undefined;
    if (m.type === 'forge') {
      const data = m.data as Record<string, Record<string, unknown>>;
      for (const key of ['tuned', 'examined']) if (data[key]) data[key].passed = Number(data[key].score) >= 0.95;
      out.push(m);
    } else if (ev?.kind === 'notice' && String(ev.text).startsWith('installed ')) {
      out.push({ ...m, event: { ...ev, level: 'warn', text: `not licensed (primary_floor_failed); next: ${NO_PASS_NEXT}` } });
    } else if (ev?.kind === 'plan_card') {
      const data = ev.data as Record<string, unknown>;
      const rungs = (data.rungs as Array<Record<string, unknown>>).map((r): Record<string, unknown> => ({ ...r, licensed: false, ships: false, limit: 0.95, codes: ['primary_floor_failed'] }));
      const last = rungs.at(-1)!;
      out.push({
        ...m,
        event: {
          ...ev,
          title: 'Result: not licensed',
          rows: [
            ['engineer', 'a tuned extractor preset (Forge off)'],
            ['floor', '0.72/0.95 (correct 72/100 · invalid 0) (primary_floor_failed)'],
            ['ladder', 'Nemotron-3_5-Lightning 39/100 refused · nemotron-3-super-120b-a12b 64/100 refused · Nemotron-3-Ultra-550b-a55b 72/100 refused'],
            ['cost', '$0.31 to build; up to $0.02 and 60 s per run'],
            ['destination', 'nothing installed or exported'],
            ['next', NO_PASS_NEXT],
          ],
          data: { ...data, licensed: false, bar: 0.95, model: last.model, score: last.score, exam: last.counts, rungs },
        },
      });
    } else out.push(m);
  }
  return out;
}

export const DEMO_PLAN: Msg[] = PLAN_MSGS;
export const DEMO_BUILDING: Msg[] = [...PLAN_MSGS, ...BUILDING_MSGS];
export const DEMO_PASSED: Msg[] = [...PLAN_MSGS, ...BUILD_MSGS];
export const DEMO_NO_PASS: Msg[] = [...noPassPlan(), ...noPassBuild()];
export const DEMO_EXPORT_CONFIRM: Msg[] = [...DEMO_PASSED, ...EXPORT_MSGS];
export const DEMO_FILES_WRITTEN: Msg[] = [...DEMO_EXPORT_CONFIRM, ...EXPORTED_MSGS];
export const DEMO_CONNECT_APPROVE: Msg[] = [...DEMO_FILES_WRITTEN, ...CONNECT_MSGS];
export const DEMO_CONNECTED: Msg[] = [...DEMO_CONNECT_APPROVE, ...CONNECTED_MSGS];

/** The export's 13 files, as its plan lists them. */
export const DEMO_FILES = ['.gitattributes', 'EVIDENCE.json', 'EVIDENCE.md', 'MANIFEST.json', 'README.md', '__init__.py', 'artifact/config.json', 'artifact/harness.py', 'artifact/report.json', 'artifact/spec.json', 'schemas/input.schema.json', 'schemas/output.schema.json', 'tests/test_receipt_reader.py'];

/** The receipt Try it ran in the film (TAKES R1: examples/expense-app/tests/test_rules.py "digits-only"). */
export const DEMO_RECEIPT = '1 PAKET NASI AYAM Rp 35.000\n1 TEH MANIS Rp 6.000\nSUBTOTAL Rp 41.000\nTAX Rp 4.100\nTOTAL Rp 45.100\nTUNAI Rp 50.000\nKEMBALI Rp 4.900';

/** Its run as `run_agent` answers it (TAKES R1: the run's own record). */
export const DEMO_RUN = {
  output: { item_count: 2, subtotal: 41000, tax: 4100, total: 45100 },
  cost_usd: 0.0002793,
  seconds: 1.094,
  calls: 1,
  completion_id: 'chatcmpl-a5ca0c4c610c99c7',
};

/**
 * B4's `sample`: the first practice case of the agent's floor, exactly as
 * architect/desktop/data.py `_practice_sample` returns it for the demo workspace
 * (C:/rec/ws13, floor bench-receipts-v2-evidence, the tuning half). Never an exam case.
 */
export const DEMO_SAMPLE = { receipt: '1 SPGTHY BOLOGNASE 58,000\n1 PEPPER AUS 165,000\nWELL DONE\n1 WAGYU RIBEYE 195,000\nMEDIUM WELL\n1 ICED LEMON TEA 22,000\n1 FUSION TEA LYCHE 28,000\n1 NUTTELA BROWNIES 35,000\nSUBTOTAL 503,000\n52,815\nPB1\nSVC CHRG 25,150\nDUE 580,965' };

/** The installed agent as `agent` answers for it: its name, license, schemas (receipt-reader.json) and B4's practice sample. */
export const DEMO_DETAIL = {
  name: 'receipt-reader',
  licensed: true,
  sample: DEMO_SAMPLE,
  spec: {
    task: DEMO_TASK,
    input_schema: { type: 'object', properties: { receipt: { type: 'string' } }, required: ['receipt'], additionalProperties: false },
    output_schema: {
      type: 'object',
      properties: {
        total: { type: ['integer', 'null'] },
        subtotal: { type: ['integer', 'null'] },
        tax: { type: ['integer', 'null'] },
        item_count: { type: 'integer' },
      },
      required: ['total', 'subtotal', 'tax', 'item_count'],
      additionalProperties: false,
    },
  },
};

/**
 * A fixture replayed as a reopened conversation is (store `rebuild`): each message folds
 * at its own `at`, quiet turns stay out of the transcript, and the Forge feed is the same
 * fold the live page runs.
 */
export function demo(msgs: Msg[]): { timeline: Timeline; forge: ForgeFeed } {
  const { timeline, forge } = rebuild(msgs as never, undefined, emptyFeed());
  return { timeline, forge };
}

/** A build conversation at that point, in the demo's project. */
export function demoConv(msgs: Msg[], patch: Partial<Conv> = {}): Conv {
  return {
    id: 'c1', status: 'ready', links: [], ...demo(msgs),
    state: { started: true, state: 'forge', permission: 'ask', model: DEMO_LADDER[1], models: DEMO_LADDER, cwd: DEMO_CWD },
    ...patch,
  };
}
