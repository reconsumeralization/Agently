"""
TDD tests for SeatbeltExecutionResourceProvider — locks down BUG-1/2/3 structure.

These tests verify:
- BUG-2: SBPL mach(*) syntax must be mach*
- BUG-3: SBPL (deny network) must be (deny network-outbound)
- BUG-1: sandbox-exec must use temp file, not stdin (-f -)
- Regression: writable_paths, protected_paths, deny_read_paths, extra_rules
"""

import platform
import sys
from unittest.mock import patch, MagicMock

import pytest

from agently.builtins.plugins.ExecutionResourceProvider.SeatbeltExecutionResourceProvider import (
    SeatbeltCodeExecutionResource,
    _build_sbpl_profile,
    _realpath,
)


# ── BUG-2: mach syntax ────────────────────────────────────────

class TestSBPLMachSyntax:
    """BUG-2: (allow mach(*)) is invalid SBPL, must be (allow mach*)"""

    def test_sbpl_mach_syntax(self):
        profile = _build_sbpl_profile()
        assert "(allow mach*)" in profile, "Profile must contain (allow mach*)"
        assert "(allow mach(*))" not in profile, "Profile must NOT contain invalid (allow mach(*))"


# ── BUG-3: network deny syntax ────────────────────────────────

class TestSBPLNetworkSyntax:
    """BUG-3: (deny network) is invalid SBPL, must be (deny network-outbound)"""

    def test_sbpl_deny_network_syntax(self):
        profile = _build_sbpl_profile(network=False)
        assert "(deny network-outbound)" in profile, "Profile must contain (deny network-outbound)"
        # Ensure bare (deny network) is NOT present (but (deny network-outbound) is OK)
        lines = profile.splitlines()
        for line in lines:
            stripped = line.strip()
            if stripped == "(deny network)":
                pytest.fail(f"Profile must NOT contain bare '(deny network)', found: {stripped}")

    def test_sbpl_allow_network_outbound(self):
        profile = _build_sbpl_profile(network=True)
        assert "(allow network-outbound)" in profile, "Profile must contain (allow network-outbound) when network=True"
        assert "(deny network)" not in profile.replace("(deny network-outbound)", ""), \
            "Profile must NOT contain bare (deny network)"


# ── BUG-1: sandbox-exec invocation ────────────────────────────

class TestSandboxExecInvocation:
    """BUG-1: sandbox-exec -f - doesn't support stdin, must use temp file"""

    @pytest.mark.skipif(platform.system() != "Darwin", reason="sandbox-exec is macOS only")
    def test_run_python_code_uses_file_not_stdin(self):
        """Verify sandbox-exec uses a profile file, not stdin (-f -)."""
        from unittest.mock import MagicMock
        mock_grant = MagicMock()
        mock_grant.roots = []
        resource = SeatbeltCodeExecutionResource(
            grant=mock_grant,
            network=False,
            writable_paths=["/tmp"],
        )

        # Verify _step_argv produces sandbox-exec -f <file> ...
        from pathlib import Path
        argv = resource._step_argv(
            step=MagicMock(argv=["python3", "-c", "print('OK')"]),
            area=Path("/tmp/test-area"),
        )
        assert argv[0] == "sandbox-exec", f"First arg should be sandbox-exec, got {argv[0]}"
        assert argv[1] == "-f", f"Second arg should be -f, got {argv[1]}"
        assert argv[2] != "-", f"Third arg must NOT be '-' (stdin), got: {argv[2]}"

    @pytest.mark.skipif(platform.system() != "Darwin", reason="sandbox-exec is macOS only")
    def test_run_python_code_cleans_up_temp_file(self):
        """Verify profile file is written inside the workspace area (not leaked)."""
        from pathlib import Path
        mock_grant = MagicMock()
        mock_grant.roots = []
        resource = SeatbeltCodeExecutionResource(
            grant=mock_grant,
            network=False,
        )
        argv = resource._step_argv(
            step=MagicMock(argv=["python3", "-c", "pass"]),
            area=Path("/tmp/test-area"),
        )
        # Profile file is inside the workspace area logs dir
        profile_path = argv[2]
        assert "/tmp/test-area" in profile_path, \
            f"Profile file should be inside workspace area, got: {profile_path}"


# ── Regression tests ──────────────────────────────────────────

class TestSBPLRegression:
    """Regression tests for SBPL profile generation"""

    def test_sbpl_writable_paths_realpath(self):
        """writable_paths should be resolved via _realpath"""
        profile = _build_sbpl_profile(writable_paths=["/tmp/mywork"])
        real_path = _realpath("/tmp/mywork")
        expected = f'(allow file-write* (subpath "{real_path}"))'
        assert expected in profile, f"Expected '{expected}' in profile"

    def test_sbpl_protected_paths_order(self):
        """deny rules for protected_paths must appear AFTER allow rules (last-match-wins)"""
        profile = _build_sbpl_profile(
            writable_paths=["/tmp/mywork"],
            protected_paths=["/tmp/mywork/.git"],
        )
        real_work = _realpath("/tmp/mywork")
        real_git = _realpath("/tmp/mywork/.git")
        allow_line = f'(allow file-write* (subpath "{real_work}"))'
        deny_line = f'(deny file-write* (subpath "{real_git}"))'
        assert allow_line in profile
        assert deny_line in profile
        assert profile.index(allow_line) < profile.index(deny_line), \
            "deny rule must appear AFTER allow rule (last-match-wins)"

    def test_sbpl_deny_read_paths(self):
        """deny_read_paths should generate both deny read and deny write"""
        profile = _build_sbpl_profile(deny_read_paths=["/etc/secrets"])
        real_secrets = _realpath("/etc/secrets")
        assert f'(deny file-read* (subpath "{real_secrets}"))' in profile
        assert f'(deny file-write* (subpath "{real_secrets}"))' in profile

    def test_sbpl_extra_rules_appended(self):
        """extra_rules should be appended verbatim"""
        profile = _build_sbpl_profile(extra_rules="(deny iokit-open)")
        assert "(deny iokit-open)" in profile

    def test_sbpl_default_deny(self):
        """Profile should start with (deny default)"""
        profile = _build_sbpl_profile()
        assert "(deny default)" in profile

    def test_sbpl_temp_dirs_always_writable(self):
        """Temp directories should always be writable"""
        profile = _build_sbpl_profile()
        assert '(allow file-write* (subpath "/private/tmp"))' in profile
