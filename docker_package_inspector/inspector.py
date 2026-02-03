"""Docker image inspection logic."""

import json
import signal
import sys

import docker
from docker.errors import APIError, DockerException, ImageNotFound

from .package_metadata import PackageMetadataFetcher


class DockerImageInspector:
    """Inspects Docker images and extracts package information."""

    def __init__(self, verbose=False):
        """Initialize the inspector.

        Args:
            verbose: Enable verbose logging
        """
        self.verbose = verbose
        self.client = None
        self.metadata_fetcher = PackageMetadataFetcher()

    def _log(self, message):
        """Log a message if verbose mode is enabled."""
        if self.verbose:
            print(f"[INFO] {message}", file=sys.stderr)

    def _get_docker_client(self):
        """Get or create Docker client."""
        if self.client is None:
            try:
                self.client = docker.from_env()
                self._log("Connected to Docker daemon")
            except DockerException as e:
                raise Exception(f"Failed to connect to Docker daemon: {e}")
        return self.client

    def inspect_image(self, image_name, architecture=None, pull=True):
        """Inspect a Docker image and extract package information.

        Args:
            image_name: Name of the Docker image
            architecture: Target architecture (amd64, arm64, etc.)
            pull: Whether to pull the image if not available

        Returns:
            Dictionary with python_packages and binary_packages lists
        """
        client = self._get_docker_client()

        # Pull image if requested
        if pull:
            self._log(f"Pulling image: {image_name}")
            try:
                platform = f"linux/{architecture}" if architecture else None
                client.images.pull(image_name, platform=platform)
                self._log("Image pulled successfully")
            except Exception as e:
                self._log(f"Warning: Failed to pull image: {e}")

        # Get image
        try:
            image = client.images.get(image_name)
        except ImageNotFound:
            raise Exception(f"Image not found: {image_name}. Try with --pull flag.")

        # Get image architecture and digest
        image_arch = image.attrs.get("Architecture", "unknown")
        image_digest = ""
        if image.attrs.get("RepoDigests"):
            full_digest = (
                image.attrs["RepoDigests"][0] if image.attrs["RepoDigests"] else ""
            )
            # Extract only the checksum part (after @)
            if "@" in full_digest:
                image_digest = full_digest.split("@", 1)[1]
            else:
                image_digest = full_digest
        self._log(f"Image architecture: {image_arch}")
        self._log(f"Image digest: {image_digest}")

        # Create and run container
        self._log("Creating temporary container...")
        container = None
        try:
            container = client.containers.create(
                image_name, command="sleep infinity", detach=True
            )

            # Start the container
            container.start()
            self._log("Container started")

            python_packages = self._extract_python_packages(container)
            binary_packages = self._extract_binary_packages(container)

            # Get Python package dependencies
            python_deps = self._get_python_dependencies(container)

            # Mark packages with dependency information
            all_packages = []

            # Process Python packages
            for pkg in python_packages:
                pkg["package_type"] = "python"
                pkg["is_dependency"] = False
                pkg["parent_packages"] = []
                all_packages.append(pkg)

            # Process binary packages
            for pkg in binary_packages:
                pkg["package_type"] = "binary"
                pkg["is_dependency"] = False
                pkg["parent_packages"] = []
                all_packages.append(pkg)

            # Mark Python dependencies
            for pkg_name, deps in python_deps.items():
                for dep in deps:
                    for pkg in all_packages:
                        if (
                            pkg["name"].lower() == dep.lower()
                            and pkg["package_type"] == "python"
                        ):
                            pkg["is_dependency"] = True
                            if pkg_name not in pkg["parent_packages"]:
                                pkg["parent_packages"].append(pkg_name)

            # Post-process: Check source URLs for packages with unknown licenses
            self._enrich_unknown_licenses(all_packages, container)

            return {
                "digest": image_digest,
                "architecture": image_arch,
                "packages": all_packages,
            }

        except KeyboardInterrupt:
            self._log("Interrupted by user, cleaning up container...")
            raise
        except Exception as e:
            raise Exception(f"Failed to inspect container: {e}")
        finally:
            # Ensure container is always removed, even on interrupt
            if container is not None:
                try:
                    container.remove(force=True)
                    self._log("Temporary container removed")
                except Exception as cleanup_error:
                    self._log(f"Warning: Failed to remove container: {cleanup_error}")

    def _extract_python_packages(self, container):
        """Extract Python packages from container.

        Args:
            container: Docker container object

        Returns:
            List of Python package dictionaries
        """
        self._log("Extracting Python packages...")
        packages = []

        # Try to get pip list output
        try:
            # Try pip list --format=json
            exit_code, output = container.exec_run(
                "pip list --format=json", demux=False
            )

            if exit_code == 0 and output:
                output_str = output.decode("utf-8")
                # Filter out pip notices/warnings - only keep JSON lines
                lines = output_str.split("\n")
                json_line = None
                for line in lines:
                    if line.strip().startswith("["):
                        json_line = line
                        break

                if json_line:
                    pip_packages = json.loads(json_line)
                    self._log(f"Found {len(pip_packages)} Python packages")
                else:
                    self._log("No valid JSON output from pip list")
                    return packages

                for pkg in pip_packages:
                    name = pkg.get("name", "")
                    version = pkg.get("version", "")

                    if name:
                        # Get metadata from PyPI
                        metadata = self.metadata_fetcher.get_pypi_metadata(
                            name, version
                        )
                        packages.append(
                            {
                                "name": name,
                                "version": version,
                                "source": metadata.get(
                                    "source", f"https://pypi.org/project/{name}/"
                                ),
                                "license": metadata.get("license", "Unknown"),
                                "license_source": metadata.get(
                                    "license_source", "PyPI API"
                                ),
                                "source_code_url": metadata.get("source_code_url", ""),
                                "package_provider": "PIP",
                            }
                        )
        except Exception as e:
            self._log(f"Warning: Failed to extract Python packages: {e}")

        return packages

    def _get_python_dependencies(self, container):
        """Get dependency information for Python packages.

        Args:
            container: Docker container object

        Returns:
            Dictionary mapping package names to their dependencies
        """
        self._log("Extracting Python package dependencies...")
        dependencies = {}

        try:
            # Get list of all packages first
            exit_code, output = container.exec_run(
                "pip list --format=json", demux=False
            )

            if exit_code != 0:
                return dependencies

            output_str = output.decode("utf-8")
            lines = output_str.split("\n")
            json_line = None
            for line in lines:
                if line.strip().startswith("["):
                    json_line = line
                    break

            if not json_line:
                return dependencies

            packages = json.loads(json_line)

            # Get dependencies for each package
            for pkg in packages:
                pkg_name = pkg.get("name", "")
                if not pkg_name:
                    continue

                # Use pip show to get dependencies
                exit_code, show_output = container.exec_run(
                    f"pip show {pkg_name}", demux=False
                )

                if exit_code == 0:
                    show_str = show_output.decode("utf-8")
                    for line in show_str.split("\n"):
                        if line.startswith("Requires:"):
                            deps_str = line.split("Requires:")[1].strip()
                            if deps_str and deps_str != "":
                                deps = [d.strip() for d in deps_str.split(",")]
                                dependencies[pkg_name] = deps
                            break

            self._log(f"Found dependencies for {len(dependencies)} packages")
        except Exception as e:
            self._log(f"Warning: Failed to extract dependencies: {e}")

        return dependencies

    def _extract_binary_packages(self, container):
        """Extract binary/system packages from container.

        Args:
            container: Docker container object

        Returns:
            List of binary package dictionaries
        """
        self._log("Extracting binary packages...")
        packages = []

        # Try dpkg (Debian/Ubuntu)
        packages_dpkg = self._extract_dpkg_packages(container)
        if packages_dpkg:
            packages.extend(packages_dpkg)
            return packages

        # Try rpm (RedHat/CentOS)
        packages_rpm = self._extract_rpm_packages(container)
        if packages_rpm:
            packages.extend(packages_rpm)
            return packages

        # Try apk (Alpine)
        packages_apk = self._extract_apk_packages(container)
        if packages_apk:
            packages.extend(packages_apk)

        return packages

    def _extract_dpkg_packages(self, container):
        """Extract packages using dpkg (Debian/Ubuntu)."""
        try:
            exit_code, output = container.exec_run(
                "dpkg-query -W -f='${Package}|${Version}|${Homepage}|${Source}\n'",
                demux=False,
            )

            if exit_code != 0:
                return []

            packages = []
            lines = output.decode("utf-8").strip().split("\n")
            self._log(f"Found {len(lines)} dpkg packages")

            for line in lines:
                if not line:
                    continue
                parts = line.split("|")
                if len(parts) >= 2:
                    name = parts[0]
                    version = parts[1]
                    homepage = parts[2] if len(parts) > 2 else ""
                    source = parts[3] if len(parts) > 3 else name

                    # Get license info
                    license_info, license_source = self._get_dpkg_license(
                        container, name
                    )

                    packages.append(
                        {
                            "name": name,
                            "version": version,
                            "source": homepage or f"https://packages.ubuntu.com/{name}",
                            "license": license_info,
                            "license_source": license_source,
                            "source_code_url": homepage,
                            "package_provider": "dpkg",
                        }
                    )

            return packages
        except Exception as e:
            self._log(f"dpkg extraction failed: {e}")
            return []

    def _get_dpkg_license(self, container, package_name):
        """Get license information for a dpkg package.

        Returns:
            Tuple of (license_info, license_source)
        """
        try:
            copyright_file = f"/usr/share/doc/{package_name}/copyright"
            exit_code, output = container.exec_run(f"cat {copyright_file}", demux=False)
            if exit_code == 0:
                content = output.decode("utf-8", errors="ignore")
                license_info = self._parse_copyright_file(content)
                return license_info, copyright_file
        except:
            pass
        return "Unknown", "Not found"

    def _parse_copyright_file(self, content):
        """Parse copyright file to extract license information.

        Args:
            content: Copyright file content

        Returns:
            License string (may contain multiple licenses separated by " | ") or "Unknown"
        """
        if not content:
            return "Unknown"

        licenses = []

        # First, look for explicit License: field
        lines = content.split("\n")
        explicit_licenses = []

        for line in lines:
            line_stripped = line.strip()

            # Match "License: <name>" pattern
            if line_stripped.startswith("License:"):
                license_part = line_stripped.split("License:", 1)[1].strip()
                # Extract the license name
                license_text = license_part.split("\n")[0].strip()
                if license_text:
                    # Parse multiple licenses from the field
                    parsed = self._parse_license_field(license_text)
                    explicit_licenses.extend(parsed)

        # Use the metadata fetcher's sophisticated license detection on full content
        detected_license = self.metadata_fetcher._detect_license_from_content(content)

        # Combine explicit and detected licenses
        if explicit_licenses:
            licenses.extend(explicit_licenses)
        if detected_license:
            # detected_license may already be multiple licenses joined by " | "
            for lic in detected_license.split(" | "):
                if lic not in licenses:
                    licenses.append(lic)

        # Also check if content contains proprietary indicators, even if standard licenses were found
        # This handles cases like "Apache-2.0. Some components under proprietary license"
        import re

        proprietary_indicators = [
            r"\bproprietary\b",
            r"\bcommercial\b",
            r"\bcorporate\b",
        ]
        lower_content = content.lower()
        has_proprietary = any(
            re.search(pattern, lower_content) for pattern in proprietary_indicators
        )

        if has_proprietary:
            # Try to extract just the proprietary part
            # Look for sentences or phrases containing proprietary indicators
            sentences = re.split(r"[.!]\s+", content)
            for sentence in sentences:
                sentence_lower = sentence.lower()
                if any(
                    re.search(pattern, sentence_lower)
                    for pattern in proprietary_indicators
                ):
                    # This sentence contains proprietary info
                    # Parse it to extract the license
                    parsed = self._parse_license_field(sentence)
                    for lic in parsed:
                        # Check if this looks like a proprietary license (not a standard one)
                        # and not already in licenses
                        if lic not in licenses and any(
                            re.search(pattern, lic.lower())
                            for pattern in proprietary_indicators
                        ):
                            licenses.append(lic)

        # If no licenses found yet, try to parse the entire content as a license field
        # This handles cases like proprietary licenses that don't match standard patterns
        if not licenses:
            parsed = self._parse_license_field(content)
            if parsed:
                licenses.extend(parsed)

        if licenses:
            return " | ".join(licenses)

        return "Unknown"

    def _parse_license_field(self, license_text):
        """Parse a license field that may contain multiple licenses.

        Handles formats like:
        - "MIT or Apache-2.0"
        - "GPL-2.0+"
        - "Apache-2.0 and BSD-3-Clause"
        - "NVIDIA Proprietary"
        - "Custom Corporate License"

        Args:
            license_text: License field text

        Returns:
            List of license identifiers
        """
        if not license_text:
            return []

        licenses = []
        import re

        # Check for corporate/proprietary licenses first
        # These often contain terms like "Proprietary", "Commercial", company names, etc.
        proprietary_indicators = [
            r"\bproprietary\b",
            r"\bcommercial\b",
            r"\bcorporate\b",
            r"\bnvidia\b",
            r"\bcustom\b",
            r"\binternal\b",
        ]

        lower_text = license_text.lower()
        is_proprietary = any(
            re.search(pattern, lower_text) for pattern in proprietary_indicators
        )

        # Try to detect standard licenses in the text
        detected = self.metadata_fetcher._detect_license_from_content(license_text)
        if detected:
            # detected may be multiple licenses joined by " | "
            licenses.extend(detected.split(" | "))

        if is_proprietary:
            # Extract the proprietary license name
            # Try to find the license name (often before a period or newline)
            clean_license = re.sub(r"\s+", " ", license_text.strip())

            # Look for common patterns:
            # "NVIDIA Proprietary License"
            # "Proprietary and confidential"
            # "Commercial License"

            # Try to extract just the license statement (first line or sentence)
            lines = clean_license.split("\n")
            first_line = lines[0].strip()

            # If first line is short and contains proprietary indicator, use it
            if len(first_line) < 100 and any(
                re.search(pattern, first_line.lower())
                for pattern in proprietary_indicators
            ):
                proprietary_license = first_line
            else:
                # Try to extract first sentence
                parts = clean_license.split(".", 1)
                if (
                    parts[0]
                    and len(parts[0]) < 100
                    and any(
                        re.search(pattern, parts[0].lower())
                        for pattern in proprietary_indicators
                    )
                ):
                    proprietary_license = parts[0].strip()
                else:
                    # Fall back to first 100 chars
                    proprietary_license = clean_license[:100].strip()
                    if len(clean_license) > 100:
                        proprietary_license += "..."

            # Add if not already in licenses
            if proprietary_license not in licenses:
                licenses.append(proprietary_license)

        elif not licenses:
            # No standard license detected and not obviously proprietary
            # Return the text as-is (but sanitized and truncated)
            clean_license = re.sub(r"\s+", " ", license_text.strip())

            # Try to extract just the license name
            # Check if it looks like a full license text (contains multiple sentences)
            if "." in clean_license and len(clean_license) > 200:
                # Likely full license text, try to extract name from first sentence
                first_sentence = clean_license.split(".")[0].strip()
                if len(first_sentence) < 100:
                    clean_license = first_sentence
                else:
                    clean_license = clean_license[:100].strip() + "..."
            elif len(clean_license) > 100:
                clean_license = clean_license[:100].strip() + "..."

            licenses.append(clean_license)

        return licenses

    def _enrich_unknown_licenses(self, packages, container):
        """Try to find licenses for packages with unknown licenses.

        Args:
            packages: List of package dictionaries
            container: Docker container object
        """
        self._log("Enriching unknown licenses...")
        enriched_count = 0

        for pkg in packages:
            if pkg.get("license") != "Unknown":
                continue

            license_found = None
            license_source = None

            # Try to get license from installed package files (for Python packages)
            if pkg["package_type"] == "python":
                result = self._get_python_package_license(container, pkg["name"])
                if result:
                    license_found, license_source = result

            # If still unknown, try source code URL
            if not license_found and pkg.get("source_code_url"):
                license_found = self.metadata_fetcher.get_license_from_source_url(
                    pkg["source_code_url"]
                )
                if license_found:
                    license_source = f"Source URL: {pkg['source_code_url']}"

            if license_found:
                pkg["license"] = license_found
                pkg["license_source"] = license_source or "Enriched"
                enriched_count += 1
                self._log(f"  Found license for {pkg['name']}: {license_found}")

        if enriched_count > 0:
            self._log(f"Enriched licenses for {enriched_count} packages")

    def _get_python_package_license(self, container, package_name):
        """Try to find license from Python package metadata files.

        Args:
            container: Docker container object
            package_name: Name of the Python package

        Returns:
            Tuple of (license_string, license_source) or None
        """
        try:
            # Try to find package metadata directory
            exit_code, output = container.exec_run(
                f"pip show -f {package_name}", demux=False
            )

            if exit_code != 0:
                return None

            output_str = output.decode("utf-8", errors="ignore")

            # First check if pip show already has license info
            for line in output_str.split("\n"):
                if line.startswith("License:"):
                    license_info = line.split("License:", 1)[1].strip()
                    if license_info and license_info not in ["", "UNKNOWN", "Unknown"]:
                        # Parse the license text to identify the actual license type
                        # Parse multiple licenses if present
                        parsed_licenses = self._parse_license_field(license_info)
                        if parsed_licenses:
                            license_str = " | ".join(parsed_licenses)
                            return license_str, "pip show metadata (parsed)"
                        # Otherwise fall through to read LICENSE files

            # Try to find and read LICENSE files from package
            lines = output_str.split("\n")
            in_files_section = False
            license_files = []

            for line in lines:
                if line.startswith("Files:"):
                    in_files_section = True
                    continue
                if in_files_section and line.strip():
                    # Look for LICENSE files
                    file_path = line.strip()
                    if any(
                        name in file_path.upper()
                        for name in ["LICENSE", "COPYING", "COPYRIGHT"]
                    ):
                        license_files.append(file_path)

            # Try to read LICENSE files
            if license_files:
                # Get Location (installation directory)
                location = None
                for line in lines:
                    if line.startswith("Location:"):
                        location = line.split("Location:", 1)[1].strip()
                        break

                if location:
                    for license_file in license_files[
                        :3
                    ]:  # Check first 3 license files
                        full_path = f"{location}/{license_file}"
                        exit_code, content_output = container.exec_run(
                            f"cat {full_path}", demux=False
                        )
                        if exit_code == 0:
                            content = content_output.decode("utf-8", errors="ignore")
                            # Use the same detection logic as for source URLs
                            license_detected = (
                                self.metadata_fetcher._detect_license_from_content(
                                    content[:5000]
                                )
                            )
                            if license_detected:
                                return license_detected, full_path

        except Exception as e:
            self._log(f"  Warning: Failed to get license for {package_name}: {e}")

        return None

    def _extract_rpm_packages(self, container):
        """Extract packages using rpm (RedHat/CentOS)."""
        try:
            exit_code, output = container.exec_run(
                "rpm -qa --queryformat '%{NAME}|%{VERSION}-%{RELEASE}|%{LICENSE}|%{URL}\n'",
                demux=False,
            )

            if exit_code != 0:
                return []

            packages = []
            lines = output.decode("utf-8").strip().split("\n")
            self._log(f"Found {len(lines)} rpm packages")

            for line in lines:
                if not line:
                    continue
                parts = line.split("|")
                if len(parts) >= 2:
                    packages.append(
                        {
                            "name": parts[0],
                            "version": parts[1],
                            "source": parts[3] if len(parts) > 3 else "",
                            "license": parts[2] if len(parts) > 2 else "Unknown",
                            "license_source": "RPM metadata",
                            "source_code_url": parts[3] if len(parts) > 3 else "",
                            "package_provider": "RPM",
                        }
                    )

            return packages
        except Exception as e:
            self._log(f"rpm extraction failed: {e}")
            return []

    def _extract_apk_packages(self, container):
        """Extract packages using apk (Alpine)."""
        try:
            exit_code, output = container.exec_run("apk info -v", demux=False)

            if exit_code != 0:
                return []

            packages = []
            lines = output.decode("utf-8").strip().split("\n")

            for line in lines:
                if not line:
                    continue
                # Skip warning messages
                if line.startswith("WARNING:") or "No such file" in line:
                    continue
                # Format is typically: package-version-release
                parts = line.rsplit("-", 2)
                if len(parts) >= 2:
                    name = parts[0]
                    version = "-".join(parts[1:])
                    packages.append(
                        {
                            "name": name,
                            "version": version,
                            "source": f"https://pkgs.alpinelinux.org/package/edge/main/x86_64/{name}",
                            "license": "Unknown",
                            "license_source": "Not available",
                            "source_code_url": "",
                            "package_provider": "APK",
                        }
                    )

            self._log(f"Found {len(packages)} apk packages")
            return packages
        except Exception as e:
            self._log(f"apk extraction failed: {e}")
            return []
