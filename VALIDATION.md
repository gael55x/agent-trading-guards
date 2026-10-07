# Validation and technical reference

This file holds the detailed input rules, interfaces and test evidence behind the README. All checks use constructed inputs, and none measures trading returns.

## Order guard reference

The complete schema and reason codes are in the [orderguard.py](orderguard.py) module docstring. The three JSON files in `examples/` (`limits.json`, `snapshot.json`, `proposal.json`) are the smallest working inputs.

### Input rules

- The account owner sets the limits. The agent proposes an order but never supplies its own permission limits.
- Amounts are decimal strings with up to 20 integer digits and 12 fractional digits.
- The guard rejects JSON numbers, negative values, exponents, nonfinite values, unknown fields and duplicate JSON keys.
- Timestamps use the format `YYYY-MM-DDTHH:MM:SSZ` in UTC. A snapshot dated after the check time is malformed. A snapshot older than the age limit is refused.
- Confidence, when present, must be a decimal string in [0, 1]. It is metadata only and never changes a verdict or calculation.
- Duplicate-key rejection applies to the CLI loader. Python callers must reject duplicate keys in their own JSON parser before passing dictionaries to `decide`.

### Accounting

- Exposure is the held quantity times the supplied mark, plus pending buys at their supplied limit prices, plus the proposed buy.
- Pending sells do not free cash and do not reduce counted exposure.
- Sellable shares are the held quantity minus pending sells.
- Buys check every held or pending symbol against its configured cap; an over-limit position in another symbol also refuses a buy. Unknown held/pending symbols are refused. Pending sells exceeding held shares refuse a buy as inconsistent state.
- Sells skip the buy-only cash and exposure caps, so a known over-limit position can still be reduced. Freshness and known-symbol requirements still apply to sells.
- Shorts, margin, derivatives, FX conversion, fees, slippage and automatic protective orders are not modeled.

### Interfaces

- CLI: `python3 orderguard.py check --limits FILE --snapshot FILE --proposal FILE [--now TIMESTAMP]`. The exit status is 0 for admission, 2 for refusal and 3 for malformed input.
- Python: `decide(limits, snapshot, proposal, now)` returns a report dictionary and raises `Malformed` for invalid input.
- Report contents: verdict, reason codes, exact computed amounts, canonical input hashes, the ignored confidence value and the guard version.
- `examples/paper_loop.py` is the runnable integration example. It reads `verdict`, `reasons` and `computed['order_notional']`.

### Integration responsibilities

- Supply validated, current account state that includes every pending order.
- Run checking and submission in sequence under a single owner.
- Submit only on `ADMIT`. On `Malformed`, stop and fix the input. Record every refusal.
- Never update holdings because an order was admitted; update them only from actual executions. Put the unfilled remainder of an order in the next snapshot's `pending` list. Refresh the authoritative state after fills, cancellations and partial fills.
- The guard cannot verify quotes, prices after the check, missing pending orders, account ownership, concurrent submissions or live execution. An admission describes only the supplied state and prices. A static check is not a live trading safety guarantee.

## Factor audit reference

### Manifest fields

- Each input declares an ID, a relative path, an expected SHA-256 hash and a CSV date column.
- Each candidate declares:
  - an ID and a family;
  - an opaque specification path and its hash;
  - the last simulated visibility date;
  - the IDs of its construction inputs;
  - the ID of its scoring input;
  - the start and end dates of its scoring window.
- The optional `holdout_start` field adds two constraints: the visibility date must come before the holdout, and scoring must not start before it.
- `examples/campaign.json` shows every required field.

### Rules

The audit reports:

- changed files;
- construction dates after the declared visibility date;
- overlapping visibility and scoring windows;
- scoring rows outside the declared window;
- repeated candidate IDs;
- identical declared trials under different IDs.

Trial identity uses the actual hashes of the specification, construction and scoring files, plus the declared dates, so renaming an input ID cannot hide an identical trial. `REPEATED_SPEC` and all other `INFO` entries never cause a failure.

### File handling and limits

