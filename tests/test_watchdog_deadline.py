"""Exercise the actual hard exit in a child, including stuck cleanup paths."""

import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest


@pytest.mark.parametrize(
    "blocked", ["pending_lock", "file_lock", "telegram_lock", "send", "flush"]
)
def test_watchdog_hard_exit_bounds_blocked_cleanup(tmp_path, blocked):
    script = dedent(
        """\
        import argparse
        import sys
        import threading
        from pathlib import Path
        from checkota import processor, runtime
        from checkota.models import PendingNotification

        blocked, state = sys.argv[1:]
        never = threading.Event()
        ctx = runtime.RunContext(
            env={}, processed_path=Path(state), processed_titles=set(), dry_run=False
        )
        ctx.cli_args = argparse.Namespace(dry_run=False)
        ctx.pending_notifications.append(PendingNotification(
            msg="message", device_title="Device", title="TITLE", is_new_update=True
        ))

        class Notifier:
            def send(self, *args, **kwargs):
                if blocked == "send":
                    never.wait()
                return True

        processor.create_notifier = lambda *args: Notifier()
        if blocked.endswith("_lock"):
            getattr(ctx, blocked).acquire()
        if blocked == "flush":
            class Stream:
                def flush(self):
                    never.wait()
            sys.stdout = Stream()

        runtime.EMERGENCY_DRAIN_DEADLINE_SECONDS = 0.05
        runtime.start_watchdog(ctx, 0.01)
        never.wait()
        """
    )
    # A subprocess timeout is a test failure, not a leaked/hanging test worker.
    result = subprocess.run(
        [sys.executable, "-c", script, blocked, str(tmp_path / "titles")],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        check=False,  # Exit 124, rather than zero, is the expected result.
        timeout=5,
    )
    assert result.returncode == 124, result.stderr.decode(errors="replace")
