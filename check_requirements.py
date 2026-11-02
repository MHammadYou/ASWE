import argparse
import codecs
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum, auto
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

PYPI_BASE_URL = "https://pypi.org/pypi"
TARGET_PYTHON = "3.11"
HIGHEST_PATCH_TO_TRY = 99
REQUEST_TIMEOUT_SECONDS = 20
MAX_REQUEST_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 1
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
PARALLEL_LOOKUPS = 8
USER_AGENT = "check-requirements/1.0"

EXIT_ALL_CLEAR = 0
EXIT_PROBLEMS_FOUND = 1
EXIT_COULD_NOT_CHECK = 2


class PyPILookupError(Exception):
    pass


class Outcome(Enum):
    COMPATIBLE = auto()
    INCOMPATIBLE = auto()
    UNREADABLE_REQUIRES_PYTHON = auto()
    PROJECT_MISSING = auto()
    VERSION_MISSING = auto()
    UNPINNED = auto()
    LOOKUP_FAILED = auto()


@dataclass(frozen=True)
class ParsedFile:
    line_count: int
    requirements: list[Requirement]
    unchecked_lines: list[tuple[str, str]]


@dataclass(frozen=True)
class CheckResult:
    label: str
    outcome: Outcome
    requires_python: str = ""
    failure: str = ""


def decode_requirements_bytes(raw: bytes) -> str:
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig", errors="replace")


def parse_requirements_text(text: str) -> ParsedFile:
    requirements = []
    unchecked_lines = []
    joined_continuations = re.sub(r"\\\r?\n", " ", text)
    for raw_line in joined_continuations.splitlines():
        line = re.sub(r"(^|\s+)#.*$", "", raw_line).strip()
        if not line:
            continue
        if line.startswith("-"):
            unchecked_lines.append((line, "pip option"))
            continue
        without_per_requirement_options = re.split(r"\s+--", line)[0]
        try:
            requirement = Requirement(without_per_requirement_options)
        except InvalidRequirement:
            unchecked_lines.append((line, "not a PyPI requirement"))
            continue
        if requirement.url:
            unchecked_lines.append((line, "direct URL"))
            continue
        requirements.append(requirement)
    return ParsedFile(len(text.splitlines()), requirements, unchecked_lines)


def pinned_version(requirement: Requirement) -> str | None:
    for specifier in requirement.specifier:
        if specifier.operator in ("==", "===") and not specifier.version.endswith(".*"):
            return specifier.version
    return None


def fetch_json_or_none_when_missing(url_path: str) -> dict | None:
    request = Request(f"{PYPI_BASE_URL}/{url_path}/json", headers={"User-Agent": USER_AGENT})
    failure = "no attempt made"
    for attempt in range(MAX_REQUEST_ATTEMPTS):
        if attempt:
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return json.load(response)
        except HTTPError as error:
            error.close()
            if error.code == 404:
                return None
            failure = f"HTTP {error.code}"
            if error.code not in RETRYABLE_STATUS_CODES:
                break
        except (OSError, HTTPException, ValueError) as error:
            failure = str(error) or type(error).__name__
    raise PyPILookupError(failure)


def supports_target_python(specifiers: SpecifierSet) -> bool:
    patch_releases = [f"{TARGET_PYTHON}.{patch}" for patch in range(HIGHEST_PATCH_TO_TRY + 1)]
    return any(specifiers.contains(candidate, prereleases=True) for candidate in [TARGET_PYTHON, *patch_releases])


def successors_of(version: Version) -> set[Version]:
    return {
        Version(f"{version.major}.{version.minor}.{version.micro + 1}"),
        Version(f"{version.major}.{version.minor + 1}"),
        Version(f"{version.major + 1}"),
    }


def lowest_supported_python(specifiers: SpecifierSet) -> Version | None:
    candidates = {Version("0")}
    for specifier in specifiers:
        try:
            bound = Version(specifier.version.removesuffix(".*"))
        except InvalidVersion:
            continue
        candidates |= {bound} | successors_of(bound)
    for candidate in sorted(candidates):
        if specifiers.contains(candidate, prereleases=True):
            return candidate
    return None


def classify_requires_python(requires_python: str) -> Outcome:
    if not requires_python:
        return Outcome.COMPATIBLE
    try:
        specifiers = SpecifierSet(requires_python)
    except InvalidSpecifier:
        return Outcome.UNREADABLE_REQUIRES_PYTHON
    return Outcome.COMPATIBLE if supports_target_python(specifiers) else Outcome.INCOMPATIBLE


