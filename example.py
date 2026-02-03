#!/usr/bin/env python3
"""Example usage of docker-package-inspector."""

import json

from docker_package_inspector.inspector import DockerImageInspector


def main():
    """Example: Inspect a Docker image programmatically."""

    # Create inspector
    inspector = DockerImageInspector(verbose=True)

    # Inspect an image
    image_name = "python:3.11-slim"
    print(f"Inspecting image: {image_name}")

    result = inspector.inspect_image(
        image_name=image_name, architecture="amd64", pull=True
    )

    # Print results
    print("\n" + "=" * 60)
    print(f"Architecture: {result['architecture']}")
    print(f"Python packages found: {len(result['python_packages'])}")
    print(f"Binary packages found: {len(result['binary_packages'])}")
    print("=" * 60)

    # Show first few Python packages
    print("\nFirst 5 Python packages:")
    for pkg in result["python_packages"][:5]:
        print(f"  - {pkg['name']} {pkg['version']} (License: {pkg['license']})")

    # Show first few binary packages
    print("\nFirst 5 Binary packages:")
    for pkg in result["binary_packages"][:5]:
        print(f"  - {pkg['name']} {pkg['version']}")

    # Save to JSON file
    output_file = "example_output.json"
    with open(output_file, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\nFull output saved to: {output_file}")


if __name__ == "__main__":
    main()
