import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from check_requirements import (
    CheckResult,
    Outcome,
    PyPILookupError,
    check_requirement,
    classify_requires_python,
    exit_code_for,
    fetch_json_or_none_when_missing,
    find_highest_requirement,
    lowest_supported_python,
)


def http_error(status_code):
    return HTTPError("https://pypi.org", status_code, "error", {}, io.BytesIO())


class CompatibilityTests(unittest.TestCase):
    def test_outcome_for_each_requires_python(self):
        expected_outcomes = {
            "": Outcome.COMPATIBLE,
            ">=3.7": Outcome.COMPATIBLE,
            ">=3.11": Outcome.COMPATIBLE,
            ">=3.11.4": Outcome.COMPATIBLE,
            "<3.12": Outcome.COMPATIBLE,
            "<=3.11": Outcome.COMPATIBLE,
            "~=3.10": Outcome.COMPATIBLE,
            "==3.11.*": Outcome.COMPATIBLE,
            "<3.11": Outcome.INCOMPATIBLE,
            ">=3.7,<3.11": Outcome.INCOMPATIBLE,
            ">=3.7,<3.11.0": Outcome.INCOMPATIBLE,
            ">=3.12": Outcome.INCOMPATIBLE,
            "!=3.11.*": Outcome.INCOMPATIBLE,
            "==3.10.*": Outcome.INCOMPATIBLE,
            ">=3.6.*": Outcome.UNREADABLE_REQUIRES_PYTHON,
        }
        for requires_python, outcome in expected_outcomes.items():
            with self.subTest(requires_python):
                self.assertEqual(classify_requires_python(requires_python), outcome)


class MinimumVersionTests(unittest.TestCase):
    def test_lowest_supported_python(self):
        expected_minimums = {
            ">=3.10": "3.10",
            ">=3.8,<3.11": "3.8",
            ">=2.7, !=3.0.*, !=3.1.*, !=3.2.*": "2.7",
            ">=3.0, !=3.0.*, !=3.1.*": "3.2",
            ">3.8": "3.8.1",
            "~=3.9": "3.9",
            "==3.10.*": "3.10",
            "<3.12": "0",
        }
        for requires_python, minimum in expected_minimums.items():
            with self.subTest(requires_python):
                self.assertEqual(lowest_supported_python(SpecifierSet(requires_python)), Version(minimum))

    def test_impossible_range_has_no_minimum(self):
        self.assertIsNone(lowest_supported_python(SpecifierSet(">=3.12,<3.10")))


class HighestRequirementTests(unittest.TestCase):
    def test_compares_numerically_and_includes_incompatible_releases(self):
        results = [
            CheckResult("nine==1", Outcome.COMPATIBLE, ">=3.9"),
            CheckResult("ten==1", Outcome.COMPATIBLE, ">=3.10"),
            CheckResult("twelve==1", Outcome.INCOMPATIBLE, ">=3.12"),
        ]
        self.assertEqual(find_highest_requirement(results).label, "twelve==1")

    def test_ignores_releases_without_requires_python(self):
        results = [CheckResult("bare==1", Outcome.COMPATIBLE, ""), CheckResult("set==1", Outcome.COMPATIBLE, "<3.12")]
        self.assertEqual(find_highest_requirement(results).label, "set==1")

    def test_invalid_requires_python_cannot_win_or_crash(self):
        results = [CheckResult("bad==1", Outcome.UNREADABLE_REQUIRES_PYTHON, ">=3.6.*")]
        self.assertIsNone(find_highest_requirement(results))

    def test_first_in_file_wins_a_tie(self):
        results = [
            CheckResult("first==1", Outcome.COMPATIBLE, ">=3.9"),
            CheckResult("second==1", Outcome.COMPATIBLE, ">=3.9"),
        ]
        self.assertEqual(find_highest_requirement(results).label, "first==1")


@patch("check_requirements.time.sleep")
class NetworkFailureTests(unittest.TestCase):
    def test_404_means_missing(self, _sleep):
        with patch("check_requirements.urlopen", side_effect=http_error(404)) as opener:
            self.assertIsNone(fetch_json_or_none_when_missing("nope"))
        self.assertEqual(opener.call_count, 1)

    def test_retryable_failure_recovers(self, _sleep):
        success = io.BytesIO(b'{"info": {}}')
        with patch("check_requirements.urlopen", side_effect=[http_error(503), success]):
            self.assertEqual(fetch_json_or_none_when_missing("flask"), {"info": {}})

    def test_outage_is_never_reported_as_not_found(self, _sleep):
        for failure in (http_error(429), http_error(500), URLError("dns"), TimeoutError()):
            with self.subTest(failure), patch("check_requirements.urlopen", side_effect=failure):
                result = check_requirement(Requirement("flask==2.0"))
                self.assertEqual(result.outcome, Outcome.LOOKUP_FAILED)
                self.assertEqual(exit_code_for([result]), 2)

    def test_non_retryable_status_stops_immediately(self, _sleep):
        with patch("check_requirements.urlopen", side_effect=http_error(403)) as opener:
            with self.assertRaises(PyPILookupError):
                fetch_json_or_none_when_missing("flask")
        self.assertEqual(opener.call_count, 1)


if __name__ == "__main__":
    unittest.main()
