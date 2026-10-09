# Agent Trading Guards

*Two checks to run before you act on an AI trading agent: what its backtest could see, and whether its next order fits your account.*

Say an agent reports that its momentum factor scored well over 2020 and, on the strength of that, proposes buying 20 shares of AAA at $100. Its confidence is 0.9.

That message makes two claims. The score only means something if the factor was built from data that ended before 2020 began. The order is only safe if it fits the account as it stands now, including orders already waiting to fill. The 0.9 tells you nothing about either.

This repository has one check for each claim: `factoraudit.py` for the backtest and `orderguard.py` for the order. Both run on Python 3.10 or newer with only the standard library. The examples use constructed data.

```sh
git clone https://github.com/gael55x/agent-trading-guards.git
cd agent-trading-guards
```

Run everything below from that directory. Start with the score: an order built on a leaked backtest can pass every account limit and still rest on a result that was never real.

## Could the backtest see the year it was scored on?

A backtest builds a factor (here, a rule that ranks stocks by recent price trend) from one period's data, then scores it on a later period it hasn't seen. The score is meaningless if any of the later data got into the building step. You can't tell that from the score. You can tell it from a record of which files the run used, which this repository calls a manifest.

`examples/campaign.json` is a manifest for a constructed run with two folds: the same factor recipe, `factor.json`, built on one year and scored on the next. Here is the first fold, trimmed to the fields that matter:

```json
{
  "candidate_id": "fold-2020",
  "visible_through": "2019-12-31",
  "inputs": ["training"],
  "score_input": "scores",
  "score_window": {"start": "2020-01-02", "end": "2020-12-31"}
}
```

`inputs` lists the files the factor was built from. `visible_through` is the last date it may know. `score_input` is the file of recorded 2020 scores. The second fold does the same a year later. Audit the pair:

```sh
python3 factoraudit.py audit --manifest examples/campaign.json --root examples; echo "exit $?"
```

The report counts **15 PASS, 0 FAIL and 2 INFO**, and the command exits 0. Six of the passes are hash checks, one per CSV and one per fold for `factor.json`, so a file edited after it was recorded would fail. The date checks read the date column inside each CSV, not the file names, and find that `training.csv` ends on 31 December 2019. One INFO note records that both folds use identical recipe bytes. That is expected for a fixed-lookback momentum rule with nothing to fit; if your recipe holds fitted parameters, identical bytes across folds means nothing was refit.

## One extra file, and the 2020 score is hindsight

Now suppose the harness writes the manifest from the files it actually hands over. Asked to build `fold-2020` from `training.csv`, the agent's code also requests `scores.csv`. The harness records it honestly, and `examples/campaign-leak.json` is the result: the fold's `inputs` become `["training", "scores"]`.

```sh
python3 factoraudit.py audit --manifest examples/campaign-leak.json --root examples; echo "exit $?"
```

The count becomes **14 PASS, 1 FAIL and 2 INFO**, and the command exits 1. The failing entry, trimmed:

```json
{
  "rule": "INPUT_VISIBILITY",
  "status": "FAIL",
  "candidate_id": "fold-2020",
  "evidence": {
    "visible_through": "2019-12-31",
    "problems": [
      "scores: max date 2020-12-31 is after visible_through 2019-12-31",
      "scores: scoring input is also a construction input"
    ]
  }
}
```

Both problems point at `scores.csv`. A factor built from it has seen the year it is graded on, so its 2020 score can look excellent for the plainest reason: it had the answers. Nothing about a decision made at the end of 2019 follows from it.

All six hash checks still pass, because no byte changed, and the score-date check passes because those rows are valid *as scores*. Only the file's role is wrong, so an integrity check alone would wave this run through. Deleting the line from the manifest fixes the record, not the factor: rebuild the fold.

![Flowchart of the factor audit. Training files and the declared cutoff date feed a check of hashes and training dates. Recorded scores feed a check of the scoring window and then of score dates. The audit then compares recorded trials: the same factor on different valid folds passes, and a repeat of the same declared trial is flagged as a duplicate.](diagrams/visibility-window.png)

*Construction files are checked against the cutoff and scores against a later window. The same recipe on different folds passes; the same declared trial recorded twice is flagged.*

