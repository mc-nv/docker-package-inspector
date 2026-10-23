"""Tests for Rust crate attribution."""

import hashlib
import io
import json
import struct
import subprocess
import tarfile
import zlib

import pytest

from docker_package_inspector.rust_crates import (
    RECORD_LOOKUP_SCRIPT,
    git_repository,
    is_crates_io,
    maturin_module_name,
    module_stem,
    parse_auditable,
    parse_cargo_lock,
    parse_cargo_toml,
    parse_crate_archive,
    parse_hosts,
    resolve_project_crates,
    resolve_record_owners,
    runtime_closure,
    sdist_project_files,
    split_files,
)


def build_elf(sections, is_64=True, endian="<"):
    """Minimal ELF with the given {name: bytes} sections plus .shstrtab."""
    names = b"\0"
    offsets = {}
    for name in list(sections) + [".shstrtab"]:
        offsets[name] = len(names)
        names += name.encode() + b"\0"
    blobs = list(sections.values()) + [names]

    header_size = 64 if is_64 else 52
    entry_size = 64 if is_64 else 40
    data = bytearray(header_size)
    data[:4] = b"\x7fELF"
    data[4] = 2 if is_64 else 1
    data[5] = 1 if endian == "<" else 2
    positions = []
    for blob in blobs:
        positions.append(len(data))
        data += blob
    shoff = len(data)
    shnum = len(blobs) + 1  # index 0 is the null section
    if is_64:
        struct.pack_into(endian + "Q", data, 0x28, shoff)
        struct.pack_into(endian + "HHH", data, 0x3A, entry_size, shnum, shnum - 1)
    else:
        struct.pack_into(endian + "I", data, 0x20, shoff)
        struct.pack_into(endian + "HHH", data, 0x2E, entry_size, shnum, shnum - 1)
    data += bytes(entry_size)
    fmt = endian + ("IIQQQQIIQQ" if is_64 else "IIIIIIIIII")
    for name, pos, blob in zip(list(sections) + [".shstrtab"], positions, blobs):
        data += struct.pack(fmt, offsets[name], 1, 0, 0, pos, len(blob), 0, 0, 1, 0)
    return bytes(data)


AUDITABLE = {
    "packages": [
        {"name": "rg", "version": "14.1.0", "source": "local", "root": True},
        {"name": "regex", "version": "1.10.4", "source": "crates.io"},
        {"name": "cc", "version": "1.0.90", "source": "crates.io", "kind": "build"},
    ]
}


@pytest.mark.parametrize("is_64,endian", [(True, "<"), (False, ">")])
def test_parse_auditable(is_64, endian):
    payload = zlib.compress(json.dumps(AUDITABLE).encode())
    elf = build_elf({".text": b"\x90" * 8, ".dep-v0": payload}, is_64, endian)

    packages = parse_auditable(elf)

    assert [(p["name"], p["root"], p["kind"]) for p in packages] == [
        ("rg", True, "runtime"),
        ("regex", False, "runtime"),
        ("cc", False, "build"),
    ]


def test_parse_auditable_without_section_or_not_elf():
    assert parse_auditable(build_elf({".text": b"x"})) is None
    assert parse_auditable(b"#!/bin/sh\necho /rustc/" + bytes(64)) is None


LOCK = """
version = 3

[[package]]
name = "demo"
version = "0.1.0"
dependencies = [
 "pyo3",
 "serde 1.0.200",
 "criterion",
]

[[package]]
name = "pyo3"
version = "0.22.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "aaa"
dependencies = [
 "libc",
]

[[package]]
name = "libc"
version = "0.2.150"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "bbb"

[[package]]
name = "serde"
version = "1.0.200"
source = "registry+https://github.com/rust-lang/crates.io-index"

[[package]]
name = "serde"
version = "1.0.150"
source = "registry+https://github.com/rust-lang/crates.io-index"

[[package]]
name = "criterion"
version = "0.5.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
dependencies = [
 "serde 1.0.150",
]
"""

