"""Tests for image diff computation."""

from docker_package_inspector.cli import _compute_package_diff


def _pkg(name, version, package_type):
    return {"name": name, "version": version, "package_type": package_type}


def test_same_name_different_type_not_merged():
    old = {"packages": [_pkg("regex", "2024.1", "python")]}
    new = {
        "packages": [
            _pkg("regex", "2024.1", "python"),
            _pkg("regex", "1.10.0", "rust"),
        ]
    }

    diff = _compute_package_diff(old, new)

    assert [(p["name"], p["package_type"]) for p in diff["added"]] == [
        ("regex", "rust")
    ]
    assert diff["removed"] == []
    assert diff["changed"] == []


def test_name_match_is_case_insensitive():
    old = {"packages": [_pkg("Jinja2", "3.1.3", "python")]}
    new = {"packages": [_pkg("jinja2", "3.1.4", "python")]}

    diff = _compute_package_diff(old, new)

    assert diff["added"] == [] and diff["removed"] == []
    assert diff["changed"][0]["version_from"] == "3.1.3"


def test_exclusion_is_type_aware():
    old = {"packages": []}
    new = {
        "packages": [_pkg("cmake", "3.28", "python"), _pkg("cmake", "3.28", "binary")]
    }
    exclusion = {("binary", "cmake"): _pkg("cmake", "3.28", "binary")}

    diff = _compute_package_diff(old, new, exclusion, "base:1")

    by_type = {p["package_type"]: p["change_type"] for p in diff["added"]}
    assert by_type == {"python": "ADDED", "binary": "ADDED-INHERITED"}