The audit only works if the harness writes the manifest while the run happens; one rebuilt afterwards by asking the model is just another model answer. Even then, a pass is narrow. It shows the recorded files have the recorded bytes and dates, not that the record is complete, and it never reads the code that made those files. Fit a scaler on the full dataset and export only the training rows, and the CSV keeps clean dates with future values folded in, a trap described in [this r/algotrading discussion](https://www.reddit.com/r/algotrading/comments/1srv5ks/lesson_always_backtest/). A date column also records when a value applies, not when it became public, so a 2019 figure restated in 2020 passes. The [factor audit reference](VALIDATION.md#factor-audit-reference) lists what else stays out of view.

Suppose your own run passes. One kind of leak is ruled out: no recorded construction file runs past the cutoff. That is a prerequisite for trusting the score, not proof of it, and it says nothing about the order the agent builds next. Whether that order can be paid for depends on a different record: the account.

## Does the order fit the account it's about to hit?

The agent's twenty shares at $100 cost $2,000. The paper account in `examples/snapshot.json` holds $4,000 in cash. On the balance alone, the order fits twice over.

The balance leaves something out. A buy for ten AAA at $100 is already waiting to fill, and it has a claim on $1,000 of that cash. The account also holds 50 AAA valued at the current price, its mark, of $100. The owner's limits in `examples/limits.json` keep $1,000 untouched and cap AAA exposure at $8,000, counting dollars held, pending and proposed. Count everything already committed:

- **AAA exposure:** $5,000 held + $1,000 pending + $2,000 proposed = **$8,000**.
- **Cash after reservations:** $4,000 − $1,000 pending − $2,000 proposed = **$1,000**.

The order fits with nothing to spare. Leave the pending buy out of the snapshot and both lines show $1,000 of headroom the account doesn't have. That gap is the subject of [a question on r/IBKR_Official](https://www.reddit.com/r/IBKR_Official/comments/1qavlqr/does_anyone_know_how_i_can_tell_how_much/), where a trader described a buy rejected despite apparently sufficient cash, with another open order reserving the difference.

The guard does this counting from three JSON files: the owner's limits, the account snapshot and the proposal.

```sh
python3 orderguard.py check --limits examples/limits.json --snapshot examples/snapshot.json --proposal examples/proposal.json --now 2026-10-09T12:01:00Z; echo "exit $?"
```

The verdict is `ADMIT`, with exit status 0. The `computed` block repeats the arithmetic: `pending_buy_notional` is `"1000"`, `symbol_exposure_after` is `"8000"` and `cash_after` is `"1000"`. That is before fees, which the guard doesn't model, so set `min_cash_reserve` with commissions in mind.

`--now` pins the check one minute after the snapshot so the example always gives the same answer. Without it, the guard uses the current UTC time and refuses a snapshot older than `max_snapshot_age_s`. The example allows 300 seconds; for an automated loop, set it close to how long a snapshot fetch actually takes.

## One cent over the line

`examples/proposal-too-large.json` is the same order under a new ID, with the limit price raised from `"100"` to `"100.01"`.

```sh
python3 orderguard.py check --limits examples/limits.json --snapshot examples/snapshot.json --proposal examples/proposal-too-large.json --now 2026-10-09T12:01:00Z; echo "exit $?"
```

The extra cent per share adds twenty cents to the order. Exposure becomes $8,000.20 and cash after reservations $999.80. The guard returns `REFUSE`, exits with status 2 and names both reasons: `CASH_RESERVE` and `POSITION_LIMIT`. Twenty cents over gets the same answer as a thousand dollars over.

The agent's confidence is copied into the report as `confidence_ignored: "0.9"`, and no rule reads it. At 0.99 the verdict would be the same.

A breach this small only shows up if the amounts are exact. Every amount goes in as a decimal string such as `"100.01"`, and the guard rejects a JSON number in its place. Have the model write strings directly: a float that was rounded on the way in can't be turned back into the price you meant.

## An admitted order hasn't filled yet

`ADMIT` moves no cash and adds no shares. Only a fill does, and the next check needs the account as the fill left it. The paper loop admits the original order, assumes it fills in full at $100 and checks one more proposal against the new state:

```sh
python3 examples/paper_loop.py
```

Cash drops to $2,000 and holdings rise to 70 shares; the older ten-share buy is still pending, so its $1,000 stays reserved. The next proposal is a single share at $100: exposure would be $7,000 + $1,000 + $100 = $8,100, and cash $2,000 − $1,000 − $100 = $900. The `next` report refuses it for the same two reasons as the twenty-cent breach.

A real order can sit unfilled, fill in part or be cancelled, so build the next snapshot from execution reports, never from the fact that a proposal passed.

## Both checks in the agent's loop

Run the audit once per campaign, before you rank scores. Run the guard inside the executor, as the last call before every submission. Don't hand it to the agent as a tool it could skip.

![Flowchart of the order path. The agent's proposed order, the owner's limits, and the account's cash, holdings and pending orders feed one check. A refusal returns reasons. An admission goes to a single-owner executor, whose simulated fill updates the account state used by the next check. Confidence travels with the proposal only as metadata.](diagrams/order-path.png)

*The model contributes only the proposal. Limits come from the account owner and state from execution records; the verdict decides whether the executor sees the order at all.*

The factor side has the same split: the agent proposes, the harness writes the manifest, the audit reads the files. [What LLM Trading Agents Actually Do in Production](https://arxiv.org/abs/2609.05663v1), an observational study of two agent fleets, argues that fixes belong in the order path rather than in the prompt; here, that means a prompt change can't move a cash reserve or position cap. [Propose, Don't Judge](https://arxiv.org/abs/2609.27051v1) keeps the agent that mines factors apart from the statistical referee that judges them; the audit borrows that separation, not the tests.

In Python the two gates are two calls. Save this in the repository root and run it there:

```python
import json
from factoraudit import Malformed as BadCampaign, audit
from orderguard import Malformed as BadOrderInput, decide

def load(name):
    with open(f"examples/{name}.json") as f:
        return json.load(f)

# Gate 1, once per campaign, before ranking scores.
try:
    report = audit("examples/campaign.json", "examples")
except BadCampaign as error:
    raise SystemExit(f"cannot audit campaign: {error}")
if report["overall"] != "PASS":
    raise SystemExit("fix the campaign before ranking its scores")

# Gate 2, every proposal, as the last call before submission.
# The example snapshot is dated 12:00Z, so the clock is pinned here.
# In a live loop, fetch the snapshot right before this call and use real time:
#   now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
now = "2026-10-09T12:01:00Z"
proposal = load("proposal")
try:
    decision = decide(load("limits"), load("snapshot"), proposal, now)
except BadOrderInput as error:
    raise SystemExit(f"cannot check order: {error}")
if decision["verdict"] == "ADMIT":
    print("submit", proposal["proposal_id"])  # your executor; reuse the ID as the client order ID
else:
    print("refused", decision["reasons"])     # log the reasons and the input hashes
```

It prints `submit original-paper-1`; pointed at `campaign-leak.json`, it stops before any order is considered.

An `ADMIT` keeps the account inside your limits only if every order goes through the guard, the snapshot is current, and `pending` lists every order sent but not yet filled or cancelled, including ones the broker hasn't acknowledged. Two agents checked against one snapshot can both pass and breach a limit together, so give one execution owner the account while it fetches state, checks and submits. The guard has no lock and no broker connection.

Caps are tested only when a buy is proposed, so a rising mark can carry a position past its cap. Sells skip the order-size, position, gross and cash-reserve limits on purpose, because a guard that refuses sells can trap you above your own limit. The guard also doesn't compare a limit price with the mark or check tick sizes, so a sell at a mistyped price is admitted; validate prices before the call. It is built for long-only cash equities with limit orders: a sell larger than your unpromised shares is refused, a proposal without a limit price or with an extra field such as `stop_loss` is rejected as malformed, and nothing checks what kind of instrument a listed symbol is. The [order guard reference](VALIDATION.md#order-guard-reference) and the `orderguard.py` docstring cover the remaining rules and reason codes.

## What the checks establish

Five chosen examples show the behavior, not that it holds across the inputs you will send, so each tool has an independent check:

```sh
python3 check_orderguard.py
python3 check_factoraudit.py
```

`check_orderguard.py` runs 2,000 seeded buy cases through a separate implementation that uses exact fractions. Every verdict, reason and all five computed amounts matched (395 admitted, 1,605 refused), forcing confidence to 0 or 1 changed nothing, and 23 boundary and malformed-input cases behaved as specified. `check_factoraudit.py` runs 19 campaigns, a clean pair of folds plus injected future rows, overlapping windows, changed bytes, duplicate trials, escaping paths and broken CSVs; all 19 produced the expected result. These are correctness checks on constructed inputs, not error rates on real orders or trading returns. [VALIDATION.md](VALIDATION.md) describes the cases.

## Try it on your agent's last run

Go back to the agent's message: a good score and a confident order. For the score, have your harness log, for each candidate, every file it handed over, the cutoff date and the scoring window. Copy `examples/campaign.json`, fill it in from that log and hash each file with `shasum -a 256 FILE` on macOS or `sha256sum FILE` on Linux. One candidate is enough to start.

```sh
python3 factoraudit.py audit --manifest path/to/your-campaign.json --root path/to/campaign-files; echo "exit $?"
```

For the order, copy `examples/snapshot.json` and fill it with your paper account's settled cash before any open-order holds, its holdings and current marks, and every order you have sent that hasn't filled or been cancelled. Write every amount as a quoted string and set `as_of` to when you read the account. Copy `examples/limits.json` with the limits you want, shape the agent's last order like `examples/proposal.json`, and run the check within `max_snapshot_age_s` of `as_of`:

```sh
python3 orderguard.py check --limits path/to/limits.json --snapshot path/to/snapshot.json --proposal path/to/proposal.json; echo "exit $?"
```

Read the evidence and computed amounts before you wire either check into anything. In the examples, every file in the leaked run was intact and only its role was wrong. The $2,000 order looked affordable until the pending buy was counted.

## Background

The propose-then-execute shape comes from the earlier [LayeredMemoryTrader](https://github.com/gael55x/LayeredMemoryTrader); none of its code or logs is used here. Editable Mermaid sources for both diagrams are in [diagrams/](diagrams/).

## License

MIT, covering the original code and diagrams in this repository.
