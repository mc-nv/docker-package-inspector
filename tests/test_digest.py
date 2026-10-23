"""Tests for per-platform manifest digest resolution."""

import json
import subprocess

from docker_package_inspector import inspector as inspector_module
from docker_package_inspector.inspector import (
    DockerImageInspector,
    select_platform_digest,
)

INDEX = {
    "schemaVersion": 2,
    "manifests": [
        {
            "digest": "sha256:amd",
            "platform": {"architecture": "amd64", "os": "linux"},
        },
        {
            "digest": "sha256:arm",
            "platform": {"architecture": "arm64", "os": "linux", "variant": "v8"},
        },
        {
            "digest": "sha256:attestation",
            "platform": {"architecture": "unknown", "os": "unknown"},
        },
    ],
}
REF = "nvcr.io/nvidia/tritonserver@sha256:index"


def test_select_platform_digest():
    assert select_platform_digest(INDEX, "amd64") == "sha256:amd"
    assert select_platform_digest(INDEX, "arm64", "v8") == "sha256:arm"
    assert select_platform_digest(INDEX, "arm64") == "sha256:arm"
    assert select_platform_digest(INDEX, "arm64", "v7") == ""
    assert select_platform_digest(INDEX, "s390x") == ""
    assert select_platform_digest({"schemaVersion": 2, "layers": []}, "amd64") is None


def _run_returning(returncode, stdout="", stderr=""):
    def fake_run(cmd, **kwargs):
        assert cmd == ["docker", "manifest", "inspect", REF]
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

    return fake_run


def test_platform_digest_from_index(monkeypatch):
    monkeypatch.setattr(
        inspector_module.subprocess, "run", _run_returning(0, json.dumps(INDEX))
    )
    result = DockerImageInspector()._platform_digest(REF, "sha256:index", "arm64", "")
    assert result == ("sha256:arm", "sha256:index")


def test_single_platform_image_keeps_repo_digest(monkeypatch):
    single = {"schemaVersion": 2, "config": {}, "layers": []}
    monkeypatch.setattr(
        inspector_module.subprocess, "run", _run_returning(0, json.dumps(single))
    )
    result = DockerImageInspector()._platform_digest(REF, "sha256:index", "amd64", "")
    assert result == ("sha256:index", "")


def test_failures_fall_back_to_repo_digest(monkeypatch):
    insp = DockerImageInspector()

    monkeypatch.setattr(
        inspector_module.subprocess, "run", _run_returning(1, "", "unauthorized")
    )
    assert insp._platform_digest(REF, "sha256:index", "amd64", "") == (
        "sha256:index",
        "",
    )

    monkeypatch.setattr(inspector_module.subprocess, "run", _run_returning(0, "{x"))
    assert insp._platform_digest(REF, "sha256:index", "amd64", "") == (
        "sha256:index",
        "",
    )

    def missing_cli(cmd, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(inspector_module.subprocess, "run", missing_cli)
    assert insp._platform_digest(REF, "sha256:index", "amd64", "") == (
        "sha256:index",
        "",
    )

    # No arm64 entry in the index: keep the index digest rather than another
    # platform's
    monkeypatch.setattr(
        inspector_module.subprocess,
        "run",
        _run_returning(0, json.dumps({"manifests": INDEX["manifests"][:1]})),
    )
    assert insp._platform_digest(REF, "sha256:index", "arm64", "") == (
        "sha256:index",
        "",
    )


def test_tag_reference_resolves_platform_digest_without_index_digest(monkeypatch):
    tag = "python:3.12-alpine"

    def fake_run(cmd, **kwargs):
        assert cmd == ["docker", "manifest", "inspect", tag]
        return subprocess.CompletedProcess(cmd, 0, json.dumps(INDEX), "")

    monkeypatch.setattr(inspector_module.subprocess, "run", fake_run)
    assert DockerImageInspector()._platform_digest(tag, "", "arm64", "v8") == (
        "sha256:arm",
        "",
    )


def test_locally_built_image_without_repo_digest_is_not_inspected(monkeypatch):
    def must_not_run(cmd, **kwargs):
        raise AssertionError("docker manifest inspect should not run")

    monkeypatch.setattr(inspector_module.subprocess, "run", must_not_run)
    assert DockerImageInspector()._platform_digest("", "", "amd64", "") == ("", "")
