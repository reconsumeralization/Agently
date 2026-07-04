# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import sys
import importlib
import shlex
import subprocess
from types import ModuleType
from typing import Any

from importlib.metadata import version as get_installed_version, PackageNotFoundError
from packaging.version import parse as parse_version
from packaging.specifiers import SpecifierSet

from .DataFormatter import DataFormatter


class LazyImportDependencyError(ImportError):
    def __init__(
        self,
        *,
        package_name: str,
        import_name: str,
        install_name: str | None = None,
        version_constraint: str | None = None,
        installed_version: str | None = None,
        reason: str = "missing",
        original_error: BaseException | None = None,
    ) -> None:
        root_package_name = install_name or package_name.split(".")[0]
        requirement = f"{root_package_name}{version_constraint or ''}"
        install_command = [sys.executable, "-m", "pip", "install", requirement]
        message = (
            f"Required dependency is unavailable: {import_name}. "
            f"Install it with: {shlex.join(install_command)}"
        )
        if reason == "version_mismatch" and installed_version:
            message = (
                f"Required dependency version is unavailable: {import_name}; "
                f"installed {installed_version}, expected {version_constraint}. "
                f"Install it with: {shlex.join(install_command)}"
            )
        super().__init__(message)
        self.package_name = package_name
        self.import_name = import_name
        self.install_name = root_package_name
        self.version_constraint = version_constraint
        self.installed_version = installed_version
        self.reason = reason
        self.original_error = original_error
        self.install_command = install_command
        self.install_command_text = shlex.join(install_command)
        self.payload = {
            "schema_version": "lazy_import.dependency/v1",
            "code": "lazy_import.dependency_unavailable",
            "reason": reason,
            "package_name": package_name,
            "import_name": import_name,
            "install_name": root_package_name,
            "version_constraint": version_constraint,
            "installed_version": installed_version,
            "install_command": install_command,
            "install_command_text": self.install_command_text,
        }
        if original_error is not None:
            self.payload["original_error_type"] = type(original_error).__name__
            self.payload["original_error"] = str(original_error)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


