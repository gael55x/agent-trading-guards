#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""factoraudit: static audit of a declared factor-research campaign manifest.

Usage:
    python3 factoraudit.py audit --manifest FILE --root DIR [--max-bytes N]

The manifest is strict JSON describing input CSV files and candidate factor
specs. Every referenced file is read once and hashed. The declared date
column of each input CSV is scanned. The manifest is then checked against
these scoped rules:

    HASH              expected vs actual SHA-256 of input and spec files
    DUPLICATE_ID      a candidate_id recorded more than once
    WINDOW            visible_through < score start <= score end
                      (and visible_through < holdout_start <= score start)
    INPUT_VISIBILITY  construction inputs end on or before visible_through
    SCORE_DATES       recorded scoring rows fall inside the score window
    DUPLICATE_TRIAL   identical declared trial recorded under different IDs
    REPEATED_SPEC     INFO: identical spec bytes used in different trials
    RECORDED_TRIALS   INFO: counts of explicitly recorded candidates

Spec files are opaque bytes; no factor is parsed, evaluated or executed.
Overall PASS means only that these scoped checks pass. INFO never fails.

Exit status: 0 PASS, 1 FAIL, 3 MALFORMED (schema, data or I/O error).

Python API: audit(manifest_path, root, max_bytes) returns the report dict
and raises Malformed (a ValueError) where the CLI would exit 3.
"""

import argparse
import codecs
import csv
import hashlib
import io
import json
import re
import stat
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePath
from types import MappingProxyType
from typing import Callable, Iterable, Iterator, Mapping

__all__ = ["Malformed", "Manifest", "audit", "load_manifest", "parse_manifest", "main"]
__version__ = "1.0.0"

MIB = 1024 * 1024
DEFAULT_MAX_BYTES = 10 * MIB
MAX_FILE_BYTES = 100 * MIB
MAX_TOTAL_BYTES = 100 * MIB
MAX_MANIFEST_BYTES = 1 * MIB
MAX_INPUTS = 1000
MAX_CANDIDATES = 10000
MAX_PATH_CHARS = 4096
CSV_FIELD_LIMIT = 1 * MIB

ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,80}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
DAY_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
DECIMAL_RE = re.compile(r"[1-9][0-9]{0,9}")

STATUSES = ("PASS", "FAIL", "INFO")

SCOPE = (
    "PASS means only that the scoped checks pass for what this manifest declares "
    "and what the referenced files contain.",
    "Spec files are hashed as opaque bytes; no factor is parsed, evaluated or executed, "
    "and identical spec bytes may still mean different factor semantics.",
    "visible_through is the last declared simulated knowledge date; no wall clock, "
    "file mtime or trusted timestamp is consulted.",
    "SCORE_DATES checks recorded row dates only; it does not verify market-day coverage "
    "or how a spec uses its data.",
    "DUPLICATE_TRIAL flags identical declared trial identities; it is not proof of hidden retries.",
    "Only explicitly recorded candidates are counted; off-ledger trials are unobservable. "
    "No complete search denominator, multiple-testing/FDR control, trusted timestamp, "
    "pretraining-knowledge or profitability guarantee is made.",
    "No guarantee is made against concurrent or malicious filesystem changes during the audit.",
)


class Malformed(ValueError):
    """Manifest, data or I/O problem that prevents an audit (CLI exit status 3)."""


@dataclass(frozen=True)
class Window:
    start: date
    end: date


@dataclass(frozen=True)
class InputDecl:
    id: str
    path: str
    sha256: str
    date_column: str


@dataclass(frozen=True)
class CandidateDecl:
    candidate_id: str
    family: str
    spec_path: str
    spec_sha256: str
    visible_through: date
    inputs: tuple[str, ...]
    score_input: str
    score_window: Window


@dataclass(frozen=True)
class Manifest:
    campaign_id: str
    inputs: tuple[InputDecl, ...]
    candidates: tuple[CandidateDecl, ...]
    holdout_start: date | None = None


@dataclass(frozen=True)
class FileFacts:
    sha256: str
    size: int


@dataclass(frozen=True)
class DateFacts:
    rows: int
    first: date
    last: date


@dataclass(frozen=True)
class InputFacts:
    file: FileFacts
    dates: DateFacts


Trial = tuple[tuple[str, object], ...]


@dataclass(frozen=True)
class Observed:
    inputs: Mapping[str, InputFacts]
    specs: tuple[FileFacts, ...]
    trials: tuple[Trial, ...]


# --- boundary validation -----------------------------------------------------

Check = Callable[[object, str], object]


def _clip(text: str, limit: int = 40) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


def _why(exc: Exception) -> str:
    return exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)


def _str(value: object, where: str) -> str:
    if type(value) is not str:
        raise Malformed(f"{where}: expected string")
    return value


def _ident(value: object, where: str) -> str:
    if not ID_RE.fullmatch(_str(value, where)):
        raise Malformed(f"{where}: expected 1-80 characters from [A-Za-z0-9_.:-]")
    return value


def _sha256(value: object, where: str) -> str:
    if not SHA256_RE.fullmatch(_str(value, where)):
        raise Malformed(f"{where}: expected 64 lowercase hex characters")
    return value


def _day(value: object, where: str) -> date:
    text = _str(value, where)
    if DAY_RE.fullmatch(text):
        try:
            return date.fromisoformat(text)
        except ValueError:
            pass
    raise Malformed(f"{where}: expected YYYY-MM-DD calendar date, got {_clip(text)!r}")


def _column(value: object, where: str) -> str:
    if not _str(value, where):
        raise Malformed(f"{where}: must not be empty")
    return value


def _relpath(value: object, where: str) -> str:
    text = _str(value, where)
    if not text or "\x00" in text or len(text) > MAX_PATH_CHARS:
        raise Malformed(f"{where}: expected a non-empty relative path")
    pure = PurePath(text)
    if pure.is_absolute() or pure.anchor:
        raise Malformed(f"{where}: path must be relative")
    if ".." in pure.parts:
        raise Malformed(f"{where}: path must not contain '..'")
    return text


def _fields(value: object, where: str, required: Mapping[str, Check],
            optional: Mapping[str, Check] = MappingProxyType({})) -> dict[str, object]:
    if type(value) is not dict:
        raise Malformed(f"{where}: expected object")
    missing = [key for key in required if key not in value]
    if missing:
        raise Malformed(f"{where}: missing field {missing[0]!r}")
    unknown = [key for key in value if key not in required and key not in optional]
    if unknown:
        raise Malformed(f"{where}: unknown field {_clip(unknown[0])!r}")
    checks = {**required, **optional}
    return {key: checks[key](item, f"{where}.{key}") for key, item in value.items()}


def _record(cls: type, fields: Mapping[str, Check]) -> Check:
    return lambda value, where: cls(**_fields(value, where, fields))


def _array_of(limit: int, item: Check) -> Check:
    def check(value: object, where: str) -> tuple:
        if type(value) is not list:
            raise Malformed(f"{where}: expected array")
        if not value:
            raise Malformed(f"{where}: must not be empty")
        if len(value) > limit:
            raise Malformed(f"{where}: more than {limit} entries")
        return tuple(item(entry, f"{where}[{n}]") for n, entry in enumerate(value))
    return check


def _construction_ids(value: object, where: str) -> tuple[str, ...]:
    ids = _array_of(MAX_INPUTS, _ident)(value, where)
    if len(set(ids)) != len(ids):
        raise Malformed(f"{where}: duplicate input id")
    return ids


WINDOW_FIELDS = {"start": _day, "end": _day}
INPUT_FIELDS = {"id": _ident, "path": _relpath, "sha256": _sha256, "date_column": _column}
CANDIDATE_FIELDS = {
    "candidate_id": _ident,
    "family": _ident,
    "spec_path": _relpath,
    "spec_sha256": _sha256,
    "visible_through": _day,
    "inputs": _construction_ids,
    "score_input": _ident,
    "score_window": _record(Window, WINDOW_FIELDS),
}
MANIFEST_FIELDS = {
    "campaign_id": _ident,
    "inputs": _array_of(MAX_INPUTS, _record(InputDecl, INPUT_FIELDS)),
    "candidates": _array_of(MAX_CANDIDATES, _record(CandidateDecl, CANDIDATE_FIELDS)),
}
MANIFEST_OPTIONAL = {"holdout_start": _day}


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    obj: dict[str, object] = {}
    for key, value in pairs:
        if key in obj:
            raise Malformed(f"manifest: duplicate JSON key {_clip(key)!r}")
        obj[key] = value
    return obj


def _no_constant(name: str) -> None:
    raise Malformed(f"manifest: non-standard JSON constant {name}")


def parse_manifest(raw: object) -> Manifest:
    """Validate decoded JSON into a Manifest; raise Malformed on any problem."""
    manifest = Manifest(**_fields(raw, "manifest", MANIFEST_FIELDS, MANIFEST_OPTIONAL))
    known: set[str] = set()
    for n, declared in enumerate(manifest.inputs):
        if declared.id in known:
            raise Malformed(f"manifest.inputs[{n}].id: duplicate input id {declared.id!r}")
        known.add(declared.id)
    for n, candidate in enumerate(manifest.candidates):
        for ref in (*candidate.inputs, candidate.score_input):
            if ref not in known:
                raise Malformed(f"manifest.candidates[{n}]: unknown input id {ref!r}")
    return manifest


def _read(path: Path, limit: int, label: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            data = handle.read(limit + 1)
    except OSError as exc:
        raise Malformed(f"{label}: cannot read: {_why(exc)}") from exc
    if len(data) > limit:
        raise Malformed(f"{label}: larger than {limit} bytes")
    return data


def load_manifest(manifest_path: str | Path) -> Manifest:
    """Read a strict UTF-8 JSON manifest (regular file, at most 1 MiB)."""
    path = Path(manifest_path)
    try:
        info = path.stat()
    except (OSError, ValueError) as exc:
        raise Malformed(f"manifest: cannot access: {_why(exc)}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise Malformed("manifest: not a regular file")
    if info.st_size > MAX_MANIFEST_BYTES:
        raise Malformed(f"manifest: larger than {MAX_MANIFEST_BYTES} bytes")
    data = _read(path, MAX_MANIFEST_BYTES, "manifest")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Malformed(f"manifest: invalid UTF-8 at byte {exc.start}") from exc
    try:
        raw = json.loads(text, object_pairs_hook=_unique_keys, parse_constant=_no_constant)
    except Malformed:
        raise
    except RecursionError as exc:
        raise Malformed("manifest: JSON nesting too deep") from exc
    except ValueError as exc:
        raise Malformed(f"manifest: invalid JSON: {exc}") from exc
    return parse_manifest(raw)


def _root(root: str | Path) -> Path:
    try:
        path = Path(root).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise Malformed(f"root: cannot resolve: {_why(exc)}") from exc
    if not path.is_dir():
        raise Malformed("root: not a directory")
    return path


def _resolve(root: Path, rel: str, where: str) -> tuple[Path, int]:
    try:
        path = (root / rel).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise Malformed(f"{where}: cannot resolve {_clip(rel)!r}: {_why(exc)}") from exc
    if not path.is_relative_to(root):
        raise Malformed(f"{where}: {_clip(rel)!r} resolves outside root")
    try:
        info = path.stat()
    except OSError as exc:
        raise Malformed(f"{where}: cannot access {_clip(rel)!r}: {_why(exc)}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise Malformed(f"{where}: {_clip(rel)!r} is not a regular file")
    return path, info.st_size


def _scan_dates(reader, columns: Iterable[str], label: str) -> dict[str, DateFacts]:
    header = next(reader, None)
    if header is None:
        raise Malformed(f"{label}: empty CSV, header row required")
    if "" in header:
        raise Malformed(f"{label}: empty header name")
    if len(set(header)) != len(header):
        raise Malformed(f"{label}: duplicate header names")
    positions: dict[str, int] = {}
    for column in columns:
        if column not in header:
            raise Malformed(f"{label}: date column {_clip(column)!r} not in header")
        positions[column] = header.index(column)
    first: dict[str, date] = {}
    last: dict[str, date] = {}
    rows = 0
    for row in reader:
        if len(row) != len(header):
            raise Malformed(f"{label}: line {reader.line_num}: expected {len(header)} fields, found {len(row)}")
        rows += 1
        for column, position in positions.items():
            day = _day(row[position], f"{label}: line {reader.line_num}: column {_clip(column)!r}")
            if column not in first or day < first[column]:
                first[column] = day
            if column not in last or day > last[column]:
                last[column] = day
    if rows == 0:
        raise Malformed(f"{label}: no data rows")
    return {column: DateFacts(rows, first[column], last[column]) for column in positions}


def _date_facts(data: bytes, columns: Iterable[str], label: str) -> dict[str, DateFacts]:
    if data.startswith(codecs.BOM_UTF8):
        raise Malformed(f"{label}: UTF-8 byte order mark is not allowed")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Malformed(f"{label}: invalid UTF-8 at byte {exc.start}") from exc
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    previous = csv.field_size_limit(CSV_FIELD_LIMIT)
    try:
        return _scan_dates(reader, columns, label)
    except csv.Error as exc:
        raise Malformed(f"{label}: line {reader.line_num}: {exc}") from exc
    finally:
        csv.field_size_limit(previous)


def _trial(spec: FileFacts, candidate: CandidateDecl, inputs: Mapping[str, InputFacts]) -> Trial:
    # Canonical identity uses actual file hashes so aliased IDs cannot hide a repeat.
    return (
        ("spec_sha256", spec.sha256),
        ("construction_sha256", tuple(sorted({inputs[ref].file.sha256 for ref in candidate.inputs}))),
        ("visible_through", candidate.visible_through.isoformat()),
        ("score_start", candidate.score_window.start.isoformat()),
        ("score_end", candidate.score_window.end.isoformat()),
        ("score_sha256", inputs[candidate.score_input].file.sha256),
    )


def _observe(manifest: Manifest, root: Path, max_bytes: int) -> Observed:
    sizes: dict[Path, int] = {}

    def place(rel: str, where: str) -> Path:
        path, size = _resolve(root, rel, where)
        if size > max_bytes:
            raise Malformed(f"{where}: {_clip(rel)!r} is {size} bytes, limit {max_bytes}")
        sizes[path] = size
        return path

    input_paths = {
        declared.id: place(declared.path, f"manifest.inputs[{n}].path")
        for n, declared in enumerate(manifest.inputs)
    }
    spec_paths = tuple(
        place(candidate.spec_path, f"manifest.candidates[{n}].spec_path")
        for n, candidate in enumerate(manifest.candidates)
    )
    if sum(sizes.values()) > MAX_TOTAL_BYTES:
        raise Malformed(f"referenced files total {sum(sizes.values())} bytes, limit {MAX_TOTAL_BYTES}")

    columns: dict[Path, set[str]] = defaultdict(set)
    for declared in manifest.inputs:
        columns[input_paths[declared.id]].add(declared.date_column)

    # Each unique resolved file is read once; date facts are cached per (path, column).
    files: dict[Path, FileFacts] = {}
    dates: dict[tuple[Path, str], DateFacts] = {}
    consumed = 0
    for path in sizes:
        label = path.relative_to(root).as_posix()
        data = _read(path, max_bytes, label)
        consumed += len(data)
        if consumed > MAX_TOTAL_BYTES:
            raise Malformed(f"referenced files exceed {MAX_TOTAL_BYTES} bytes in total")
        files[path] = FileFacts(hashlib.sha256(data).hexdigest(), len(data))
        if path in columns:
            for column, facts in _date_facts(data, sorted(columns[path]), label).items():
                dates[path, column] = facts

    inputs = {
        declared.id: InputFacts(files[input_paths[declared.id]], dates[input_paths[declared.id], declared.date_column])
        for declared in manifest.inputs
    }
    specs = tuple(files[path] for path in spec_paths)
    trials = tuple(_trial(spec, candidate, inputs) for spec, candidate in zip(specs, manifest.candidates))
    return Observed(MappingProxyType(inputs), specs, trials)


# --- rules -------------------------------------------------------------------

Entry = dict[str, object]


def _entry(rule: str, status: str, evidence: dict[str, object], candidate_id: str | None = None) -> Entry:
    entry: Entry = {"rule": rule, "status": status, "evidence": evidence}
    if candidate_id is not None:
        entry["candidate_id"] = candidate_id
    return entry


def _verdict(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def _rule_hash(m: Manifest, obs: Observed) -> Iterator[Entry]:
    for declared in m.inputs:
        actual = obs.inputs[declared.id].file
        yield _entry("HASH", _verdict(actual.sha256 == declared.sha256), {
            "file": "input", "input_id": declared.id, "path": declared.path,
            "expected": declared.sha256, "actual": actual.sha256, "bytes": actual.size,
        })
    for n, candidate in enumerate(m.candidates):
        actual = obs.specs[n]
        yield _entry("HASH", _verdict(actual.sha256 == candidate.spec_sha256), {
            "file": "spec", "candidate_index": n, "path": candidate.spec_path,
            "expected": candidate.spec_sha256, "actual": actual.sha256, "bytes": actual.size,
        }, candidate.candidate_id)


def _rule_duplicate_id(m: Manifest, obs: Observed) -> Iterator[Entry]:
    indexes: dict[str, list[int]] = defaultdict(list)
    for n, candidate in enumerate(m.candidates):
        indexes[candidate.candidate_id].append(n)
    repeated = sorted(cid for cid, found in indexes.items() if len(found) > 1)
    for cid in repeated:
        yield _entry("DUPLICATE_ID", "FAIL", {"candidate_indexes": indexes[cid]}, cid)
    if not repeated:
        yield _entry("DUPLICATE_ID", "PASS", {"recorded_candidates": len(m.candidates)})


def _rule_window(m: Manifest, obs: Observed) -> Iterator[Entry]:
    holdout = m.holdout_start
    for n, candidate in enumerate(m.candidates):
        seen, window = candidate.visible_through, candidate.score_window
        problems = [message for ok, message in (
            (seen < window.start, "visible_through must precede score_window.start"),
            (window.start <= window.end, "score_window.start must not follow score_window.end"),
            (holdout is None or seen < holdout, "visible_through must precede holdout_start"),
            (holdout is None or holdout <= window.start, "holdout_start must not follow score_window.start"),
        ) if not ok]
        yield _entry("WINDOW", _verdict(not problems), {
            "candidate_index": n,
            "visible_through": seen.isoformat(),
            "score_start": window.start.isoformat(),
            "score_end": window.end.isoformat(),
            "holdout_start": holdout.isoformat() if holdout else None,
            "problems": problems,
        }, candidate.candidate_id)


def _rule_input_visibility(m: Manifest, obs: Observed) -> Iterator[Entry]:
    for n, candidate in enumerate(m.candidates):
        details, problems = [], []
        for ref in candidate.inputs:
            facts = obs.inputs[ref].dates
            details.append({"input_id": ref, "rows": facts.rows,
                            "min": facts.first.isoformat(), "max": facts.last.isoformat()})
            if facts.last > candidate.visible_through:
                problems.append(f"{ref}: max date {facts.last} is after visible_through {candidate.visible_through}")
            if ref == candidate.score_input:
                problems.append(f"{ref}: scoring input is also a construction input")
        yield _entry("INPUT_VISIBILITY", _verdict(not problems), {
            "candidate_index": n,
            "visible_through": candidate.visible_through.isoformat(),
            "inputs": details,
            "problems": problems,
        }, candidate.candidate_id)


def _rule_score_dates(m: Manifest, obs: Observed) -> Iterator[Entry]:
    for n, candidate in enumerate(m.candidates):
        facts, window = obs.inputs[candidate.score_input].dates, candidate.score_window
        yield _entry("SCORE_DATES", _verdict(window.start <= facts.first and facts.last <= window.end), {
            "candidate_index": n,
            "score_input": candidate.score_input,
            "rows": facts.rows,
            "min": facts.first.isoformat(),
            "max": facts.last.isoformat(),
            "start": window.start.isoformat(),
            "end": window.end.isoformat(),
        }, candidate.candidate_id)


def _rule_duplicate_trial(m: Manifest, obs: Observed) -> Iterator[Entry]:
    ids_by_trial: dict[Trial, set[str]] = defaultdict(set)
    for candidate, trial in zip(m.candidates, obs.trials):
        ids_by_trial[trial].add(candidate.candidate_id)
    for n, (candidate, trial) in enumerate(zip(m.candidates, obs.trials)):
        others = sorted(ids_by_trial[trial] - {candidate.candidate_id})
        yield _entry("DUPLICATE_TRIAL", _verdict(not others), {
            "candidate_index": n,
            "trial": dict(trial),
            "same_declared_trial_as": others,
        }, candidate.candidate_id)


def _rule_repeated_spec(m: Manifest, obs: Observed) -> Iterator[Entry]:
    by_spec: dict[str, list[tuple[str, Trial]]] = defaultdict(list)
    for spec, candidate, trial in zip(obs.specs, m.candidates, obs.trials):
        by_spec[spec.sha256].append((candidate.candidate_id, trial))
    for sha, uses in sorted(by_spec.items()):
        distinct = {trial for _, trial in uses}
        if len(distinct) > 1:
            yield _entry("REPEATED_SPEC", "INFO", {
                "spec_sha256": sha,
                "candidate_ids": sorted({cid for cid, _ in uses}),
                "distinct_trials": len(distinct),
                "note": "same spec bytes in different declared trials; informational, not a duplicate",
            })


def _rule_recorded_trials(m: Manifest, obs: Observed) -> Iterator[Entry]:
    yield _entry("RECORDED_TRIALS", "INFO", {
        "recorded_candidates": len(m.candidates),
        "distinct_candidate_ids": len({c.candidate_id for c in m.candidates}),
        "by_family": dict(sorted(Counter(c.family for c in m.candidates).items())),
        "note": "counts only candidates explicitly recorded in this manifest; off-ledger trials "
                "are unobservable, so this is not a complete search denominator",
    })


RULES: tuple[Callable[[Manifest, Observed], Iterator[Entry]], ...] = (
    _rule_hash,
    _rule_duplicate_id,
    _rule_window,
    _rule_input_visibility,
    _rule_score_dates,
    _rule_duplicate_trial,
    _rule_repeated_spec,
    _rule_recorded_trials,
)


# --- API and CLI -------------------------------------------------------------

def audit(manifest_path: str | Path, root: str | Path, max_bytes: int = DEFAULT_MAX_BYTES) -> dict:
    """Audit a campaign manifest against files under root.

    Returns a report dict whose "overall" is "PASS" or "FAIL"; INFO entries
    never cause FAIL. Raises Malformed for schema, data, path or I/O problems.
    """
    if type(max_bytes) is not int or not 0 < max_bytes <= MAX_FILE_BYTES:
        raise Malformed(f"max_bytes: expected an integer from 1 to {MAX_FILE_BYTES}")
    root_dir = _root(root)
    manifest = load_manifest(manifest_path)
    observed = _observe(manifest, root_dir, max_bytes)
    evidence = [entry for rule in RULES for entry in rule(manifest, observed)]
    counts = Counter(entry["status"] for entry in evidence)
    return {
        "tool": "factoraudit",
        "version": __version__,
        "campaign_id": manifest.campaign_id,
        "overall": "FAIL" if counts["FAIL"] else "PASS",
        "counts": {status: counts[status] for status in STATUSES},
        "evidence": evidence,
        "scope": list(SCOPE),
    }


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise Malformed(f"usage: {message}")


def _emit(report: dict) -> None:
    sys.stdout.write(json.dumps(report, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(prog="factoraudit", allow_abbrev=False,
                     description="Static audit of a declared factor-research campaign manifest.")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("audit", allow_abbrev=False, help="audit a manifest against files under --root")
    run.add_argument("--manifest", required=True, help="strict JSON campaign manifest")
    run.add_argument("--root", required=True, help="directory containing all referenced files")
    run.add_argument("--max-bytes", default=str(DEFAULT_MAX_BYTES),
                     help=f"per-file byte limit, 1..{MAX_FILE_BYTES} (default {DEFAULT_MAX_BYTES})")
    try:
        args = parser.parse_args(argv)
        if not DECIMAL_RE.fullmatch(args.max_bytes):
            raise Malformed(f"max_bytes: expected an integer from 1 to {MAX_FILE_BYTES}")
        report = audit(args.manifest, args.root, int(args.max_bytes))
    except Malformed as exc:
        _emit({"tool": "factoraudit", "version": __version__, "overall": "MALFORMED", "error": str(exc)})
        return 3
    _emit(report)
    return 1 if report["overall"] == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())