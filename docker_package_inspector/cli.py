"""Command-line interface for docker-package-inspector."""

import argparse
import csv
import json
import sys
from datetime import datetime, timezone

from .inspector import DockerImageInspector


def _parse_image_with_arch(image_str):
    """Parse image string that may include architecture.

    Format: <registry><path>:<tag>/<arch>
    Examples:
        python:3.11/amd64 -> ("python:3.11", "amd64")
        unbuntu:24.04/arm64 -> ("unbuntu:24.04", "arm64")
        python:3.11 -> ("python:3.11", None)

    Returns:
        tuple: (image_name, architecture or None)
    """
    # Check if there's a slash after the tag (architecture separator)
    # We need to be careful not to split on slashes in the registry/path

    # Find the tag separator (:)
    if ":" in image_str:
        # Split on the last occurrence of '/' after the tag
        parts = image_str.rsplit("/", 1)
        if len(parts) == 2:
            # Check if the second part looks like an architecture (no dots, no colons)
            potential_arch = parts[1]
            if (
                "." not in potential_arch
                and ":" not in potential_arch
                and potential_arch
            ):
                # This is likely an architecture
                return (parts[0], potential_arch)

    # No architecture found, or slash is part of registry path
    return (image_str, None)


def _sanitize_csv_value(value):
    """Remove newlines, carriage returns, and tabs from CSV values.

    Replaces all whitespace control characters with single spaces to ensure
    CSV values remain on a single line.
    """
    if isinstance(value, str):
        # Replace newlines, carriage returns, and tabs with spaces
        value = value.replace("\r\n", " ")  # Windows line endings first
        value = value.replace("\n", " ")  # Unix line endings
        value = value.replace("\r", " ")  # Old Mac line endings
        value = value.replace("\t", " ")  # Tabs
        # Collapse multiple spaces into single space
        import re

        value = re.sub(r"\s+", " ", value)
        return value.strip()
    return value


def _truncate_license(license_text, max_length=200):
    """Truncate long license texts to keep only license name/type.

    Args:
        license_text: The license text to truncate
        max_length: Maximum length before truncation (default: 200)

    Returns:
        Truncated license text with ellipsis if needed
    """
    if not license_text or not isinstance(license_text, str):
        return license_text

    # First sanitize to remove newlines
    license_text = _sanitize_csv_value(license_text)

    # If license is short enough, return as-is
    if len(license_text) <= max_length:
        return license_text

    # Common license text patterns that indicate full license text
    full_text_indicators = [
        "Redistribution and use",
        "Permission is hereby granted",
        "All rights reserved",
        "THE SOFTWARE IS PROVIDED",
        "This software is provided",
    ]

    # If it contains full license text indicators, extract just the name
    for indicator in full_text_indicators:
        if indicator in license_text:
            # Try to extract just the license name from the beginning
            # Look for common patterns like "BSD License. Full text...", "MIT. Permission..."
            parts = license_text.split(".", 1)
            if parts[0] and len(parts[0]) < 100:
                return parts[0].strip()

            # Otherwise, take first sentence or clause before "Redistribution", etc.
            idx = license_text.find(indicator)
            if idx > 0:
                prefix = license_text[:idx].strip()
                if len(prefix) > 10 and len(prefix) < 150:
                    return prefix.rstrip(".")

            # Last resort: just return first part
            return license_text[:max_length].strip() + "..."

    # For other long texts, truncate at word boundary
    if len(license_text) > max_length:
        truncated = license_text[:max_length].rsplit(" ", 1)[0]
        return truncated.strip() + "..."

    return license_text


def _write_csv_to_file(output_data, file_handle):
    """Write package data to CSV file handle."""
    fieldnames = [
        "image",
        "digest",
        "architecture",
        "name",
        "version",
        "package_type",
        "package_provider",
        "source",
        "license",
        "license_source",
        "source_code_url",
        "is_dependency",
        "parent_packages",
    ]

    writer = csv.DictWriter(file_handle, fieldnames=fieldnames)
    writer.writeheader()

    # Handle both old single-result and new multi-result formats
    results = output_data.get("results", [output_data])

    for result in results:
        for package in result.get("packages", []):
            row = {
                "image": _sanitize_csv_value(result.get("image", "")),
                "digest": _sanitize_csv_value(result.get("digest", "")),
                "architecture": _sanitize_csv_value(
                    result.get("architecture", "unknown")
                ),
                "name": _sanitize_csv_value(package["name"]),
                "version": _sanitize_csv_value(package["version"]),
                "package_type": _sanitize_csv_value(package["package_type"]),
                "package_provider": _sanitize_csv_value(
                    package.get("package_provider", "Unknown")
                ),
                "source": _sanitize_csv_value(package.get("source", "")),
                "license": _truncate_license(package.get("license", "Unknown")),
                "license_source": _sanitize_csv_value(
                    package.get("license_source", "Unknown")
                ),
                "source_code_url": _sanitize_csv_value(
                    package.get("source_code_url", "")
                ),
                "is_dependency": package.get("is_dependency", False),
                "parent_packages": _sanitize_csv_value(
                    "; ".join(package.get("parent_packages", []))
                ),
            }
            writer.writerow(row)


def _write_csv(output_data, filename):
    """Write package data to CSV file."""
    with open(filename, "w", encoding="utf-8", newline="") as f:
        _write_csv_to_file(output_data, f)


