"""Command-line interface for docker-package-inspector."""

import argparse
import csv
import json
import signal
import sys
from datetime import datetime, timezone

from . import __version__
from .inspector import DockerImageInspector


def _signal_handler(signum, frame):
    """Handle interrupt signals gracefully by raising KeyboardInterrupt."""
    raise KeyboardInterrupt()


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


def _sanitize_image_name_for_filename(image_name):
    """Sanitize image name to create a valid filename.

    Args:
        image_name: Docker image name (e.g., "python:3.11", "ubuntu:22.04")

    Returns:
        str: Sanitized name suitable for filename (e.g., "python_3.11", "ubuntu_22.04")
    """
    # Replace special characters with underscores
    # Replace : / . - with underscores, and remove other special chars
    sanitized = image_name.replace(":", "_").replace("/", "_").replace(".", "_")
    # Remove any other special characters that might cause issues
    import re

    sanitized = re.sub(r"[^\w\-_]", "_", sanitized)
    # Collapse multiple underscores into single underscore
    sanitized = re.sub(r"_+", "_", sanitized)
    # Remove leading/trailing underscores
    sanitized = sanitized.strip("_")
    return sanitized


def _generate_default_output_filename(image_specs, inspection_tasks, is_diff=False):
    """Generate default output filename based on images, architectures, and action.

    Args:
        image_specs: List of (image_name, arch) tuples
        inspection_tasks: List of (image_name, arch) tuples for all inspections
        is_diff: Whether this is a diff operation

    Returns:
        tuple: (json_filename, csv_filename)
    """
    if is_diff:
        # Diff mode: image1_vs_image2_arch_diff.json
        img1_name, img1_arch = inspection_tasks[0]
        img2_name, img2_arch = inspection_tasks[1]

        sanitized_img1 = _sanitize_image_name_for_filename(img1_name)
        sanitized_img2 = _sanitize_image_name_for_filename(img2_name)

        # Add architecture if specified
        arch_part = ""
        if img1_arch:
            arch_part = f"_{img1_arch}"

        base_filename = f"diff_{sanitized_img1}_vs_{sanitized_img2}{arch_part}"
    else:
        # Regular scan mode
        if len(image_specs) == 1 and len(inspection_tasks) == 1:
            # Single image, single architecture
            img_name, arch = inspection_tasks[0]
            sanitized_img = _sanitize_image_name_for_filename(img_name)
            arch_part = f"_{arch}" if arch else ""
            base_filename = f"{sanitized_img}{arch_part}"
        else:
            # Multiple images or architectures
            # Use first image name and indicate multi
            img_name = image_specs[0][0]
            sanitized_img = _sanitize_image_name_for_filename(img_name)

            num_images = len(set(img for img, _ in image_specs))
            num_archs = len(
                set(arch for _, arch in inspection_tasks if arch is not None)
            )

            if num_images > 1 and num_archs > 1:
                base_filename = f"{sanitized_img}_and_{num_images-1}_more_multi_arch"
            elif num_images > 1:
                base_filename = f"{sanitized_img}_and_{num_images-1}_more"
            elif num_archs > 1:
                base_filename = f"{sanitized_img}_multi_arch"
            else:
                base_filename = sanitized_img

    # Use /tmp/ as default output location
    json_filename = f"/tmp/{base_filename}.json"
    csv_filename = f"/tmp/{base_filename}.csv"

    return json_filename, csv_filename


def _get_parent_packages_separator(csv_delimiter):
    """Determine appropriate separator for parent_packages field based on CSV delimiter.

    Args:
        csv_delimiter: The CSV column delimiter

    Returns:
        str: Appropriate separator for parent_packages field
    """
    # Map common CSV delimiters to parent_packages separators
    # Use a separator that won't conflict with the CSV delimiter
    separator_map = {
        ",": "; ",  # Comma CSV -> semicolon for parent_packages
        ";": ", ",  # Semicolon CSV -> comma for parent_packages
        "\t": ", ",  # Tab CSV -> comma for parent_packages
        "|": ", ",  # Pipe CSV -> comma for parent_packages
    }
    return separator_map.get(csv_delimiter, "; ")


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


