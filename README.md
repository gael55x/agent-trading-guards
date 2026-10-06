# Agent Trading Guards

Two local Python tools for a trading research workflow: check a proposed cash-equity order against hard limits, and audit the files and dates declared in a factor experiment. Python 3.10 or newer; standard library only. No API keys, accounts, broker connections or package installation.

Original implementation led by Claude Opus 5.5, independently reviewed and tested by Codex. All supplied examples are constructed data. No trading returns were measured.

## Try an order in five minutes

From this directory:

```sh
python3 orderguard.py check --limits examples/limits.json --snapshot examples/snapshot.json --proposal examples/proposal.json --now 2026-10-09T12:01:00Z
```

The example has $4,000 cash, $1,000 reserved for a pending buy, a $1,000 cash floor and $5,000 already held. A proposed $2,000 buy exactly reaches the $8,000 position cap and leaves $1,000 available after pending reservations. The verdict is `ADMIT` and exit status is 0.

Increase the limit price from 100 to 100.01:

```sh
python3 orderguard.py check --limits examples/limits.json --snapshot examples/snapshot.json --proposal examples/proposal-too-large.json --now 2026-10-09T12:01:00Z
```

The verdict is `REFUSE`, exit status 2, with `CASH_RESERVE` and `POSITION_LIMIT`: available cash is 999.8 and exposure is 8000.2. Confidence remains metadata in both cases.

```sh
python3 examples/paper_loop.py
```

This runnable integration example checks the first order, assumes one full paper fill, updates the book, and refuses the next buy. It never contacts a broker. The frozen `--now` values make these examples reproducible; omit `--now` when checking a genuinely current supplied snapshot.

## Put the order check before submission

```python
from orderguard import Malformed, decide

# Account owner supplies validated current state and serializes check/submit.
try:
    report = decide(limits, snapshot, proposal, now_utc_string)
except Malformed as error:
    stop_with_input_error(str(error))
else:
    if report['verdict'] == 'ADMIT':
        submit_using_account_owner(proposal)
    else:
        record_refusal(report)
```

`stop_with_input_error`, `submit_using_account_owner` and `record_refusal` are your integration boundaries, not functions shipped here. Do not update holdings because a proposal passed: use actual execution outcomes. An unfilled order belongs in the next snapshot's `pending` remainders. Refresh authoritative state after fills, cancellations and partial fills.

Limits belong to the account owner. The model proposes an order; it does not supply its own permission limits. Amounts are **decimal strings**, up to 20 integer and 12 fractional digits. JSON numbers, negative values, exponents, nonfinite values, unknown fields and duplicate JSON keys are rejected. Timestamps use `YYYY-MM-DDTHH:MM:SSZ` in UTC; future snapshots are malformed and old snapshots are refused.

Exposure is held quantity times supplied mark, plus pending buys at their supplied limits, plus the proposed buy. Pending sells neither free cash nor reduce counted exposure. Sellable shares are held quantity minus pending sells. Sells bypass buy-only cash/exposure caps so a known over-limit position can be reduced; freshness and known-symbol requirements still apply. Shorts, margin, derivatives, FX conversion, fees, slippage and automatic protective orders are outside this tool. Include their costs in an appropriate upstream model before adopting a broader execution workflow.

An admission only describes supplied state and prices. This tool cannot verify quotes, prices after the check, missing pending orders, account ownership, concurrent submissions, or live execution. The caller must serialize checking and submission. A static check is not a live trading safety guarantee.

The JSON output includes reason codes, exact computed amounts, canonical input hashes, ignored confidence and guard version. The complete schema and reason codes are in the module docstring; the three example JSON files are the smallest working inputs. CLI exits: 0 admission, 2 refusal, 3 malformed input.

## Audit a factor experiment

```sh
python3 factoraudit.py audit --manifest examples/campaign.json --root examples
```

