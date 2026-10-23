# Docker Package Inspector

A CLI tool to inspect Docker images and extract detailed package information, including licenses, for everything shipped in the image.

## Features

- Inspect Docker images for any architecture
- Extract Python packages from their `*.dist-info` / `*.egg-info` metadata, whichever tool installed them (pip, uv, distro packages) and in any environment (system, virtualenvs, tool directories); works in images without pip or python
- Extract system binary package information (apt/dpkg, rpm/yum, apk)
- Attribute **Rust crates** statically linked into executables and Python extension modules (see [Rust crates](#rust-crates))
- Extract **Java** artifacts from jar files (including shaded jar-with-dependencies) and **npm** packages from `node_modules`
- Report OpenSSL and Python runtimes that no package manager installed (source builds, copies bundled in wheels)
- Verified in-image license files, copyright holders and the owning Debian package for every row
- Per-platform manifest digests for multi-architecture images
- Optional classification of every license against a license policy file (`--license-compliance-file`)
- Output results as structured JSON and CSV
- **Multiple license detection** - Automatically detects and parses packages with dual or multi-licensing (e.g., "MIT or Apache-2.0")

## Installation

### From GitHub

Install directly from the repository:

```bash
pip install git+https://github.com/mc-nv/docker-package-inspector.git
```

Or install a specific branch, tag, or commit:

```bash
# Install from a specific branch
pip install git+https://github.com/mc-nv/docker-package-inspector.git@branch-name

# Install from a specific tag
pip install git+https://github.com/mc-nv/docker-package-inspector.git@v1.0.0

# Install from a specific commit
pip install git+https://github.com/mc-nv/docker-package-inspector.git@abc1234
```

### From source

```bash
pip install .
```

### Development installation

```bash
pip install -e .
```

## Usage

### Basic CLI Usage

Inspect an image and print to stdout:

```bash
docker-package-inspector --image python:3.11-slim
```

Specify architecture:

```bash
docker-package-inspector --image unbuntu:24.04 --architecture amd64
```

Save output to file with verbose logging:

```bash
docker-package-inspector --image unbuntu:24.04 --output triton_packages.json --verbose
```

Use local image without pulling:

```bash
docker-package-inspector --image myapp:latest --no-pull --output packages.json
```

Export to CSV format:

```bash
docker-package-inspector --image python:3.11-slim --csv-output packages.csv
```

Export to both JSON and CSV simultaneously:

```bash
docker-package-inspector --image python:3.11-slim --output packages.json --csv-output packages.csv
```

Customize the CSV column delimiter:

```bash
# Use semicolon delimiter (common in Europe)
docker-package-inspector --image python:3.11-slim --csv-output packages.csv --delimiter ";"

# Use tab delimiter (TSV format)
docker-package-inspector --image python:3.11-slim --csv-output packages.csv --delimiter $'\t'

# Use pipe delimiter
docker-package-inspector --image python:3.11-slim --csv-output packages.csv --delimiter "|"
```

Note: `--separator` can also be used as an alias for `--delimiter`. The `parent_packages` field separator is automatically chosen based on the CSV delimiter to avoid conflicts. The same separator joins every other list-valued column (`install_path`, `license_files`, `copyright`, `license_unmatched`).

Skip the Rust crate scan (faster; crates are then missing from the report):

```bash
docker-package-inspector --image python:3.11-slim --skip-rust-scan
```

### License Policy Classification

`--license-compliance-file PATH` classifies every package license against a
policy file: a JSON object mapping a category to the license names in it
(SPDX ids, free-form names or whole expressions):

```json
{
  "low_risk": ["Example-Permissive-1.0", "..."],
  "copy_left": ["Example-Copyleft-2.0", "..."],
  "non_commercial": ["Example Non-Commercial License"]
}
```

Matching is case and whitespace insensitive: the whole license string first,
then each part (split on ` | `, `OR`, `AND` and the legacy Cargo `/`; `WITH`
exceptions fall back to the base license). Each package then gets:

- `license_category`: the most restrictive category among its parts
  (`low_risk` < `copy_left` < `non_commercial` < `unknown`)
- `osrb_review_required`: `false` only when every part is `low_risk`;
  `Unknown` licenses and names the policy does not list always require review
- `license_unmatched`: license parts the policy does not list

In diff mode `summary.added_by_license_category` counts ADDED packages per
category. The file is read before any image is inspected, so a missing or
malformed file fails immediately. It is never downloaded or bundled by this
tool. Without the option the output is unchanged.

### Rust Crates

Rust leaves no package record on disk, so crates are attributed per binary:
every ELF file containing Rust standard library paths is mapped to the wheel
that installed it (through dist-info `RECORD`), and its crates are resolved
from, in order:

1. a [cargo-auditable](https://github.com/rust-secure-code/cargo-auditable)
   `.dep-v0` section embedded in the binary,
2. a `Cargo.lock` shipped in the image next to the matching crate,
3. the `Cargo.lock` in the PyPI sdist of the installed wheel version.

The dependency graph is walked from the crate that builds the module,
excluding its own dev/build dependencies. Transitive build-time crates
(proc-macros) cannot be told apart from a `Cargo.lock` alone and are kept, so
the list may over-report but not under-report. Binaries whose crates cannot be
resolved are reported as `rust-binary` rows with an `Unknown` license.
License, repository and copyright holders come from the published `.crate`,
verified against the `Cargo.lock` checksum.

### Network Access

Besides the Docker daemon and registry, the tool contacts: PyPI (package
metadata and sdists), `static.crates.io` (published crates), `crates.io` (API
fallback, at most one request per second), Maven Central (POMs) and GitHub /
GitLab (license lookups by source URL). All requests identify the tool with a
`docker-package-inspector/<version>` User-Agent. Downloaded archives are read
in memory and never extracted or executed. Per-platform digests use
`docker manifest inspect`, so the docker CLI and registry credentials must be
available where the tool runs; without them the previous digest is kept and a
warning is logged.

### Inline Architecture Specification

You can specify the architecture directly in the image name using the format `<registry><path>:<tag>/<arch>`:

```bash
# Single image with architecture
docker-package-inspector --image python:3.11/amd64 --output packages.json

# Multiple images with different architectures
docker-package-inspector \
  --image python:3.11/amd64 \
  --image python:3.11/arm64 \
  --output multi_arch.json

# Using comma-separated format
docker-package-inspector --images "python:3.11/amd64,python:3.11/arm64,python:3.12/amd64"
```

### Multi-Image Inspection

Inspect multiple images in one call:

```bash
# Using multiple --image flags
docker-package-inspector --image python:3.10 --image python:3.11 --image python:3.12 --output comparison.json

# Using comma-separated list
docker-package-inspector --images "python:3.10,python:3.11,python:3.12" --output comparison.json

# With inline architectures
docker-package-inspector --images "python:3.10/amd64,python:3.11/amd64,python:3.12/amd64"
```

### Multi-Architecture Inspection

Inspect the same image for multiple architectures using `--arch` flags:

```bash
# Using multiple --arch flags
docker-package-inspector --image ubuntu:22.04 --arch amd64 --arch arm64 --output multi_arch.json

# Using comma-separated list
docker-package-inspector --image python:3.11 --archs "amd64,arm64,arm/v7" --output multi_arch.json

# Or use inline architecture format (recommended)
docker-package-inspector --images "python:3.11/amd64,python:3.11/arm64,python:3.11/arm" --output multi_arch.json
```

### Mixing Inline and Global Architectures

You can mix inline architecture specifications with global `--arch` flags:

```bash
# This will inspect:
# - python:3.11 on arm64 (inline)
# - python:3.12 on amd64 (global --arch)
docker-package-inspector \
  --image python:3.11/arm64 \
  --image python:3.12 \
  --arch amd64
```

### Matrix Inspection (Multiple Images × Multiple Architectures)

Inspect multiple images across multiple architectures:

```bash
docker-package-inspector \
  --images "python:3.11,python:3.12" \
  --archs "amd64,arm64" \
  --output matrix.json \
  --csv-output matrix.csv \
  --verbose
```

This will inspect all combinations:
- python:3.11 on amd64
- python:3.11 on arm64
- python:3.12 on amd64
- python:3.12 on arm64

### Diff Mode - Compare Two Images

Compare packages between exactly two images to see what has been added, removed, or changed:

```bash
# Basic diff
docker-package-inspector --diff --image python:3.11 --image python:3.12

# Diff with specific architectures
docker-package-inspector --diff --image ubuntu:22.04/amd64 --image ubuntu:24.04/amd64

# Save diff to files
docker-package-inspector --diff \
  --image python:3.11 \
  --image python:3.12 \
  --output diff.json \
  --csv-output diff.csv
```

Diff mode outputs a structured comparison showing:
- **Added packages**: Packages that exist only in the second image
- **Removed packages**: Packages that exist only in the first image
- **Changed packages**: Packages that exist in both images but with different versions

The JSON output includes a summary with counts and detailed lists of changes. The CSV output formats the diff as a table with columns for change type, package name, old/new versions, and licenses.

CSV format is ideal for importing into spreadsheets, databases, or data analysis tools. The CSV includes all package information in a flat table format with columns:
- `image` - Docker image name
- `digest` - Image digest (SHA256)
- `architecture` - Image architecture
- `name` - Package name
- `version` - Package version
- `package_type` - "python" or "binary"
- `source` - Package source URL
- `license` - Package license
- `source_code_url` - Source code repository URL
- `is_dependency` - True/False
- `parent_packages` - List of parent packages (separator adapts based on CSV delimiter set by `--delimiter`)

### Analyzing the Output

Get package counts:

```bash
# Total packages
jq '.packages | length' triton_packages.json

# Count by type
jq '[.packages[] | select(.package_type == "python")] | length' triton_packages.json
jq '[.packages[] | select(.package_type == "binary")] | length' triton_packages.json
```

Find all dependencies:

```bash
# List all packages that are dependencies
jq '.packages[] | select(.is_dependency == true) | {name, parent_packages}' triton_packages.json

# Find what depends on a specific package
jq '.packages[] | select(.parent_packages[] == "requests")' triton_packages.json
```

List Python packages only:

```bash
jq '.packages[] | select(.package_type == "python") | {name, version, license}' triton_packages.json
```

Find packages with specific license:

```bash
jq '.packages[] | select(.license | contains("MIT")) | .name' triton_packages.json
```

Find packages with multiple licenses:

```bash
# Find dual-licensed packages
jq '.packages[] | select(.license | contains("|")) | {name, license}' triton_packages.json

# Find packages with proprietary licenses
jq '.packages[] | select(.license | test("Proprietary|Commercial")) | {name, license}' triton_packages.json
```

Get image digest and architecture:

```bash
jq '{digest, architecture}' triton_packages.json
```

### Advanced Examples

**Find all GPL-licensed packages:**

```bash
jq '.packages[] | select(.license | test("GPL")) | {name, version, license, package_type}' triton_packages.json
```

**Create a dependency tree for a package:**

```bash
# Find what package X depends on
PACKAGE="requests"
jq --arg pkg "$PACKAGE" '.packages[] | select(.name == $pkg) | {name, depends_on: .parent_packages}' triton_packages.json
```

**Export to CSV:**

```bash
jq -r '.packages[] | [.name, .version, .package_type, .license] | @csv' triton_packages.json > packages.csv
```

**Compare packages between two images:**

```bash
# Using diff mode (recommended)
docker-package-inspector --diff --image python:3.10 --image python:3.11

# With output files
docker-package-inspector --diff --image python:3.10 --image python:3.11 --output diff.json --csv-output diff.csv

# Compare specific architectures
docker-package-inspector --diff --image python:3.10/amd64 --image python:3.11/amd64

# Alternative: Generate separate reports and manually compare
docker-package-inspector --image python:3.10 --output python310.json
docker-package-inspector --image python:3.11 --output python311.json

# Find packages only in 3.11
jq -s '.[1].packages - .[0].packages | .[] | .name' python310.json python311.json
```

### Programmatic Usage

Use as a Python library:

```python
from docker_package_inspector.inspector import DockerImageInspector

# Create inspector
inspector = DockerImageInspector(verbose=True)

# Inspect image
result = inspector.inspect_image(
    image_name="python:3.11-slim",
    architecture="amd64",
    pull=True
)

# Access results
print(f"Image digest: {result['digest']}")
print(f"Total packages: {len(result['packages'])}")

# Filter Python packages
python_pkgs = [p for p in result['packages'] if p['package_type'] == 'python']
print(f"Python packages: {len(python_pkgs)}")

# Find dependencies
deps = [p for p in result['packages'] if p['is_dependency']]
for dep in deps:
    print(f"{dep['name']} is required by: {', '.join(dep['parent_packages'])}")
```

## Output Format

### Diff Mode Output

When using `--diff`, the JSON output structure is:

```json
{
  "version": "1.0.0",
  "inspection_date": "2024-01-15T10:30:00+00:00",
  "comparison_type": "diff",
  "image_from": {
    "name": "python:3.11",
    "digest": "python@sha256:abc123...",
    "architecture": "amd64",
    "total_packages": 150
  },
  "image_to": {
    "name": "python:3.12",
    "digest": "python@sha256:def456...",
    "architecture": "amd64",
    "total_packages": 160
  },
  "summary": {
    "added": 15,
    "removed": 5,
    "changed": 8,
    "unchanged": 137
  },
  "differences": {
    "added": [
      {
        "name": "new-package",
        "version": "1.0.0",
        "package_type": "python",
        "license": "MIT",
        "source": "https://pypi.org/project/new-package/"
      }
    ],
    "removed": [
      {
        "name": "old-package",
        "version": "0.5.0",
        "package_type": "python",
        "license": "Apache 2.0",
        "source": "https://pypi.org/project/old-package/"
      }
    ],
    "changed": [
      {
        "name": "updated-package",
        "version_from": "2.0.0",
        "version_to": "2.1.0",
        "package_type": "python",
        "license_from": "MIT",
        "license_to": "MIT"
      }
    ]
  }
}
```

The CSV output for diff mode has these columns:
- `change_type` - "ADDED", "REMOVED", or "CHANGED"
- `name` - Package name
- `version_from` - Version in the first image (empty for ADDED)
- `version_to` - Version in the second image (empty for REMOVED)
- `package_type` - "python" or "binary"
- `license_from` - License in the first image (empty for ADDED)
- `license_to` - License in the second image (empty for REMOVED)
- `source` - Package source URL

### Single Image Output

For single image inspection, the JSON structure is:

```json
{
  "image": "unbuntu:24.04",
  "digest": "nvcr.io/nvidia/tritonserver@sha256:abc123...",
  "architecture": "amd64",
  "inspection_date": "2024-01-15T10:30:00",
  "packages": [
    {
      "name": "requests",
      "version": "2.28.1",
      "source": "https://pypi.org/project/requests/",
      "license": "Apache 2.0",
      "source_code_url": "https://github.com/psf/requests",
      "package_type": "python",
      "is_dependency": false,
      "parent_packages": []
    },
    {
      "name": "urllib3",
      "version": "1.26.0",
      "source": "https://pypi.org/project/urllib3/",
      "license": "MIT",
      "source_code_url": "https://github.com/urllib3/urllib3",
      "package_type": "python",
      "is_dependency": true,
      "parent_packages": ["requests"]
    },
    {
      "name": "curl",
      "version": "7.68.0-1ubuntu2.14",
      "source": "http://archive.ubuntu.com/ubuntu",
      "license": "curl",
      "source_code_url": "https://curl.se/",
      "package_type": "binary",
      "is_dependency": false,
      "parent_packages": []
    }
  ]
}
```

### Multi-Image/Multi-Arch Output

When inspecting multiple images or architectures, the structure changes to:

```json
{
  "inspection_date": "2024-01-15T10:30:00+00:00",
  "total_images": 2,
  "total_architectures": 2,
  "results": [
    {
      "image": "python:3.11",
      "digest": "python@sha256:abc123...",
      "architecture": "amd64",
      "packages": [...]
    },
    {
      "image": "python:3.11",
      "digest": "python@sha256:def456...",
      "architecture": "arm64",
      "packages": [...]
    }
  ]
}
```

### Package Fields

- `name`: Package name (`group:artifact` for Maven artifacts)
- `version`: Package version; versions of copies found in several places are joined with `, `
- `source`: Registry page of the package (PyPI, crates.io, Maven Central, npm) or repository
- `license`: Package license. For packages with multiple licenses, they are separated by ` | ` (e.g., "MIT | Apache-2.0"). Never guessed: `Unknown` when no license could be established.
- `license_source`: Where the license came from (metadata field, file path, API)
- `source_code_url`: URL to source code repository
- `package_type`: `python`, `binary`, `rust`, `rust-binary`, `java` or `npm`
- `package_provider`: What installed it: `PIP`, `uv`, `dpkg`, `RPM`, `APK`, `maven`, `jar`, `npm`, `unowned`, or how a crate was resolved (`cargo-auditable`, `Cargo.lock(image)`, `Cargo.lock(sdist)`)
- `is_dependency`: Boolean indicating if this package is a dependency of another package
- `parent_packages`: Packages that depend on this package; for crates, the binaries that embed them; for Java artifacts, the jars that contain them
- `install_path`: Where the package is installed (dist-info directories, module directories, runtime paths)
- `license_files`: License/copyright/notice files present in the image, each verified to exist and be non-empty (`<jar>!/<entry>` for files inside a jar)
- `copyright`: Copyright holder lines from those files (and crate/npm authors)
- `is_private_env`: `true` when every copy lives in a non-main environment (vendored `_vendor` copies, installer caches, tool-private interpreters such as Nsight Systems, backend-private wheel directories, `~/.m2`); such packages are flagged, never dropped
- `owning_dpkg`: Debian package that installed the files, when there is one
- `license_category`, `osrb_review_required`, `license_unmatched`: only with `--license-compliance-file`

The fields from `install_path` onwards are appended after the original CSV
columns, so positional CSV readers keep working.

Image entries carry `digest`, the per-platform manifest digest, and
`index_digest`, the manifest list digest shared by all platforms (empty for
single-platform images).

### License Parsing

The tool automatically detects and handles multiple licenses:

- **Dual/Multi-licensing**: Packages licensed under multiple licenses (e.g., "MIT or Apache-2.0") are parsed and stored as "MIT | Apache-2.0"
- **Python**: `License-Expression` from the package metadata when present, then PyPI, then the metadata `License` field and classifiers
- **Debian copyright files**: the license short names of machine-readable (DEP-5) files, with `or`/`and` written as `OR`/`AND`; otherwise licenses recognised from the text. Sentences from the copyright text are never reported as license names. Packages without a copyright file fall back to license, EULA and notice files the package itself installs.

For more details, see [MULTIPLE_LICENSES.md](MULTIPLE_LICENSES.md)

## Requirements

- Python 3.8+
- Docker daemon running and accessible
- Docker Python SDK

## License

MIT License
