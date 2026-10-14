# Phase 12 live build (2026-09-30, Windows, claude CLI)

Conductor claude-sonnet-5, play model claude-haiku-4-5, Forge agent claude-sonnet-5, all through the logged-in claude CLI. Costs are API-equivalent estimates, not a bill. The floor is SYNTHETIC (12 drafted cases, 6 held out): it shows agreement with drafted labels, not production quality.

```
> /make an agent that extracts the invoice number, invoice date and total amount from the plain text of an invoice
* make(an agent that extracts the invoice number, invoice date and total amount from the plain text of an i)
  ... interview: reading the request with claude-haiku-4-5-20251001 ...
What will the agent be given on each run? [the text described in the request] 
Which output fields should it return? [the fields named in the request] 
What counts as a correct output? [every output field matches the answer key exactly] 
  ... stage 0 infer spec: one call to claude-haiku-4-5-20251001 ...
== Plan: extracts-invoice-number ==
  task          Extract invoice number, invoice date, and total amount from plain text invoice content
  engineer      Forge (claude-sonnet-5) writes a custom harness; baseline to beat: extractor
  inputs        invoice_text
  outputs       invoice_date, invoice_number, total_amount
  floor         held_out_tasks >= 0.95 on 'invoice_extraction_test_set'
  floor data    configured floor 'invoice_extraction_test_set': 12 cases, 12 SYNTHETIC (6 tuning / 6 held-out)
  budget        $0.69 and 2 min per run; compile up to $5.00 and 30 min
  forge budget  up to $5.00 and 60 min on top of the compile budget (6 submissions, 24 measures, 3 sub-harnesses)
  model         claude-haiku-4-5-20251001
  install as    extracts-invoice-number (installed agent)
  note          per-run budget raised to $0.69 so two calls of the model fit and 2 min so two calls of the model can finish
spec 697b376f. Enter compiles; answer no to edit with /floor, /budget or /model first.
Compile this agent? [yes] 
  [compiler] compiling extracts-invoice-number (spec 697b376f)
  ... stage 1 select preset: 2 calls, $0.05
  ... stage 2 tune: 2 calls, $0.05
  ... stage 3 forge: 14 calls, $0.08
  [forge] 1 submissions, 1 measures, 0 sub-harnesses, agent $0.85
  ... stage 4 verify: 45 calls, $0.95
installed extracts-invoice-number v1: floor 1.00/0.95 (SYNTHETIC floor), $0.97 spent. a custom harness written by Forge (408 nodes, 3203 bytes, 0 licensed sub-harnesses) after 1 submissions and 1 measures; it outranked the tuned extractor baseline on tuning
  -> {"licensed": true, "installed": "extracts-invoice-number", "revision": 1, "floor": "1.00/0.95", "hel...
The agent **extracts-invoice-number** is built and installed (revision 1).

- Extracts: invoice number, invoice date, total amount
- Floor: 1.00/0.95, tested against 6 held-out cases — passed
- Model: claude-haiku-4-5
- Custom harness by Forge (408 nodes) that outranked the tuned extractor baseline
- Cost to build: $0.966

Run it with:
```
/run extracts-invoice-number <input>
```
> 
> 
> 
> 
> /agents
Installed agents
  extracts-invoice-number v1  licensed  floor 1.00/0.95  extractor on claude-haiku-4-5-20251001
> /exit
exit 0
```

The installed harness (Forge's own docstring, shown by `/report`):

```
Invoice field extractor: a single concise call asks the model for invoice_number,
invoice_date and total_amount as JSON, instructing it to copy the date and total
verbatim from the source text and to strip a leading '#' from the invoice number.
Code then checks each field appears exactly in the invoice text (with the '#'
allowance for the number); only fields that fail this substring check trigger one
targeted repair call, keeping cost low while catching hallucinated or reformatted
values.
```

Two unseen invoices, run with `/run`:

```
> /run extracts-invoice-number {"invoice_text": "ACME Supplies Ltd\nINVOICE #INV-88213\nIssued: 14 March 2026\n...\nTOTAL DUE: EUR 42.50\n..."}
{"invoice_date": "14 March 2026", "invoice_number": "INV-88213", "total_amount": "EUR 42.50"}
extracts-invoice-number: $0.0026
> /run extracts-invoice-number Northwind Traders LLC -- Invoice No. NW-2026-0457 -- Date: 2026-02-03 -- Subtotal 1,200.00 -- VAT 20% 240.00 -- Grand total: $1,440.00
{"invoice_date": "2026-02-03", "invoice_number": "NW-2026-0457", "total_amount": "$1,440.00"}
extracts-invoice-number: $0.0031
```

An earlier attempt with the same request was not licensed: the interview default of
"3 seconds per run" made every held-out run exceed its time budget (floor 0.00/0.95,
primary_budget_failed). Build mode now lifts inferred per-run seconds to fit two calls of
the slowest bound model.
