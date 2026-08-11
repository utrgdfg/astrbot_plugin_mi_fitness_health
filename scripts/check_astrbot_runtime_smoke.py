"""Smoke-test plugin import and privacy isolation against a real AstrBot tree."""

from __future__ import annotations

import importlib
from types import SimpleNamespace


def main() -> int:
    """Import the plugin, install the guard, and verify one fallback scrub."""
    plugin = importlib.import_module("astrbot_plugin_mi_fitness_health.main")
    if plugin.MiFitnessHealthPlugin.__name__ != "MiFitnessHealthPlugin":
        raise AssertionError("plugin entrypoint import returned an unexpected class")

    guard = importlib.import_module(
        "astrbot_plugin_mi_fitness_health.compat.runner_privacy_guard"
    )
    if not guard.install_private_context_guard():
        raise AssertionError("real AstrBot runner privacy guard could not be installed")

    canary = (
        '<private_life_context source="mi_fitness">\n'
        "SYNTHETIC-PRIVATE-CANARY\n"
        "</private_life_context>"
    )
    request = SimpleNamespace(
        extra_user_content_parts=[SimpleNamespace(text=canary)],
        func_tool=None,
    )
    guard.isolate_private_context_request(request)
    raw_tool_set = request.func_tool
    if not raw_tool_set or not raw_tool_set.empty() or raw_tool_set.tools:
        raise AssertionError("isolated request exposed an ordinary tool schema")

    runner = SimpleNamespace(
        req=request,
        run_context=SimpleNamespace(messages=[{"role": "user", "content": canary}]),
        _skill_like_raw_tool_set=raw_tool_set,
    )
    if not guard._is_private_no_tool_request(runner):
        raise AssertionError("isolated request marker was not recognized")
    guard._strip_private_context_before_fallback(runner)

    rendered = repr(request.extra_user_content_parts) + repr(
        runner.run_context.messages
    )
    if "SYNTHETIC-PRIVATE-CANARY" in rendered:
        raise AssertionError("fallback scrub retained private context")
    if request.func_tool is not None:
        raise AssertionError("fallback scrub retained the isolated tool set")

    print("AstrBot runtime smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
