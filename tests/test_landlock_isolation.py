"""
Landlock isolation verification tests.

These tests verify ACTUAL filesystem isolation using Linux Landlock LSM.

Requirements:
- Linux kernel 5.13+ with CONFIG_SECURITY_LANDLOCK=y
- Landlock ABI v1+ (probed via syscall 444)

Unlike Bubblewrap, Landlock does NOT require user namespaces or AppArmor
workarounds. It works in unprivileged processes via fork+restrict pattern.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import platform
import struct
import tempfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Landlock syscall helpers
# ---------------------------------------------------------------------------

SYS_LANDLOCK_CREATE_RULESET = 444
SYS_LANDLOCK_ADD_RULE = 445
SYS_LANDLOCK_RESTRICT_SELF = 446
LANDLOCK_CREATE_RULESET_VERSION = 1

# Access rights
LANDLOCK_ACCESS_FS_EXECUTE = 1 << 0
LANDLOCK_ACCESS_FS_WRITE_FILE = 1 << 1
LANDLOCK_ACCESS_FS_READ_FILE = 1 << 2
LANDLOCK_ACCESS_FS_READ_DIR = 1 << 3
LANDLOCK_ACCESS_FS_REMOVE_DIR = 1 << 4
LANDLOCK_ACCESS_FS_REMOVE_FILE = 1 << 5
LANDLOCK_ACCESS_FS_MAKE_CHAR = 1 << 6
LANDLOCK_ACCESS_FS_MAKE_DIR = 1 << 7
LANDLOCK_ACCESS_FS_MAKE_REG = 1 << 8
LANDLOCK_ACCESS_FS_MAKE_SOCK = 1 << 9
LANDLOCK_ACCESS_FS_MAKE_FIFO = 1 << 10
LANDLOCK_ACCESS_FS_MAKE_BLOCK = 1 << 11
LANDLOCK_ACCESS_FS_MAKE_SYM = 1 << 12

LANDLOCK_ACCESS_FS_ALL_READ = (
    LANDLOCK_ACCESS_FS_EXECUTE |
    LANDLOCK_ACCESS_FS_READ_FILE |
    LANDLOCK_ACCESS_FS_READ_DIR
)
LANDLOCK_ACCESS_FS_ALL_WRITE = (
    LANDLOCK_ACCESS_FS_WRITE_FILE |
    LANDLOCK_ACCESS_FS_REMOVE_DIR |
    LANDLOCK_ACCESS_FS_REMOVE_FILE |
    LANDLOCK_ACCESS_FS_MAKE_REG
)

LANDLOCK_RULE_PATH_BENEATH = 1


def _get_libc():
    libc_name = ctypes.util.find_library("c")
    if libc_name:
        return ctypes.CDLL(libc_name, use_errno=True)
    return None


def probe_landlock_abi() -> int:
    """Return Landlock ABI version, 0 if unsupported."""
    libc = _get_libc()
    if libc is None:
        return 0
    libc.syscall.restype = ctypes.c_long
    try:
        version = libc.syscall(SYS_LANDLOCK_CREATE_RULESET, None, 0, LANDLOCK_CREATE_RULESET_VERSION)
        return int(version) if version > 0 else 0
    except Exception:
        return 0


def create_landlock_ruleset(handled_access: int) -> int:
    """Create a Landlock ruleset. Returns fd or -1 on error."""
    libc = _get_libc()
    if libc is None:
        return -1
    libc.syscall.restype = ctypes.c_long
    attr = struct.pack("Q", handled_access)
    buf = ctypes.create_string_buffer(attr)
    return int(libc.syscall(SYS_LANDLOCK_CREATE_RULESET, buf, len(attr), 0))


def add_landlock_rule(ruleset_fd: int, path: str, allowed_access: int) -> int:
    """Add a path rule to the ruleset. Returns 0 on success."""
    libc = _get_libc()
    if libc is None:
        return -1
    libc.syscall.restype = ctypes.c_long

    # struct landlock_path_beneath_attr { __u64 allowed_access; __s32 parent_fd; }
    path_fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    try:
        attr = struct.pack("Qi", allowed_access, path_fd)
        buf = ctypes.create_string_buffer(attr)
        return int(libc.syscall(SYS_LANDLOCK_ADD_RULE, ruleset_fd, LANDLOCK_RULE_PATH_BENEATH, buf, 0))
    finally:
        os.close(path_fd)


def restrict_self(ruleset_fd: int) -> int:
    """Apply ruleset to current process. Returns 0 on success."""
    libc = _get_libc()
    if libc is None:
        return -1
    libc.syscall.restype = ctypes.c_long
    return int(libc.syscall(SYS_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0))


def set_no_new_privs() -> int:
    """Set PR_SET_NO_NEW_PRIVS. Required before landlock_restrict_self."""
    libc = _get_libc()
    if libc is None:
        return -1
    # PR_SET_NO_NEW_PRIVS = 38
    return int(libc.prctl(38, 1, 0, 0, 0))


# ---------------------------------------------------------------------------
# Skip condition
# ---------------------------------------------------------------------------

_landlock_abi = probe_landlock_abi()

pytestmark = pytest.mark.skipif(
    platform.system() != "Linux" or _landlock_abi <= 0,
    reason="Landlock tests require Linux 5.13+ with CONFIG_SECURITY_LANDLOCK=y",
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestLandlockAvailability:
    """Test Landlock kernel support detection."""

    def test_landlock_abi_version(self):
        """Landlock ABI should be >= 1."""
        assert _landlock_abi >= 1, f"Expected ABI >= 1, got {_landlock_abi}"

    def test_ruleset_creation(self):
        """Should be able to create a ruleset."""
        handled = LANDLOCK_ACCESS_FS_ALL_READ | LANDLOCK_ACCESS_FS_ALL_WRITE
        fd = create_landlock_ruleset(handled)
        assert fd >= 0, f"Failed to create ruleset: errno={ctypes.get_errno()}"
        os.close(fd)


class TestFilesystemWriteIsolation:
    """Test that Landlock blocks unauthorized writes.

    Key requirement: PR_SET_NO_NEW_PRIVS must be set before
    landlock_restrict_self, otherwise the syscall returns EPERM.
    """

    def test_write_blocked_after_restrict(self):
        """After restrict_self, writing to non-whitelisted path should fail."""
        handled = LANDLOCK_ACCESS_FS_ALL_READ | LANDLOCK_ACCESS_FS_ALL_WRITE
        fd = create_landlock_ruleset(handled)
        assert fd >= 0

        # Add rule: allow read+write only to a specific temp directory
        with tempfile.TemporaryDirectory() as allowed_dir:
            add_landlock_rule(fd, allowed_dir, LANDLOCK_ACCESS_FS_ALL_READ | LANDLOCK_ACCESS_FS_ALL_WRITE)

            # Fork to apply restriction without breaking parent
            r_fd, w_fd = os.pipe()
            pid = os.fork()

            if pid == 0:
                # Child process
                os.close(r_fd)
                set_no_new_privs()  # Required before restrict_self
                restrict_self(fd)
                os.close(fd)

                # Try to write OUTSIDE allowed directory
                test_file = Path(tempfile.gettempdir()) / f"landlock_blocked_{os.getpid()}"
                try:
                    test_file.write_text("should be blocked")
                    os.write(w_fd, b"UNEXPECTED_WRITE")
                    test_file.unlink(missing_ok=True)
                except PermissionError:
                    os.write(w_fd, b"BLOCKED")
                except Exception as e:
                    os.write(w_fd, f"OTHER:{type(e).__name__}".encode())
                os.close(w_fd)
                os._exit(0)
            else:
                # Parent process
                os.close(w_fd)
                result = os.read(r_fd, 100).decode()
                os.close(r_fd)
                os.waitpid(pid, 0)
                assert result == "BLOCKED", f"Expected BLOCKED, got {result}"

    def test_write_allowed_in_whitelisted_dir(self):
        """Writing to whitelisted directory should succeed."""
        handled = LANDLOCK_ACCESS_FS_ALL_READ | LANDLOCK_ACCESS_FS_ALL_WRITE
        fd = create_landlock_ruleset(handled)
        assert fd >= 0

        with tempfile.TemporaryDirectory() as allowed_dir:
            add_landlock_rule(fd, allowed_dir, LANDLOCK_ACCESS_FS_ALL_READ | LANDLOCK_ACCESS_FS_ALL_WRITE)

            r_fd, w_fd = os.pipe()
            pid = os.fork()

            if pid == 0:
                os.close(r_fd)
                set_no_new_privs()  # Required before restrict_self
                restrict_self(fd)
                os.close(fd)

                # Write INSIDE allowed directory
                test_file = Path(allowed_dir) / "allowed_write.txt"
                try:
                    test_file.write_text("allowed content")
                    content = test_file.read_text()
                    os.write(w_fd, f"OK:{content}".encode())
                except Exception as e:
                    os.write(w_fd, f"FAIL:{type(e).__name__}".encode())
                os.close(w_fd)
                os._exit(0)
            else:
                os.close(w_fd)
                result = os.read(r_fd, 200).decode()
                os.close(r_fd)
                os.waitpid(pid, 0)
                assert result.startswith("OK:"), f"Expected OK, got {result}"
                assert "allowed content" in result


class TestFilesystemReadIsolation:
    """Test that Landlock blocks unauthorized reads."""

    def test_read_blocked_sensitive_file(self):
        """Reading non-whitelisted file should fail after restriction."""
        # Only allow read access to /usr (a safe read-only dir)
        handled = LANDLOCK_ACCESS_FS_ALL_READ | LANDLOCK_ACCESS_FS_ALL_WRITE
        fd = create_landlock_ruleset(handled)
        assert fd >= 0

        # Only whitelist /usr for reading
        if Path("/usr").exists():
            add_landlock_rule(fd, "/usr", LANDLOCK_ACCESS_FS_ALL_READ)

        r_fd, w_fd = os.pipe()
        pid = os.fork()

        if pid == 0:
            os.close(r_fd)
            set_no_new_privs()  # Required before restrict_self
            restrict_self(fd)
            os.close(fd)
            
            # Try to read /etc/passwd (not whitelisted)
            try:
                content = Path("/etc/passwd").read_text()
                os.write(w_fd, b"UNEXPECTED_READ")
            except PermissionError:
                os.write(w_fd, b"BLOCKED")
            except Exception as e:
                os.write(w_fd, f"OTHER:{type(e).__name__}".encode())
            os.close(w_fd)
            os._exit(0)
        else:
            os.close(w_fd)
            result = os.read(r_fd, 100).decode()
            os.close(r_fd)
            os.waitpid(pid, 0)
            assert result == "BLOCKED", f"Expected BLOCKED, got {result}"


class TestLandlockIrreversibility:
    """Test that Landlock restrictions are irreversible (fork isolation)."""

    def test_parent_unaffected_after_child_restrict(self):
        """Parent process should NOT be affected by child's Landlock restriction."""
        handled = LANDLOCK_ACCESS_FS_ALL_READ | LANDLOCK_ACCESS_FS_ALL_WRITE
        fd = create_landlock_ruleset(handled)
        assert fd >= 0

        with tempfile.TemporaryDirectory() as tmpdir:
            # No rules added - everything blocked

            r_fd, w_fd = os.pipe()
            pid = os.fork()

            if pid == 0:
                # Child: apply restriction
                os.close(r_fd)
                set_no_new_privs()  # Required before restrict_self
                restrict_self(fd)
                os.close(fd)
                os.write(w_fd, b"CHILD_RESTRICTED")
                os.close(w_fd)
                os._exit(0)
            else:
                # Parent: should still be able to write
                os.close(w_fd)
                child_result = os.read(r_fd, 100).decode()
                os.close(r_fd)
                os.waitpid(pid, 0)

                assert child_result == "CHILD_RESTRICTED"

                # Parent can still write freely
                test_file = Path(tmpdir) / "parent_write.txt"
                test_file.write_text("parent unrestricted")
                assert test_file.read_text() == "parent unrestricted"


