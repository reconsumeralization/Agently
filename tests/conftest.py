from pathlib import Path
import sys
import os
from urllib.parse import urlparse

import pytest
import httpx


PROJECT_ROOT = Path(__file__).resolve().parents[1]
project_root_str = str(PROJECT_ROOT)
if project_root_str not in sys.path:
    sys.path.insert(0, project_root_str)


def is_ollama_available() -> bool:
    try:
        base_url = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1")
        model = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
        parsed = urlparse(base_url)
        if not parsed.scheme or not parsed.netloc:
            return False
        response = httpx.get(f"{base_url.rstrip('/')}/models", timeout=2.0)
        if not response.is_success:
            return False
        payload = response.json()
        models = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            return False
        return any(isinstance(item, dict) and item.get("id") == model for item in models)
    except Exception:
        return False


@pytest.fixture
def require_ollama():
    if not is_ollama_available():
        pytest.skip("Ollama not reachable")


@pytest.fixture(autouse=True)
def reset_deprecation_warning_registry():
    from agently.utils import reset_deprecation_warning_registry as reset_registry

    reset_registry()
    yield
    reset_registry()
