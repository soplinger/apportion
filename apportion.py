#!/usr/bin/env python3
"""apportion: offline seat apportionment for plain-text election files.

Allocates seats with D'Hondt, Sainte-Laguë, modified Sainte-Laguë, Hamilton
(largest remainder) and Huntington-Hill. All arithmetic uses fractions.Fraction
or integers, so results never depend on floating-point rounding.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

METHODS = {
    "dhondt": "D'Hondt (highest averages)",
    "sainte-lague": "Sainte-Laguë (odd divisors)",
    "modified-sainte-lague": "Modified Sainte-Laguë (1.4 first divisor)",
    "hamilton": "Hamilton (largest remainder)",
    "huntington-hill": "Huntington-Hill (equal proportions)",
}
METHOD_ALIASES = {
    "d'hondt": "dhondt",
    "jefferson": "dhondt",
    "saint-lague": "sainte-lague",
    "webster": "sainte-lague",
    "sl": "sainte-lague",
    "msl": "modified-sainte-lague",
    "hare": "hamilton",
    "largest-remainder": "hamilton",
    "hh": "huntington-hill",
    "equal-proportions": "huntington-hill",
}
SINGLE_DIRECTIVES = ("seats", "method", "threshold")
WHOLE_NUMBER = re.compile(r"[0-9]+")
PERCENT = re.compile(r"([0-9]+(?:\.[0-9]+)?)%")


@dataclass
class Party:
    name: str
    votes: int
    line: int


@dataclass
class Election:
    seats: int | None = None
    method: str = "dhondt"
    threshold: Fraction = Fraction(0)  # percent of all valid votes
    parties: list[Party] = field(default_factory=list)


@dataclass(frozen=True)
class Issue:
    line: int  # 0 means the problem is about the file as a whole
    level: str  # "error" or "warning"
    message: str


@dataclass
class Result:
    method: str
    seats: int
    total_votes: int
    eligible_votes: int
    won: dict[str, int]  # every party in file order, excluded ones at 0
    ideal: dict[str, Fraction]  # exact quota for eligible parties only
    excluded: list[str]
    ties: list[str]
    log: list[str]


def normalize_method(text: str) -> str | None:
    key = text.strip().lower().replace("ë", "e")
    if key in METHODS:
        return key
    return METHOD_ALIASES.get(key)


def fmt(value: Fraction, places: int = 3) -> str:
    """Format an exact value with round-half-up to a fixed number of places."""
    sign = "-" if value < 0 else ""
    scaled = math.floor(abs(value) * 10**places + Fraction(1, 2))
    whole, frac = divmod(scaled, 10**places)
    if places == 0:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{frac:0{places}d}"


# ---------------------------------------------------------------- parsing

def parse_election(text: str) -> tuple[Election, list[Issue]]:
    election = Election()
    issues: list[Issue] = []
    first_line: dict[str, int] = {}
    party_lines: dict[str, int] = {}

    for lineno, raw in enumerate(text.splitlines(), start=1):
        body = raw.split("#", 1)[0].strip()
        if not body:
            continue
        parts = body.split(None, 1)
        directive = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        def error(message: str) -> None:
            issues.append(Issue(lineno, "error", message))

        if directive in SINGLE_DIRECTIVES and directive in first_line:
            error(f"duplicate '{directive}' line (first on line {first_line[directive]})")
            continue

        if directive == "seats":
            if WHOLE_NUMBER.fullmatch(rest) and int(rest) >= 1:
                election.seats = int(rest)
            else:
                error("seats must be a whole number of at least 1")
                continue
        elif directive == "method":
            method = normalize_method(rest)
            if method is None:
                error(f"unknown method {rest!r}; choose one of: {', '.join(METHODS)}")
                continue
            election.method = method
        elif directive == "threshold":
            match = PERCENT.fullmatch(rest)
            if match is None or not 0 <= Fraction(match.group(1)) <= 100:
                error("threshold must look like '5%' and be between 0% and 100%")
                continue
            election.threshold = Fraction(match.group(1))
        elif directive == "party":
            pieces = rest.rsplit(None, 1)
            if len(pieces) != 2:
                error("party needs a name and a vote count, e.g. 'party Alpha 1200'")
                continue
            name, votes_text = pieces[0].strip(), pieces[1]
            if not WHOLE_NUMBER.fullmatch(votes_text):
                error(f"vote count {votes_text!r} must be a whole number")
                continue
            key = name.casefold()
            if key in party_lines:
                error(f"duplicate party {name!r} (first on line {party_lines[key]})")
                continue
            party_lines[key] = lineno
            election.parties.append(Party(name, int(votes_text), lineno))
        else:
            error(f"unknown directive {parts[0]!r}")
            continue

        if directive in SINGLE_DIRECTIVES:
            first_line[directive] = lineno

    return election, issues


def lint(election: Election) -> list[Issue]:
    """Semantic checks that need the whole file."""
    issues: list[Issue] = []
    if election.seats is None:
        issues.append(Issue(0, "error", "missing 'seats' line"))
    if not election.parties:
        issues.append(Issue(0, "error", "no 'party' lines found"))
        return issues
    total = sum(p.votes for p in election.parties)
    if total == 0:
        issues.append(Issue(0, "error", "every party has 0 votes; nothing to apportion"))
        return issues
    for p in election.parties:
        if p.votes == 0:
            issues.append(Issue(p.line, "warning", f"party {p.name!r} has no votes and cannot win a seat"))
    kept, excluded = _split(election)
    for name in excluded:
        party = next(p for p in election.parties if p.name == name)
        share = fmt(Fraction(party.votes * 100, total), 2)
        issues.append(Issue(party.line, "warning",
                            f"party {name!r} has {share}% of votes, below the threshold, and is excluded"))
    if election.method == "huntington-hill" and election.seats is not None and len(kept) > election.seats:
        issues.append(Issue(0, "error",
                            f"seats ({election.seats}) is fewer than the {len(kept)} eligible parties; "
                            "Huntington-Hill gives each eligible party one seat first"))
    return issues


# ---------------------------------------------------------- allocation

def _split(election: Election) -> tuple[list[Party], list[str]]:
    """Return (eligible parties, names excluded by the threshold)."""
    total = sum(p.votes for p in election.parties)
    kept = [p for p in election.parties
            if p.votes > 0 and p.votes * 100 >= election.threshold * total]
    kept_names = {p.name for p in kept}
    excluded = [p.name for p in election.parties if p.votes > 0 and p.name not in kept_names]
    return kept, excluded


def _divisor(method: str, n: int) -> Fraction:
    """Divisor used to price the next seat for a party that already holds n seats."""
    if method == "dhondt":
        return Fraction(n + 1)
    if method == "modified-sainte-lague" and n == 0:
        return Fraction(7, 5)
    return Fraction(2 * n + 1)


def _priority(method: str, votes: int, n: int) -> Fraction:
    if method == "huntington-hill":
        # Squared priority votes / sqrt(n(n+1)); squaring keeps it exact.
        return Fraction(votes * votes, n * (n + 1))
    return Fraction(votes) / _divisor(method, n)


def _highest_quotients(method: str, kept: list[Party], seats: int) -> tuple[dict[str, int], list[str], list[str]]:
    won = {p.name: 0 for p in kept}
    ties: list[str] = []
    log: list[str] = []
    first = 1
    if method == "huntington-hill":
        for p in kept:
            won[p.name] = 1
        first = len(kept) + 1
        log.append(f"each of the {len(kept)} eligible parties takes one seat first")
    label = "squared priority" if method == "huntington-hill" else "quotient"
    for seat in range(first, seats + 1):
        best_value: Fraction | None = None
        best: list[Party] = []
        for p in kept:
            value = _priority(method, p.votes, won[p.name])
            if best_value is None or value > best_value:
                best_value, best = value, [p]
            elif value == best_value:
                best.append(p)
        winner = best[0]
        if len(best) > 1:
            names = ", ".join(p.name for p in best)
            ties.append(f"seat {seat}: {names} tied at {label} {fmt(best_value, 4)}; "
                        f"{winner.name} wins by file order")
        won[winner.name] += 1
        log.append(f"seat {seat}: {winner.name} with {label} {fmt(best_value, 4)}")
    if seats >= first:
        next_best = max(kept, key=lambda p: _priority(method, p.votes, won[p.name]))
        next_value = _priority(method, next_best.votes, won[next_best.name])
        log.append(f"next seat would go to {next_best.name} with {label} {fmt(next_value, 4)}")
    return won, ties, log


def _largest_remainder(kept: list[Party], seats: int) -> tuple[dict[str, int], list[str], list[str]]:
    total = sum(p.votes for p in kept)
    base: dict[str, int] = {}
    remainder: dict[str, int] = {}
    log = [f"standard quota = {total} / {seats} = {fmt(Fraction(total, seats), 4)}"]
    for p in kept:
        q, r = divmod(p.votes * seats, total)
        base[p.name], remainder[p.name] = q, r
        log.append(f"{p.name}: {p.votes} x {seats} / {total} = {q} remainder {r}")
    left = seats - sum(base.values())
    order = sorted(kept, key=lambda p: -remainder[p.name])  # stable: file order breaks ties
    won = dict(base)
    for p in order[:left]:
        won[p.name] += 1
    ties: list[str] = []
    if 0 < left < len(order) and remainder[order[left - 1].name] == remainder[order[left].name]:
        a, b = order[left - 1].name, order[left].name
        ties.append(f"remainder {remainder[a]}/{total} tied between {a} and {b}; "
                    f"{a} keeps the seat by file order")
    log.append(f"{left} leftover seat(s) go to the largest remainders: "
               + (", ".join(p.name for p in order[:left]) or "none"))
    return won, ties, log


def allocate(election: Election, method: str | None = None, seats: int | None = None) -> Result:
    method = normalize_method(method or election.method)
    if method is None:
        raise ValueError(f"unknown method; choose one of: {', '.join(METHODS)}")
    seats = election.seats if seats is None else seats
    if seats is None or seats < 1:
        raise ValueError("seats must be at least 1")
    kept, excluded = _split(election)
    if not kept:
        raise ValueError("no party is eligible for a seat")
    if method == "huntington-hill" and seats < len(kept):
        raise ValueError(f"seats ({seats}) is fewer than the {len(kept)} eligible parties")

    eligible_votes = sum(p.votes for p in kept)
    if method == "hamilton":
        won_kept, ties, log = _largest_remainder(kept, seats)
    else:
        won_kept, ties, log = _highest_quotients(method, kept, seats)
    won = {p.name: won_kept.get(p.name, 0) for p in election.parties}
    ideal = {p.name: Fraction(p.votes * seats, eligible_votes) for p in kept}
    return Result(
        method=method,
        seats=seats,
        total_votes=sum(p.votes for p in election.parties),
        eligible_votes=eligible_votes,
        won=won,
        ideal=ideal,
        excluded=excluded,
        ties=ties,
        log=log,
    )


def quota_violations(result: Result) -> list[tuple[str, Fraction, int, int, int]]:
    """Parties outside the lower/upper quota, the Balinski-Young fairness test."""
    bad = []
    for name, ideal in result.ideal.items():
        lower, upper = math.floor(ideal), math.ceil(ideal)
        got = result.won[name]
        if got < lower or got > upper:
            bad.append((name, ideal, lower, upper, got))
    return bad


def loosemore_hanby(election: Election, result: Result) -> Fraction:
    """Half the sum of absolute gaps between vote share and seat share."""
    total = result.total_votes
    gap = sum(abs(Fraction(p.votes, total) - Fraction(result.won[p.name], result.seats))
              for p in election.parties)
    return gap / 2


def house_paradoxes(results: list[Result]) -> list[str]:
    """Report parties that lose a seat when the house grows by one (Alabama paradox)."""
    found = []
    for small, large in zip(results, results[1:]):
        for name, before in small.won.items():
            after = large.won[name]
            if after < before:
                found.append(f"{name} loses a seat when the house grows from "
                             f"{small.seats} to {large.seats} ({before} -> {after})")
    return found


# ----------------------------------------------------------- rendering

def render_run(election: Election, result: Result) -> str:
    total = result.total_votes
    width = max([len("Party")] + [len(p.name) for p in election.parties])
    bad = {name for name, *_ in quota_violations(result)}
    lines = [
        f"Method:  {METHODS[result.method]}",
        f"Seats:   {result.seats}",
        f"Votes:   {result.eligible_votes} eligible of {total} valid",
        "",
        f"{'Party':<{width}}  {'Votes':>8}  {'Share':>8}  {'Ideal':>8}  {'Seats':>5}  Quota",
    ]
    for p in election.parties:
        share = fmt(Fraction(p.votes * 100, total), 2) + "%"
        if p.name in result.ideal:
            ideal = fmt(result.ideal[p.name], 3)
            quota = "VIOLATION" if p.name in bad else "ok"
        else:
            ideal, quota = "-", "excluded" if p.name in result.excluded else "no votes"
        lines.append(f"{p.name:<{width}}  {p.votes:>8}  {share:>8}  {ideal:>8}  "
                     f"{result.won[p.name]:>5}  {quota}")
    lines.append("")
    if result.excluded:
        lines.append(f"Excluded by threshold: {', '.join(result.excluded)}")
    for tie in result.ties:
        lines.append(f"Tie: {tie}")
    lines.append(f"Disproportionality (Loosemore-Hanby): {fmt(loosemore_hanby(election, result) * 100, 2)}%")
    return "\n".join(lines)


def to_json(election: Election, result: Result) -> str:
    data = {
        "method": result.method,
        "seats": result.seats,
        "total_votes": result.total_votes,
        "eligible_votes": result.eligible_votes,
        "allocation": result.won,
        "excluded": result.excluded,
        "ties": result.ties,
        "quota_violations": [
            {"party": name, "ideal": fmt(ideal, 6), "lower": lo, "upper": hi, "seats": got}
            for name, ideal, lo, hi, got in quota_violations(result)
        ],
        "loosemore_hanby": fmt(loosemore_hanby(election, result) * 100, 4),
    }
    return json.dumps(data, indent=2, ensure_ascii=False)


def render_compare(election: Election, seats: int) -> str:
    methods = list(METHODS)
    results = {}
    for m in methods:
        try:
            results[m] = allocate(election, m, seats)
        except ValueError:
            results[m] = None
    width = max([len("Party")] + [len(p.name) for p in election.parties])
    cols = [max(len(m), 5) for m in methods]
    header = f"{'Party':<{width}}  " + "  ".join(f"{m:>{c}}" for m, c in zip(methods, cols))
    lines = [f"Seats: {seats}", "", header]
    for p in election.parties:
        cells = []
        for m, c in zip(methods, cols):
            r = results[m]
            cells.append(f"{r.won[p.name] if r else 'n/a':>{c}}")
        lines.append(f"{p.name:<{width}}  " + "  ".join(cells))
    dis = []
    for m, c in zip(methods, cols):
        r = results[m]
        dis.append(f"{(fmt(loosemore_hanby(election, r) * 100, 2) + '%') if r else 'n/a':>{c}}")
    lines.append(f"{'Disprop.':<{width}}  " + "  ".join(dis))
    lines.append("")
    lines.append("Methods: " + ", ".join(f"{m} = {METHODS[m]}" for m in methods))
    return "\n".join(lines)


def render_explain(election: Election, result: Result) -> str:
    lines = [f"Working for {METHODS[result.method]}, {result.seats} seats:", ""]
    lines += [f"  {entry}" for entry in result.log]
    if result.excluded:
        lines.append("")
        lines.append(f"  excluded by threshold: {', '.join(result.excluded)}")
    if result.ties:
        lines.append("")
        lines += [f"  tie: {tie}" for tie in result.ties]
    return "\n".join(lines)


def render_sweep(election: Election, results: list[Result]) -> str:
    names = [p.name for p in election.parties]
    width = max([len("Seats")] + [len(n) for n in names])
    cols = [max(len(n), 3) for n in names]
    header = f"{'Seats':>{width}}  " + "  ".join(f"{n:>{c}}" for n, c in zip(names, cols))
    lines = [f"Method: {METHODS[results[0].method]}", "", header]
    for r in results:
        cells = "  ".join(f"{r.won[n]:>{c}}" for n, c in zip(names, cols))
        lines.append(f"{r.seats:>{width}}  {cells}")
    warnings = house_paradoxes(results)
    lines.append("")
    if warnings:
        lines += [f"Paradox: {w}" for w in warnings]
    else:
        lines.append("No house-size paradox in this range.")
    return "\n".join(lines)


# ----------------------------------------------------------------- CLI

def format_issue(path: str, issue: Issue) -> str:
    where = f"{path}:{issue.line}" if issue.line else path
    return f"{where}: {issue.level}: {issue.message}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="apportion", description="Offline seat apportionment calculator.")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="lint an election file (exit 2 on errors)")
    check.add_argument("file")

    run = sub.add_parser("run", help="allocate seats with one method")
    run.add_argument("file")
    run.add_argument("--method", help="override the file's method")
    run.add_argument("--seats", type=int, help="override the file's seat count")
    run.add_argument("--json", action="store_true", help="print machine-readable output")

    compare = sub.add_parser("compare", help="show every method side by side")
    compare.add_argument("file")
    compare.add_argument("--seats", type=int, help="override the file's seat count")

    explain = sub.add_parser("explain", help="show the step-by-step working")
    explain.add_argument("file")
    explain.add_argument("--method", help="override the file's method")
    explain.add_argument("--seats", type=int, help="override the file's seat count")

    sweep = sub.add_parser("sweep", help="seats per party across a range of house sizes")
    sweep.add_argument("file")
    sweep.add_argument("--from", dest="start", type=int, required=True)
    sweep.add_argument("--to", dest="stop", type=int, required=True)
    sweep.add_argument("--method", help="override the file's method")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    path = args.file
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"apportion: {exc}", file=sys.stderr)
        return 2

    election, issues = parse_election(text)
    issues = issues + lint(election)
    for issue in issues:
        stream = sys.stdout if args.command == "check" else sys.stderr
        print(format_issue(path, issue), file=stream)
    if args.command == "check":
        if not issues:
            print(f"{path}: ok ({len(election.parties)} parties, {election.seats} seats)")
        return 2 if any(i.level == "error" for i in issues) else 0
    if any(i.level == "error" for i in issues):
        return 2

    try:
        if args.command == "run":
            result = allocate(election, args.method, args.seats)
            print(to_json(election, result) if args.json else render_run(election, result))
        elif args.command == "compare":
            print(render_compare(election, args.seats or election.seats))
        elif args.command == "explain":
            result = allocate(election, args.method, args.seats)
            print(render_explain(election, result))
        elif args.command == "sweep":
            if args.stop < args.start:
                raise ValueError("--to must be at least --from")
            method = args.method or election.method
            results = [allocate(election, method, h) for h in range(args.start, args.stop + 1)]
            print(render_sweep(election, results))
    except ValueError as exc:
        print(f"apportion: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
