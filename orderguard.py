#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""orderguard: deterministic pre-trade limit check over caller-supplied state.

This is a pure preflight over supplied state. It does not attach protective
orders, contact any broker or market, observe live state, or provide
concurrency control. A single owner of the account must refresh the snapshot
and serialize check-then-submit itself. A verdict only describes the inputs
it was given.

API
    decide(limits, snapshot, proposal, now) -> dict
        Validates all inputs and raises Malformed on any invalid data. The
        input objects are never modified.

    class Malformed(ValueError)
        Raised for any invalid input: schema, type, name, decimal or
        timestamp violations, a snapshot dated after `now`, oversized
        collections, and (CLI) unreadable, oversized, non-regular or
        invalid/duplicate-key JSON files.

CLI
    python3 orderguard.py check --limits FILE --snapshot FILE --proposal FILE
                                [--now YYYY-MM-DDTHH:MM:SSZ]
    Prints a JSON object on stdout. Exit 0 ADMIT, 2 REFUSE, 3 MALFORMED.
    `--now` defaults to the current UTC time.

Inputs (all amounts are decimal strings matching
(0|[1-9][0-9]{0,19})(\\.[0-9]{1,12})?; JSON numbers are rejected)
    limits:   max_order_notional, max_gross_notional, min_cash_reserve,
              max_snapshot_age_s (positive integer string <= 86400),
              symbols: {SYMBOL: {max_position_notional}}
    snapshot: as_of, cash, positions: {SYMBOL: {qty, mark}},
              pending: [{side, symbol, qty, limit_price}]  (unfilled remainders)
    proposal: proposal_id, symbol, side, qty, limit_price, optional confidence
              (in [0, 1]; echoed as confidence_ignored, never used in decisions)

Reason codes
    STALE_SNAPSHOT, SYMBOL_NOT_ALLOWED, UNTRACKED_EXPOSURE, UNTRACKED_PENDING,
    INVALID_PENDING_POSITION (buy), POSITION_LIMIT (buy), GROSS_LIMIT (buy),
    ORDER_NOTIONAL (buy), CASH_RESERVE (buy), SELL_EXCEEDS_SELLABLE (sell)

Exposure for each symbol is held qty * mark + pending buy qty * limit_price,
plus the order for the proposed symbol. Pending sells never reduce exposure
or create cash. Sells skip the order, position, gross and reserve limits so
that excessive exposure can be reduced. Sells are still subject to the
freshness, allowed-symbol and tracked-exposure rules.

Input hashes are SHA-256 over canonical JSON: sorted keys, compact
separators, ASCII-escaped.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import (
    Context,
    Decimal,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
    ROUND_HALF_EVEN,
    localcontext,
)
from types import MappingProxyType
from typing import Mapping

__all__ = ["GUARD_VERSION", "Malformed", "decide", "main"]

GUARD_VERSION = "1.0.0"
MAX_FILE_BYTES = 1024 * 1024
MAX_ITEMS = 1000
MAX_AGE_LIMIT_S = 86400

_DECIMAL = re.compile(r"(0|[1-9][0-9]{0,19})(\.[0-9]{1,12})?")
_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,80}")
_AGE = re.compile(r"[1-9][0-9]{0,4}")
_TIME = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z")
_SIDES = ("buy", "sell")
_ZERO = Decimal(0)
_CTX = Context(
    prec=100,
    rounding=ROUND_HALF_EVEN,
    traps=[InvalidOperation, DivisionByZero, Overflow, Inexact],
)


class Malformed(ValueError):
    """Input data is invalid; no verdict can be given."""


@dataclass(frozen=True)
class Limits:
    max_order_notional: Decimal
    max_gross_notional: Decimal
    min_cash_reserve: Decimal
    max_snapshot_age_s: int
    position_caps: Mapping[str, Decimal]


@dataclass(frozen=True)
class Position:
    qty: Decimal
    mark: Decimal


