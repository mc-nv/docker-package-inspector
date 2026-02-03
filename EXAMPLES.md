# Docker Package Inspector - Usage Examples

This document provides comprehensive examples for using docker-package-inspector in various scenarios.

## Table of Contents

- [Basic Usage](#basic-usage)
- [Output Analysis with jq](#output-analysis-with-jq)
- [Security and License Compliance](#security-and-license-compliance)
- [Dependency Analysis](#dependency-analysis)
- [Comparison and Reporting](#comparison-and-reporting)
- [Programmatic Usage](#programmatic-usage)

## Basic Usage

### Inspect a Public Image

```bash
docker-package-inspector --image python:3.11-slim --output python.json --verbose
```

Output:
```
[INFO] Connected to Docker daemon
[INFO] Pulling image: python:3.11-slim
[INFO] Image pulled successfully
[INFO] Image architecture: amd64
[INFO] Image digest: python@sha256:5be45dbade29bebd...
[INFO] Found 3 Python packages
[INFO] Found 87 dpkg packages
[INFO] Extracting Python package dependencies...
Output written to: python.json
```

### Inspect NVIDIA Triton Server

```bash
docker-package-inspector \
  --image unbuntu:24.04 \
  --architecture amd64 \
  --output triton_packages.json \
  --verbose
```

### Inspect Local Custom Image

```bash
# Build your image first
docker build -t myapp:latest .

# Inspect without pulling
docker-package-inspector \
  --image myapp:latest \
  --no-pull \
  --output myapp_packages.json
```

### Export to CSV

```bash
# CSV only
docker-package-inspector --image python:3.11-slim --csv-output packages.csv

# Both JSON and CSV
docker-package-inspector \
  --image unbuntu:24.04 \
  --output triton.json \
  --csv-output triton.csv \
  --verbose
```

### Multi-Image Inspection

**Compare multiple Python versions:**
```bash
docker-package-inspector \
  --images "python:3.9-slim,python:3.10-slim,python:3.11-slim,python:3.12-slim" \
  --output python_versions.json \
  --verbose
```

**Analyze different base images:**
```bash
docker-package-inspector \
  --image ubuntu:20.04 \
  --image ubuntu:22.04 \
  --image debian:bullseye \
  --image debian:bookworm \
  --output base_images.json
```

### Multi-Architecture Inspection

**Inline architecture specification (recommended):**
```bash
# Clean syntax with architecture in image name
docker-package-inspector \
  --images "mycompany/app:latest/amd64,mycompany/app:latest/arm64,mycompany/app:latest/arm" \
  --output multiarch_report.json \
  --csv-output multiarch_report.csv
```

**Using --arch flags:**
```bash
docker-package-inspector \
  --image mycompany/app:latest \
  --archs "amd64,arm64,arm/v7" \
  --output multiarch_report.json
```

**Compare ARM and x86 packages:**
```bash
# Inline format
docker-package-inspector \
  --images "alpine:latest/amd64,alpine:latest/arm64" \
  --output alpine_multiarch.json

# Or using --arch flags
docker-package-inspector \
  --image alpine:latest \
  --arch amd64 \
  --arch arm64 \
  --output alpine_multiarch.json
```

**Complex registry paths with architecture:**
```bash
# Works with full registry paths
docker-package-inspector \
  --image unbuntu:24.04/amd64 \
  --image registry.gitlab.com/myproject/app:v1.2.3/arm64 \
  --output complex_registries.json
```

### Matrix Inspection

**Full version × architecture matrix:**
```bash
docker-package-inspector \
  --images "python:3.11,python:3.12" \
  --archs "amd64,arm64" \
  --output python_matrix.json \
  --csv-output python_matrix.csv \
  --verbose
```

This inspects:
- python:3.11 on amd64
- python:3.11 on arm64
- python:3.12 on amd64
- python:3.12 on arm64

## Output Analysis with jq

### Package Statistics

**Get total package count (single image):**
```bash
jq '.packages | length' packages.json
```

**Get package counts (multi-image/multi-arch):**
```bash
# Summary of all results
jq '.results[] | {image, arch: .architecture, packages: (.packages | length)}' multi.json

# Total packages across all results
jq '[.results[].packages | length] | add' multi.json
```

**Count packages by type:**
```bash
# Single image
echo "Python packages: $(jq '[.packages[] | select(.package_type == "python")] | length' packages.json)"
echo "Binary packages: $(jq '[.packages[] | select(.package_type == "binary")] | length' packages.json)"

# Multi-image breakdown
jq -r '.results[] | "\(.image) (\(.architecture)): \([.packages[] | select(.package_type == "python")] | length) Python, \([.packages[] | select(.package_type == "binary")] | length) binary"' multi.json
```

**Count dependencies:**
```bash
jq '[.packages[] | select(.is_dependency == true)] | length' packages.json
```

### Multi-Image Queries

**Compare package counts across images:**
```bash
jq -r '.results[] | "\(.image): \(.packages | length) packages"' multi.json
```

**Find packages unique to one image:**
```bash
# Packages in first image but not in second
jq -s '
  (.[0].results[0].packages | map(.name)) as $first |
  (.[0].results[1].packages | map(.name)) as $second |
  ($first - $second)
' multi.json
```

**Compare architectures:**
```bash
# Show package differences between amd64 and arm64
jq '.results | group_by(.architecture) | map({
  arch: .[0].architecture,
  package_count: ([.[].packages[]] | length),
  images: (. | length)
})' multi_arch.json
```

### Package Queries

**List all Python packages with versions:**
```bash
jq -r '.packages[] | select(.package_type == "python") | "\(.name)==\(.version)"' packages.json
```

Output:
```
pip==24.0
setuptools==79.0.1
wheel==0.45.1
```

**Find package by name:**
```bash
jq '.packages[] | select(.name == "numpy")' packages.json
```

**List packages with source code URLs:**
```bash
jq -r '.packages[] | select(.source_code_url != "") | "\(.name): \(.source_code_url)"' packages.json
```

**Find all packages from a specific source:**
```bash
jq '.packages[] | select(.source_code_url | contains("github.com"))' packages.json
```

## Security and License Compliance

### License Audit

**List all unique licenses:**
```bash
jq -r '.packages[] | .license' packages.json | sort -u
```

**Find GPL-licensed packages:**
```bash
jq -r '.packages[] | select(.license | test("GPL"; "i")) | "\(.name) (\(.license))"' packages.json
```

**Find packages with unknown licenses:**
```bash
jq '.packages[] | select(.license == "Unknown") | {name, version, package_type}' packages.json
```

**Generate license report:**
```bash
jq -r '.packages[] | [.name, .version, .license, .package_type] | @tsv' packages.json \
  | column -t -s $'\t' > license_report.txt
```

### Vulnerability Scanning Preparation

**Generate Python requirements file:**
```bash
jq -r '.packages[] | select(.package_type == "python") | "\(.name)==\(.version)"' packages.json > requirements.txt

# Use with safety or pip-audit
pip-audit -r requirements.txt
```

**Export package list for CVE scanning:**
```bash
jq -r '.packages[] | "\(.name),\(.version),\(.package_type)"' packages.json > packages_for_scanning.csv
```

## Dependency Analysis

### Find Direct Dependencies

**Show what each package depends on:**
```bash
jq -r '.packages[] | select(.parent_packages | length > 0) | "\(.name) is required by: \(.parent_packages | join(", "))"' packages.json
```

**Find all transitive dependencies:**
```bash
# Packages that are dependencies but not in parent list (leaf packages)
jq '.packages[] | select(.is_dependency == true)' packages.json
```

### Dependency Tree

**Create a simple dependency tree:**
```bash
#!/bin/bash
PACKAGE="requests"

echo "Dependencies for $PACKAGE:"
jq --arg pkg "$PACKAGE" '
  .packages[] |
  select(.parent_packages[] == $pkg) |
  "  → \(.name) (\(.version))"
' packages.json -r
```

**Find dependency depth (packages with most dependents):**
```bash
jq '.packages[] | {name, dependent_count: (.parent_packages | length)} | select(.dependent_count > 0)' packages.json \
  | jq -s 'sort_by(-.dependent_count)'
```

## Comparison and Reporting

### Compare Two Image Versions

```bash
# Inspect both versions
docker-package-inspector --image python:3.10 --output python310.json
docker-package-inspector --image python:3.11 --output python311.json

# Find new packages in 3.11
jq -s '
  (.[0].packages | map(.name)) as $old |
  (.[1].packages | map(.name)) as $new |
  ($new - $old)
' python310.json python311.json
```

### Generate HTML Report

```bash
# Create a simple HTML report
jq -r '
"<html><head><title>Package Report: \(.image)</title></head><body>",
"<h1>Package Report</h1>",
"<p><strong>Image:</strong> \(.image)</p>",
"<p><strong>Digest:</strong> \(.digest)</p>",
"<p><strong>Architecture:</strong> \(.architecture)</p>",
"<h2>Packages (\(.packages | length) total)</h2>",
"<table border=\"1\">",
"<tr><th>Name</th><th>Version</th><th>Type</th><th>License</th><th>Dependency</th></tr>",
(.packages[] |
  "<tr><td>\(.name)</td><td>\(.version)</td><td>\(.package_type)</td><td>\(.license)</td><td>\(.is_dependency)</td></tr>"
),
"</table></body></html>"
' packages.json > report.html
```

### Export to Different Formats

**Direct CSV Export (recommended):**
```bash
docker-package-inspector --image python:3.11 --csv-output packages.csv
```

**Manual CSV conversion from JSON:**
```bash
jq -r '["Name","Version","Type","License","Source"] | @csv' packages.json > /dev/null
jq -r '.packages[] | [.name, .version, .package_type, .license, .source] | @csv' packages.json >> packages.csv
```

**Working with CSV files:**
```bash
# Import into SQLite
sqlite3 packages.db <<EOF
CREATE TABLE packages (
  image TEXT,
  digest TEXT,
  architecture TEXT,
  name TEXT,
  version TEXT,
  package_type TEXT,
  source TEXT,
  license TEXT,
  source_code_url TEXT,
  is_dependency BOOLEAN,
  parent_packages TEXT
);
.mode csv
.import packages.csv packages
.headers on
SELECT * FROM packages WHERE license LIKE '%GPL%';
EOF

# Filter CSV with standard tools
head -1 packages.csv > python_only.csv
grep ",python," packages.csv >> python_only.csv

# Sort by package name
(head -1 packages.csv && tail -n +2 packages.csv | sort -t, -k4) > sorted_packages.csv
```

**Markdown Table:**
```bash
echo "| Name | Version | Type | License |"
echo "|------|---------|------|---------|"
jq -r '.packages[] | "| \(.name) | \(.version) | \(.package_type) | \(.license) |"' packages.json
```

## Programmatic Usage

### Basic Python Script

```python
#!/usr/bin/env python3
from docker_package_inspector.inspector import DockerImageInspector
import json

def analyze_image(image_name):
    inspector = DockerImageInspector(verbose=True)
    result = inspector.inspect_image(image_name, pull=True)

    print(f"\n{'='*60}")
    print(f"Analysis of {image_name}")
    print(f"{'='*60}")
    print(f"Digest: {result['digest']}")
    print(f"Architecture: {result['architecture']}")
    print(f"Total packages: {len(result['packages'])}")

    python_pkgs = [p for p in result['packages'] if p['package_type'] == 'python']
    binary_pkgs = [p for p in result['packages'] if p['package_type'] == 'binary']

    print(f"Python packages: {len(python_pkgs)}")
    print(f"Binary packages: {len(binary_pkgs)}")

    return result

if __name__ == "__main__":
    result = analyze_image("python:3.11-slim")

    # Save to file
    with open("output.json", "w") as f:
        json.dump(result, f, indent=2)
```

### Find Specific Package Information

```python
from docker_package_inspector.inspector import DockerImageInspector

def find_package_info(image_name, package_name):
    inspector = DockerImageInspector(verbose=False)
    result = inspector.inspect_image(image_name, pull=True)

    for pkg in result['packages']:
        if pkg['name'].lower() == package_name.lower():
            print(f"Package: {pkg['name']}")
            print(f"Version: {pkg['version']}")
            print(f"Type: {pkg['package_type']}")
            print(f"License: {pkg['license']}")
            print(f"Source: {pkg['source_code_url']}")

            if pkg['is_dependency']:
                print(f"Required by: {', '.join(pkg['parent_packages'])}")
            return pkg

    print(f"Package '{package_name}' not found")
    return None

# Usage
find_package_info("python:3.11-slim", "pip")
```

### License Compliance Check

```python
from docker_package_inspector.inspector import DockerImageInspector

FORBIDDEN_LICENSES = ["GPL-3.0", "AGPL"]
ALLOWED_LICENSES = ["MIT", "Apache", "BSD"]

def check_license_compliance(image_name):
    inspector = DockerImageInspector(verbose=False)
    result = inspector.inspect_image(image_name, pull=True)

    violations = []
    unknown = []

    for pkg in result['packages']:
        license = pkg['license']

        # Check for forbidden licenses
        if any(forbidden in license for forbidden in FORBIDDEN_LICENSES):
            violations.append({
                'name': pkg['name'],
                'license': license,
                'type': pkg['package_type']
            })

        # Track unknown licenses
        if license == "Unknown":
            unknown.append(pkg['name'])

    print(f"License Compliance Report for {image_name}")
    print(f"{'='*60}")

    if violations:
        print(f"\n⚠️  Found {len(violations)} license violations:")
        for v in violations:
            print(f"  - {v['name']} ({v['type']}): {v['license']}")
    else:
        print("✅ No license violations found")

    if unknown:
        print(f"\n⚠️  Found {len(unknown)} packages with unknown licenses:")
        for name in unknown[:10]:  # Show first 10
            print(f"  - {name}")
        if len(unknown) > 10:
            print(f"  ... and {len(unknown) - 10} more")

    return len(violations) == 0

# Usage
check_license_compliance("python:3.11-slim")
```

### Batch Processing Multiple Images

```python
from docker_package_inspector.inspector import DockerImageInspector
import json
from datetime import datetime

def inspect_multiple_images(image_list):
    inspector = DockerImageInspector(verbose=True)
    results = {}

    for image_name in image_list:
        print(f"\n{'='*60}")
        print(f"Inspecting {image_name}...")
        print(f"{'='*60}")

        try:
            result = inspector.inspect_image(image_name, pull=True)
            results[image_name] = {
                'digest': result['digest'],
                'architecture': result['architecture'],
                'package_count': len(result['packages']),
                'python_count': len([p for p in result['packages'] if p['package_type'] == 'python']),
                'binary_count': len([p for p in result['packages'] if p['package_type'] == 'binary']),
                'timestamp': datetime.now().isoformat()
            }
        except Exception as e:
            print(f"Error inspecting {image_name}: {e}")
            results[image_name] = {'error': str(e)}

    # Save summary
    with open('inspection_summary.json', 'w') as f:
        json.dump(results, f, indent=2)

    return results

# Usage
images = [
    "python:3.9-slim",
    "python:3.10-slim",
    "python:3.11-slim",
    "python:3.12-slim"
]

results = inspect_multiple_images(images)
print("\nSummary:")
for image, data in results.items():
    if 'error' not in data:
        print(f"{image}: {data['package_count']} packages")
```

## Tips and Best Practices

1. **Use `--verbose` for debugging**: See what the tool is doing step-by-step
2. **Cache results**: Save JSON outputs to avoid re-inspecting the same image
3. **Combine with jq**: jq is powerful for querying and transforming JSON data
4. **Automate in CI/CD**: Run as part of your build pipeline to track package changes
5. **Regular audits**: Schedule periodic scans to catch license or security issues
6. **Compare versions**: Track package changes between image versions
7. **Document findings**: Generate reports for compliance and security teams