- Files must resolve under `--root`. Path traversal and symlinks that escape the root are rejected.
- Specifications are hashed as opaque bytes and never evaluated.
- CSV files must be UTF-8 without a byte order mark, with unique nonempty headers, consistent row width, nonempty data and valid calendar dates.
- Referenced files must be regular files. Unknown fields, duplicate JSON keys, malformed IDs, absolute paths, `..` segments and undeclared input IDs are rejected. The complete schema is documented in [factoraudit.py](factoraudit.py).
- Size limits:
  - 1 MiB for the manifest;
  - 10 MiB per referenced file by default, adjustable with `--max-bytes` up to 100 MiB;
  - 100 MiB in total across unique referenced files;
  - 1,000 inputs;
  - 10,000 candidate entries.
- Audit files only while they are not changing. The audit is not a secure snapshot against malicious or concurrent changes to the filesystem.

### Interfaces

- CLI: `python3 factoraudit.py audit --manifest FILE --root DIR [--max-bytes N]`. The exit status is 0 if all checks in scope pass, 1 if at least one rule fails, and 3 for malformed data or an I/O error.
- Python: `audit(manifest_path, root, max_bytes=10*1024*1024)` raises `Malformed` for invalid input.
- Each report contains evidence for each rule, the recorded candidate counts and an explicit scope.

### What a pass cannot establish

A pass cannot establish:

- what a model actually knew;
- data availability delays or point-in-time correctness;
- what a factor means;
- market-day coverage;
- unrecorded retries or a complete trial denominator;
- trusted timestamps;
- statistical significance or control of false discoveries;
- profitability.

This is a manifest audit, not an implementation of an anytime-valid statistical referee.

## Validation evidence

### Order guard

A separate oracle built on `fractions.Fraction` compares reasons and five accounting outputs against 2,000 seeded valid buy cases: 395 admissions and 1,605 refusals. Another 4,000 comparisons verify that confidence values of 0 and 1 change neither the decisions nor the calculations. The inputs are not mutated. The seed is 20261009.

The 23 explicit boundary cases cover:

- exact cash, position and age limits, and a price one step above the caps;
- stale state and future state;
- reducing an over-limit position by selling;
- pending sell reservations, with no cash credit for pending sales;
- unknown exposure, inconsistent pending sells and caps on other symbols;
- exact decimal arithmetic;
- malformed amounts, fields and confidence values;
- CLI output and exit codes;
- duplicate JSON keys and nonregular files.

The paper loop admits a $2,000 buy and explicitly assumes one full fill at the supplied price. It updates cash and shares, and then refuses the next $100 buy. Pending buy reservations remain outstanding. It is an integration example, not a replay of historical trading decisions or a live execution test.

### Factor audit

Nineteen CLI scenarios built from original files cover:

- two valid folds;
- construction data from the future;
- overlapping visibility and scoring dates;
- score rows outside the window;
- repeated exact trials and repeated candidate IDs;
- renamed input aliases;
- changed bytes;
- paths and symlinks that escape the root;
- invalid dates, missing or duplicate headers, row-width errors and empty data;
- nonregular files and byte caps;
- unknown scoring IDs and duplicate JSON keys.

Reusing the same specification across legitimate folds passes, with an information entry only.

## Maintenance

Each tool has one runnable check script that acts as its regression gate. After you change arithmetic, schemas, file handling or rules, rerun that tool's check and its working example. CI runs both checks, the paper loop and the clean factor example on Python 3.10 and 3.12.

A release also requires a clean checkout and passing GitHub Actions for the exact release revision. A successful local run does not establish the remote CI status.

## What is distributed

This repository distributes only original code, original sample data and original diagrams. It contains no upstream source code and no raw decision-log data. The `diagrams/` directory holds the editable Mermaid sources, the rendered PNG and SVG files, and a manifest with captions, alt text and hashes.

## Reproducing the released tool

The reviewed tool release is [v1.0.0](https://github.com/gael55x/agent-trading-guards/releases/tag/v1.0.0), pinned to `c37ec77eb8587bccd454a91924b6fbef4570dba2`. Use `git checkout v1.0.0` after cloning to reproduce that version. Documentation on `main` may improve without changing the release. The [release CI run](https://github.com/gael55x/agent-trading-guards/actions/runs/37467983959) passed on Python 3.10 and 3.12.
