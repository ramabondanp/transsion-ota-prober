"""M4 fix — drain must run after Ctrl-C / stop_event, with sessions still alive.

The bug originally caught this audit was a race where a worker that finishes
after the executor "completes" could append to `ctx.pending_notifications`
after the drain took its snapshot. The fix is sequencing: drain must run
AFTER `executor.shutdown(wait=True)` so workers are dead before the snapshot.

The Ctrl-C / KeyboardInterrupt edge case is harder: the signal handler AND
the `except KeyboardInterrupt` arm in main() both set `stop_event`. The drain
helper checks stop_event between iterations and aborts with 130 if set.

Resolution: `cli.main`'s outer `finally` clears `stop_event` after workers are
stopped and before calling `drain_pending_notifications`. Workers are already
dead (executor.shutdown(wait=True) blocked), so clearing is safe -- it cannot
resurrect them, it only tells the drain to proceed. Sessions are still alive
(ctx.stop() runs after drain).
"""

import argparse
import threading
import typing

from checkota.constants import EMERGENCY_DRAIN_MAX_SENDS
from checkota.runtime import RunContext


def _ctx(tmp_path):
    return RunContext(
        env={},
        processed_path=tmp_path / "processed_updates.txt",
        processed_titles=set(),
        dry_run=False,
    )


def test_m4_drain_completes_after_stop_event_set(tmp_path, monkeypatch, capsys):
    """Simulate the cli.main flow after a Ctrl-C interrupt.

    Sequence:
      1. cli.main installs signal handler, starts watchdog, runs pool
      2. SIGINT arrives -> signal handler sets stop_event, raises KeyboardInterrupt
      3. main's `except KeyboardInterrupt` sets stop_event (already set)
      4. main's `finally` runs executor.shutdown(wait=True)
      5. main's `finally` clears stop_event (workers dead, can't come back)
      6. main's `finally` runs drain_pending_notifications -> should succeed
      7. main's `finally` closes sessions

    We exercise step 6 in isolation but assert that drain sees the cleared
    event and sends both buffered notifications.
    """
    from checkota import processor
    from checkota.models import PendingNotification

    ctx = _ctx(tmp_path)
    # Step 2+3 effect: stop_event is set.
    ctx.stop_event.set()
    ctx.pending_notifications.append(
        PendingNotification(
            msg="<b>A</b>", device_title="A", title="alpha", is_new_update=True
        )
    )
    ctx.pending_notifications.append(
        PendingNotification(
            msg="<b>B</b>", device_title="B", title="beta", is_new_update=True
        )
    )

    sent: list[str] = []

    class _StubNotifier:
        def send(self, msg, truncate_desc=True, device_title=None):
            assert device_title is not None
            sent.append(device_title)
            return True

    monkeypatch.setattr(processor, "create_notifier", lambda c, a: _StubNotifier())
    monkeypatch.setattr(processor, "SWEEP_TELEGRAM_DELAY", 0)

    args = argparse.Namespace(skip_telegram=False, register_update=False, dry_run=False)
    # Step 5: cli.main clears stop_event before drain (workers already stopped).
    ctx.stop_event.clear()
    rc = processor.drain_pending_notifications(ctx, args)
    assert rc == 0, f"Expected drain to succeed, got rc={rc}"
    assert sent == ["A", "B"], f"Expected both notifications sent, got {sent}"