class LazyImport:
    @staticmethod
    def from_import(
        from_package: str,
        target_modules: str | list[str],
        *,
        auto_install: bool = False,
        version_constraint: str | None = None,
        install_name: str | None = None,
        _attempted_install: bool = False,
    ) -> Any:
        version_constraint = LazyImport._normalize_version_constraint(version_constraint)

        if not isinstance(target_modules, list):
            target_modules = [target_modules]

        loaded_modules = []
        module_name = ""

        try:
            for module_name in target_modules:
                try:
                    module_name = DataFormatter.to_str(module_name)
                    loaded_modules.append(importlib.import_module(f"{from_package}.{module_name}"))
                except ModuleNotFoundError:
                    try:
                        base_module = importlib.import_module(from_package)
                    except ImportError as error:
                        return LazyImport._handle_dependency_error(
                            package_name=from_package,
                            import_name=from_package,
                            install_name=install_name,
                            version_constraint=version_constraint,
                            auto_install=auto_install,
                            _attempted_install=_attempted_install,
                            retry=lambda: LazyImport.from_import(
                                from_package,
                                target_modules,
                                auto_install=auto_install,
                                version_constraint=version_constraint,
                                install_name=install_name,
                                _attempted_install=True,
                            ),
                            original_error=error,
                        )
                    try:
                        module_attr = getattr(base_module, module_name)
                        loaded_modules.append(module_attr)
                    except AttributeError:
                        raise ModuleNotFoundError(
                            f"Required module not found: {module_name}\n"
                            f"Found package '{from_package}' but no module or attribute named '{module_name}' in it."
                        )
            if version_constraint:
                try:
                    root_package_name = install_name or from_package
                    installed_version = get_installed_version(root_package_name)
                    spec = SpecifierSet(version_constraint)
                    if parse_version(installed_version) not in spec:
                        return LazyImport._handle_dependency_error(
                            package_name=from_package,
                            import_name=from_package,
                            install_name=install_name,
                            version_constraint=version_constraint,
                            installed_version=installed_version,
                            reason="version_mismatch",
                            auto_install=auto_install,
                            _attempted_install=_attempted_install,
                            retry=lambda: LazyImport.from_import(
                                from_package,
                                target_modules,
                                auto_install=auto_install,
                                version_constraint=version_constraint,
                                install_name=install_name,
                                _attempted_install=True,
                            ),
                        )
                except PackageNotFoundError:
                    pass
            return (tuple(loaded_modules) if len(loaded_modules) > 1 else loaded_modules[0]) if loaded_modules else None
        except ModuleNotFoundError:
            raise
        except ImportError as error:
            if isinstance(error, LazyImportDependencyError):
                raise
            return LazyImport._handle_dependency_error(
                package_name=from_package,
                import_name=f"{from_package}.{module_name}" if module_name else from_package,
                install_name=install_name,
                version_constraint=version_constraint,
                auto_install=auto_install,
                _attempted_install=_attempted_install,
                retry=lambda: LazyImport.from_import(
                    from_package,
                    target_modules,
                    auto_install=auto_install,
                    version_constraint=version_constraint,
                    install_name=install_name,
                    _attempted_install=True,
                ),
                original_error=error,
            )

    @staticmethod
    def import_package(
        package_name: str,
        *,
        auto_install: bool = False,
        version_constraint: str | None = None,
        install_name: str | None = None,
        _attempted_install: bool = False,
    ) -> ModuleType:
        version_constraint = LazyImport._normalize_version_constraint(version_constraint)

        try:
            # Attempt to import the package
            module = importlib.import_module(package_name)
            if version_constraint:
                try:
                    root_package_name = install_name or package_name.split(".")[0]
                    installed_version = get_installed_version(root_package_name)
                    spec = SpecifierSet(version_constraint)
                    if parse_version(installed_version) not in spec:
                        return LazyImport._handle_dependency_error(
                            package_name=package_name,
                            import_name=package_name,
                            install_name=install_name,
                            version_constraint=version_constraint,
                            installed_version=installed_version,
                            reason="version_mismatch",
                            auto_install=auto_install,
                            _attempted_install=_attempted_install,
                            retry=lambda: LazyImport.import_package(
                                package_name,
                                auto_install=auto_install,
                                version_constraint=version_constraint,
                                install_name=install_name,
                                _attempted_install=True,
                            ),
                        )
                except PackageNotFoundError:
                    pass
            return module
        except ImportError as error:
            if isinstance(error, LazyImportDependencyError):
                raise
            return LazyImport._handle_dependency_error(
                package_name=package_name,
                import_name=package_name,
                install_name=install_name,
                version_constraint=version_constraint,
                auto_install=auto_install,
                _attempted_install=_attempted_install,
                retry=lambda: LazyImport.import_package(
                    package_name,
                    auto_install=auto_install,
                    version_constraint=version_constraint,
                    install_name=install_name,
                    _attempted_install=True,
                ),
                original_error=error,
            )

    @staticmethod
    def _normalize_version_constraint(version_constraint: str | None) -> str | None:
        if version_constraint and not any(
            version_constraint.startswith(op) for op in ("==", ">=", "<=", "!=", ">", "<")
        ):
            return f"=={version_constraint}"
        return version_constraint

    @staticmethod
    def _handle_dependency_error(
        *,
        package_name: str,
        import_name: str,
        install_name: str | None,
        version_constraint: str | None,
        auto_install: bool,
        _attempted_install: bool,
        retry,
        installed_version: str | None = None,
        reason: str = "missing",
        original_error: BaseException | None = None,
    ) -> Any:
        error = LazyImportDependencyError(
            package_name=package_name,
            import_name=import_name,
            install_name=install_name,
            version_constraint=version_constraint,
            installed_version=installed_version,
            reason=reason,
            original_error=original_error,
        )
        if auto_install and not _attempted_install:
            confirm = input(f"{error}\nInstall now? [y/N]: ").strip().lower()
            if confirm == "y":
                subprocess.check_call(error.install_command)
                return retry()
        raise error
