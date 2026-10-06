# Validation and integration boundaries

Implementation: Claude Opus 5.5. Independent source review, exact rational oracle, error injection and integration checks: Codex. Checks use constructed inputs and measure no trading returns.

## Order admission

The separate `fractions.Fraction` oracle compares reasons and five accounting outputs against 2,000 seeded valid buy cases: 395 admissions and 1,605 refusals. Another 4,000 comparisons verify that confidence 0 and 1 do not change those decisions or calculations. Inputs remain unchanged. Seed: 20261009.

The 23 explicit boundary cases cover exact cash/position/age limits, a price step above the caps, stale state, over-limit risk reduction by selling, pending sell reservations, no pending-sale cash credit, unknown exposure, inconsistent pending sells, other-symbol caps, exact decimal arithmetic, malformed amounts/fields/confidence, future state, CLI output/exit codes, duplicate JSON keys and nonregular files.

The runnable paper loop admits a $2,000 buy, explicitly assumes one full fill at the supplied price, updates cash and shares, and refuses the next $100 buy. Pending buy reservations remain outstanding. This is an integration example, not a replay of historical trading decisions or a live execution test.

## Factor manifest audit

Nineteen original CLI scenarios cover two valid folds, future construction data, overlapping visibility/scoring dates, score rows outside the window, repeated exact trials and candidate IDs, renamed input aliases, changed bytes, escaping paths/symlinks, invalid dates, missing/duplicate headers, row-width errors, empty data, nonregular files, byte caps, unknown scoring IDs and duplicate JSON keys. Same-spec reuse across legitimate folds passes with information only.

## Scope and maintenance

Account state, quote correctness, fees, fills and serialization belong to the caller. The factor audit checks declared files/dates, not model knowledge, factor semantics, hidden retries or statistical significance. Read each report's scope before using it.

One runnable check per tool is the regression gate. After changing arithmetic, schemas, file handling or rules, rerun that tool's check and its working example. Public release validation also requires a clean checkout and passing GitHub Actions for the exact release revision. A successful local run alone does not establish remote CI status.

The original research articles and model-provider receipts are not bundled. Only original code, original sample data and original diagrams are released. Existing trading and weekly research repositories are independent and remain unchanged.