def check_requirement(requirement: Requirement) -> CheckResult:
    name = requirement.name
    version = pinned_version(requirement)
    label = f"{name}=={version}" if version else name
    try:
        if version is None:
            project_exists = fetch_json_or_none_when_missing(quote(name)) is not None
            return CheckResult(label, Outcome.UNPINNED if project_exists else Outcome.PROJECT_MISSING)
        release = fetch_json_or_none_when_missing(f"{quote(name)}/{quote(version, safe='')}")
        if release is None:
            project_exists = fetch_json_or_none_when_missing(quote(name)) is not None
            return CheckResult(label, Outcome.VERSION_MISSING if project_exists else Outcome.PROJECT_MISSING)
    except PyPILookupError as error:
        return CheckResult(label, Outcome.LOOKUP_FAILED, failure=str(error))
    requires_python = (release["info"].get("requires_python") or "").strip()
    return CheckResult(label, classify_requires_python(requires_python), requires_python)


def find_highest_requirement(results: list[CheckResult]) -> CheckResult | None:
    ranked = []
    for result in results:
        if result.outcome not in (Outcome.COMPATIBLE, Outcome.INCOMPATIBLE) or not result.requires_python:
            continue
        minimum = lowest_supported_python(SpecifierSet(result.requires_python))
        if minimum is not None:
            ranked.append((minimum, result))
    return max(ranked, key=lambda entry: entry[0])[1] if ranked else None


def print_section(title: str, entries: list[str]) -> None:
    print(f"{title}: {len(entries)}")
    for entry in entries:
        print(f"  {entry}")


def print_report(parsed: ParsedFile, results: list[CheckResult]) -> None:
    def with_outcome(*outcomes: Outcome) -> list[CheckResult]:
        return [result for result in results if result.outcome in outcomes]

    missing_reasons = {
        Outcome.PROJECT_MISSING: "package does not exist",
        Outcome.VERSION_MISSING: "pinned version does not exist",
    }
    print(f"Lines in file: {parsed.line_count}")
    print(f"Requirements checked: {len(parsed.requirements)}")
    print_section(
        "Not found on PyPI",
        [
            f"{result.label} - {missing_reasons[result.outcome]}"
            for result in with_outcome(Outcome.PROJECT_MISSING, Outcome.VERSION_MISSING)
        ],
    )
    print_section(
        f"Incompatible with Python {TARGET_PYTHON}",
        [f"{result.label} - requires_python {result.requires_python}" for result in with_outcome(Outcome.INCOMPATIBLE)],
    )
    highest = find_highest_requirement(results)
    print(f"Highest requirement: {highest.label if highest else 'none'}")

    optional_sections = {
        "Lookup failed, status unknown": [
            f"{result.label} - {result.failure}" for result in with_outcome(Outcome.LOOKUP_FAILED)
        ],
        "Invalid requires_python, treated as compatible and not ranked": [
            f"{result.label} - {result.requires_python}"
            for result in with_outcome(Outcome.UNREADABLE_REQUIRES_PYTHON)
        ],
        "Exists but unpinned, no release to check": [result.label for result in with_outcome(Outcome.UNPINNED)],
        "Skipped lines": [f"{line} - {reason}" for line, reason in parsed.unchecked_lines],
    }
    for title, entries in optional_sections.items():
        if entries:
            print_section(title, entries)


def exit_code_for(results: list[CheckResult]) -> int:
    outcomes = {result.outcome for result in results}
    if Outcome.LOOKUP_FAILED in outcomes:
        return EXIT_COULD_NOT_CHECK
    if outcomes & {Outcome.PROJECT_MISSING, Outcome.VERSION_MISSING, Outcome.INCOMPATIBLE}:
        return EXIT_PROBLEMS_FOUND
    return EXIT_ALL_CLEAR


def main() -> int:
    argument_parser = argparse.ArgumentParser(
        description=f"Check a requirements file against PyPI for Python {TARGET_PYTHON} without downloading packages."
    )
    argument_parser.add_argument("requirements_file", nargs="?", default="requirements.txt", type=Path)
    arguments = argument_parser.parse_args()
    try:
        raw = arguments.requirements_file.read_bytes()
    except OSError as error:
        print(f"Cannot read {arguments.requirements_file}: {error}", file=sys.stderr)
        return EXIT_COULD_NOT_CHECK
    parsed = parse_requirements_text(decode_requirements_bytes(raw))
    with ThreadPoolExecutor(max_workers=PARALLEL_LOOKUPS) as executor:
        results = list(executor.map(check_requirement, parsed.requirements))
    print_report(parsed, results)
    return exit_code_for(results)


if __name__ == "__main__":
    sys.exit(main())
