import subprocess
import sys

import pytest

from app.private_files import secure_existing, write_private

windows = pytest.mark.skipif(sys.platform != "win32", reason="Windows ACLs")


def acl_entries(path) -> list[str]:
    """icacls lines naming a principal, e.g. 'DOMAIN\\user:(F)' or 'BUILTIN\\Users:(I)(RX)'."""
    out = subprocess.run(["icacls", str(path)], capture_output=True, text=True).stdout
    lines = [ln.replace(str(path), "").strip() for ln in out.splitlines()]
    return [ln for ln in lines if ":(" in ln]


def test_write_private_writes_and_replaces_without_leftovers(tmp_path):
    secret = tmp_path / "data" / "youtube_token.json"
    write_private(secret, '{"token": "one"}')
    write_private(secret, '{"token": "two"}')
    assert secret.read_text(encoding="utf-8") == '{"token": "two"}'
    assert [p.name for p in secret.parent.iterdir()] == ["youtube_token.json"]


@windows
def test_secret_is_readable_only_by_the_current_user(tmp_path):
    secret = tmp_path / "token.json"
    write_private(secret, "secret")
    entries = acl_entries(secret)
    assert len(entries) == 1 and entries[0].endswith(":(F)")       # one principal: us, full control
    assert not any("(I)" in e for e in entries)                     # nothing inherited from the folder


@windows
def test_existing_secret_files_are_locked_down(tmp_path):
    old = tmp_path / "ntfy.json"
    old.write_text('{"topic": "clipfactory-x"}', encoding="utf-8")   # written the old way: inherits the folder ACL
    assert any("(I)" in e for e in acl_entries(old))
    secure_existing([old, tmp_path / "missing.json"])
    entries = acl_entries(old)
    assert len(entries) == 1 and not any("(I)" in e for e in entries)
    assert old.read_text(encoding="utf-8") == '{"topic": "clipfactory-x"}'   # still readable by us


@windows
def test_explicit_grants_to_other_users_are_removed_too(tmp_path):
    """A browser-saved file (the OAuth client JSON) can carry explicit 'Users: read' entries, not inherited ones."""
    f = tmp_path / "client_secret.json"
    f.write_text("{}", encoding="utf-8")
    subprocess.run(["icacls", str(f), "/grant", "*S-1-5-32-545:(RX)"], capture_output=True, check=True)   # Users
    assert any(e.endswith("Users:(RX)") for e in acl_entries(f))
    secure_existing([f])
    entries = acl_entries(f)
    assert len(entries) == 1 and entries[0].endswith(":(F)")