def _write_csv_to_file(output_data, file_handle, delimiter=","):
    """Write package data to CSV file handle.

    Args:
        output_data: Package data to write
        file_handle: File handle to write to
        delimiter: CSV column delimiter (default: ",")
    """
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

    # Determine appropriate separator for parent_packages field
    parent_separator = _get_parent_packages_separator(delimiter)

    writer = csv.DictWriter(file_handle, fieldnames=fieldnames, delimiter=delimiter)
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
                    parent_separator.join(package.get("parent_packages", []))
                ),
            }
            writer.writerow(row)


def _write_csv(output_data, filename, delimiter=","):
    """Write package data to CSV file.

    Args:
        output_data: Package data to write
        filename: Output file path
        delimiter: CSV column delimiter (default: ",")
    """
    with open(filename, "w", encoding="utf-8", newline="") as f:
        _write_csv_to_file(output_data, f, delimiter)


def _compute_package_diff(
    result1, result2, exclusion_packages=None, exclusion_image_name=None
):
    """Compute the difference between two package lists.

    Args:
        result1: First image result with 'packages' list
        result2: Second image result with 'packages' list
        exclusion_packages: Optional dict of packages to mark as inherited (keyed by name)
        exclusion_image_name: Optional name of the exclusion image

    Returns:
        dict: Structured diff with 'added', 'removed', and 'changed' lists
    """
    # Create dictionaries keyed by package name for easy lookup
    packages1 = {pkg["name"]: pkg for pkg in result1.get("packages", [])}
    packages2 = {pkg["name"]: pkg for pkg in result2.get("packages", [])}

    exclusion_names = set(exclusion_packages.keys()) if exclusion_packages else set()

    # Find packages only in image 2 (added)
    added = []
    for name, pkg in packages2.items():
        if name not in packages1:
            # Preserve all package fields and add change_type
            pkg_copy = pkg.copy()
            if name in exclusion_names:
                pkg_copy["change_type"] = "ADDED-INHERITED"
                pkg_copy["inherited_from"] = exclusion_image_name
            else:
                pkg_copy["change_type"] = "ADDED"
            added.append(pkg_copy)

    # Find packages only in image 1 (removed) - NO exclusion logic
    removed = []
    for name, pkg in packages1.items():
        if name not in packages2:
            # Preserve all package fields and add change_type
            pkg_copy = pkg.copy()
            pkg_copy["change_type"] = "REMOVED"
            removed.append(pkg_copy)

    # Find packages in both but with different versions (changed) - NO exclusion logic
    changed = []
    for name in packages1:
        if name in packages2:
            pkg1 = packages1[name]
            pkg2 = packages2[name]
            if pkg1["version"] != pkg2["version"]:
                # Use pkg2 as base (new version) and preserve all fields
                pkg_copy = pkg2.copy()
                pkg_copy["change_type"] = "CHANGED"
                # Add version_from for changed packages to show the transition
                pkg_copy["version_from"] = pkg1["version"]
                changed.append(pkg_copy)

    # Sort all lists by name
    added.sort(key=lambda x: x["name"])
    removed.sort(key=lambda x: x["name"])
    changed.sort(key=lambda x: x["name"])

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
    }


