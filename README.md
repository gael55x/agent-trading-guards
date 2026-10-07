# Agent Trading Guards

An agent can propose a trade or a factor. These two Python tools check the proposal against your account limits or the experiment's declared data before you act on it.

Both tools run locally on Python 3.10 or newer and use only the standard library. They need no installation, API keys, accounts or network access. The examples use constructed data, and nothing here measures trading returns.

## Which tool do you need?

| If you are… | Use | It answers |
|---|---|---|
| letting an agent propose orders for an account | `orderguard.py` | Does this order fit the supplied cash, holdings, pending orders and limits? |
| comparing factors that an agent proposed and backtested | `factoraudit.py` | Do the declared files, hashes and dates keep construction data out of the scoring window? |

The two tools are independent. Neither imports the other, so you can adopt one without the other.

## Get the code

```sh
git clone https://github.com/gael55x/agent-trading-guards.git
cd agent-trading-guards
python3 --version   # 3.10 or newer
```

Run every command below from the repository root.

## Order guard: check before you submit

![Flowchart of the order path. The agent's proposed order, the user's limits, and the account's cash, holdings and pending orders all feed one check. A refusal returns reasons. An admission goes to a single-owner executor, and its simulated fill updates the account state used by the next check. Confidence is attached to the agent's proposal only as metadata.](diagrams/order-path.png)

The agent proposes an order, and the account owner supplies the limits and the current account state. The guard then admits the order or refuses it with reason codes. Only an admitted order should reach your submission code. The next check should use account state refreshed from what actually executed.

**Admit.** The example account has $4,000 cash, $1,000 reserved for a pending buy, a $1,000 cash floor and $5,000 already held. A $2,000 buy lands exactly on the $8,000 position cap:

```sh
python3 orderguard.py check --limits examples/limits.json --snapshot examples/snapshot.json --proposal examples/proposal.json --now 2026-10-09T12:01:00Z
```

The result is `ADMIT` with exit status 0.

**Refuse.** This example raises the limit price from 100 to 100.01:

```sh
python3 orderguard.py check --limits examples/limits.json --snapshot examples/snapshot.json --proposal examples/proposal-too-large.json --now 2026-10-09T12:01:00Z
```

The result is `REFUSE` with exit status 2 and the reasons `CASH_RESERVE` and `POSITION_LIMIT`. Available cash would be 999.8 and exposure would be 8000.2.

**Loop.** This example runs a check, a fill and a second check:

```sh
python3 examples/paper_loop.py
```

It admits the first buy, assumes one full paper fill, updates cash and shares, and then refuses a further $100 buy. It never contacts a broker. For your own loop:

1. Call `decide(limits, snapshot, proposal, now)`.
2. Treat `Malformed` as an input error.
3. Submit only on `ADMIT`.
4. Build the next snapshot from real executions.

The `--now` values are fixed so the examples are reproducible. Omit `--now` when you check a genuinely current snapshot.

### Connecting it to your account

The account owner sets the limits. Supply current cash, holdings, marks and every pending order; the guard cannot verify quotes or notice an omitted order. Confidence stays metadata and never changes a verdict.

Your execution code must serialize checking and submission, handle fees, slippage, fills and cancellations, and refresh the snapshot from actual executions. Two concurrent checks against the same snapshot can both pass. An admission is not a fill or a live safety guarantee.

The tool covers cash accounts, long-only equities and limit-priced orders. Shorts, margin, derivatives, FX and protective orders are outside its model.

Inputs are JSON files with amounts written as decimal strings and timestamps in UTC. Stale snapshots are refused. The exit status is 0 for admit, 2 for refuse and 3 for malformed input. [VALIDATION.md](VALIDATION.md#order-guard-reference) has the full input rules, the exposure arithmetic and the output fields.

## Factor audit: check the experiment before the scores

![Flowchart of the factor audit. Training files and the declared cutoff date feed a check of hashes and training dates. Recorded scores feed a check of the scoring window and then of score dates. The audit then compares recorded trials: the same factor on different valid folds passes, and a repeat of the same declared trial is flagged as a duplicate.](diagrams/visibility-window.png)

When you construct a factor, write a manifest that declares:

- its input files, each with a path, SHA-256 hash and date column;
- its last visibility date;
- which inputs built it;
- which file scored it, and over which window.

The audit hashes the actual bytes and scans the actual CSV dates. It then checks that the construction data end by the visibility date and that scoring falls after it. Run the audit before you compare results.

**Pass.** This manifest records two historical folds of the same opaque factor specification:

```sh
python3 factoraudit.py audit --manifest examples/campaign.json --root examples
```

Both folds pass, with exit status 0. The audit reports `REPEATED_SPEC` as information only, because reusing a specification across legitimate folds is fine.

**Fail.** This manifest declares the 2020 scoring file as a construction input for the first fold:

```sh
python3 factoraudit.py audit --manifest examples/campaign-leak.json --root examples
```

The `INPUT_VISIBILITY` rule fails, with exit status 1. Fix the declaration, or rebuild with the intended data, before you interpret the scores.

`examples/campaign.json` shows every required manifest field. The exit status is 0 if all rules pass, 1 if any rule fails, and 3 for malformed data or an I/O error.

### What a pass means

Only the declared files, hashes and date windows passed. The audit cannot see a model's knowledge, inspect factor code, find omitted trials or test statistical significance. Specifications are hashed and never executed. Audit stable files, then continue with your planned statistical evaluation. [VALIDATION.md](VALIDATION.md#factor-audit-reference) explains the fields, file rules and remaining gaps.

## Evaluation

Run the two independent checks from the repository root:

```sh
python3 check_orderguard.py
python3 check_factoraudit.py
```

| Check | Observed result |
|---|---|
| Order accounting | All 2,000 seeded cases matched an exact-fraction oracle: 395 admitted, 1,605 refused. All 4,000 confidence comparisons and 23 boundary cases passed. |
| Factor audit | All 19 scenarios passed, covering valid folds and injected date, hash, duplicate and path errors. The clean example reports 15 PASS / 0 FAIL / 2 INFO; the leak reports 14 PASS / 1 FAIL / 2 INFO. |

These are constructed-input correctness checks, not measured trading returns or estimates of general accuracy. CI runs both checks, the paper loop and the clean factor example on Python 3.10 and 3.12. [VALIDATION.md](VALIDATION.md) contains the evidence, technical reference and release pin.

## Background

[What LLM Trading Agents Actually Do in Production](https://arxiv.org/abs/2609.05663v1) informs the separation between model proposals and account constraints. [Propose, Don't Judge](https://arxiv.org/abs/2609.27051v1) informs the separation between factor proposals and evaluation. These tools implement narrower checks; they do not reproduce either paper's methods or results.

The earlier [LayeredMemoryTrader](https://github.com/gael55x/LayeredMemoryTrader) supplies workflow context, not a dependency or historical replay. Its decision log lacks the order quantities, prices and account state needed for replay. No upstream code or raw log data is redistributed.

Editable Mermaid sources and rendered images are in [diagrams/](diagrams/). Code and example data are original.

## License

MIT, covering the original code and diagrams in this repository.
