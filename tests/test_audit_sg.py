"""Tests for scripts/audit-sg.sh using a fake aws CLI on PATH."""

import os
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "audit-sg.sh"


def _fake_aws(tmp_path, ip_permissions_json: str) -> dict:
    """Create a fake `aws` executable that prints the given SG ingress JSON."""
    fake = tmp_path / "aws"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f"cat <<'EOF'\n{ip_permissions_json}\nEOF\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    return env


def test_audit_passes_when_ports_restricted(tmp_path):
    perms = '[{"FromPort":8443,"ToPort":8443,"IpRanges":[{"CidrIp":"1.2.3.4/32"}]},' \
            '{"FromPort":1080,"ToPort":1080,"IpRanges":[{"CidrIp":"1.2.3.4/32"}]}]'
    env = _fake_aws(tmp_path, perms)
    r = subprocess.run(["bash", str(SCRIPT), "--sg-id", "sg-1", "--relay-port", "8443"],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_audit_fails_when_relay_port_open(tmp_path):
    perms = '[{"FromPort":8443,"ToPort":8443,"IpRanges":[{"CidrIp":"0.0.0.0/0"}]}]'
    env = _fake_aws(tmp_path, perms)
    r = subprocess.run(["bash", str(SCRIPT), "--sg-id", "sg-1", "--relay-port", "8443"],
                       env=env, capture_output=True, text=True)
    assert r.returncode != 0
    assert "0.0.0.0/0" in (r.stdout + r.stderr)


def test_audit_fails_when_socks_port_open(tmp_path):
    perms = '[{"FromPort":1080,"ToPort":1080,"IpRanges":[{"CidrIp":"0.0.0.0/0"}]}]'
    env = _fake_aws(tmp_path, perms)
    r = subprocess.run(["bash", str(SCRIPT), "--sg-id", "sg-1", "--relay-port", "8443"],
                       env=env, capture_output=True, text=True)
    assert r.returncode != 0
    assert "1080" in (r.stdout + r.stderr)


def test_audit_passes_when_port_in_range_but_restricted(tmp_path):
    """A wide port range that includes the relay port but is not 0.0.0.0/0 passes."""
    perms = '[{"FromPort":8000,"ToPort":9000,"IpRanges":[{"CidrIp":"1.2.3.4/32"}]}]'
    env = _fake_aws(tmp_path, perms)
    r = subprocess.run(["bash", str(SCRIPT), "--sg-id", "sg-1", "--relay-port", "8443"],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
