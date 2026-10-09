# Agent Trading Guards

*Two checks to run before you act on an AI trading agent: what its backtest could see, and whether its next order fits your account.*

Picture an agent that reports a strong 2020 score for a momentum factor and, on the strength of it, proposes buying 20 shares of AAA at $100. Its confidence is 0.9.

That message carries two claims, and the agent can't verify either one for you. The score is only worth acting on if the factor was built from data that ended before 2020 began. The order is only safe to send if it fits the account as it stands right now, including orders already waiting to fill. An agent that is wrong about either sounds exactly as confident as one that is right.

This repository has one small check for each claim. `factoraudit.py` reads the record of a factor run and tests the actual files and dates against the cutoff you declared. `orderguard.py` tests a proposed order against limits you set and account state you supply. Both run locally on Python 3.10 or newer with only the standard library, so there is nothing to install and no account, API key or network connection. The examples use constructed data.

```sh
git clone https://github.com/gael55x/agent-trading-guards.git
cd agent-trading-guards
```

Run everything below from that directory. Start with the score: an order built on a leaked backtest can pass every account limit and still rest on a result that was never real.

## Could the backtest see the year it was scored on?

A score can't tell you which files built the factor. A record of the run can, and `factoraudit.py` checks that record against the files themselves: their bytes, their dates and the role each one was declared to play.

`examples/campaign.json` records two constructed walk-forward folds sharing one factor specification, `factor.json`. `fold-2020` is built from the `training` file, may see nothing after 31 December 2019 (its `visible_through` date) and is scored over 2020. `fold-2021` shifts every date forward a year.

```sh
python3 factoraudit.py audit --manifest examples/campaign.json --root examples; echo "exit $?"
```

The JSON report counts **15 PASS, 0 FAIL and 2 INFO**, overall `PASS`, exit status 0. Six of those passes are hash checks, one for each of the four CSVs and one for the spec as each fold declares it, so a file edited after it was recorded fails here.

The date checks read the declared date column inside each CSV. Naming a file `training_2019.csv` earns it nothing; the audit finds the earliest and latest rows and compares them with the fold's cutoff and scoring window.

The two INFO entries are notes, not warnings: reusing one specification across folds is normal walk-forward practice, and the audit counts only the trials the manifest lists.

## One extra input, and the 2020 score stops meaning anything

`examples/campaign-leak.json` makes one edit. The construction inputs for `fold-2020` go from `["training"]` to `["training", "scores"]`, so the file that scores 2020 is now also declared as material the factor was built from.

```sh
python3 factoraudit.py audit --manifest examples/campaign-leak.json --root examples; echo "exit $?"
```

The count becomes **14 PASS, 1 FAIL and 2 INFO**, with exit status 1. The failing entry, trimmed to the fields that matter, names the fold and the file:

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

Both problems point at `scores.csv`. Its last row sits a year past the cutoff, and it now plays both roles at once. A factor built from those rows has seen the period it is graded on, so its 2020 score says nothing about a decision made at the end of 2019.

All six hash checks still pass. No byte changed; the file's declared role did. An integrity check alone would wave this campaign through, and the score-date check passes as well, since the same rows are perfectly valid *as scoring data*. The failure sends you back to the experiment: correct the record if it is wrong, or rebuild the candidate if it really consumed the scoring file. Deleting the entry from the manifest does not undo its use.

Everything here rests on the manifest being honest, so the harness that hands files to the model should write it during the run, before anyone looks at scores. A manifest rebuilt afterward by asking the model which files it used is one more model answer, and the audit would inherit its reliability.

![Flowchart of the factor audit. Training files and the declared cutoff date feed a check of hashes and training dates. Recorded scores feed a check of the scoring window and then of score dates. The audit then compares recorded trials: the same factor on different valid folds passes, and a repeat of the same declared trial is flagged as a duplicate.](diagrams/visibility-window.png)

*Construction files are checked against the declared cutoff and scores against a later window; reusing a factor across folds passes, repeating a declared trial is flagged.*

