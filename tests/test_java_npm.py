"""Tests for Java (Maven) and npm package discovery."""

import io
import json
import subprocess
import zipfile

from docker_package_inspector.java_npm import (
    NPM_SCAN_SCRIPT,
    jar_display_name,
    maven_coordinates_from_m2_path,
    maven_pom_url,
    parse_jar,
    parse_npm_scan,
    parse_pom,
)

POM = """<?xml version="1.0"?>
<project>
  <!-- <licenses><license><name>Commented Out</name></license></licenses> -->
  <parent>
    <groupId>com.google.protobuf</groupId>
    <artifactId>protobuf-parent</artifactId>
    <version>3.25.1</version>
  </parent>
  <licenses>
    <license>
      <name>BSD-3-Clause</name>
      <url>https://opensource.org/licenses/BSD-3-Clause</url>
    </license>
    <license><url>https://www.apache.org/licenses/LICENSE-2.0</url></license>
  </licenses>
  <scm><url>https://github.com/protocolbuffers/protobuf</url></scm>
</project>
"""


def test_parse_pom():
    pom = parse_pom(POM)
    assert pom["licenses"] == [
        "BSD-3-Clause",
        "https://www.apache.org/licenses/LICENSE-2.0",
    ]
    assert pom["parent"] == ("com.google.protobuf", "protobuf-parent", "3.25.1")
    assert pom["url"] == "https://github.com/protocolbuffers/protobuf"
    assert parse_pom("<project/>") == {"licenses": [], "parent": None, "url": ""}


def _jar(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as jar:
        for name, text in entries.items():
            jar.writestr(name, text)
    return buffer.getvalue()


def test_parse_shaded_jar():
    jar = _jar(
        {
            "META-INF/MANIFEST.MF": "Manifest-Version: 1.0\n",
            "META-INF/maven/triton/java-api/pom.properties": "groupId=triton\n"
            "artifactId=java-api\nversion=0.0.1\n",
            "META-INF/maven/triton/java-api/pom.xml": "<project/>",
            "META-INF/maven/com.google.protobuf/protobuf-java/pom.properties": (
                "#Generated\ngroupId=com.google.protobuf\n"
                "artifactId=protobuf-java\nversion=3.25.1\n"
            ),
            "META-INF/maven/com.google.protobuf/protobuf-java/pom.xml": POM,
            "META-INF/LICENSE.txt": "Copyright 2008 Google Inc.\n",
            "META-INF/sub/LICENSE": "ignored, not top-level META-INF",
        }
    )

    parsed = parse_jar(jar)

    assert sorted(
        (a["group"], a["artifact"], a["version"]) for a in parsed["artifacts"]
    ) == [
        ("com.google.protobuf", "protobuf-java", "3.25.1"),
        ("triton", "java-api", "0.0.1"),
    ]
    assert parsed["license_entries"] == ["META-INF/LICENSE.txt"]
    assert parsed["copyright"] == ["Copyright 2008 Google Inc."]
    protobuf = [a for a in parsed["artifacts"] if a["artifact"] == "protobuf-java"][0]
    assert "BSD-3-Clause" in protobuf["pom"]


def test_parse_plain_jar_manifest():
    jar = _jar(
        {
            "META-INF/MANIFEST.MF": "Manifest-Version: 1.0\n"
            "Implementation-Title: javacpp\nImplementation-Version: 1.5.10\n"
            "Bundle-License: https://opensource.org/lice\n nses/BSD-3-Clause\n"
        }
    )
    parsed = parse_jar(jar)
    assert parsed["artifacts"] == []
    assert parsed["manifest"] == {
        "title": "javacpp",
        "version": "1.5.10",
        "license": "https://opensource.org/licenses/BSD-3-Clause",
    }


def test_maven_helpers():
    assert maven_coordinates_from_m2_path(
        "/root/.m2/repository/com/google/protobuf/protobuf-java/3.25.1/"
        "protobuf-java-3.25.1.jar"
    ) == ("com.google.protobuf", "protobuf-java", "3.25.1")
    assert maven_coordinates_from_m2_path("/opt/app/lib/protobuf-java.jar") is None
    assert maven_pom_url("com.google.protobuf", "protobuf-java", "3.25.1") == (
        "https://repo1.maven.org/maven2/com/google/protobuf/protobuf-java/3.25.1/"
        "protobuf-java-3.25.1.pom"
    )
    assert jar_display_name("/opt/lib/javacpp-1.5.10-linux-x86_64.jar") == "javacpp"


def _write_package(directory, manifest, license_text=None):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "package.json").write_text(json.dumps(manifest))
    if license_text is not None:
        (directory / "LICENSE").write_text(license_text)


def test_npm_scan_on_fake_root(tmp_path):
    app = tmp_path / "workspace" / "client"
    modules = app / "node_modules"
    _write_package(
        app,
        {
            "name": "simple-grpc-tritonclient",
            "version": "1.0.0",
            "license": "BSD-3-Clause",
            "devDependencies": {"mocha": "10"},
        },
    )
    _write_package(
        modules / "@grpc" / "grpc-js",
        {
            "name": "@grpc/grpc-js",
            "version": "1.9.0",
            "license": "Apache-2.0",
            "author": {"name": "Google Inc."},
            "repository": "grpc/grpc-node",
        },
        "Copyright 2019 gRPC authors\n",
    )
    _write_package(
        modules / "legacy",
        {
            "name": "legacy",
            "version": "0.1.0",
            "licenses": [{"type": "MIT"}, {"type": "GPL-2.0"}],
        },
        "",
    )
    # Nested package.json used for module exports, not a package of its own
    _write_package(modules / "legacy" / "esm", {"type": "module"})
    _write_package(modules / "legacy" / "lib", {"name": "inner", "version": "1"})
    # A package.json without node_modules next to it is not an installed app
    _write_package(tmp_path / "srv" / "assets", {"name": "assets", "version": "1"})

    script = NPM_SCAN_SCRIPT.replace("find / -xdev", f"find {tmp_path} -xdev")
    output = subprocess.run(
        ["sh", "-c", script], capture_output=True, text=True, check=True
    ).stdout
    found = {p["name"]: p for p in parse_npm_scan(output)}

    assert set(found) == {"simple-grpc-tritonclient", "@grpc/grpc-js", "legacy"}
    grpc = found["@grpc/grpc-js"]
    assert grpc["license"] == "Apache-2.0"
    assert grpc["repository"] == "https://github.com/grpc/grpc-node"
    assert grpc["license_files"] == [str(modules / "@grpc" / "grpc-js" / "LICENSE")]
    assert grpc["copyright"] == ["Copyright 2019 gRPC authors", "Google Inc."]
    assert grpc["is_module"] is True
    assert found["legacy"]["license"] == "MIT OR GPL-2.0"
    assert found["legacy"]["license_files"] == []  # empty LICENSE is not counted
    assert found["simple-grpc-tritonclient"]["is_module"] is False
