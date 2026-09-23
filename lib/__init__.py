"""whosaid — local, speaker-attributed transcription for Apple Silicon."""

# Single source of the package version: pyproject.toml (hatch dynamic version)
# and lib/mcp_server.py both read this; the bash launcher's WHOSAID_VERSION is
# the only other copy, and test/version_test.sh fails if they disagree.
__version__ = "1.6.0"