def test_m4_drain_aborts_on_second_interrupt(tmp_path, monkeypatch, capsys):
    """A second Ctrl-C during the inter-send wait must still abort cleanly."""
    from checkota import processor
    from checkota.models import PendingNotification

    ctx = _ctx(tmp_path)
    ctx.pending_notifications.extend(
        [
            PendingNotification(
                msg="<b>X</b>", device_title="X", title="x", is_new_update=True
            ),
            PendingNotification(
                msg="<b>Y</b>", device_title="Y", title="y", is_new_update=True
            ),
        ]
    )

    class _StubNotifier:
        def send(self, msg, truncate_desc=True, device_title=None):
            return True

    monkeypatch.setattr(processor, "create_notifier", lambda c, a: _StubNotifier())
    monkeypatch.setattr(processor, "SWEEP_TELEGRAM_DELAY", 5)

    args = argparse.Namespace(skip_telegram=False, register_update=False, dry_run=False)
    # Step 5 of the main path: clear once.
    ctx.stop_event.clear()

    # Step 6 alt: a SECOND interrupt during the wait must abort drain with 130.
    def _interrupt():
        ctx.stop_event.set()

    threading.Timer(0.1, _interrupt).start()
    rc = processor.drain_pending_notifications(ctx, args)
    assert rc == 130, f"Expected 130 on second interrupt during wait, got {rc}"


def test_signal_handler_only_sets_stop_event(monkeypatch):
    """The signal handler must NOT call ctx.stop() (which would close sessions).

    Closing sessions before drain means `create_notifier(ctx, args)` from inside
    drain gets a closed session and the notification fails.
    """
    captured = []

    class _FakeEvent:
        def __init__(self):
            self._set = False

        def set(self):
            self._set = True
            captured.append("event_set")

        def is_set(self):
            return self._set

    class _FakeStop:
        def __call__(self):
            captured.append("stop_called")

    class _CtxStub:
        stop_event = _FakeEvent()
        stop = _FakeStop()

    from checkota.runtime import install_interrupt_handler

    monkeypatch.setattr("signal.signal", lambda sig, handler: None)
    install_interrupt_handler(_CtxStub())  # type: ignore[arg-type]

    # Find the installed handler and call it directly.

    # We can't easily recover the handler from monkeypatched signal.signal,
    # but we can re-derive the behaviour by reading install_interrupt_handler's
    # source: it should ONLY call stop_event.set(); not ctx.stop().
    # Re-check by inspecting the runtime source.
    import inspect

    from checkota import runtime as _rt

    src = inspect.getsource(_rt.install_interrupt_handler)
    assert "ctx.stop_event.set()" in src, "Signal handler must set stop_event"
    assert "ctx.stop()" not in src, (
        "Signal handler must NOT call ctx.stop() -- that closes sessions "
        "and the drain would fail."
    )


def test_watchdog_only_signals_and_hard_exits(monkeypatch):
    from checkota import runtime

    calls = []

    class _Event:
        def set(self):
            calls.append("event_set")

    class _Ctx:
        stop_event = _Event()
        # Nothing buffered: the emergency notification drain must be a no-op.
        pending_notifications: typing.ClassVar[list[object]] = []
        pending_lock = threading.Lock()
        cli_args = None
        drain_lock = threading.Lock()

        def stop(self):
            calls.append("stop_called")

    class _Timer:
        def __init__(self, timeout, callback):
            self.callback = callback

        def start(self):
            pass

    monkeypatch.setattr(runtime.threading, "Timer", _Timer)
    monkeypatch.setattr(runtime.os, "_exit", lambda code: calls.append(("exit", code)))

    class _Stream:
        def __init__(self, name):
            self._name = name

        def flush(self):
            calls.append(f"flush_{self._name}")

    monkeypatch.setattr(runtime.sys, "stdout", _Stream("stdout"))
    monkeypatch.setattr(runtime.sys, "stderr", _Stream("stderr"))
    monkeypatch.setattr(
        runtime.Log,
        "e",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("watchdog must not log before hard exit")
        ),
    )

    watchdog = runtime.start_watchdog(_Ctx(), 1)  # type: ignore[arg-type]
    watchdog.callback()  # type: ignore[union-attr,attr-defined]

    # Buffered stdio must be flushed before the hard exit or piped output is
    # lost; the flush runs again after the (no-op) emergency drain.
    assert calls == [
        "event_set",
        "flush_stdout",
        "flush_stderr",
        "flush_stdout",
        "flush_stderr",
        ("exit", 124),
    ]