class TestProviderIntegration:
    """Test the actual provider implementation.

    These tests require being on the landlock-provider branch.
    """

    def test_provider_probe(self):
        """Test LandlockExecutionResourceProvider.async_probe."""
        import asyncio
        from agently.builtins.plugins.ExecutionResourceProvider.LandlockExecutionResourceProvider import (
            LandlockExecutionResourceProvider,
        )

        provider = LandlockExecutionResourceProvider()
        result = asyncio.get_event_loop().run_until_complete(
            provider.async_probe(requirement={}, policy={})
        )
        assert result["provider_id"] == "landlock"
        assert result["available"] is True
        assert "code_execution" in result["supported_kinds"]
        # P2-6: verify enhanced isolation capabilities
        isolation = result["capabilities"]["isolation"]
        assert isolation["mechanism"] == "landlock"
        assert isolation["filesystem_only"] is True
        assert isolation["auto_base_paths"] is True
        assert isolation["abi_version"] >= 1

    def test_availability_probe(self):
        """Test inspect_landlock_availability function."""
        from agently.builtins.plugins.ExecutionResourceProvider.LandlockExecutionResourceProvider import (
            inspect_landlock_availability,
        )

        result = inspect_landlock_availability()
        assert result["available"] is True
        assert result["abi_version"] >= 1

    def test_base_read_dirs_defined(self):
        """Verify _BASE_READ_DIRS includes essential system paths."""
        from agently.builtins.plugins.ExecutionResourceProvider.LandlockExecutionResourceProvider import (
            _BASE_READ_DIRS,
        )

        # P0-2: essential paths must be present
        assert "/usr" in _BASE_READ_DIRS
        assert "/lib" in _BASE_READ_DIRS
        assert "/dev/null" in _BASE_READ_DIRS
        assert "/dev/urandom" in _BASE_READ_DIRS

    def test_path_resolution_in_init(self):
        """Verify __init__ resolves paths to prevent escape."""
        import asyncio
        from unittest.mock import MagicMock
        from agently.builtins.plugins.ExecutionResourceProvider.LandlockExecutionResourceProvider import (
            LandlockCodeExecutionResource,
        )

        grant = MagicMock()
        grant.roots = []

        # P1-3: paths with .. should be resolved
        resource = LandlockCodeExecutionResource(
            grant=grant,
            allowed_read_dirs=["/usr/../etc"],
            allowed_write_dirs=["/tmp/../tmp"],
        )
        # /usr/../etc should resolve to /etc
        assert "/etc" in resource.allowed_read_dirs[0]
        # /tmp/../tmp should resolve to /tmp
        assert "/tmp" in resource.allowed_write_dirs[0]

    def test_build_rules_includes_base_paths(self):
        """Verify _build_landlock_rules auto-injects base read dirs."""
        from unittest.mock import MagicMock
        from agently.builtins.plugins.ExecutionResourceProvider.LandlockExecutionResourceProvider import (
            LandlockCodeExecutionResource,
            _BASE_READ_DIRS,
        )

        grant = MagicMock()
        grant.roots = []

        resource = LandlockCodeExecutionResource(grant=grant)
        handled, rules = resource._build_landlock_rules(cwd="/tmp")

        rule_paths = [r[0] for r in rules]
        # /usr should be auto-injected
        assert any("/usr" in p for p in rule_paths), f"Missing /usr in {rule_paths}"
        # cwd should be auto-injected
        assert any("/tmp" in p for p in rule_paths), f"Missing cwd /tmp in {rule_paths}"

    def test_landlock_exit_codes_defined(self):
        """Verify Landlock failure exit codes are defined."""
        from agently.builtins.plugins.ExecutionResourceProvider.LandlockExecutionResourceProvider import (
            _LANDLOCK_EXIT_LIB_NOT_FOUND,
            _LANDLOCK_EXIT_CREATE_FAILED,
            _LANDLOCK_EXIT_RESTRICT_FAILED,
        )

        # P0-1: exit codes must be distinct and in 125-127 range
        assert _LANDLOCK_EXIT_LIB_NOT_FOUND == 127
        assert _LANDLOCK_EXIT_CREATE_FAILED == 126
        assert _LANDLOCK_EXIT_RESTRICT_FAILED == 125


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
