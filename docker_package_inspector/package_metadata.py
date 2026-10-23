"""Package metadata fetching from PyPI and other sources."""

import re
import sys
import time
from typing import Dict, Optional

import requests

from . import __version__
from .rust_crates import crate_download_url, parse_crate_archive

# crates.io asks API clients to identify themselves and stay at or below one
# request per second; static.crates.io downloads are CDN-served and exempt
USER_AGENT = (
    f"docker-package-inspector/{__version__} "
    "(+https://github.com/mc-nv/docker-package-inspector)"
)
CRATES_IO_API_INTERVAL = 1.0
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024


class PackageMetadataFetcher:
    """Fetches package metadata from various sources."""

    def __init__(self, timeout=10):
        """Initialize the metadata fetcher.

        Args:
            timeout: HTTP request timeout in seconds
        """
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.cache = {}
        self._last_crates_io_call = 0.0

    def fetch_bytes(self, url: str, max_bytes: int = MAX_DOWNLOAD_BYTES):
        """Download a URL into memory, refusing bodies larger than max_bytes.

        Returns:
            The body as bytes, or None on any error or oversize body
        """
        try:
            with self.session.get(url, timeout=self.timeout, stream=True) as response:
                if response.status_code != 200:
                    return None
                chunks = []
                size = 0
                for chunk in response.iter_content(chunk_size=65536):
                    size += len(chunk)
                    if size > max_bytes:
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)
        except Exception:
            return None

    def get_maven_pom(self, url: str) -> str:
        """Text of a POM from Maven Central (cached), or '' if unavailable."""
        cache_key = f"pom:{url}"
        if cache_key not in self.cache:
            body = self.fetch_bytes(url, max_bytes=4 * 1024 * 1024)
            self.cache[cache_key] = body.decode("utf-8", "replace") if body else ""
        return self.cache[cache_key]

    def get_pypi_sdist_url(self, package_name: str, version: str) -> Optional[str]:
        """URL of the source distribution of an exact PyPI release, if any."""
        cache_key = f"sdist:{package_name}:{version}"
        if cache_key in self.cache:
            return self.cache[cache_key]
        url = None
        try:
            response = self.session.get(
                f"https://pypi.org/pypi/{package_name}/{version}/json",
                timeout=self.timeout,
            )
            if response.status_code == 200:
                for entry in response.json().get("urls", []):
                    if entry.get("packagetype") == "sdist":
                        url = entry.get("url")
                        break
        except Exception:
            pass
        self.cache[cache_key] = url
        return url

    def get_crate_metadata(self, name: str, version: str, checksum: str = ""):
        """License, repository and copyright holders of a crates.io crate.

        The published .crate is read in memory and, when a Cargo.lock checksum
        is known, verified against it. The crates.io API is consulted only
        when the crate's Cargo.toml declares no SPDX license expression.

        Returns:
            Dict with license, license_source, repository, copyright
        """
        cache_key = f"crate:{name}:{version}"
        if cache_key in self.cache:
            return self.cache[cache_key]

        metadata = {
            "license": "Unknown",
            "license_source": "Not found",
            "repository": "",
            "copyright": [],
        }
        archive = self.fetch_bytes(crate_download_url(name, version))
        try:
            parsed = parse_crate_archive(archive, checksum) if archive else None
        except Exception:
            parsed = None
        if archive and parsed is None:
            metadata["license_source"] = "crate checksum mismatch"
        if parsed:
            metadata["repository"] = parsed["repository"]
            metadata["copyright"] = parsed["copyright"]
            if parsed["license"]:
                metadata["license"] = parsed["license"]
                metadata["license_source"] = "crate Cargo.toml"
            else:
                texts = parsed["license_texts"]
                for file_name in sorted(texts):
                    detected = self._detect_license_from_content(texts[file_name])
                    if detected:
                        metadata["license"] = detected
                        metadata["license_source"] = f"crate {file_name}"
                        break

        if metadata["license"] == "Unknown":
            api_license = self._get_crates_io_api_license(name, version)
            if api_license:
                metadata["license"] = api_license
                metadata["license_source"] = "crates.io API"

        self.cache[cache_key] = metadata
        return metadata

    def _get_crates_io_api_license(self, name: str, version: str) -> Optional[str]:
        """License of a crate version from the crates.io API (rate limited)."""
        wait = CRATES_IO_API_INTERVAL - (time.monotonic() - self._last_crates_io_call)
        if wait > 0:
            time.sleep(wait)
        self._last_crates_io_call = time.monotonic()
        try:
            response = self.session.get(
                f"https://crates.io/api/v1/crates/{name}/{version}",
                timeout=self.timeout,
            )
            if response.status_code == 200:
                return response.json().get("version", {}).get("license") or None
        except Exception:
            pass
        return None

    def get_pypi_metadata(
        self, package_name: str, version: Optional[str] = None
    ) -> Dict:
        """Fetch metadata for a Python package from PyPI.

        Args:
            package_name: Name of the package
            version: Optional specific version

        Returns:
            Dictionary with package metadata
        """
        cache_key = f"{package_name}:{version}"
        if cache_key in self.cache:
            return self.cache[cache_key]

        metadata = {
            "source": f"https://pypi.org/project/{package_name}/",
            "license": "Unknown",
            "license_source": "PyPI API",
            "source_code_url": "",
        }

        try:
            # Query PyPI JSON API
            if version:
                url = f"https://pypi.org/pypi/{package_name}/{version}/json"
            else:
                url = f"https://pypi.org/pypi/{package_name}/json"

            response = self.session.get(url, timeout=self.timeout)

            if response.status_code == 200:
                data = response.json()
                info = data.get("info", {})

                # Extract license
                license_info = info.get("license")
                licenses = []

                if license_info:
                    # Parse the license text to identify the actual license type(s)
                    detected_license = self._detect_license_from_content(license_info)
                    if detected_license:
                        # detected_license may already contain multiple licenses joined by " | "
                        licenses.extend(detected_license.split(" | "))
                        metadata["license_source"] = "PyPI API (license field, parsed)"
                    else:
                        # If detection fails, parse the license field for multiple licenses
                        parsed_licenses = self._parse_license_field_from_pypi(
                            license_info
                        )
                        if parsed_licenses:
                            licenses.extend(parsed_licenses)
                            metadata["license_source"] = "PyPI API (license field)"
                        else:
                            # Use the raw license text
                            licenses.append(license_info)
                            metadata["license_source"] = "PyPI API (license field)"

                # Also check classifiers for additional licenses
                classifiers = info.get("classifiers", [])
                for classifier in classifiers:
                    if classifier.startswith("License ::"):
                        classifier_license = classifier.split("::")[-1].strip()
                        if classifier_license not in licenses:
                            licenses.append(classifier_license)
                            if not license_info:
                                metadata["license_source"] = "PyPI API (classifier)"

                if licenses:
                    metadata["license"] = " | ".join(licenses)
                else:
                    metadata["license"] = "Unknown"

                # Extract source code URL
                project_urls = info.get("project_urls", {})

                # Try common source code URL keys
                for key in [
                    "Source",
                    "Source Code",
                    "Repository",
                    "Homepage",
                    "GitHub",
                ]:
                    if key in project_urls:
                        metadata["source_code_url"] = project_urls[key]
                        break

                # Fallback to home_page or package_url
                if not metadata["source_code_url"]:
                    metadata["source_code_url"] = info.get("home_page") or info.get(
                        "package_url", ""
                    )

        except Exception as e:
            # Silently fail and return default metadata
            pass

        self.cache[cache_key] = metadata
        return metadata

    def get_license_from_source_url(self, source_url: str) -> Optional[str]:
        """Fetch license information from source code repository URL.

        Args:
            source_url: URL to source code repository (GitHub, GitLab, etc.)

        Returns:
            License name or None if not found
        """
        if not source_url or source_url == "Unknown":
            return None

        cache_key = f"license:{source_url}"
        if cache_key in self.cache:
            return self.cache[cache_key]

        license_info = None

        try:
            # GitHub API
            if "github.com" in source_url:
                license_info = self._get_github_license(source_url)

            # GitLab API
            elif "gitlab" in source_url:
                license_info = self._get_gitlab_license(source_url)

            # Fallback: Try to fetch LICENSE file directly
            if not license_info:
                license_info = self._fetch_license_file(source_url)

        except Exception:
            pass

        self.cache[cache_key] = license_info
        return license_info

    def _get_github_license(self, github_url: str) -> Optional[str]:
        """Get license from GitHub repository using API.

        Args:
            github_url: GitHub repository URL

        Returns:
            License name or None
        """
        try:
            # Extract owner/repo from URL
            # Format: https://github.com/owner/repo or https://github.com/owner/repo/...
            match = re.search(r"github\.com/([^/]+)/([^/]+)", github_url)
            if not match:
                return None

            owner, repo = match.groups()
            # Remove .git suffix if present
            repo = repo.replace(".git", "")

            # Use GitHub API to get license
            api_url = f"https://api.github.com/repos/{owner}/{repo}/license"
            response = self.session.get(
                api_url,
                timeout=self.timeout,
                headers={"Accept": "application/vnd.github.v3+json"},
            )

            if response.status_code == 200:
                data = response.json()
                license_obj = data.get("license", {})
                license_name = license_obj.get("spdx_id") or license_obj.get("name")
                if license_name and license_name != "NOASSERTION":
                    return license_name

        except Exception:
            pass

        return None

    def _get_gitlab_license(self, gitlab_url: str) -> Optional[str]:
        """Get license from GitLab repository.

        Args:
            gitlab_url: GitLab repository URL

        Returns:
            License name or None
        """
        try:
            # Try to extract project path and construct API URL
            # Format: https://gitlab.com/owner/repo or https://gitlab.example.com/owner/repo
            match = re.search(r"(gitlab[^/]*)/(.+?)(?:\.git)?(?:/|$)", gitlab_url)
            if not match:
                return None

            gitlab_host = match.group(1)
            project_path = match.group(2).rstrip("/")

            # URL encode the project path
            import urllib.parse

            encoded_path = urllib.parse.quote(project_path, safe="")

            # Try GitLab API
            api_url = f"https://{gitlab_host}/api/v4/projects/{encoded_path}"
            response = self.session.get(api_url, timeout=self.timeout)

            if response.status_code == 200:
                data = response.json()
                # GitLab doesn't have a direct license field, try to get LICENSE file
                license_url = data.get("license_url")
                if license_url:
                    return self._fetch_license_file(license_url)

        except Exception:
            pass

        return None

    def _fetch_license_file(self, base_url: str) -> Optional[str]:
        """Try to fetch and parse LICENSE file from repository.

        Args:
            base_url: Base URL of repository

        Returns:
            Detected license name or None
        """
        try:
            # Common license file names
            license_files = [
                "LICENSE",
                "LICENSE.txt",
                "LICENSE.md",
                "COPYING",
                "COPYING.txt",
            ]

            # Try raw content URLs for GitHub
            if "github.com" in base_url:
                match = re.search(r"github\.com/([^/]+)/([^/]+)", base_url)
                if match:
                    owner, repo = match.groups()
                    repo = repo.replace(".git", "")

                    for license_file in license_files:
                        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/main/{license_file}"
                        content = self._try_fetch_url(raw_url)
                        if content:
                            return self._detect_license_from_content(content)

                        # Try master branch
                        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/master/{license_file}"
                        content = self._try_fetch_url(raw_url)
                        if content:
                            return self._detect_license_from_content(content)

        except Exception:
            pass

        return None

    def _try_fetch_url(self, url: str) -> Optional[str]:
        """Try to fetch content from URL.

        Args:
            url: URL to fetch

        Returns:
            Content as string or None
        """
        try:
            response = self.session.get(url, timeout=self.timeout)
            if response.status_code == 200:
                return response.text[:5000]  # Limit to first 5KB
        except Exception:
            pass
        return None

    def _parse_license_field_from_pypi(self, license_text: str) -> list:
        """Parse a license field from PyPI that may contain multiple licenses.

        Handles formats like:
        - "MIT or Apache-2.0"
        - "GPL-2.0+"
        - "Apache-2.0 and BSD-3-Clause"
        - "NVIDIA Proprietary"

        Args:
            license_text: License field text from PyPI

        Returns:
            List of license identifiers
        """
        if not license_text:
            return []

        licenses = []

        # Check for corporate/proprietary licenses
        proprietary_indicators = [
            r"\bproprietary\b",
            r"\bcommercial\b",
            r"\bcorporate\b",
            r"\bcustom\b",
        ]

        lower_text = license_text.lower()
        is_proprietary = any(
            re.search(pattern, lower_text) for pattern in proprietary_indicators
        )

        if is_proprietary:
            # Keep the original text as the license name
            clean_license = re.sub(r"\s+", " ", license_text.strip())
            if len(clean_license) > 100:
                clean_license = clean_license[:100].strip() + "..."
            licenses.append(clean_license)
            return licenses

        # Try to split on common separators
        # Split on " or ", " and ", " / " while preserving the parts
        parts = re.split(r"\s+(?:or|and|/)\s+", license_text, flags=re.IGNORECASE)

        for part in parts:
            part = part.strip()
            if part:
                # Try to detect standard license from this part
                detected = self._detect_license_from_content(part)
                if detected:
                    for lic in detected.split(" | "):
                        if lic not in licenses:
                            licenses.append(lic)
                else:
                    # Keep the original part if not detected
                    if part not in licenses:
                        licenses.append(part)

        return licenses

    def _detect_license_from_content(self, content: str) -> Optional[str]:
        """Detect license type from license file content.

        Args:
            content: License file content

        Returns:
            License identifier or list of identifiers separated by " | ", or None
        """
        if not content:
            return None

        content_lower = content.lower()

        # License detection patterns (ordered by specificity)
        # Note: Patterns are checked in order, but all matches are collected
        license_patterns = [
            # Apache patterns
            (r"apache license\s*,?\s*version 2\.0", "Apache-2.0"),
            (r"apache license version 2", "Apache-2.0"),
            (r"apache-2\.0", "Apache-2.0"),
            (r"\bapache\s+2\.0\b", "Apache-2.0"),
            (r"\bapache2\b", "Apache-2.0"),
            # GPL patterns
            (r"gnu general public license.*version 3", "GPL-3.0"),
            (r"gnu general public license.*version 2", "GPL-2.0"),
            (r"gpl-3\.0", "GPL-3.0"),
            (r"gpl-2\.0", "GPL-2.0"),
            (r"\bgpl\s+3\b", "GPL-3.0"),
            (r"\bgpl\s+2\b", "GPL-2.0"),
            (r"\bgplv3\b", "GPL-3.0"),
            (r"\bgplv2\b", "GPL-2.0"),
            # LGPL patterns
            (r"gnu lesser general public license.*version 3", "LGPL-3.0"),
            (r"gnu lesser general public license.*version 2", "LGPL-2.0"),
            (r"lgpl-3\.0", "LGPL-3.0"),
            (r"lgpl-2\.0", "LGPL-2.0"),
            (r"\blgpl\s+3\b", "LGPL-3.0"),
            (r"\blgpl\s+2\b", "LGPL-2.0"),
            # MIT patterns
            (r"mit license", "MIT"),
            (r"permission is hereby granted, free of charge", "MIT"),
            (r"\bmit\b(?!\s*(?:license|$))", "MIT"),  # Match standalone "MIT"
            # BSD-3-Clause patterns (ordered by specificity)
            (r"bsd[- ]3[- ]clause", "BSD-3-Clause"),
            (r"3[- ]clause bsd", "BSD-3-Clause"),
            # Key phrase that appears in BSD-3-Clause but not BSD-2-Clause
            (r"neither the name of.*nor the names of its contributors", "BSD-3-Clause"),
            # BSD-3-Clause with numbered clauses
            (
                r"redistribution and use in source and binary forms.*?\n.*?1\..*?\n.*?2\..*?\n.*?3\.",
                "BSD-3-Clause",
            ),
            # General BSD pattern with "redistribution and use" - check for 3-clause indicator
            (
                r"redistribution and use in source and binary forms.*neither the name",
                "BSD-3-Clause",
            ),
            # BSD-2-Clause patterns
            (r"bsd[- ]2[- ]clause", "BSD-2-Clause"),
            (r"2[- ]clause bsd", "BSD-2-Clause"),
            # Generic BSD (if no specific clause count found)
            (r"redistribution and use in source and binary forms", "BSD"),
            (r"\bbsd\b(?!\s*(?:license|$))", "BSD"),  # Match standalone "BSD"
            # Mozilla patterns
            (r"mozilla public license.*version 2\.0", "MPL-2.0"),
            (r"mpl-2\.0", "MPL-2.0"),
            (r"\bmpl\s+2\b", "MPL-2.0"),
            # ISC patterns
            (r"isc license", "ISC"),
            (r"\bisc\b(?!\s*(?:license|$))", "ISC"),  # Match standalone "ISC"
        ]

        detected_licenses = []
        for pattern, license_id in license_patterns:
            if re.search(pattern, content_lower):
                if license_id not in detected_licenses:
                    detected_licenses.append(license_id)

        if detected_licenses:
            return " | ".join(detected_licenses)

        return None
