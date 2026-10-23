"""Tests for dist-info based Python package discovery."""

import subprocess
import textwrap

from docker_package_inspector.python_dists import (
    DIST_SCAN_SCRIPT,
    dist_info_version,
    extract_copyright_lines,
    group_dists,
    installer_to_provider,
    is_private_env,
    local_license,
    local_license_fallback,
    parse_dist_scan,
    project_url,
    runtime_requirements,
)

SCAN_OUTPUT = textwrap.dedent(
    """\
    @@DIST /opt/venv/lib/python3.12/site-packages/requests-2.32.3.dist-info
    @@INSTALLER uv
    @@METADATA
    Metadata-Version: 2.1
    Name: requests
    Version: 2.32.3
    License: Apache-2.0
    Requires-Dist: charset_normalizer<4,>=2
    Requires-Dist: idna<4,>=2.5
    Requires-Dist: PySocks!=1.5.7,>=1.5.6; extra == "socks"
    Project-URL: Source, https://github.com/psf/requests

    @@LICENSE_FILE /opt/venv/lib/python3.12/site-packages/requests-2.32.3.dist-info/LICENSE
    @@COPYRIGHT    Copyright 2019 Kenneth Reitz
    @@COPYRIGHT       copyright notice that is included in or attached to the work
    @@END
    @@DIST /usr/lib/python3/dist-packages/setuptools/_vendor/packaging-24.2.dist-info
    @@INSTALLER pip
    @@METADATA
    Metadata-Version: 2.3
    Name: packaging
    Version: 24.2

    @@END
    @@DIST /usr/local/lib/python3.12/dist-packages/packaging-24.1.dist-info
    @@INSTALLER pip
    @@METADATA
    Metadata-Version: 2.4
    Name: packaging
    Version: 24.1
    License-Expression: Apache-2.0 OR BSD-2-Clause

    @@END
    @@DIST /broken.dist-info
    @@INSTALLER
    @@METADATA

    @@END
    """
)


def test_parse_dist_scan_skips_entries_without_name():
    dists = parse_dist_scan(SCAN_OUTPUT)

    assert [d["metadata"]["Name"] for d in dists] == [
        "requests",
        "packaging",
        "packaging",
    ]
    assert dists[0]["installer"] == "uv"
    assert dists[0]["license_files"] == [
        "/opt/venv/lib/python3.12/site-packages/requests-2.32.3.dist-info/LICENSE"
    ]
    assert dists[0]["copyright"] == ["Copyright 2019 Kenneth Reitz"]


def test_group_prefers_main_environment_and_keeps_all_versions():
    grouped = {g["name"]: g for g in group_dists(parse_dist_scan(SCAN_OUTPUT))}
    packaging = grouped["packaging"]

    # The vendored 24.2 copy is listed by path but does not describe the
    # main-environment version
    assert packaging["version"] == "24.1"
    assert packaging["provider"] == "PIP"
    assert packaging["is_private_env"] is False
    assert packaging["primary"]["path"].startswith("/usr/local/lib")
    assert len(packaging["paths"]) == 2
    assert grouped["requests"]["requires"] == ["charset_normalizer", "idna"]


def test_group_of_main_copies_joins_versions_and_all_private_is_flagged():
    def dist(path, version, installer="pip"):
        return parse_dist_scan(
            f"@@DIST {path}\n@@INSTALLER {installer}\n@@METADATA\n"
            f"Name: numpy\nVersion: {version}\n\n@@END\n"
        )[0]

    main = group_dists(
        [
            dist(
                "/usr/local/lib/python3.12/dist-packages/numpy-2.1.0.dist-info", "2.1.0"
            ),
            dist("/opt/venv/lib/site-packages/numpy-1.26.4.dist-info", "1.26.4", "uv"),
        ]
    )[0]
    assert main["version"] == "1.26.4, 2.1.0"
    assert main["provider"] == "PIP, uv"

    cached = group_dists(
        [dist("/root/.cache/uv/archive-v0/x/numpy-2.1.0.dist-info", "2.1.0", "")]
    )[0]
    assert cached["is_private_env"] is True
    assert cached["version"] == "2.1.0"


