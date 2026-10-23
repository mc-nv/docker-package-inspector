"""Tests for unowned runtime discovery."""

from docker_package_inspector.package_metadata import PackageMetadataFetcher
from docker_package_inspector.runtimes import (
    parse_runtime_scan,
    python_license_candidates,
)


def test_parse_runtime_scan():
    output = (
        "@@RUNTIME\topenssl\t3.0.13\t/usr/local/lib/python3.12/site-packages/"
        "psycopg2_binary.libs/libssl-81ffa89e.so.3\n"
        "@@RUNTIME\topenssl\t\t/opt/x/libcrypto.so.1.1\n"
        "@@RUNTIME\tpython\t3.12.7\t/usr/local/bin/python3.12\n"
        "@@PREFIX\t/usr/local/bin/python3.12\t/usr/local\n"
        "noise\n"
    )
    runtimes = parse_runtime_scan(output)

    assert [(r["kind"], r["version"]) for r in runtimes] == [
        ("openssl", "3.0.13"),
        ("openssl", ""),
        ("python", "3.12.7"),
    ]
    assert runtimes[2]["prefix"] == "/usr/local"
    assert python_license_candidates(runtimes[2]) == [
        "/usr/local/lib/python3.12/LICENSE.txt"
    ]
    assert python_license_candidates(runtimes[0]) == []


def test_psf_license_detected_from_cpython_license_text():
    text = "PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2\n----\n1. This LICENSE"
    assert PackageMetadataFetcher()._detect_license_from_content(text) == "PSF-2.0"
