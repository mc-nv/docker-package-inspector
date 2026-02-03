# Package Diff Feature

The `--diff` option allows you to compare packages between exactly two Docker images and see what has been added, removed, or changed.

## Usage

```bash
# Basic comparison between two images
docker-package-inspector --diff --image python:3.11 --image python:3.12

# Compare specific architectures
docker-package-inspector --diff \
  --image ubuntu:22.04/amd64 \
  --image ubuntu:24.04/amd64

# Save diff to files
docker-package-inspector --diff \
  --image python:3.11 \
  --image python:3.12 \
  --output diff.json \
  --csv-output diff.csv
```

## Requirements

- Exactly 2 images must be specified
- Cannot use multiple global architectures (use inline syntax instead)
- Inline architecture syntax is supported (e.g., `image:tag/arch`)

## Output Format

### JSON Output

The JSON output includes:
- Metadata about both images (name, digest, architecture, package count)
- Summary statistics (added, removed, changed, unchanged counts)
- Detailed lists of differences

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

### CSV Output

The CSV output provides a flat table format with the following columns:

| Column | Description |
|--------|-------------|
| `change_type` | "ADDED", "REMOVED", or "CHANGED" |
| `name` | Package name |
| `version_from` | Version in the first image (empty for ADDED) |
| `version_to` | Version in the second image (empty for REMOVED) |
| `package_type` | "python" or "binary" |
| `license_from` | License in the first image (empty for ADDED) |
| `license_to` | License in the second image (empty for REMOVED) |
| `source` | Package source URL |

Example CSV output:

```csv
change_type,name,version_from,version_to,package_type,license_from,license_to,source
ADDED,new-package,,1.0.0,python,,MIT,https://pypi.org/project/new-package/
REMOVED,old-package,0.5.0,,python,Apache 2.0,,,https://pypi.org/project/old-package/
CHANGED,updated-package,2.0.0,2.1.0,python,MIT,MIT,
```

## Error Handling

The tool validates input and provides clear error messages:

```bash
# Error: Only 1 image provided
$ docker-package-inspector --diff --image python:3.11
Error: --diff requires exactly 2 images to compare

# Error: Too many images
$ docker-package-inspector --diff --image python:3.11 --image python:3.12 --image python:3.13
Error: --diff requires exactly 2 images to compare

# Error: Multiple architectures
$ docker-package-inspector --diff --image python:3.11 --image python:3.12 --arch amd64 --arch arm64
Error: --diff cannot be used with multiple architectures. Use inline architecture syntax (e.g., image:tag/arch) for each image
```

## Use Cases

1. **Version comparison**: Compare different versions of the same image to see what packages changed
2. **Architecture comparison**: Compare the same image across different architectures
3. **Security audits**: Identify new or removed packages between releases
4. **License compliance**: Track license changes across versions
5. **Dependency tracking**: Monitor package version updates

## Examples

### Compare Python versions
```bash
docker-package-inspector --diff \
  --image python:3.11-slim \
  --image python:3.12-slim \
  --output python-versions-diff.json
```

### Compare Ubuntu releases
```bash
docker-package-inspector --diff \
  --image ubuntu:22.04/amd64 \
  --image ubuntu:24.04/amd64 \
  --csv-output ubuntu-upgrade.csv
```

### Compare development and production images
```bash
docker-package-inspector --diff \
  --image myapp:dev \
  --image myapp:prod \
  --output dev-vs-prod.json
```