def test_private_env_rules():
    assert is_private_env("/root/.cache/uv/archive-v0/abc/jinja2-3.1.6.dist-info")
    assert is_private_env("/usr/lib/python3/dist-packages/setuptools/_vendor/x")
    assert is_private_env(
        "/usr/local/cuda-13.0/NsightSystems-cli-2025.1/target/python/lib/site-packages"
    )
    assert is_private_env("/opt/tritonserver/backends/dali/wheel/dali/x.dist-info")
    assert not is_private_env("/opt/venv-tritonserver/lib/site-packages/x")
    assert not is_private_env("/usr/local/lib/python3.12/dist-packages/x")


def test_dist_info_version():
    assert dist_info_version("/x/tokenizers-0.23.3.dist-info") == "0.23.3"
    assert dist_info_version("/x/hf_xet-1.7.0.dist-info/") == "1.7.0"
    assert dist_info_version("/x/legacy-0.1.egg-info") == ""


def test_installer_to_provider():
    assert installer_to_provider("pip") == "PIP"
    assert installer_to_provider("uv\n") == "uv"
    assert installer_to_provider("conda") == "conda"
    assert installer_to_provider("") == "Unknown"


def test_license_precedence():
    dists = {d["path"]: d["metadata"] for d in parse_dist_scan(SCAN_OUTPUT)}
    expression = dists[
        "/usr/local/lib/python3.12/dist-packages/packaging-24.1.dist-info"
    ]
    free_form = dists[
        "/opt/venv/lib/python3.12/site-packages/requests-2.32.3.dist-info"
    ]

    assert local_license(expression) == (
        "Apache-2.0 OR BSD-2-Clause",
        "METADATA License-Expression",
    )
    assert local_license(free_form) == (None, None)
    assert local_license_fallback(free_form) == "Apache-2.0"
    assert project_url(free_form) == "https://github.com/psf/requests"


def test_runtime_requirements_skip_extras():
    metadata = parse_dist_scan(SCAN_OUTPUT)[0]["metadata"]
    assert runtime_requirements(metadata) == ["charset_normalizer", "idna"]


def test_extract_copyright_lines_filters_placeholders_and_prose():
    lines = [
        "Copyright [yyyy] [name of copyright owner]",
        "      copyright notice that is included",
        "Copyright (c) 2015-2024 The Pallets Team",
        "(c) 2001 Python Software Foundation",
        "Copyright (c) 2015-2024   The Pallets Team",
    ]
    assert extract_copyright_lines(lines) == [
        "Copyright (c) 2015-2024 The Pallets Team",
        "(c) 2001 Python Software Foundation",
    ]


def test_scan_script_on_fake_root(tmp_path):
    site = tmp_path / "venv" / "lib" / "site-packages"
    dist = site / "demo-1.0.dist-info"
    (dist / "licenses").mkdir(parents=True)
    (dist / "INSTALLER").write_text("uv\n")
    (dist / "METADATA").write_text(
        "Metadata-Version: 2.4\nName: demo\nVersion: 1.0\n\nLong description\n"
    )
    (dist / "licenses" / "LICENSE").write_text("Copyright (c) 2024 Demo Authors\n")
    (dist / "licenses" / "EMPTY").write_text("")
    (site / "demo").mkdir()
    (site / "demo" / "COPYING").write_text("MIT License\n")
    (site / "demo" / "license.py").write_text("x = 1\n")
    (dist / "RECORD").write_text(
        "demo/__init__.py,,\ndemo/COPYING,,\ndemo/license.py,,\n"
        "demo-1.0.dist-info/licenses/LICENSE,,\n"
    )
    egg = site / "legacy-0.1.egg-info"
    egg.write_text("Metadata-Version: 1.0\nName: legacy\nVersion: 0.1\n")

    script = DIST_SCAN_SCRIPT.replace("find / -xdev", f"find {tmp_path} -xdev")
    output = subprocess.run(
        ["sh", "-c", script], capture_output=True, text=True, check=True
    ).stdout
    dists = {d["metadata"]["Name"]: d for d in parse_dist_scan(output)}

    assert set(dists) == {"demo", "legacy"}
    assert dists["demo"]["installer"] == "uv"
    assert dists["demo"]["metadata"]["Version"] == "1.0"
    assert sorted(dists["demo"]["license_files"]) == [
        str(dist / "licenses" / "LICENSE"),
        str(site / "demo" / "COPYING"),
    ]
    assert dists["demo"]["copyright"] == ["Copyright (c) 2024 Demo Authors"]
    assert dists["legacy"]["license_files"] == []