def test_watchdog_exits_even_when_stdio_flush_fails(monkeypatch):
    from checkota import runtime

    calls = []

    class _Event:
        def set(self):
            calls.append("event_set")

    class _Ctx:
        stop_event = _Event()
        pending_notifications: typing.ClassVar[list[object]] = []
        pending_lock = threading.Lock()
        cli_args = None
        drain_lock = threading.Lock()

    class _Timer:
        def __init__(self, timeout, callback):
            self.callback = callback

        def start(self):
            pass

    class _BrokenStream:
        def flush(self):
            calls.append("flush_failed")
            raise BrokenPipeError("closed pipe")

    monkeypatch.setattr(runtime.threading, "Timer", _Timer)
    monkeypatch.setattr(runtime.os, "_exit", lambda code: calls.append(("exit", code)))
    monkeypatch.setattr(runtime.sys, "stdout", _BrokenStream())
    monkeypatch.setattr(runtime.sys, "stderr", _BrokenStream())

    watchdog = runtime.start_watchdog(_Ctx(), 1)  # type: ignore[arg-type]
    watchdog.callback()  # type: ignore[union-attr,attr-defined]

    assert calls == [
        "event_set",
        "flush_failed",
        "flush_failed",
        "flush_failed",
        "flush_failed",
        ("exit", 124),
    ]


def test_watchdog_emergency_drains_before_hard_exit(monkeypatch):
    """Timeout mid-sweep must flush buffered notifications, not discard them.

    A one-shot cron run that finds updates but then hits --timeout on a stuck
    socket used to lose every buffered notification; now the watchdog performs
    a bounded best-effort drain between the stdio flushes and the hard exit.
    """
    from checkota import runtime

    calls = []

    class _Event:
        def set(self):
            calls.append("event_set")

        def clear(self):
            pass

        def is_set(self):
            return True

    class _Lock:
        def acquire(self, blocking=True):
            return True

        def release(self):
            pass

    class _Ctx:
        stop_event = _Event()
        pending_notifications: typing.ClassVar[list[object]] = [object()]
        pending_lock = threading.Lock()
        cli_args = argparse.Namespace(dry_run=False)
        drain_lock = _Lock()

    class _Timer:
        def __init__(self, timeout, callback):
            self.callback = callback

        def start(self):
            pass

    drained = []

    def _fake_drain(ctx, args, *, max_sends=None):
        drained.append((ctx, args, max_sends))

    monkeypatch.setattr(runtime.threading, "Timer", _Timer)
    monkeypatch.setattr(runtime.os, "_exit", lambda code: calls.append(("exit", code)))
    monkeypatch.setattr(
        runtime.sys,
        "stdout",
        type("_S", (), {"flush": staticmethod(lambda: calls.append("flush_stdout"))})(),
    )
    monkeypatch.setattr(
        runtime.sys,
        "stderr",
        type("_S", (), {"flush": staticmethod(lambda: calls.append("flush_stderr"))})(),
    )
    monkeypatch.setattr("checkota.processor.drain_pending_notifications", _fake_drain)

    ctx = _Ctx()
    watchdog = runtime.start_watchdog(ctx, 1)  # type: ignore[arg-type]
    watchdog.callback()  # type: ignore[union-attr,attr-defined]

    assert len(drained) == 1
    assert drained[0][0] is ctx
    assert drained[0][2] == EMERGENCY_DRAIN_MAX_SENDS
    # Emergency drain runs between the stdio flushes; stop_event is re-armed
    # afterwards so the process still exits via the same hard-exit path.
    assert calls == [
        "event_set",
        "flush_stdout",
        "flush_stderr",
        "event_set",
        "flush_stdout",
        "flush_stderr",
        ("exit", 124),
    ]
    assert ctx.stop_event.is_set()
