#!/usr/bin/env python3
"""Operator CLI over the plugin state DB — see kanban_task_threads/reconcile.py.

Usage: reconcile.py <state.db> attention
       reconcile.py <state.db> clear|rearm|recreate <board> <task_id>
       reconcile.py <state.db> adopt <board> <task_id> <thread_id> <message_id>
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from kanban_task_threads.reconcile import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