The example records two legitimate historical folds using the same opaque factor specification. Training data end on the declared visibility date; recorded score dates fall in a later window. Both folds pass. `REPEATED_SPEC` is informational and does not reject legitimate reuse.

```sh
python3 factoraudit.py audit --manifest examples/campaign-leak.json --root examples
```

This deliberately adds the 2020 scoring file to the first fold's declared construction inputs. It fails `INPUT_VISIBILITY` and exits 1. Fix the data declaration or rebuild the experiment using the intended construction data before interpreting the scores.

Create a manifest when constructing a factor, then audit it before comparing experiment results. Each input declares an ID, relative path, expected SHA-256 and CSV date column. Each candidate records its ID and family, opaque specification path/hash, last simulated visibility date, construction input IDs, scoring input ID, and start/end scoring dates. `examples/campaign.json` shows every required field. Optional `holdout_start` adds the constraint that visibility precedes the holdout and scoring does not start before it.

The tool hashes actual bytes and scans actual CSV dates. It reports changed files, construction dates after declared visibility, overlapping windows, scoring rows outside the declared window, repeated candidate IDs and identical declared trials under different IDs. Trial identity uses actual specification, construction and scoring file hashes plus declared dates, so renaming an input ID cannot hide an identical trial.

Files must resolve under `--root`; traversal and escaping symlinks are rejected. Specs are hashed as opaque bytes and never evaluated. CSVs require unique nonempty headers, consistent row width, nonempty data and calendar dates. Limits are 1 MiB for the manifest, 10 MiB per referenced file by default, 100 MiB total unique referenced bytes, 1,000 inputs and 10,000 candidate entries. `--max-bytes` may change the per-file limit up to 100 MiB. Audit files while they are stable; this is not a secure snapshot against malicious or concurrent filesystem changes.

CLI exits: 0 scoped checks pass, 1 at least one rule fails, 3 malformed data or I/O error. `INFO` entries never fail. Reports contain evidence for each rule, recorded candidate counts and explicit scope. The Python API is `audit(manifest_path, root, max_bytes=10*1024*1024)` and raises `Malformed` for invalid input.

Passing cannot establish what a model actually knew, data availability delays, point-in-time correctness, semantics of a factor, market-day coverage, unrecorded retries, a complete trial denominator, trusted timestamps, statistical significance, false discovery control or profitability. This is a manifest audit, not an implementation of an anytime-valid statistical referee.

## Run the independent checks

```sh
python3 check_orderguard.py
python3 check_factoraudit.py
```

The order check uses a separate exact rational accounting oracle for 2,000 seeded one-symbol cases, 4,000 confidence comparisons, and explicit multi-symbol, sell, freshness, decimal and malformed-input cases. The factor check uses original files with deliberately injected date, hash, duplicate and path errors, including a passing pair of historical folds. CI runs both checks and runnable examples on Python 3.10 and 3.12.

These checks establish behavior on the supplied cases. They do not reproduce a research paper's full evaluation or measure market performance. `VALIDATION.md` records the acceptance evidence and remaining integration responsibilities.

## Research and prior workflow

The practical separation of model proposals from account constraints is motivated by [What LLM Trading Agents Actually Do in Production](https://arxiv.org/abs/2609.05663v1). Separating factor proposals from a declared evaluation boundary is motivated by [Propose, Don't Judge](https://arxiv.org/abs/2609.27051v1). These are original tools with narrower scopes than those studies; no paper implementation or reported trading results are claimed.

The related [Layered Memory Trader](https://github.com/gael55x/LayeredMemoryTrader/blob/82ffbc6fc3306644ba40e871fdadf0b74d379e32/trader.py) is context for the workflow, not a dependency. Its public decision log lacks the quantities, prices and account state needed for exact execution replay. No upstream source or raw decision-log data are redistributed here.

Editable Mermaid sources and rendered PNG/SVG diagrams are in `diagrams/`, with captions, alt text and hashes in its manifest. MIT license covers the original code and diagrams in this repository.
