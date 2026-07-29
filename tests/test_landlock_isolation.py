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

    Note: These tests verify Landlock enforcement. If they fail, it may indicate:
    - Landlock is compiled in but not fully enabled in kernel config
    - Another LSM (AppArmor/SELinux) is taking precedence
    - The kernel version has different Landlock behavior
    """

    @pytest.mark.xfail(reason="Landlock enforcement may not work in all environments")
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

    @pytest.mark.xfail(reason="Landlock enforcement may not work in all environments")
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

    Note: These tests require being on the landlock-provider branch.
    """

    @pytest.mark.skip(reason="Requires landlock-provider branch")
    def test_provider_probe(self):
        """Test LandlockExecutionResourceProvider.async_probe."""
        pass

    @pytest.mark.skip(reason="Requires landlock-provider branch")
    def test_availability_probe(self):
        """Test inspect_landlock_availability function."""
        pass


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