@dataclass(frozen=True)
class Order:
    side: str
    symbol: str
    qty: Decimal
    limit_price: Decimal


@dataclass(frozen=True)
class Snapshot:
    as_of: datetime
    cash: Decimal
    positions: Mapping[str, Position]
    pending: tuple[Order, ...]


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    order: Order
    confidence: str | None


# ---------------------------------------------------------------- validation


def _fields(value: object, where: str, required: tuple[str, ...], optional: tuple[str, ...] = ()) -> dict:
    if not isinstance(value, dict):
        raise Malformed(f"{where}: expected object")
    unknown = set(value) - set(required) - set(optional)
    if unknown:
        raise Malformed(f"{where}: unknown field(s) {', '.join(sorted(map(repr, unknown)))}")
    missing = set(required) - set(value)
    if missing:
        raise Malformed(f"{where}: missing field(s) {', '.join(sorted(missing))}")
    return value


def _collection(value: object, kind: type, where: str):
    if not isinstance(value, kind):
        raise Malformed(f"{where}: expected {'object' if kind is dict else 'array'}")
    if len(value) > MAX_ITEMS:
        raise Malformed(f"{where}: more than {MAX_ITEMS} entries")
    return value


def _name(value: object, where: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise Malformed(f"{where}: name must be 1-80 characters of [A-Za-z0-9_.:-]")
    return value


def _decimal(value: object, where: str, positive: bool = False) -> Decimal:
    if not isinstance(value, str) or not _DECIMAL.fullmatch(value):
        raise Malformed(f"{where}: expected nonnegative decimal string")
    amount = Decimal(value)
    if positive and amount == 0:
        raise Malformed(f"{where}: must be greater than 0")
    return amount


def _side(value: object, where: str) -> str:
    if not isinstance(value, str) or value not in _SIDES:
        raise Malformed(f"{where}: expected 'buy' or 'sell'")
    return value


def _time(value: object, where: str) -> datetime:
    match = _TIME.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise Malformed(f"{where}: expected UTC time YYYY-MM-DDTHH:MM:SSZ")
    try:
        return datetime(*map(int, match.groups()), tzinfo=timezone.utc)
    except ValueError:
        raise Malformed(f"{where}: not a real calendar date/time") from None


def _age_limit(value: object, where: str) -> int:
    if not isinstance(value, str) or not _AGE.fullmatch(value) or int(value) > MAX_AGE_LIMIT_S:
        raise Malformed(f"{where}: expected positive integer string <= {MAX_AGE_LIMIT_S}")
    return int(value)


def _order(raw: dict, where: str) -> Order:
    return Order(
        side=_side(raw["side"], f"{where}.side"),
        symbol=_name(raw["symbol"], f"{where}.symbol"),
        qty=_decimal(raw["qty"], f"{where}.qty", positive=True),
        limit_price=_decimal(raw["limit_price"], f"{where}.limit_price", positive=True),
    )


def _parse_limits(raw: object) -> Limits:
    raw = _fields(raw, "limits", ("max_order_notional", "max_gross_notional", "min_cash_reserve",
                                  "max_snapshot_age_s", "symbols"))
    caps = {}
    for symbol, entry in _collection(raw["symbols"], dict, "limits.symbols").items():
        where = f"limits.symbols.{_name(symbol, 'limits.symbols key')}"
        entry = _fields(entry, where, ("max_position_notional",))
        caps[symbol] = _decimal(entry["max_position_notional"], f"{where}.max_position_notional")
    return Limits(
        max_order_notional=_decimal(raw["max_order_notional"], "limits.max_order_notional"),
        max_gross_notional=_decimal(raw["max_gross_notional"], "limits.max_gross_notional"),
        min_cash_reserve=_decimal(raw["min_cash_reserve"], "limits.min_cash_reserve"),
        max_snapshot_age_s=_age_limit(raw["max_snapshot_age_s"], "limits.max_snapshot_age_s"),
        position_caps=MappingProxyType(caps),
    )


def _parse_snapshot(raw: object) -> Snapshot:
    raw = _fields(raw, "snapshot", ("as_of", "cash", "positions", "pending"))
    positions = {}
    for symbol, entry in _collection(raw["positions"], dict, "snapshot.positions").items():
        where = f"snapshot.positions.{_name(symbol, 'snapshot.positions key')}"
        entry = _fields(entry, where, ("qty", "mark"))
        positions[symbol] = Position(
            qty=_decimal(entry["qty"], f"{where}.qty"),
            mark=_decimal(entry["mark"], f"{where}.mark", positive=True),
        )
    pending = tuple(
        _order(_fields(entry, f"snapshot.pending[{i}]", ("side", "symbol", "qty", "limit_price")),
               f"snapshot.pending[{i}]")
        for i, entry in enumerate(_collection(raw["pending"], list, "snapshot.pending"))
    )
    return Snapshot(
        as_of=_time(raw["as_of"], "snapshot.as_of"),
        cash=_decimal(raw["cash"], "snapshot.cash"),
        positions=MappingProxyType(positions),
        pending=pending,
    )


def _parse_proposal(raw: object) -> Proposal:
    raw = _fields(raw, "proposal", ("proposal_id", "symbol", "side", "qty", "limit_price"), ("confidence",))
    confidence = raw.get("confidence")
    if confidence is not None or "confidence" in raw:
        if _decimal(confidence, "proposal.confidence") > 1:
            raise Malformed("proposal.confidence: must be within [0, 1]")
    return Proposal(
        proposal_id=_name(raw["proposal_id"], "proposal.proposal_id"),
        order=_order(raw, "proposal"),
        confidence=confidence,
    )


# ------------------------------------------------------------------ decision


def _sha256(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _fmt(value: Decimal) -> str:
    return "0" if value == 0 else format(value.normalize(_CTX), "f")


def _buy(limits: Limits, snap: Snapshot, order: Order, notional: Decimal,
         held: dict[str, Decimal], pending_sell: dict[str, Decimal], reasons: set[str]) -> dict[str, Decimal]:
    if any(qty > held.get(symbol, _ZERO) for symbol, qty in pending_sell.items()):
        reasons.add("INVALID_PENDING_POSITION")

    exposure = {symbol: pos.qty * pos.mark for symbol, pos in snap.positions.items()}
    pending_buy = _ZERO
    for p in snap.pending:
        if p.side == "buy":
            value = p.qty * p.limit_price
            exposure[p.symbol] = exposure.get(p.symbol, _ZERO) + value
            pending_buy += value
    exposure[order.symbol] = exposure.get(order.symbol, _ZERO) + notional
    gross = sum(exposure.values(), _ZERO)
    cash_after = snap.cash - pending_buy - notional

    # Untracked symbols have no cap; they are already refused as UNTRACKED_*/SYMBOL_NOT_ALLOWED.
    caps = limits.position_caps
    if any(symbol in caps and value > caps[symbol] for symbol, value in exposure.items()):
        reasons.add("POSITION_LIMIT")
    if gross > limits.max_gross_notional:
        reasons.add("GROSS_LIMIT")
    if notional > limits.max_order_notional:
        reasons.add("ORDER_NOTIONAL")
    if cash_after < limits.min_cash_reserve:
        reasons.add("CASH_RESERVE")

    return {
        "order_notional": notional,
        "pending_buy_notional": pending_buy,
        "symbol_exposure_after": exposure[order.symbol],
        "gross_after": gross,
        "cash_after": cash_after,
    }


def _sell(order: Order, notional: Decimal, held: dict[str, Decimal],
          pending_sell: dict[str, Decimal], reasons: set[str]) -> dict[str, Decimal]:
    sellable = held.get(order.symbol, _ZERO) - pending_sell.get(order.symbol, _ZERO)
    if order.qty > sellable:
        reasons.add("SELL_EXCEEDS_SELLABLE")
    return {"order_notional": notional, "sellable": sellable}


def decide(limits: dict, snapshot: dict, proposal: dict, now: str) -> dict:
    """Return an ADMIT/REFUSE verdict for `proposal`; raise Malformed on invalid input."""
    lim = _parse_limits(limits)
    snap = _parse_snapshot(snapshot)
    prop = _parse_proposal(proposal)
    now_t = _time(now, "now")
    if snap.as_of > now_t:
        raise Malformed("snapshot.as_of: later than now")

    order = prop.order
    caps = lim.position_caps
    reasons = set()
    if (now_t - snap.as_of) // timedelta(seconds=1) > lim.max_snapshot_age_s:
        reasons.add("STALE_SNAPSHOT")
    if order.symbol not in caps:
        reasons.add("SYMBOL_NOT_ALLOWED")
    if any(symbol not in caps for symbol in snap.positions):
        reasons.add("UNTRACKED_EXPOSURE")
    if any(p.symbol not in caps for p in snap.pending):
        reasons.add("UNTRACKED_PENDING")

    with localcontext(_CTX):
        held = {symbol: pos.qty for symbol, pos in snap.positions.items()}
        pending_sell: dict[str, Decimal] = {}
        for p in snap.pending:
            if p.side == "sell":
                pending_sell[p.symbol] = pending_sell.get(p.symbol, _ZERO) + p.qty
        notional = order.qty * order.limit_price
        if order.side == "buy":
            computed = _buy(lim, snap, order, notional, held, pending_sell, reasons)
        else:
            computed = _sell(order, notional, held, pending_sell, reasons)
        computed_text = {key: _fmt(value) for key, value in computed.items()}

    return {
        "verdict": "REFUSE" if reasons else "ADMIT",
        "reasons": sorted(reasons),
        "computed": computed_text,
        "limits_sha256": _sha256(limits),
        "snapshot_sha256": _sha256(snapshot),
        "proposal_sha256": _sha256(proposal),
        "confidence_ignored": prop.confidence,
        "guard_version": GUARD_VERSION,
    }


# ----------------------------------------------------------------------- CLI


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise Malformed(f"duplicate key {key!r}")
        result[key] = value
    return result


def _reject_constant(name: str) -> object:
    raise Malformed(f"non-standard JSON constant {name}")


def _load(path: str) -> object:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as fh:
            info = os.fstat(fh.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise Malformed(f"{path}: not a regular file")
            if info.st_size > MAX_FILE_BYTES:
                raise Malformed(f"{path}: larger than {MAX_FILE_BYTES} bytes")
            data = fh.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise Malformed(f"{path}: cannot read: {exc.strerror}") from None
    if len(data) > MAX_FILE_BYTES:
        raise Malformed(f"{path}: larger than {MAX_FILE_BYTES} bytes")
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_reject_constant)
    except Malformed as exc:
        raise Malformed(f"{path}: {exc}") from None
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise Malformed(f"{path}: invalid JSON: {exc}") from None


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        raise Malformed(f"usage: {message}")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="orderguard.py", description="Pre-trade limit check over supplied state.",
                     allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", allow_abbrev=False,
                                help="check one proposal; exit 0 ADMIT, 2 REFUSE, 3 MALFORMED")
    check.add_argument("--limits", required=True, metavar="FILE")
    check.add_argument("--snapshot", required=True, metavar="FILE")
    check.add_argument("--proposal", required=True, metavar="FILE")
    check.add_argument("--now", metavar="YYYY-MM-DDTHH:MM:SSZ", help="evaluation time (default: current UTC)")
    return parser


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, sort_keys=True, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        now = args.now if args.now is not None else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = decide(_load(args.limits), _load(args.snapshot), _load(args.proposal), now)
    except Malformed as exc:
        _emit({"verdict": "MALFORMED", "error": str(exc), "guard_version": GUARD_VERSION})
        return 3
    _emit(result)
    return 0 if result["verdict"] == "ADMIT" else 2


if __name__ == "__main__":
    sys.exit(main())