def _write_diff_csv_to_file(diff_data, file_handle, delimiter=","):
    """Write diff data to CSV file handle.

    Args:
        diff_data: Diff data to write
        file_handle: File handle to write to
        delimiter: CSV column delimiter (default: ",")
    """
    # Use same fieldnames as regular output, but add change_type, version_from, and inherited_from
    fieldnames = [
        "change_type",
        "name",
        "version",
        "version_from",
        "package_type",
        "package_provider",
        "source",
        "license",
        "license_source",
        "source_code_url",
        "is_dependency",
        "parent_packages",
        "inherited_from",
    ]

    # Determine appropriate separator for parent_packages field
    parent_separator = _get_parent_packages_separator(delimiter)

    writer = csv.DictWriter(
        file_handle, fieldnames=fieldnames, delimiter=delimiter, extrasaction="ignore"
    )
    writer.writeheader()

    # Write all packages (added, removed, changed) with preserved fields
    all_packages = []
    all_packages.extend(diff_data["added"])
    all_packages.extend(diff_data["removed"])
    all_packages.extend(diff_data["changed"])

    # Sort by change_type (ADDED, ADDED-INHERITED, CHANGED, REMOVED) and then by name
    all_packages.sort(key=lambda x: (x["change_type"], x["name"]))

    for pkg in all_packages:
        row = {
            "change_type": pkg["change_type"],
            "name": _sanitize_csv_value(pkg["name"]),
            "version": _sanitize_csv_value(pkg["version"]),
            "version_from": _sanitize_csv_value(pkg.get("version_from", "")),
            "package_type": _sanitize_csv_value(pkg["package_type"]),
            "package_provider": _sanitize_csv_value(
                pkg.get("package_provider", "Unknown")
            ),
            "source": _sanitize_csv_value(pkg.get("source", "")),
            "license": _truncate_license(pkg.get("license", "Unknown")),
            "license_source": _sanitize_csv_value(pkg.get("license_source", "Unknown")),
            "source_code_url": _sanitize_csv_value(pkg.get("source_code_url", "")),
            "is_dependency": pkg.get("is_dependency", False),
            "parent_packages": _sanitize_csv_value(
                parent_separator.join(pkg.get("parent_packages", []))
            ),
            "inherited_from": _sanitize_csv_value(pkg.get("inherited_from", "")),
        }
        writer.writerow(row)


def _write_diff_csv(diff_data, filename, delimiter=","):
    """Write diff data to CSV file.

    Args:
        diff_data: Diff data to write
        filename: Output file path
        delimiter: CSV column delimiter (default: ",")
    """
    with open(filename, "w", encoding="utf-8", newline="") as f:
        _write_diff_csv_to_file(diff_data, f, delimiter)


