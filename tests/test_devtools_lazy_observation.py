from typing import Any

import agently.base as agently_base
from agently import Agently


def test_agently_observe_uses_lazy_import_and_binds_bridge(monkeypatch):
    calls: dict[str, Any] = {}

    class FakeObservationBridge:
        def __init__(self, owner: Any, **options: Any):
            calls["owner"] = owner
            calls["options"] = options
            self.watched: tuple[Any, ...] = ()

        def watch(self, *targets: Any):
            self.watched = targets
            calls["watched"] = targets
            return self

    class FakeDevToolsModule:
        ObservationBridge = FakeObservationBridge

    def fake_import_package(package_name: str, **kwargs: Any):
        calls["package_name"] = package_name
        calls["kwargs"] = kwargs
        return FakeDevToolsModule

    monkeypatch.setattr(agently_base.LazyImport, "import_package", fake_import_package)

    agent = Agently.create_agent("devtools-agent")
    bridge = Agently.observe(agent, app_id="agently-main-tests", group_id="lazy-devtools")

    assert isinstance(bridge, FakeObservationBridge)
    assert calls["package_name"] == "agently_devtools"
    assert calls["kwargs"]["install_name"] == "agently-devtools"
    assert calls["kwargs"]["auto_install"] is False
    assert calls["owner"] is Agently
    assert calls["options"] == {"app_id": "agently-main-tests", "group_id": "lazy-devtools"}
    assert calls["watched"] == (agent,)
