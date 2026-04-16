from agently.builtins.hookers._runtime_log_profiles import (
    should_render_console_event,
    should_render_storage_event,
)
from agently.types.data import RuntimeEvent
from agently.utils import Settings


def _build_settings(profile: str = "off") -> Settings:
    return Settings(
        {
            "runtime": {
                "show_model_logs": profile,
                "show_tool_logs": profile,
                "show_trigger_flow_logs": profile,
                "show_runtime_logs": profile,
            }
        }
    )


def test_runtime_log_profiles_keep_default_off_quiet():
    settings = _build_settings("off")

    assert not should_render_console_event(RuntimeEvent(event_type="model.requesting"), settings)
    assert not should_render_console_event(RuntimeEvent(event_type="tool.loop_started"), settings)
    assert not should_render_console_event(RuntimeEvent(event_type="triggerflow.execution_started"), settings)

    assert not should_render_storage_event(RuntimeEvent(event_type="model.requesting", level="INFO"), settings)
    assert not should_render_storage_event(RuntimeEvent(event_type="request.completed", level="INFO"), settings)
    assert should_render_storage_event(RuntimeEvent(event_type="runtime.print", level="INFO", message="hello"), settings)
    assert should_render_storage_event(RuntimeEvent(event_type="model.requester.error", level="ERROR"), settings)
    assert should_render_storage_event(RuntimeEvent(event_type="request.failed", level="WARNING"), settings)


def test_runtime_log_profiles_simple_mode_uses_summary_whitelists():
    settings = _build_settings("simple")

    assert should_render_console_event(RuntimeEvent(event_type="model.requesting", message="requesting"), settings)
    assert not should_render_console_event(RuntimeEvent(event_type="model.streaming", message="delta"), settings)
    assert should_render_console_event(RuntimeEvent(event_type="tool.loop_started", message="started"), settings)
    assert not should_render_console_event(RuntimeEvent(event_type="tool.plan_ready", message="ready"), settings)
    assert should_render_console_event(
        RuntimeEvent(event_type="triggerflow.execution_started", message="execution started"),
        settings,
    )
    assert not should_render_console_event(RuntimeEvent(event_type="triggerflow.signal", message="signal"), settings)

    assert not should_render_storage_event(RuntimeEvent(event_type="model.requesting", level="INFO"), settings)
    assert not should_render_storage_event(RuntimeEvent(event_type="request.completed", level="INFO"), settings)
    assert should_render_storage_event(RuntimeEvent(event_type="request.failed", level="ERROR"), settings)


def test_runtime_log_profiles_detail_mode_allows_full_runtime_detail():
    settings = _build_settings("detail")

    assert should_render_console_event(RuntimeEvent(event_type="model.streaming", message="delta"), settings)
    assert should_render_console_event(RuntimeEvent(event_type="tool.plan_ready", message="ready"), settings)
    assert should_render_console_event(RuntimeEvent(event_type="triggerflow.signal", message="signal"), settings)

    assert not should_render_storage_event(RuntimeEvent(event_type="model.completed", level="INFO"), settings)
    assert should_render_storage_event(RuntimeEvent(event_type="request.completed", level="INFO"), settings)
