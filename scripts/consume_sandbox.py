#!/usr/bin/env python3
"""Run the consumer against the sandbox board with a console transport.

No Discord, no credentials: every operation the transport would send is
printed instead. State (cursor, task → post mapping) is durable in
`.sandbox/plugin-state.db`, so a second run with no new events prints nothing —
which is the property being demonstrated.

Usage: consume_sandbox.py <board.db> <state.db>   (the sandbox script calls it)
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from kanban_task_threads.consumer import Consumer
from kanban_task_threads.store import StateStore
from kanban_task_threads.transport import ThreadRef


class ConsoleTransport:
    """The seam's operations, printed. Thread ids are invented locally."""

    def __init__(self):
        self._n = 0

    def capabilities(self):
        return frozenset({"rich_card", "live_timestamps", "per_message_identity"})

    def open_thread(self, *, title, card):
        self._n += 1
        print(f"OPEN   [{title}]")
        print("       " + card.description.replace("\n", "\n       "))
        return ThreadRef(thread_id=f"console-th{self._n}", message_id=f"console-msg{self._n}")

    def edit_card(self, ref, card):
        print(f"EDIT   {ref.thread_id}: {card.description.splitlines()[0]}")

    def append(self, ref, *, content, username=None):
        self._n += 1
        who = f" ({username})" if username else ""
        print(f"REPLY  {ref.thread_id}{who}: {content}")
        return f"console-msg{self._n}"


def main():
    board_db, state_db = sys.argv[1], sys.argv[2]
    consumer = Consumer(
        board_db,
        StateStore(state_db),
        ConsoleTransport(),
        board="default",
        holder="sandbox-console",
        destination="console",
    )
    report = consumer.run_once()
    print(
        f"-- opened={len(report.opened)} replies={report.replied} "
        f"edits={len(report.edited)} errors={len(report.errors)}"
    )
    for line in report.errors:
        print("ERROR:", line)


if __name__ == "__main__":
    main()
