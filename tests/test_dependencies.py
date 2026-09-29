"""Guards on the resolved dependency set (security-motivated, see CHANGELOG 0.2.0)."""

from __future__ import annotations

import importlib.util
from importlib.metadata import version


def test_fastmcp_is_version_4_or_newer():
    assert int(version("fastmcp").split(".")[0]) >= 4


def test_diskcache_is_not_installed():
    # diskcache has an unfixed pickle-deserialization advisory. It used to come in
    # via fastmcp[disk] -> py-key-value-aio; FastMCP 4 no longer pulls it in.
    assert importlib.util.find_spec("diskcache") is None
