# Docker Package Inspector

A CLI tool to inspect Docker images and extract detailed package information including Python packages and system binary packages.

## Features

- Inspect Docker images for any architecture
- Extract Python package information (name, version, source, license)
- Extract system binary package information (apt/dpkg, rpm/yum)
- Query PyPI for Python package metadata
- Output results as structured JSON
- Support for multi-architecture images
- **Multiple license detection** - Automatically detects and parses packages with dual or multi-licensing (e.g., "MIT or Apache-2.0")
- **Corporate/proprietary license support** - Properly identifies and preserves corporate and proprietary license names

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
- `parent_packages` - Semicolon-separated list of parent packages

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
# Generate reports for two images
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

- `name`: Package name
- `version`: Package version
- `source`: PyPI URL for Python packages, repository for binary packages
- `license`: Package license. For packages with multiple licenses, they are separated by ` | ` (e.g., "MIT | Apache-2.0"). Corporate and proprietary licenses are preserved with their full names (e.g., "NVIDIA Proprietary License").
- `source_code_url`: URL to source code repository
- `package_type`: Either "python" or "binary"
- `is_dependency`: Boolean indicating if this package is a dependency of another package
- `parent_packages`: Array of package names that depend on this package

### License Parsing

The tool automatically detects and handles multiple licenses:

- **Dual/Multi-licensing**: Packages licensed under multiple licenses (e.g., "MIT or Apache-2.0") are parsed and stored as "MIT | Apache-2.0"
- **Corporate/Proprietary**: Corporate and proprietary licenses (e.g., "NVIDIA Proprietary", "Commercial License") are detected and preserved with their original names
- **Mixed licensing**: Packages with both standard and proprietary components are properly identified (e.g., "Apache-2.0 | NVIDIA Proprietary License")

For more details, see [MULTIPLE_LICENSES.md](MULTIPLE_LICENSES.md)

## Requirements

- Python 3.8+
- Docker daemon running and accessible
- Docker Python SDK

## License

MIT License
