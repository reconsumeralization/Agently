from agently import Agently
from agently_devtools import ObservationBridge


def register_example_observation(
    *,
    group_id: str,
    app_id: str = "agently-main-examples",
) -> ObservationBridge:
    bridge = ObservationBridge(app_id=app_id, group_id=group_id)
    bridge.register(Agently)
    return bridge


def unregister_example_observation(bridge: ObservationBridge) -> None:
    bridge.unregister(Agently)