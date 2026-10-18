"""Tests for the apportion seat calculator. Run with: python3 -m pytest tests"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import apportion  # noqa: E402

SAMPLE = ROOT / "examples" / "council.txt"


def load(text):
    election, issues = apportion.parse_election(text)
    return election, issues + apportion.lint(election)


def sample_election():
    election, issues = load(SAMPLE.read_text(encoding="utf-8"))
    assert not [i for i in issues if i.level == "error"]
    return election


class ParsingTests(unittest.TestCase):
    def test_parses_sample_file_fields(self):
        election = sample_election()
        self.assertEqual(election.seats, 9)
        self.assertEqual(election.method, "dhondt")
        self.assertEqual(election.threshold, Fraction(5))
        self.assertEqual([p.name for p in election.parties],
                         ["Greenfield", "Riverside", "Northgate", "Hilltop", "Fringe"])
        self.assertEqual(election.parties[0].votes, 41200)

    def test_errors_carry_line_numbers(self):
        text = "seats 0\nparty A x\nfoo bar\nmethod nope\nthreshold 5\nparty B 10\nparty b 3\n"
        _, issues = apportion.parse_election(text)
        errors = [i.line for i in issues if i.level == "error"]
        self.assertEqual(errors, [1, 2, 3, 4, 5, 7])

    def test_duplicate_single_directive_is_an_error(self):
        _, issues = apportion.parse_election("seats 3\nseats 4\nparty A 1\n")
        self.assertEqual([(i.line, i.level) for i in issues], [(2, "error")])

    def test_missing_seats_and_no_parties_are_reported(self):
        _, issues = load("method sainte-lague\n")
        messages = [i.message for i in issues]
        self.assertIn("missing 'seats' line", messages)
        self.assertIn("no 'party' lines found", messages)

    def test_method_aliases_normalize(self):
        self.assertEqual(apportion.normalize_method("Sainte-Laguë"), "sainte-lague")
        self.assertEqual(apportion.normalize_method("Hare"), "hamilton")
        self.assertEqual(apportion.normalize_method("jefferson"), "dhondt")
        self.assertIsNone(apportion.normalize_method("borda"))


class FormattingTests(unittest.TestCase):
    def test_round_half_up_exact(self):
        self.assertEqual(apportion.fmt(Fraction(1, 3), 3), "0.333")
        self.assertEqual(apportion.fmt(Fraction(1, 8), 2), "0.13")
        self.assertEqual(apportion.fmt(Fraction(5, 2), 0), "3")
        self.assertEqual(apportion.fmt(Fraction(-1, 4), 1), "-0.3")


class AllocationTests(unittest.TestCase):
    def test_dhondt_on_sample(self):
        result = apportion.allocate(sample_election())
        self.assertEqual(result.won, {"Greenfield": 4, "Riverside": 3, "Northgate": 2,
                                      "Hilltop": 0, "Fringe": 0})
        self.assertEqual(result.excluded, ["Fringe"])

    def test_sainte_lague_gives_hilltop_a_seat(self):
        result = apportion.allocate(sample_election(), "sainte-lague")
        self.assertEqual([result.won[n] for n in
                          ["Greenfield", "Riverside", "Northgate", "Hilltop"]], [4, 2, 2, 1])

    def test_hamilton_on_sample(self):
        result = apportion.allocate(sample_election(), "hamilton")
        self.assertEqual([result.won[n] for n in
                          ["Greenfield", "Riverside", "Northgate", "Hilltop"]], [4, 3, 2, 0])

    def test_huntington_hill_on_sample(self):
        result = apportion.allocate(sample_election(), "huntington-hill")
        self.assertEqual([result.won[n] for n in
                          ["Greenfield", "Riverside", "Northgate", "Hilltop"]], [4, 2, 2, 1])

    def test_every_method_hands_out_exactly_the_seats(self):
        election = sample_election()
        for method in apportion.METHODS:
            with self.subTest(method=method):
                result = apportion.allocate(election, method)
                self.assertEqual(sum(result.won.values()), election.seats)

    def test_hamilton_gives_leftover_seats_to_largest_remainders(self):
        election, _ = load("seats 3\nparty A 5\nparty B 3\nparty C 2\n")
        result = apportion.allocate(election, "hamilton")
        self.assertEqual(result.won, {"A": 1, "B": 1, "C": 1})
        self.assertEqual(result.ties, [])

    def test_hamilton_tie_is_recorded_and_resolved_by_file_order(self):
        election, _ = load("seats 1\nparty A 1\nparty B 1\n")
        result = apportion.allocate(election, "hamilton")
        self.assertEqual(result.won, {"A": 1, "B": 0})
        self.assertEqual(len(result.ties), 1)
        self.assertIn("A and B", result.ties[0])

    def test_huntington_hill_needs_one_seat_per_party(self):
        election = sample_election()
        with self.assertRaises(ValueError):
            apportion.allocate(election, "huntington-hill", seats=3)
        errors = [i for i in apportion.lint(election) if i.level == "error"]
        self.assertEqual(errors, [])

    def test_threshold_uses_share_of_all_valid_votes(self):
        election = sample_election()
        kept, excluded = apportion._split(election)
        self.assertEqual(excluded, ["Fringe"])
        self.assertIn("Hilltop", [p.name for p in kept])

    def test_zero_vote_party_never_wins(self):
        election, _ = load("seats 2\nparty A 10\nparty Z 0\n")
        result = apportion.allocate(election, "dhondt")
        self.assertEqual(result.won, {"A": 2, "Z": 0})


class QuotaAndDisproportionalityTests(unittest.TestCase):
    def test_quota_violation_is_detected(self):
        election, _ = load("seats 2\nparty A 1\nparty B 1\n")
        result = apportion.Result(
            method="dhondt", seats=2, total_votes=2, eligible_votes=2,
            won={"A": 3, "B": 0}, ideal={"A": Fraction(3, 2), "B": Fraction(1, 2)},
            excluded=[], ties=[], log=[])
        self.assertEqual([v[0] for v in apportion.quota_violations(result)], ["A"])

    def test_sample_respects_quota(self):
        result = apportion.allocate(sample_election())
        self.assertEqual(apportion.quota_violations(result), [])

    def test_loosemore_hanby_index(self):
        election, _ = load("seats 2\nparty A 3\nparty B 1\n")
        result = apportion.allocate(election, "dhondt")
        self.assertEqual(result.won, {"A": 2, "B": 0})
        self.assertEqual(apportion.loosemore_hanby(election, result), Fraction(1, 4))

    def test_proportional_result_has_zero_index(self):
        election, _ = load("seats 4\nparty A 3\nparty B 1\n")
        result = apportion.allocate(election, "hamilton")
        self.assertEqual(apportion.loosemore_hanby(election, result), 0)


class ParadoxTests(unittest.TestCase):
    def test_divisor_methods_never_lose_a_seat_when_house_grows(self):
        election, _ = load("party A 17\nparty B 9\nparty C 4\n")
        for method in ("dhondt", "sainte-lague", "modified-sainte-lague"):
            with self.subTest(method=method):
                results = [apportion.allocate(election, method, h) for h in range(1, 26)]
                self.assertEqual(apportion.house_paradoxes(results), [])

    def test_paradox_report_names_the_party_and_sizes(self):
        before = apportion.Result("hamilton", 10, 0, 0, {"X": 3, "Y": 7}, {}, [], [], [])
        after = apportion.Result("hamilton", 11, 0, 0, {"X": 2, "Y": 9}, {}, [], [], [])
        report = apportion.house_paradoxes([before, after])
        self.assertEqual(report, ["X loses a seat when the house grows from 10 to 11 (3 -> 2)"])


class CliTests(unittest.TestCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = apportion.main(list(argv))
        return code, out.getvalue()

    def test_check_passes_on_sample(self):
        code, out = self.run_cli("check", str(SAMPLE))
        self.assertEqual(code, 0)
        self.assertIn("Fringe", out)  # threshold warning is printed

    def test_check_fails_on_bad_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.txt"
            bad.write_text("seats many\n", encoding="utf-8")
            code, out = self.run_cli("check", str(bad))
        self.assertEqual(code, 2)
        self.assertIn(":1: error:", out)

    def test_run_json_matches_allocation(self):
        code, out = self.run_cli("run", str(SAMPLE), "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["method"], "dhondt")
        self.assertEqual(data["allocation"]["Greenfield"], 4)
        self.assertEqual(data["excluded"], ["Fringe"])

    def test_run_text_shows_table_and_index(self):
        code, out = self.run_cli("run", str(SAMPLE))
        self.assertEqual(code, 0)
        self.assertIn("Greenfield", out)
        self.assertIn("Disproportionality (Loosemore-Hanby)", out)

    def test_unknown_method_flag_exits_2(self):
        code, _ = self.run_cli("run", str(SAMPLE), "--method", "borda")
        self.assertEqual(code, 2)

    def test_sweep_reports_range(self):
        code, out = self.run_cli("sweep", str(SAMPLE), "--from", "4", "--to", "6")
        self.assertEqual(code, 0)
        self.assertIn("No house-size paradox", out)

    def test_missing_file_exits_2(self):
        code, _ = self.run_cli("check", str(ROOT / "does-not-exist.txt"))
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
