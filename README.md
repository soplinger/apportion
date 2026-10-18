# apportion

**Offline seat apportionment for plain-text election files.**

`apportion` turns a small text file of party vote totals into exact seat allocations under five standard methods: D'Hondt, Sainte-Laguë, modified Sainte-Laguë, Hamilton (largest remainder) and Huntington-Hill. It uses exact fractions, so no answer depends on floating-point rounding. Ties and threshold exclusions are reported on the record, and every error points to a file line.

No network, no accounts, no API keys, and only the Python 3 standard library.

## Features

- **Five allocation methods**: D'Hondt, Sainte-Laguë, modified Sainte-Laguë (1.4 first divisor), Hamilton / largest remainder, and Huntington-Hill (equal proportions), with common aliases such as `jefferson`, `webster`, `hare` and `hh`.
- **Exact arithmetic**: quotients, quotas and priorities use `fractions.Fraction` and integers, so results are reproducible on any machine.
- **Plain-text input** with `seats`, `method`, `threshold` and `party NAME VOTES` lines, and `#` comments.
- **Electoral threshold** (for example `threshold 5%`), tested against all valid votes, with the excluded parties listed.
- **Line-numbered lint**: parse and semantic errors and warnings print as `file:line: level: message`.
- **Tie reporting**: every tie is recorded and resolved by file order, and the result says so.
- **Quota check**: flags any party whose seats fall outside the lower and upper quota (the Balinski-Young fairness test).
- **Disproportionality index**: the Loosemore-Hanby index, computed exactly from vote shares and seat shares.
- **Side-by-side comparison** of all five methods for the same election.
- **Step-by-step explanation** of each method's working, including the cutoff quotient and the next seat that would be awarded.
- **House-size sweep**: seats per party across a range of house sizes, with Alabama-paradox detection (a party losing a seat when the house grows).
- **JSON output** (`run --json`) for scripts, plus `--method` and `--seats` overrides for quick what-ifs.
- **CI-friendly exit codes**: `0` for success, `2` for input errors or invalid requests.

## Quick start

Save an election file, or use the bundled `examples/council.txt`:

```text
# Town council: 9 seats, five lists, 5% threshold.
seats 9
method dhondt
threshold 5%

party Greenfield 41200
party Riverside 28950
party Northgate 19870
party Hilltop 6010
party Fringe 1180
```

Then run:

```sh
python3 apportion.py check examples/council.txt
python3 apportion.py run examples/council.txt
python3 apportion.py compare examples/council.txt
python3 apportion.py explain examples/council.txt --method hamilton
python3 apportion.py sweep examples/council.txt --from 7 --to 11
```

With D'Hondt, the nine seats split 4 / 3 / 2 / 0 among Greenfield, Riverside, Northgate and Hilltop. Fringe is excluded by the 5% threshold (1180 of 97210 votes is about 1.2%). Sainte-Laguë instead gives Hilltop one seat, and Hamilton gives it none. Running `compare` shows that side by side.

## How to run

Requires Python 3 (the code uses `from __future__ import annotations`). There is nothing to install.

| Command | What it does |
| --- | --- |
| `check FILE` | Lint the file. Prints any issues and exits `2` on errors. |
| `run FILE [--method M] [--seats N] [--json]` | Allocate seats and print a table, or JSON with `--json`. |
| `compare FILE [--seats N]` | Show every method's seats for the same election. |
| `explain FILE [--method M] [--seats N]` | Print the working, one seat at a time. |
| `sweep FILE --from A --to B [--method M]` | Seats per party across house sizes A to B, with paradox warnings. |

Method names: `dhondt`, `sainte-lague`, `modified-sainte-lague`, `hamilton`, `huntington-hill`.

### Input format

- `seats N`: the house size (required, at least 1).
- `method NAME`: the default method. Defaults to `dhondt`.
- `threshold P%`: minimum share of all valid votes to be eligible. Defaults to `0%`.
- `party NAME VOTES`: one line per party. The last token is the vote count, so names may contain spaces.
- `#` starts a comment. Each directive may appear once, except `party`.

## How to test

```sh
python3 -m pytest tests
```

The tests use only the standard library's `unittest`, so `python3 -m unittest discover tests` works too. They check the exact seat outcomes for the sample on each method, Hamilton tie handling, the quota and Loosemore-Hanby calculations, the no-paradox property of divisor methods, line-numbered parse errors, and the CLI exit codes.

## Project layout

```text
apportion.py           the whole tool: parser, allocation, reports and CLI
examples/council.txt   sample election file
tests/test_apportion.py
```

## Notes

- Divisor methods (D'Hondt, Sainte-Laguë, modified Sainte-Laguë) cannot lose a seat when the house grows, and the sweep checks this. Hamilton can, which is the Alabama paradox.
- Huntington-Hill gives every eligible party one seat before applying its priority values, so the house must have at least as many seats as eligible parties.
- Ties are settled by file order and always reported. For a real election, check the ties against the governing rules.