TOML = """
[package]
name = "demo"
license = "Apache-2.0"
authors = ["Jane Doe <jane@example.com>"]

[lib]
name = "_demo"
crate-type = ["cdylib"]

[dependencies]
pyo3 = { version = "0.22",
  features = ["extension-module"] }
serde = "1"

[dev-dependencies]
criterion = "0.5"

[target.'cfg(unix)'.build-dependencies]
cc = "1"

[dev-dependencies.proptest]
version = "1"

[build-dependencies]
my-bindgen = { package = "bindgen", version = "0.69" }
"""


def test_parse_cargo_lock():
    packages = {(p["name"], p["version"]): p for p in parse_cargo_lock(LOCK)}
    assert packages[("pyo3", "0.22.0")]["dependencies"] == ["libc"]
    assert packages[("pyo3", "0.22.0")]["checksum"] == "aaa"
    assert packages[("demo", "0.1.0")]["source"] == ""


def test_parse_cargo_toml():
    toml = parse_cargo_toml(TOML)
    assert toml["name"] == "demo"
    assert toml["lib_name"] == "_demo"
    assert toml["crate_types"] == ["cdylib"]
    assert toml["authors"] == ["Jane Doe <jane@example.com>"]
    assert toml["normal_deps"] == {"pyo3", "serde"}
    assert toml["non_runtime_deps"] == {"criterion", "cc", "proptest", "bindgen"}


def test_runtime_closure_excludes_root_dev_deps_and_root():
    crates = runtime_closure(parse_cargo_lock(LOCK), "demo", parse_cargo_toml(TOML))
    assert [(c["name"], c["version"]) for c in crates] == [
        ("libc", "0.2.150"),
        ("pyo3", "0.22.0"),
        ("serde", "1.0.200"),
    ]


def test_resolve_project_crates_finds_lock_above_root():
    files = {
        "/src/proj/Cargo.lock": LOCK,
        "/src/proj/bindings/python/Cargo.toml": TOML,
        "/src/proj/other/Cargo.toml": '[package]\nname = "other"\n',
    }
    crates, lock = resolve_project_crates(files, "_demo")
    assert lock == "/src/proj/Cargo.lock"
    assert {c["name"] for c in crates} == {"libc", "pyo3", "serde"}

    # The only cdylib is used for an unmatched stem only when the files are
    # known to belong to the host's project (sdist), never for image files
    assert resolve_project_crates(files, "unrelated") == (None, None)
    assert resolve_project_crates(files, "unrelated", allow_single_cdylib=True) == (
        crates,
        lock,
    )
    assert resolve_project_crates({"/x/Cargo.toml": TOML}, "_demo") == (None, None)


def test_binding_crate_preferred_over_same_named_core_crate():
    # tokenizers: the core crate and the Python binding share lib name, and
    # only the binding has a Cargo.lock above it
    core = '[package]\nname = "tokenizers"\n\n[lib]\nname = "tokenizers"\n'
    binding = TOML.replace('name = "_demo"', 'name = "tokenizers"')
    files = {
        "/t/tokenizers/Cargo.toml": core,
        "/t/bindings/python/Cargo.toml": binding,
        "/t/bindings/python/Cargo.lock": LOCK,
    }
    crates, lock = resolve_project_crates(files, "tokenizers")
    assert lock == "/t/bindings/python/Cargo.lock"
    assert {c["name"] for c in crates} == {"libc", "pyo3", "serde"}


def test_select_root_by_maturin_module_name():
    toml = TOML.replace('crate-type = ["cdylib"]', "")
    files = {"/p/Cargo.lock": LOCK, "/p/Cargo.toml": toml}
    assert resolve_project_crates(files, "_rust") == (None, None)
    crates, _ = resolve_project_crates(
        {**files, "/p/Cargo.toml": toml.replace('name = "_demo"', 'name = "x"')},
        "_rust",
        "demo",
    )
    assert {c["name"] for c in crates} == {"libc", "pyo3", "serde"}
    assert (
        maturin_module_name(
            '[tool.maturin]\nmodule-name = "cryptography.hazmat.bindings._rust"\n'
        )
        == "_rust"
    )


