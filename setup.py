"""Setup configuration for docker-package-inspector."""

from setuptools import find_packages, setup

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="docker-package-inspector",
    version="0.3.1",
    author="Your Name",
    author_email="your.email@example.com",
    description="A CLI tool to inspect Docker images and extract package information",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/yourusername/docker-package-inspector",
    packages=find_packages(),
    classifiers=[
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires=">=3.8",
    install_requires=[
        "docker>=6.0.0",
        "requests>=2.28.0",
    ],
    entry_points={
        "console_scripts": [
            "docker-package-inspector=docker_package_inspector.cli:main",
        ],
    },
)