def main():
    """Main entry point for the CLI."""
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

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
  docker-package-inspector --image python:3.11/amd64 --json-output packages.json --csv-output packages.csv

  # Diff mode - compare two images
  docker-package-inspector --diff --image python:3.11 --image python:3.12
  docker-package-inspector --diff --image ubuntu:22.04/amd64 --image ubuntu:24.04/amd64 --json-output diff.json
        """,
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
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
        "--json-output",
        "-o",
        default=None,
        help="Output JSON file path. If not specified, uses auto-generated filename in /tmp/",
    )

    parser.add_argument(
        "--csv-output",
        default=None,
        help="Output CSV file path. If not specified, uses auto-generated filename in /tmp/",
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

    parser.add_argument(
        "--diff",
        action="store_true",
        help="Compare packages between exactly two images and show differences (added, removed, changed)",
    )

    parser.add_argument(
        "--delimiter",
        "--separator",
        dest="delimiter",
        default=",",
        help='CSV column delimiter (default: ","). Common values: "," (comma), ";" (semicolon), "\\t" (tab), "|" (pipe)',
    )

    parser.add_argument(
        "--exclude-packages-from-image",
        metavar="IMAGE",
        help="Exclude packages present in this base image by marking them as INHERITED in diff output",
    )

    args = parser.parse_args()

    if args.verbose:
        print(f"docker-package-inspector version {__version__}", file=sys.stderr)

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

        # Validate --diff requirements
        if args.diff:
            if len(image_specs) != 2:
                parser.error("--diff requires exactly 2 images to compare")
            if global_architectures:
                parser.error(
                    "--diff cannot be used with multiple architectures. Use inline architecture syntax (e.g., image:tag/arch) for each image"
                )

        # Validate --exclude-packages-from-image requirements
        if args.exclude_packages_from_image and not args.diff:
            parser.error("--exclude-packages-from-image requires --diff mode")

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
            try:
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
            except KeyboardInterrupt:
                if args.verbose:
                    print(
                        f"\nInterrupted while processing image {idx}/{total_combinations}",
                        file=sys.stderr,
                    )
                raise

        # Inspect exclusion image if provided (only in diff mode)
        exclusion_packages = None
        exclusion_image_name = None
        if args.diff and args.exclude_packages_from_image:
            # Parse exclusion image name and architecture
            excl_image, excl_arch = _parse_image_with_arch(
                args.exclude_packages_from_image
            )

            # Use inline arch if specified, otherwise use same arch as first compared image
            excl_architecture = excl_arch if excl_arch else results[0]["architecture"]

            if args.verbose:
                print(
                    f"\nInspecting exclusion image: {excl_image}",
                    file=sys.stderr,
                )
                if excl_architecture:
                    print(f"Architecture: {excl_architecture}", file=sys.stderr)

            exclusion_image_name = args.exclude_packages_from_image
            exclusion_result = inspector.inspect_image(
                image_name=excl_image, architecture=excl_architecture, pull=args.pull
            )
            exclusion_packages = {
                pkg["name"]: pkg for pkg in exclusion_result.get("packages", [])
            }
            if args.verbose:
                print(
                    f"Found {len(exclusion_packages)} packages in exclusion image",
                    file=sys.stderr,
                )

        # Create output data
        if args.diff:
            # Diff mode - compare the two images
            diff_result = _compute_package_diff(
                results[0],
                results[1],
                exclusion_packages=exclusion_packages,
                exclusion_image_name=exclusion_image_name,
            )

            output_data = {
                "version": __version__,
                "inspection_date": datetime.now(timezone.utc).isoformat(),
                "comparison_type": "diff",
                "image_from": {
                    "name": results[0]["image"],
                    "digest": results[0]["digest"],
                    "architecture": results[0]["architecture"],
                    "total_packages": len(results[0]["packages"]),
                },
                "image_to": {
                    "name": results[1]["image"],
                    "digest": results[1]["digest"],
                    "architecture": results[1]["architecture"],
                    "total_packages": len(results[1]["packages"]),
                },
                "summary": {
                    "added": len(diff_result["added"]),
                    "removed": len(diff_result["removed"]),
                    "changed": len(diff_result["changed"]),
                    "unchanged": len(results[0]["packages"])
                    - len(diff_result["removed"])
                    - len(diff_result["changed"]),
                },
                "differences": diff_result,
            }

            # Add exclusion image info if provided
            if exclusion_image_name:
                output_data["exclusion_image"] = {
                    "name": exclusion_image_name,
                    "total_packages": len(exclusion_packages)
                    if exclusion_packages
                    else 0,
                }
        else:
            # Normal mode - list all packages
            # Calculate unique images and architectures
            unique_images = len(set(img for img, _ in image_specs))
            unique_archs = len(
                set(arch for _, arch in inspection_tasks if arch is not None)
            )
            if unique_archs == 0:
                unique_archs = 1  # At least one (host default)

            output_data = {
                "version": __version__,
                "inspection_date": datetime.now(timezone.utc).isoformat(),
                "total_images": unique_images,
                "total_architectures": unique_archs,
                "results": results,
            }

        # Generate output based on specified options
        # If no output files specified, generate default filenames
        if not args.json_output and not args.csv_output:
            default_json, default_csv = _generate_default_output_filename(
                image_specs, inspection_tasks, is_diff=args.diff
            )
            json_output_file = default_json
            csv_output_file = default_csv
            if args.verbose:
                print(
                    f"\nNo output files specified. Using default filenames:",
                    file=sys.stderr,
                )
                print(f"  JSON: {json_output_file}", file=sys.stderr)
                print(f"  CSV:  {csv_output_file}", file=sys.stderr)
        else:
            json_output_file = args.json_output
            csv_output_file = args.csv_output

        # Write JSON output
        if json_output_file:
            json_output = json.dumps(output_data, indent=2)
            with open(json_output_file, "w", encoding="utf-8") as f:
                f.write(json_output)
            if args.verbose or not args.json_output:
                print(f"JSON output written to: {json_output_file}", file=sys.stderr)

        # Write CSV output
        if csv_output_file:
            if args.diff:
                _write_diff_csv(
                    output_data["differences"], csv_output_file, args.delimiter
                )
            else:
                _write_csv(output_data, csv_output_file, args.delimiter)
            if args.verbose or not args.csv_output:
                print(f"CSV output written to: {csv_output_file}", file=sys.stderr)

        return 0

    except KeyboardInterrupt:
        print("\n\nInterrupted by user. Exiting...", file=sys.stderr)
        return 130  # Standard exit code for SIGINT
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        if args.verbose:
            import traceback

            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