def _tar_gz(members):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, text in members.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_sdist_project_files_skips_vendored_and_tests():
    archive = _tar_gz(
        {
            "demo-0.1.0/Cargo.toml": TOML,
            "demo-0.1.0/Cargo.lock": LOCK,
            "demo-0.1.0/pyproject.toml": "[tool.maturin]\n",
            "demo-0.1.0/vendor/x/Cargo.toml": "",
            "demo-0.1.0/tests/Cargo.toml": "",
            "demo-0.1.0/src/lib.rs": "",
        }
    )
    assert sorted(sdist_project_files(archive)) == [
        "/demo-0.1.0/Cargo.lock",
        "/demo-0.1.0/Cargo.toml",
        "/demo-0.1.0/pyproject.toml",
    ]


def test_parse_crate_archive_and_checksum():
    archive = _tar_gz(
        {
            "libc-0.2.150/Cargo.toml": '[package]\nname = "libc"\n'
            'license = "MIT OR Apache-2.0"\n'
            'repository = "https://github.com/rust-lang/libc"\n'
            'authors = ["The Rust Project Developers"]\n',
            "libc-0.2.150/LICENSE-MIT": "Copyright (c) 2014-2020 The Rust Project "
            "Developers\n\nPermission is hereby granted",
            "libc-0.2.150/src/LICENSE-nested": "Copyright 1999 Not top level",
        }
    )
    checksum = hashlib.sha256(archive).hexdigest()

    meta = parse_crate_archive(archive, checksum)

    assert meta["license"] == "MIT OR Apache-2.0"
    assert meta["repository"] == "https://github.com/rust-lang/libc"
    assert meta["copyright"] == [
        "Copyright (c) 2014-2020 The Rust Project Developers",
        "The Rust Project Developers",
    ]
    assert parse_crate_archive(archive, "0" * 64) is None


def test_record_owner_resolution_handles_bin_paths(tmp_path):
    site = tmp_path / "lib" / "site-packages"
    dist = site / "uv-0.5.0.dist-info"
    dist.mkdir(parents=True)
    (dist / "RECORD").write_text(
        "../../bin/uv,sha256=x,1\nuv/__init__.py,,\nuv-0.5.0.dist-info/RECORD,,\n"
    )
    tok = site / "tokenizers-0.21.0.dist-info"
    tok.mkdir()
    (tok / "RECORD").write_text("tokenizers/tokenizers.abi3.so,sha256=y,2\n")
    hosts = [
        str(tmp_path / "bin" / "uv"),
        str(site / "tokenizers" / "tokenizers.abi3.so"),
        "/usr/bin/unowned",
    ]

    output = subprocess.run(
        ["sh", "-c", RECORD_LOOKUP_SCRIPT, "sh"]
        + hosts
        + ["--", str(dist / "RECORD"), str(tok / "RECORD")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert resolve_record_owners(output, hosts) == {
        hosts[0]: str(dist),
        hosts[1]: str(tok),
    }


def test_small_helpers():
    assert parse_hosts("@@HOST plain /a b.so\n@@HOST auditable /usr/bin/rg\nx") == [
        ("/a b.so", False),
        ("/usr/bin/rg", True),
    ]
    assert split_files("@@FILE /a\nx\ny\n@@FILE /b\nz") == {"/a": "x\ny", "/b": "z"}
    assert module_stem("/x/_rust.abi3.so") == "_rust"
    assert module_stem("/x/tokenizers.cpython-312-x86_64-linux-gnu.so") == "tokenizers"
    assert is_crates_io("registry+https://github.com/rust-lang/crates.io-index")
    assert is_crates_io("crates.io")
    assert not is_crates_io("git+https://github.com/a/b?rev=1#abc")
    assert git_repository("git+https://github.com/a/b?rev=1#abc") == (
        "https://github.com/a/b"
    )
