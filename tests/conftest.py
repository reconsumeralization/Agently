from pathlib import Path
import sys

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
project_root_str = str(PROJECT_ROOT)
if project_root_str not in sys.path:
    sys.path.insert(0, project_root_str)

@pytest.fixture(autouse=True)
def reset_deprecation_warning_registry():
    from agently.utils import reset_deprecation_warning_registry as reset_registry

    reset_registry()
    yield
    reset_registry()