def main():
    """Main entry point for the CLI."""
    parser = argparse.ArgumentParser(
        description="Inspect Docker images and extract package information",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single image
  docker-package-inspector --image python:3.11

  # With inline architecture
  docker-package-inspector --image python:3.11/amd64

  # Multiple images with inline architectures
  docker-package-inspector --images "python:3.11/amd64,python:3.11/arm64"

  # Mix inline and global architectures
  docker-package-inspector --image python:3.11/arm64 --image python:3.12 --arch amd64

  # Multi-arch using --arch flag
  docker-package-inspector --image ubuntu:22.04 --archs "amd64,arm64"

  # Multiple images and architectures (matrix)
  docker-package-inspector --images "python:3.11,python:3.12" --archs "amd64,arm64"

  # Complex example with inline architectures
  docker-package-inspector --images "unbuntu:24.04/amd64,python:3.11/arm64"

  # Output formats
  docker-package-inspector --image python:3.11/amd64 --output packages.json --csv-output packages.csv
        """,
    )

    parser.add_argument(
        "--image",
        action="append",
        help="Docker image name with optional architecture (e.g., python:3.11/amd64, unbuntu:24.04/arm64). Can be specified multiple times",
    )

    parser.add_argument(
        "--images",
        help="Comma-separated list of Docker images with optional architectures",
    )

    parser.add_argument(
        "--architecture",
        "--arch",
        action="append",
        help="Target architecture (e.g., amd64, arm64). Can be specified multiple times for multi-arch inspection",
    )

    parser.add_argument(
        "--architectures",
        "--archs",
        help="Comma-separated list of architectures (e.g., amd64,arm64)",
    )

    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Output JSON file path. If not specified, prints JSON to stdout",
    )

    parser.add_argument(
        "--csv-output",
        default=None,
        help="Output CSV file path. Can be used alongside --output for dual format export",
    )

    parser.add_argument(
        "--format",
        "-f",
        choices=["json", "csv"],
        default="json",
        help="Output format for stdout: json or csv (default: json). Ignored if --output or --csv-output is specified",
    )

    parser.add_argument(
        "--pull",
        action="store_true",
        default=True,
        help="Pull the image if not available locally (default: True)",
    )

    parser.add_argument(
        "--no-pull",
        action="store_false",
        dest="pull",
        help="Do not pull the image, only use local images",
    )

    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable verbose output"
    )

    args = parser.parse_args()

    try:
        # Parse image list with inline architectures
        image_specs = []  # List of (image_name, arch) tuples

        if args.image:
            for img in args.image:
                image_specs.append(_parse_image_with_arch(img))
        if args.images:
            for img in args.images.split(","):
                image_specs.append(_parse_image_with_arch(img.strip()))

        if not image_specs:
            parser.error("At least one image must be specified via --image or --images")

        # Parse global architecture list (fallback if not specified inline)
        global_architectures = []
        if args.architecture:
            global_architectures.extend(args.architecture)
        if args.architectures:
            global_architectures.extend(
                [arch.strip() for arch in args.architectures.split(",")]
            )

        inspector = DockerImageInspector(verbose=args.verbose)
        results = []

        # Build the list of (image, arch) combinations to inspect
        inspection_tasks = []

        for image_name, inline_arch in image_specs:
            if inline_arch:
                # Architecture specified inline - use only that
                inspection_tasks.append((image_name, inline_arch))
            elif global_architectures:
                # No inline arch, but global architectures specified - use all global archs
                for arch in global_architectures:
                    inspection_tasks.append((image_name, arch))
            else:
                # No architecture specified anywhere - use None (default to host arch)
                inspection_tasks.append((image_name, None))

        # Inspect each image-architecture combination
        total_combinations = len(inspection_tasks)

        for idx, (image_name, arch) in enumerate(inspection_tasks, 1):
            if args.verbose:
                print(
                    f"\n[{idx}/{total_combinations}] Inspecting image: {image_name}",
                    file=sys.stderr,
                )
                if arch:
                    print(f"Architecture: {arch}", file=sys.stderr)

            result = inspector.inspect_image(
                image_name=image_name, architecture=arch, pull=args.pull
            )

            results.append(
                {
                    "image": image_name,
                    "digest": result.get("digest", ""),
                    "architecture": result.get("architecture", "unknown"),
                    "packages": result.get("packages", []),
                }
            )

        # Create output data
        # Calculate unique images and architectures
        unique_images = len(set(img for img, _ in image_specs))
        unique_archs = len(
            set(arch for _, arch in inspection_tasks if arch is not None)
        )
        if unique_archs == 0:
            unique_archs = 1  # At least one (host default)

        output_data = {
            "inspection_date": datetime.now(timezone.utc).isoformat(),
            "total_images": unique_images,
            "total_architectures": unique_archs,
            "results": results,
        }

        # Generate output based on specified options
        json_written = False
        csv_written = False

        # Write JSON output
        if args.output:
            json_output = json.dumps(output_data, indent=2)
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(json_output)
            if args.verbose:
                print(f"JSON output written to: {args.output}", file=sys.stderr)
            json_written = True

        # Write CSV output
        if args.csv_output:
            _write_csv(output_data, args.csv_output)
            if args.verbose:
                print(f"CSV output written to: {args.csv_output}", file=sys.stderr)
            csv_written = True

        # If no file output was specified, write to stdout based on format
        if not json_written and not csv_written:
            if args.format == "json":
                output_content = json.dumps(output_data, indent=2)
                print(output_content)
            elif args.format == "csv":
                import io

                output = io.StringIO()
                _write_csv_to_file(output_data, output)
                print(output.getvalue(), end="")

        return 0

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        if args.verbose:
            import traceback

            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
