import builtins
import importlib

import pytest

from agently.utils import LazyImport, LazyImportDependencyError


def test_import_package():
    json5 = LazyImport.import_package("json5")
    assert json5.loads('{"test":"ok"}') == {"test": "ok"}  # type: ignore
    loads = LazyImport.from_import("json5", "loads")
    assert loads('{"test":"ok"}') == {"test": "ok"}  # type: ignore
    with pytest.raises(ImportError):
        LazyImport.import_package("unknown_package", auto_install=False)
    with pytest.raises(ModuleNotFoundError):
        loads = LazyImport.from_import("json5", "unknown_method", auto_install=False)


def test_import_package_default_disables_install_prompt(monkeypatch):
    prompts: list[str] = []

    def fail_if_prompted(prompt: str):
        prompts.append(prompt)
        raise AssertionError("LazyImport should not prompt when auto_install is omitted")

    monkeypatch.setattr(builtins, "input", fail_if_prompted)

    with pytest.raises(LazyImportDependencyError) as error_info:
        LazyImport.import_package("unknown_package_for_lazy_import_test", install_name="unknown-package-for-test")

    error = error_info.value
    assert prompts == []
    assert error.package_name == "unknown_package_for_lazy_import_test"
    assert error.install_name == "unknown-package-for-test"
    assert error.reason == "missing"
    assert error.payload["schema_version"] == "lazy_import.dependency/v1"
    assert error.payload["install_command"][-1] == "unknown-package-for-test"


def test_from_import_default_disables_install_prompt(monkeypatch):
    monkeypatch.setattr(
        builtins,
        "input",
        lambda prompt: (_ for _ in ()).throw(AssertionError("LazyImport should not prompt by default")),
    )

    with pytest.raises(LazyImportDependencyError) as error_info:
        LazyImport.from_import("unknown_package_for_lazy_import_test", "missing")

    assert error_info.value.import_name == "unknown_package_for_lazy_import_test"
    assert error_info.value.payload["install_name"] == "unknown_package_for_lazy_import_test"


def test_import_package_auto_install_true_prompts_before_failure(monkeypatch):
    lazy_import_module = importlib.import_module("agently.utils.LazyImport")
    prompts: list[str] = []
    install_calls: list[list[str]] = []

    def deny_install(prompt: str) -> str:
        prompts.append(prompt)
        return "n"

    monkeypatch.setattr(builtins, "input", deny_install)
    monkeypatch.setattr(lazy_import_module.subprocess, "check_call", lambda command: install_calls.append(command))

    with pytest.raises(LazyImportDependencyError):
        LazyImport.import_package(
            "unknown_package_for_lazy_import_test",
            auto_install=True,
            install_name="unknown-package-for-test",
        )

    assert len(prompts) == 1
    assert "Install now?" in prompts[0]
    assert install_calls == []


def test_import_package_version_mismatch_is_structured_error_by_default():
    with pytest.raises(LazyImportDependencyError) as error_info:
        LazyImport.import_package("json5", version_constraint=">=999999")

    error = error_info.value
    assert error.reason == "version_mismatch"
    assert error.installed_version
    assert error.version_constraint == ">=999999"
    assert error.payload["install_command"][-1] == "json5>=999999"


if __name__ == "__main__":
    agently_stage = LazyImport.import_package("agently_stage", auto_install=True, install_name="agently-stage")