A pass is narrower than the word suggests. Matching hashes prove the files equal the recorded bytes, not that the record lists everything the model saw. The audit also reads exported files, never the code that produced them. Fit a scaler on the full dataset, export only the training rows, and the CSV keeps clean dates with future values already folded in, a risk raised in [this r/algotrading discussion](https://www.reddit.com/r/algotrading/comments/1srv5ks/lesson_always_backtest/). Vendor publication delays, pretraining knowledge and trials left off the manifest are just as invisible; the [factor audit reference](VALIDATION.md#factor-audit-reference) lists the rest.

Suppose your own campaign passes. You now know its recorded inputs respect the dates you intended, which is enough to take the score seriously. The report says nothing about the order the agent builds from it. Whether that order can be paid for depends on a different record: the account.

## Does the order fit the account it's about to hit?

The agent's twenty shares at $100 cost $2,000. The paper account in `examples/snapshot.json` holds $4,000 in cash. On the balance alone, the order fits twice over.

The balance leaves something out. A buy for ten AAA at $100 is already waiting to fill, and it has a claim on $1,000 of that cash. The account also holds 50 AAA marked at $100, and the owner's limits in `examples/limits.json` keep $1,000 untouched and cap AAA exposure at $8,000. Count everything the account has already committed:

- **AAA exposure:** $5,000 held + $1,000 pending + $2,000 proposed = **$8,000**.
- **Cash after reservations:** $4,000 − $1,000 pending − $2,000 proposed = **$1,000**.

The order fits with nothing to spare. Leave the pending buy out of the snapshot and both lines show $1,000 of headroom the account doesn't have. That gap is the subject of [a question on r/IBKR_Official](https://www.reddit.com/r/IBKR_Official/comments/1qavlqr/does_anyone_know_how_i_can_tell_how_much/), where a trader described a buy rejected despite apparently sufficient cash, with another open order reserving the difference.

The order guard does this counting from three JSON files: the owner's limits, the account snapshot and the proposal. Check the agent's order:

```sh
python3 orderguard.py check --limits examples/limits.json --snapshot examples/snapshot.json --proposal examples/proposal.json --now 2026-10-09T12:01:00Z; echo "exit $?"
```

The verdict is `ADMIT`, with exit status 0. The `computed` block repeats the arithmetic above: `pending_buy_notional` is `"1000"`, `symbol_exposure_after` is `"8000"` and `cash_after` is `"1000"`. The $3,000 single-order cap and the $20,000 gross cap also hold, with room to spare. Exposure and cash have none: both sit exactly on their limits.

`--now` pins the check one minute after the snapshot was taken, so the example gives the same answer whenever you run it. With a real snapshot, omit it: the guard uses the current UTC time and refuses any snapshot older than the `max_snapshot_age_s` you set, 300 seconds in the example limits.

## One cent over the line

`examples/proposal-too-large.json` is the same order under a new ID, with the limit price raised from `"100"` to `"100.01"`. Still twenty shares, still confidence 0.9.

```sh
python3 orderguard.py check --limits examples/limits.json --snapshot examples/snapshot.json --proposal examples/proposal-too-large.json --now 2026-10-09T12:01:00Z; echo "exit $?"
```

The cent per share adds twenty cents to the order. Order notional becomes $2,000.20, AAA exposure $8,000.20 and cash after reservations $999.80. The guard returns `REFUSE`, exits with status 2 and names both reasons: `CASH_RESERVE` and `POSITION_LIMIT`.

No tolerance band absorbs the breach. A cap you set at $8,000 is treated as $8,000, and twenty cents over gets the same answer as a thousand dollars over.

The agent's 0.9 appears in this report too, copied as `confidence_ignored: "0.9"`. No rule reads it. Raising it to 0.99 would leave the verdict where it is, because a surer model doesn't add cash to the account.

A breach this small is only visible if the amounts are exact. Every amount goes in as a decimal string such as `"100.01"`, and the guard rejects a JSON number in its place. Have the model emit strings directly: a float rounded on the way in cannot be turned back into the price you meant.

## An admitted order hasn't filled yet

Go back to the twenty shares at $100 that passed. `ADMIT` is a statement about the snapshot you supplied. It moves no cash and adds no shares; only a fill does that, and the next check needs the account as the fill left it.

The paper loop plays this out. It checks the original order, assumes it fills in full at $100, builds the next snapshot and checks one more proposal against it:

```sh
python3 examples/paper_loop.py
```

In `assumed_fill_snapshot`, cash has dropped from $4,000 to $2,000 and holdings have risen from 50 to 70 shares. The older ten-share buy is still listed under `pending`, so its $1,000 stays reserved.

The next proposal is a single share at $100. Exposure would be $7,000 + $1,000 + $100 = $8,100, and cash after reservations $2,000 − $1,000 − $100 = $900. The `next` report refuses it for the same two reasons as the twenty-cent breach. The first fill used all the room the limits allowed.

The loop assumes a full fill to keep the example small, and that assumption marks where the guard's job ends. Updating holdings when a proposal merely passes would make the next check count shares that might never arrive: a limit order can sit unfilled, fill in part or be cancelled. The next snapshot has to come from execution reports, with the filled quantity, the pending remainder, any cancellations and fees, and the guard checks whatever that snapshot says.

## Both checks in the agent's loop

The two checks sit at different points in the same loop and never call each other. The audit runs once per campaign, before you rank scores. The guard runs on every proposal, immediately before submission.

![Flowchart of the order path. The agent's proposed order, the owner's limits, and the account's cash, holdings and pending orders feed one check. A refusal returns reasons. An admission goes to a single-owner executor, whose simulated fill updates the account state used by the next check. Confidence travels with the proposal only as metadata.](diagrams/order-path.png)

*The model contributes only the proposal. Limits come from the account owner and state comes from execution records; the verdict decides whether the executor sees the order at all.*

The factor half follows the same split. The agent proposes candidates, but the harness that supplies the files writes the manifest, and the audit reads the files themselves. Two recent papers informed that separation. [What LLM Trading Agents Actually Do in Production](https://arxiv.org/abs/2609.05663v1) is an observational record of two agent fleets, one of which ran a funded vault for 21 days; the idea borrowed here is only that a prompt change can't move the cash reserve or position cap. [Propose, Don't Judge](https://arxiv.org/abs/2609.27051v1) separates the agent that mines factors from an anytime-valid statistical referee. The audit runs before any such referee and implements none of its tests, and neither tool reproduces either paper's results.

Here are both gates in one script. Save it in the repository root and run it there; the two `print` lines mark where your executor and your refusal log go:

```python
import json
from factoraudit import audit
from orderguard import decide

# Before ranking: the recorded campaign must pass.
report = audit("examples/campaign.json", "examples")
if report["overall"] != "PASS":
    raise SystemExit("fix the campaign before ranking its scores")

# Before submitting: the proposal must fit the current account state.
def load(name):
    with open(f"examples/{name}.json") as f:
        return json.load(f)

proposal = load("proposal")
decision = decide(load("limits"), load("snapshot"), proposal, "2026-10-09T12:01:00Z")
if decision["verdict"] == "ADMIT":
    print("submit", proposal["proposal_id"])   # your executor goes here
else:
    print("refused", decision["reasons"])      # record the refusal
```

It prints `submit original-paper-1`. Swap in `campaign-leak.json` and the script stops before any order is considered. Both `audit` and `decide` raise `Malformed` when an input can't be checked at all; treat that as an input error to fix, separate from a failed rule or a refused order.

What the script can't do is make its inputs true. Two agents checked against the same snapshot can both pass, then breach a limit together, so give one execution owner control of the account while it fetches state, checks and submits. The guard has no lock and no broker connection, and a lock inside one Python process won't stop another process, or a person, trading the same account.

The guard models cash accounts, long-only equities and limit orders. It rejects an unsupported field such as `stop_loss` as malformed rather than dropping it. The [order guard reference](VALIDATION.md#order-guard-reference) covers the exclusions and the sell path; the full input schema and reason codes are in the `orderguard.py` module docstring.

## What the checks establish

The worked examples cover five decisions: a clean campaign and a leaked one, then an order that fits, an order one cent over, and one share after the fill. Hand-picked cases like these show the behavior. They don't show the accounting is right across the inputs you'll actually send, so each tool also has an independent check:

```sh
python3 check_orderguard.py
python3 check_factoraudit.py
```

| Script | What it compares | Observed |
|---|---|---|
| `check_orderguard.py` | 2,000 seeded buy cases against a separate exact-fraction implementation, 4,000 reruns with confidence set to 0 and 1, and 23 boundary and malformed-input cases | Every verdict, reason and all five accounting outputs matched: 395 admitted, 1,605 refused. Confidence changed nothing. All 23 cases passed. |
| `check_factoraudit.py` | 19 CLI scenarios built from original files: valid folds, plus injected future data, overlapping windows, changed bytes, duplicate trials, escaping paths and malformed CSVs | All 19 passed. |

These are correctness checks on constructed inputs. The admit/refuse split reflects how the cases were generated, not an error rate on real orders, and nothing here measures trading returns. [VALIDATION.md](VALIDATION.md) describes the cases, input rules and file limits.

## Try it on your agent's last run

Go back to the agent's message: a good score and a confident order. Each part now has a question you can ask without taking the agent's word for it.

For the score, copy `examples/campaign.json` and fill it in from your harness's own log of one finished campaign, not from asking the model: the files it actually supplied, their hashes (`shasum -a 256` on macOS, `sha256sum` on Linux), each candidate's cutoff and its scoring window.

```sh
python3 factoraudit.py audit --manifest path/to/your-campaign.json --root path/to/campaign-files; echo "exit $?"
```

For the order, copy `examples/snapshot.json` and replace its contents with your paper account's cash, holdings, marks and every open order, with `as_of` set to when you read them. Set the limits you actually want, then check the last order your agent proposed:

```sh
python3 orderguard.py check --limits path/to/limits.json --snapshot path/to/snapshot.json --proposal path/to/proposal.json; echo "exit $?"
```

Read the evidence and computed amounts before you wire either check into anything. In the examples, every file in the leaked campaign was intact; only its declared role was wrong. The $2,000 order looked affordable until the pending buy was counted. Neither problem showed up in the agent's confidence.

## Background

The earlier [LayeredMemoryTrader](https://github.com/gael55x/LayeredMemoryTrader) informed the propose-then-execute workflow. It isn't a dependency, and its public decision log can't be replayed exactly here because it lacks the quantities, prices and account state the guard needs. No upstream code or raw log data is included. Editable Mermaid sources for both diagrams are in [diagrams/](diagrams/).

## License

MIT, covering the original code and diagrams in this repository.
