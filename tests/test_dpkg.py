"""Tests for dpkg license handling and Debian Python ownership."""

from docker_package_inspector.inspector import DockerImageInspector


class FakeContainer:
    """Answers exec_run from a list of (predicate, (exit_code, output))."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def exec_run(self, cmd, demux=False):
        self.calls.append(cmd)
        text = cmd if isinstance(cmd, str) else " ".join(cmd)
        for needle, response in self.responses:
            if needle in text:
                return response
        return 1, b""


DEP5 = b"""Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Files: *
Copyright: 2008-2024 The pip developers
License: Expat

Files: src/pip/_vendor/*
Copyright: 2012 Kenneth Reitz
License: Expat
"""


def test_copyright_file_licenses_are_deduplicated():
    inspector = DockerImageInspector()
    content = "License: GPL-2+\n" * 5 + "License: LGPL-2.1+\n"
    assert inspector._parse_copyright_file(content).count("GPL-2+") == 1


def test_dpkg_license_from_copyright_file():
    container = FakeContainer([("cat /usr/share/doc/python3-pip/copyright", (0, DEP5))])
    license_info, source, files, holders = DockerImageInspector()._get_dpkg_license(
        container, "python3-pip"
    )

    assert license_info.count("Expat") == 1
    assert source == files[0] == "/usr/share/doc/python3-pip/copyright"
    assert holders == [
        "Copyright: 2008-2024 The pip developers",
        "Copyright: 2012 Kenneth Reitz",
    ]


def test_dpkg_without_copyright_falls_back_to_owned_files():
    eula = "/opt/nvidia/nsight-systems/EULA.txt"
    container = FakeContainer(
        [
            ("dpkg-query -L", (0, f"{eula}\n".encode())),
            (f"cat {eula}", (0, b"Permission is hereby granted, free of charge")),
        ]
    )
    license_info, source, files, _ = DockerImageInspector()._get_dpkg_license(
        container, "nsight-systems-cli"
    )

    assert (license_info, source, files) == ("MIT", eula, [eula])


def test_dpkg_without_any_license_stays_unknown():
    container = FakeContainer([])
    result = DockerImageInspector()._get_dpkg_license(container, "cuda-keyring")
    assert result == ("Unknown", "Not found", [], [])


def test_unrecognised_owned_file_is_listed_but_not_guessed():
    path = "/usr/share/doc/dpdk/NOTICE"
    container = FakeContainer(
        [("dpkg-query -L", (0, f"{path}\n".encode())), (f"cat {path}", (0, b"x"))]
    )
    license_info, source, files, _ = DockerImageInspector()._get_dpkg_license(
        container, "dpdk-community"
    )
    assert (license_info, source, files) == (
        "Unknown",
        "Not detected in package files",
        [path],
    )


def test_debian_python_dist_linked_to_owning_dpkg():
    dist = "/usr/lib/python3/dist-packages/pip-24.0.dist-info"
    pip = {
        "name": "pip",
        "package_type": "python",
        "package_provider": "Unknown",
        "license": "Unknown",
        "install_path": [dist],
        "license_files": [],
        "copyright": [],
        "owning_dpkg": "",
    }
    dpkg_row = {
        "name": "python3-pip",
        "package_type": "binary",
        "package_provider": "dpkg",
        "license": "MIT",
        "license_source": "/usr/share/doc/python3-pip/copyright",
        "license_files": ["/usr/share/doc/python3-pip/copyright"],
        "copyright": ["Copyright: 2008-2024 The pip developers"],
    }
    container = FakeContainer(
        [
            (
                "dpkg-query -S",
                (
                    0,
                    f"diversion by x from: {dist}\npython3-pip: {dist}\n".encode(),
                ),
            )
        ]
    )

    DockerImageInspector()._link_python_to_dpkg(container, [pip, dpkg_row])

    assert pip["owning_dpkg"] == "python3-pip"
    assert pip["license_files"] == ["/usr/share/doc/python3-pip/copyright"]
    assert pip["copyright"] == ["Copyright: 2008-2024 The pip developers"]
    assert pip["license"] == "MIT"
    assert pip["license_source"].startswith("dpkg python3-pip")


def test_non_usr_lib_dists_are_not_queried():
    pkg = {
        "name": "requests",
        "package_type": "python",
        "install_path": ["/opt/venv/lib/site-packages/requests-2.32.3.dist-info"],
    }
    container = FakeContainer([])
    DockerImageInspector()._link_python_to_dpkg(container, [pkg])
    assert container.calls == []
