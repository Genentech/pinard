"""Pytest configuration for the memory service test suite."""
import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live: smoke tests that require a live SurrealDB instance "
        "(SURREAL_URL, SURREAL_USER, SURREAL_PASS env vars). "
        "Run with: pytest -m live",
    )
