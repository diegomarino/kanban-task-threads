"""ADR-0002: two OS processes consuming one task's events — exactly one post.

Each subprocess runs a real Consumer against the same board and state DB with
a transport that appends its operations to a shared log file. SQLite's writer
lock (lease + CAS) is the only arbiter, exactly as in production.
"""

import pathlib
import subprocess
import sys

from conftest import add_event, insert_task, make_board

REPO = pathlib.Path(__file__).resolve().parents[1]

WORKER = """
import json, pathlib, sys, time
sys.path.insert(0, {repo!r})
from kanban_task_threads.consumer import Consumer
from kanban_task_threads.store import StateStore
from kanban_task_threads.transport import ThreadRef

class FileTransport:
    def __init__(self, log, holder):
        self.log, self.holder, self.n = pathlib.Path(log), holder, 0
    def _record(self, op):
        with open(self.log, "a") as fh:
            fh.write(json.dumps({{"holder": self.holder, "op": op}}) + "\\n")
    def capabilities(self):
        return frozenset({{"rich_card", "live_timestamps", "per_message_identity"}})
    def open_thread(self, *, title, card):
        self._record("open_thread")
        time.sleep(0.05)  # widen the race window
        self.n += 1
        return ThreadRef(thread_id=f"{{self.holder}}-th{{self.n}}",
                         message_id=f"{{self.holder}}-m{{self.n}}")
    def edit_card(self, ref, card):
        self._record("edit_card")
    def append(self, ref, *, content, username=None, message_type="default"):
        self._record("append")
        self.n += 1
        return f"{{self.holder}}-m{{self.n}}"

board_db, state_db, log, holder = sys.argv[1:5]
# start barrier: both workers announce themselves and wait for the other, so
# the lease contention the docstring claims is actually exercised — without
# this, a slow runner could serialize the processes and prove nothing
barrier = pathlib.Path(log).parent
(barrier / f"ready-{{holder}}").touch()
deadline = time.time() + 10
while len(list(barrier.glob("ready-*"))) < 2 and time.time() < deadline:
    time.sleep(0.005)
consumer = Consumer(board_db, StateStore(state_db), FileTransport(log, holder),
                    board="default", holder=holder)
for _ in range(20):  # keep trying until the batch is drained or done
    report = consumer.run_once()
    if report.acquired and not report.opened and not report.replied:
        break
    time.sleep(0.01)
"""


def test_two_processes_produce_exactly_one_post(tmp_path):
    board = make_board(tmp_path / "kanban.db")
    insert_task(board, "t_1", status="done")
    add_event(board, "t_1", "created", {"status": "ready"})
    add_event(board, "t_1", "commented", {"author": "a", "len": 3})
    add_event(board, "t_1", "completed", {"summary": "ok"})
    board.close()

    script = tmp_path / "worker.py"
    script.write_text(WORKER.format(repo=str(REPO)))
    log = tmp_path / "ops.jsonl"
    log.touch()

    procs = [
        subprocess.Popen(
            [
                sys.executable,
                str(script),
                str(tmp_path / "kanban.db"),
                str(tmp_path / "state.db"),
                str(log),
                holder,
            ]
        )
        for holder in ("p1", "p2")
    ]
    for proc in procs:
        assert proc.wait(timeout=30) == 0

    import json

    ops = [json.loads(line)["op"] for line in log.read_text().splitlines()]
    assert ops.count("open_thread") == 1, f"duplicate post! ops: {ops}"
    assert ops.count("append") == 2  # commented + completed, exactly once each
