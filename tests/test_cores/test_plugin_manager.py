from typing import Any
import pytest
from agently import Agently
from agently.core import PluginManager
from agently.types.config import options_schema_registry, settings_schema_registry
from agently.types.options import ExecutionOptions
from agently.types.settings import OpenAICompatibleSettings


def test_plugin_manager():
    plugin_manager = PluginManager(Agently.settings)
    assert plugin_manager.get_plugin_list() == {}

    from agently.types.plugins import PromptGenerator

    class TestPromptGenerator(PromptGenerator):
        name = "Test"
        SETTINGS_SCHEMAS = {"tests.plugin.settings": OpenAICompatibleSettings}
        OPTIONS_SCHEMAS = {"tests.plugin.options": ExecutionOptions}

        def to_text(self, *args, **kwargs) -> str:
            return "OK"

        def to_messages(self, *args, **kwargs) -> list[dict[str, Any]]:
            return []

    plugin_manager.register("PromptGenerator", TestPromptGenerator, activate=False)

    assert plugin_manager.get_plugin_list() == {"PromptGenerator": ["Test"]}
    assert plugin_manager.get_plugin("PromptGenerator", "Test") == TestPromptGenerator
    assert settings_schema_registry.get("tests.plugin.settings") is OpenAICompatibleSettings
    assert options_schema_registry.get("tests.plugin.options") is ExecutionOptions

    plugin_manager.unregister("PromptGenerator", TestPromptGenerator)

    assert settings_schema_registry.get("tests.plugin.settings") is None
    assert options_schema_registry.get("tests.plugin.options") is None


if __name__ == "__main__":
    test_plugin_manager()
