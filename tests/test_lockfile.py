"""StoreLock serialises a store transaction across processes too."""

import multiprocessing
import sys
import time

import pytest

from coach.lockfile import StoreLock


def _hold(path: str, seconds: float, ready, done) -> None:
    with StoreLock(path):
        ready.set()
        time.sleep(seconds)
    done.set()


def test_nested_acquisition_does_not_deadlock(tmp_path):
    lock = StoreLock(tmp_path / ".lock")
    with lock:
        with lock:
            pass


def test_the_lock_file_is_created(tmp_path):
    with StoreLock(tmp_path / ".lock"):
        pass
    assert (tmp_path / ".lock").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="uses the POSIX fork start method")
def test_a_second_process_waits_for_the_lock(tmp_path):
    path = str(tmp_path / ".lock")
    ready, done = multiprocessing.Event(), multiprocessing.Event()
    child = multiprocessing.Process(target=_hold, args=(path, 0.3, ready, done))
    child.start()
    try:
        assert ready.wait(5)
        started = time.monotonic()
        with StoreLock(path):
            waited = time.monotonic() - started
        assert waited >= 0.2
    finally:
        child.join(5)
        if child.is_alive():  # pragma: no cover - a hung child fails the test
            child.terminate()
            child.join(5)
