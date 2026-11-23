import subprocess
import sys
import time

import pytest

from app.chat.collector import CutoffStore
from app.chat.store import ChatMessage, ChatStore
from app.db import utcnow
from app.procs import kill_tree


def pid_alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
    return str(pid) in out.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="Windows launcher behaviour")
def test_kill_tree_kills_grandchild(tmp_path):
    """Mimic a console-script shim: the launcher spawns the real worker and waits on it."""
    pidfile = tmp_path / "child.pid"
    worker = tmp_path / "worker.py"
    worker.write_text("import os, sys, time\nopen(sys.argv[1], 'w').write(str(os.getpid()))\ntime.sleep(60)\n")
    launcher = tmp_path / "launcher.py"
    launcher.write_text("import subprocess, sys\nsubprocess.Popen([sys.executable] + sys.argv[1:]).wait()\n")
    proc = subprocess.Popen([sys.executable, str(launcher), str(worker), str(pidfile)])
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text():
            break
        time.sleep(0.05)
    child = int(pidfile.read_text())
    assert pid_alive(child)

    kill_tree(proc)

    time.sleep(0.3)
    assert proc.poll() is not None
    assert not pid_alive(child)


def test_cutoff_store_drops_replayed_history(db):
    sid = db.execute("INSERT INTO live_sessions (video_id, channel_id, status, detected_at) VALUES ('v','UC','live',?)",
                     (utcnow(),)).lastrowid
    store = ChatStore(db)
    store.add(sid, [ChatMessage("ytA", 100, "text")], "ytdlp")
    # official source replays the same message under a different ID, plus a genuinely new one
    added = CutoffStore(store, 100).add(sid, [ChatMessage("offA", 100, "text"), ChatMessage("offB", 101, "text")],
                                        "official")
    assert added == 1
    assert [r["msg_id"] for r in db.query("SELECT msg_id FROM chat_messages ORDER BY published_us")] == ["ytA", "offB"]
