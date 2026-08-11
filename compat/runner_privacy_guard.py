"""Version-checked isolation for private context in AstrBot's local runner.

This module is the only place allowed to depend on ToolLoopAgentRunner internals.
The public feature layer only asks it to install the guard and isolate one request.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from astrbot.api import logger
from astrbot.core.agent.tool import ToolSet

_REQUEST_GUARD_MARKER = "_mi_fitness_reject_unadvertised_tool_calls"
_RUNNER_GUARD_ORIGINAL = "_mi_fitness_unadvertised_tool_guard_original_v1"
_RUNNER_GUARD_WRAPPER = "_mi_fitness_unadvertised_tool_guard_wrapper_v1"
_RUNNER_GUARD_TOKEN = "_mi_fitness_unadvertised_tool_guard_token_v1"
_RUNNER_TOOL_GUARD_ORIGINAL = "_mi_fitness_provider_tool_guard_original_v1"
_RUNNER_TOOL_GUARD_WRAPPER = "_mi_fitness_provider_tool_guard_wrapper_v1"
_RUNNER_TOOL_GUARD_TOKEN = "_mi_fitness_provider_tool_guard_token_v1"
_RUNNER_FORCE_NONE_MARKER = "_mi_fitness_force_none_tools_for_provider"
_RUNNER_PRIMARY_PROVIDER_MARKER = "_mi_fitness_private_context_primary_provider"
_GUARD_IMPLEMENTATION_TOKEN = object()

_PRIVATE_CONTEXT_OPEN_TAG = '<private_life_context source="mi_fitness">'
_PRIVATE_CONTEXT_CLOSE_TAG = "</private_life_context>"


class _RunnerSafeEmptyToolSet(ToolSet):
    """Remain truthy only long enough for AstrBot skills-like runner reset."""

    _mi_fitness_reject_unadvertised_tool_calls = True

    def __bool__(self) -> bool:
        return True

    def get_light_tool_set(self) -> ToolSet:
        """Keep the request marker while exposing no skills-like schemas."""
        return type(self)()

    def get_param_only_tool_set(self) -> ToolSet:
        """Keep parser normalization enabled without advertising parameters."""
        return type(self)()


def _is_private_no_tool_request(runner: object) -> bool:
    """Recognize only requests isolated by this plugin, including skills-like mode."""
    request = getattr(runner, "req", None)
    if bool(getattr(request, _REQUEST_GUARD_MARKER, False)):
        return True
    candidates = (
        getattr(request, "func_tool", None),
        getattr(runner, "_skill_like_raw_tool_set", None),
    )
    return any(
        bool(getattr(candidate, "_mi_fitness_reject_unadvertised_tool_calls", False))
        for candidate in candidates
    )


def _without_private_context(text: str) -> str:
    """Remove every plugin-owned private context block while preserving user text."""
    cleaned = text
    while True:
        start = cleaned.find(_PRIVATE_CONTEXT_OPEN_TAG)
        if start < 0:
            return cleaned
        end = cleaned.find(_PRIVATE_CONTEXT_CLOSE_TAG, start)
        if end < 0:
            return cleaned[:start].rstrip()
        cleaned = (
            cleaned[:start] + cleaned[end + len(_PRIVATE_CONTEXT_CLOSE_TAG) :]
        ).strip()


def _strip_private_context_before_fallback(runner: object) -> None:
    """Remove private records before allowing AstrBot to use a fallback provider."""

    def scrub_parts(parts: list[object]) -> list[object]:
        cleaned_parts: list[object] = []
        for part in parts:
            if isinstance(part, dict):
                value = part.get("text")
                if not isinstance(value, str):
                    cleaned_parts.append(part)
                    continue
                cleaned = _without_private_context(value)
                if cleaned:
                    copy = dict(part)
                    copy["text"] = cleaned
                    cleaned_parts.append(copy)
                continue
            value = getattr(part, "text", None)
            if not isinstance(value, str):
                cleaned_parts.append(part)
                continue
            cleaned = _without_private_context(value)
            if cleaned:
                part.text = cleaned
                cleaned_parts.append(part)
        return cleaned_parts

    request = getattr(runner, "req", None)
    extra_parts = getattr(request, "extra_user_content_parts", None)
    if isinstance(extra_parts, list):
        request.extra_user_content_parts = scrub_parts(extra_parts)

    run_context = getattr(runner, "run_context", None)
    messages = getattr(run_context, "messages", None)
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    message["content"] = _without_private_context(content)
                elif isinstance(content, list):
                    message["content"] = scrub_parts(content)
                continue
            content = getattr(message, "content", None)
            if isinstance(content, str):
                message.content = _without_private_context(content)
            elif isinstance(content, list):
                message.content = scrub_parts(content)

    if request is not None:
        try:
            delattr(request, _REQUEST_GUARD_MARKER)
        except AttributeError:
            pass
        request.func_tool = None
    if bool(
        getattr(
            getattr(runner, "_skill_like_raw_tool_set", None),
            "_mi_fitness_reject_unadvertised_tool_calls",
            False,
        )
    ):
        runner._skill_like_raw_tool_set = None


def _strip_unadvertised_tool_call(response: object) -> None:
    """Turn an impossible no-schema tool call into one safe terminal text response."""
    if not getattr(response, "tools_call_name", None):
        return
    response.tools_call_name = []
    response.tools_call_args = []
    response.tools_call_ids = []
    for attribute in ("tools_call_extra_content", "tool_calls_extra_content"):
        if hasattr(response, attribute):
            setattr(response, attribute, {})
    if hasattr(response, "reasoning_signature"):
        response.reasoning_signature = None
    if hasattr(response, "raw_completion"):
        response.raw_completion = None
    response.role = "assistant"
    has_text = bool(str(getattr(response, "completion_text", "") or "").strip())
    result_chain = getattr(response, "result_chain", None)
    has_result_chain = bool(getattr(result_chain, "chain", None))
    if not getattr(response, "is_chunk", False) and not (has_text or has_result_chain):
        response.completion_text = "这次回复没有正常完成，请再说一次。"
        if hasattr(response, "reasoning_content"):
            response.reasoning_content = None
    logger.warning(
        "Mi Fitness blocked an unadvertised tool call from a no-tool "
        "private-context request"
    )


def install_private_context_guard() -> bool:
    """Install a narrow runner guard or fail closed on incompatible AstrBot builds."""
    try:
        from astrbot.core.agent.runners.tool_loop_agent_runner import (
            ToolLoopAgentRunner,
        )
    except (ImportError, AttributeError):
        return False

    try:
        runner_source = inspect.getsourcefile(ToolLoopAgentRunner)
        if not runner_source:
            return False
        runner_source_path = Path(runner_source).resolve()
    except Exception:
        return False

    def validated_original(
        method_name: str,
        original_name: str,
        wrapper_name: str,
    ) -> tuple[object | None, bool]:
        current_method = getattr(ToolLoopAgentRunner, method_name, None)
        installed_method = getattr(ToolLoopAgentRunner, wrapper_name, None)
        original_method = getattr(ToolLoopAgentRunner, original_name, None)
        if not callable(current_method):
            return None, False
        if installed_method is None and original_method is None:
            try:
                method_source_path = Path(current_method.__code__.co_filename).resolve()
                is_astrbot_method = (
                    getattr(current_method, "__module__", None)
                    == "astrbot.core.agent.runners.tool_loop_agent_runner"
                    and getattr(current_method, "__name__", None) == method_name
                    and not hasattr(current_method, "__wrapped__")
                    and method_source_path == runner_source_path
                )
            except Exception:
                return None, False
            return (current_method, True) if is_astrbot_method else (None, False)
        if installed_method is current_method and callable(original_method):
            return original_method, True
        if current_method is original_method and callable(original_method):
            return original_method, True
        return None, False

    response_original, response_valid = validated_original(
        "_iter_llm_responses",
        _RUNNER_GUARD_ORIGINAL,
        _RUNNER_GUARD_WRAPPER,
    )
    tool_original, tool_valid = validated_original(
        "_func_tool_for_provider",
        _RUNNER_TOOL_GUARD_ORIGINAL,
        _RUNNER_TOOL_GUARD_WRAPPER,
    )
    response_current = getattr(ToolLoopAgentRunner, "_iter_llm_responses", None)
    tool_current = getattr(ToolLoopAgentRunner, "_func_tool_for_provider", None)
    response_installed = getattr(ToolLoopAgentRunner, _RUNNER_GUARD_WRAPPER, None)
    tool_installed = getattr(ToolLoopAgentRunner, _RUNNER_TOOL_GUARD_WRAPPER, None)
    response_token = getattr(ToolLoopAgentRunner, _RUNNER_GUARD_TOKEN, None)
    tool_token = getattr(ToolLoopAgentRunner, _RUNNER_TOOL_GUARD_TOKEN, None)
    if (
        response_installed is response_current
        and tool_installed is tool_current
        and response_token is _GUARD_IMPLEMENTATION_TOKEN
        and tool_token is _GUARD_IMPLEMENTATION_TOKEN
    ):
        return True
    if not response_valid or not tool_valid:
        logger.warning(
            "Mi Fitness detected another runner patch after its privacy guard; "
            "skipping private health context for this turn"
        )
        return False

    async def guarded_responses(runner, *args, **kwargs):
        private_no_tool_request = _is_private_no_tool_request(runner)
        request = getattr(runner, "req", None)
        if private_no_tool_request:
            current_provider = getattr(runner, "provider", None)
            primary_provider = getattr(runner, _RUNNER_PRIMARY_PROVIDER_MARKER, None)
            if primary_provider is None:
                if current_provider is None:
                    raise RuntimeError(
                        "Mi Fitness could not bind private context to one provider"
                    )
                setattr(
                    runner,
                    _RUNNER_PRIMARY_PROVIDER_MARKER,
                    current_provider,
                )
            elif current_provider is not primary_provider:
                logger.warning(
                    "Mi Fitness removed private health context before switching to a "
                    "fallback provider"
                )
                _strip_private_context_before_fallback(runner)
                private_no_tool_request = False
        provider_config = getattr(
            getattr(runner, "provider", None), "provider_config", None
        )
        hide_tools_from_gemini = (
            private_no_tool_request
            and request is not None
            and isinstance(provider_config, dict)
            and provider_config.get("type") == "googlegenai_chat_completion"
        )
        responses = response_original(runner, *args, **kwargs)
        while True:
            if hide_tools_from_gemini:
                previous_force_none = getattr(runner, _RUNNER_FORCE_NONE_MARKER, None)
                setattr(runner, _RUNNER_FORCE_NONE_MARKER, True)
            try:
                response = await anext(responses)
            except StopAsyncIteration:
                return
            finally:
                if hide_tools_from_gemini:
                    if previous_force_none is None:
                        try:
                            delattr(runner, _RUNNER_FORCE_NONE_MARKER)
                        except AttributeError:
                            pass
                    else:
                        setattr(runner, _RUNNER_FORCE_NONE_MARKER, previous_force_none)
            if private_no_tool_request:
                _strip_unadvertised_tool_call(response)
            yield response

    def guarded_provider_tools(runner, *args, **kwargs):
        if _is_private_no_tool_request(runner):
            if bool(getattr(runner, _RUNNER_FORCE_NONE_MARKER, False)):
                return None
            # Never trust req.func_tool here: a later hook or the runner's
            # max-step handling may replace it with real tools or None.
            return _RunnerSafeEmptyToolSet()
        return tool_original(runner, *args, **kwargs)

    previous_values = {
        name: getattr(ToolLoopAgentRunner, name, None)
        for name in (
            "_iter_llm_responses",
            "_func_tool_for_provider",
            _RUNNER_GUARD_ORIGINAL,
            _RUNNER_GUARD_WRAPPER,
            _RUNNER_GUARD_TOKEN,
            _RUNNER_TOOL_GUARD_ORIGINAL,
            _RUNNER_TOOL_GUARD_WRAPPER,
            _RUNNER_TOOL_GUARD_TOKEN,
        )
    }
    missing_names = {
        name for name in previous_values if not hasattr(ToolLoopAgentRunner, name)
    }
    try:
        setattr(ToolLoopAgentRunner, _RUNNER_GUARD_ORIGINAL, response_original)
        setattr(ToolLoopAgentRunner, _RUNNER_TOOL_GUARD_ORIGINAL, tool_original)
        setattr(ToolLoopAgentRunner, "_iter_llm_responses", guarded_responses)
        setattr(ToolLoopAgentRunner, "_func_tool_for_provider", guarded_provider_tools)
        setattr(ToolLoopAgentRunner, _RUNNER_GUARD_WRAPPER, guarded_responses)
        setattr(ToolLoopAgentRunner, _RUNNER_TOOL_GUARD_WRAPPER, guarded_provider_tools)
        setattr(ToolLoopAgentRunner, _RUNNER_GUARD_TOKEN, _GUARD_IMPLEMENTATION_TOKEN)
        setattr(
            ToolLoopAgentRunner,
            _RUNNER_TOOL_GUARD_TOKEN,
            _GUARD_IMPLEMENTATION_TOKEN,
        )
    except Exception:
        for name, value in previous_values.items():
            if name in missing_names:
                try:
                    delattr(ToolLoopAgentRunner, name)
                except AttributeError:
                    pass
            else:
                setattr(ToolLoopAgentRunner, name, value)
        return False
    return True


def isolate_private_context_request(request: object) -> None:
    """Mark one request and expose no ordinary tools to its reply provider."""
    setattr(request, _REQUEST_GUARD_MARKER, True)
    request.func_tool = _RunnerSafeEmptyToolSet()
