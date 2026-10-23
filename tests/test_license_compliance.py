"""Tests for license policy classification.

The policy below is synthetic: invented license names in the same shape as a
real policy file. No real policy content is used in this repository.
"""

import csv
import io
import json
import sys

import pytest

from docker_package_inspector import cli
from docker_package_inspector.license_compliance import (
    LicensePolicy,
    LicensePolicyError,
)

POLICY = {
    "low_risk": [
        "Alpha-1.0",
        "Beta License",
        "alpha-1.0",
        "Gamma-2.0 OR Alpha-1.0",
        "Shared-1.0",
    ],
    "copy_left": ["Delta-3.0", "Delta-3.0-only", "Shared-1.0"],
    "non_commerical": ["Epsilon Non-Commercial License"],
}


@pytest.fixture
def policy():
    return LicensePolicy(POLICY)


def test_whole_expression_entry_matches_first(policy):
    assert policy.evaluate("Gamma-2.0 OR Alpha-1.0") == {
        "license_category": "low_risk",
        "osrb_review_required": False,
        "license_unmatched": [],
    }


def test_matching_is_case_and_whitespace_insensitive(policy):
    assert policy.evaluate("  beta   LICENSE ")["license_category"] == "low_risk"


def test_most_restrictive_part_wins(policy):
    result = policy.evaluate("Alpha-1.0 | Delta-3.0")
    assert result["license_category"] == "copy_left"
    assert result["osrb_review_required"] is True

    assert policy.evaluate("(Alpha-1.0 AND Epsilon Non-Commercial License)")[
        "license_category"
    ] == ("non_commercial")


def test_legacy_cargo_slash_alternatives(policy):
    assert policy.evaluate("Alpha-1.0/Delta-3.0")["license_category"] == "copy_left"
    assert policy.evaluate("Alpha-1.0 / Beta License")["license_category"] == (
        "low_risk"
    )


def test_with_exception_falls_back_to_base_license(policy):
    assert policy.evaluate("Delta-3.0 WITH Some-exception")["license_category"] == (
        "copy_left"
    )


def test_unknown_and_unmatched_always_require_review(policy):
    for text in ("Unknown", "", None):
        assert policy.evaluate(text) == {
            "license_category": "unknown",
            "osrb_review_required": True,
            "license_unmatched": [],
        }

    result = policy.evaluate("Alpha-1.0 | Made-Up-9.9")
    assert result["license_category"] == "unknown"
    assert result["osrb_review_required"] is True
    assert result["license_unmatched"] == ["Made-Up-9.9"]

    url = "https://example.org/licenses/alpha"
    assert policy.evaluate(url)["license_unmatched"] == [url]


def test_name_listed_in_two_categories_keeps_most_restrictive(policy):
    assert policy.conflicts == 1
    assert policy.evaluate("Shared-1.0")["license_category"] == "copy_left"


@pytest.mark.parametrize(
    "content, message",
    [
        ("{not json", "cannot read"),
        ("[]", "expected a JSON object"),
        ('{"low_risk": "Alpha-1.0"}', "not a string list"),
        ('{"mystery": ["Alpha-1.0"]}', "unknown license category"),
        ('{"low_risk": []}', "no license names"),
    ],
)
def test_malformed_policy_files_fail(tmp_path, content, message):
    path = tmp_path / "policy.json"
    path.write_text(content)
    with pytest.raises(LicensePolicyError, match=message):
        LicensePolicy.from_file(str(path))


def test_cli_rejects_bad_policy_before_inspecting(tmp_path, monkeypatch, capsys):
    path = tmp_path / "policy.json"
    path.write_text("{truncated")

    def must_not_inspect(*args, **kwargs):
        raise AssertionError("images must not be inspected with a bad policy")

    monkeypatch.setattr(cli, "DockerImageInspector", must_not_inspect)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "docker-package-inspector",
            "--image",
            "x:1",
            "--license-compliance-file",
            str(path),
        ],
    )
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code != 0
    assert "--license-compliance-file" in capsys.readouterr().err


def _run_cli(tmp_path, monkeypatch, extra_args):
    packages = [
        {
            "name": "alpha-lib",
            "version": "1.0",
            "package_type": "python",
            "license": "Alpha-1.0",
        },
        {
            "name": "delta-lib",
            "version": "2.0",
            "package_type": "python",
            "license": "Delta-3.0",
        },
    ]

    class FakeInspector:
        def __init__(self, **kwargs):
            pass

        def inspect_image(self, image_name, architecture=None, pull=True):
            return {
                "digest": "sha256:x",
                "index_digest": "",
                "architecture": "amd64",
                "packages": [dict(p) for p in packages],
            }

    monkeypatch.setattr(cli, "DockerImageInspector", FakeInspector)
    json_path, csv_path = tmp_path / "out.json", tmp_path / "out.csv"
    monkeypatch.setattr(
        sys,
        "argv",
        ["docker-package-inspector", "--image", "x:1", "--json-output", str(json_path)]
        + ["--csv-output", str(csv_path)]
        + extra_args,
    )
    assert cli.main() == 0
    rows = list(csv.DictReader(io.StringIO(csv_path.read_text())))
    return json.loads(json_path.read_text()), rows


def test_cli_adds_compliance_fields_only_with_policy(tmp_path, monkeypatch):
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(POLICY))

    data, rows = _run_cli(
        tmp_path, monkeypatch, ["--license-compliance-file", str(policy_path)]
    )
    by_name = {p["name"]: p for p in data["results"][0]["packages"]}
    assert by_name["alpha-lib"]["osrb_review_required"] is False
    assert by_name["delta-lib"]["license_category"] == "copy_left"
    assert rows[0]["license_category"] == "low_risk"

    data, rows = _run_cli(tmp_path, monkeypatch, [])
    assert "license_category" not in data["results"][0]["packages"][0]
    assert "license_category" not in rows[0]
