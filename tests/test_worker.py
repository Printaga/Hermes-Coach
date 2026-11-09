"""The worker refuses new work when the in-flight review is overdue."""

import threading
import time

from coach.worker import Worker


def test_submit_refuses_when_the_in_flight_review_is_overdue():
    started = threading.Event()
    release = threading.Event()

    def run(session_id, turn_id):
        started.set()
        release.wait(5)

    worker = Worker(run, timeout=0.01)
    try:
        assert worker.submit("s", "t1") is True
        assert started.wait(2)
        time.sleep(0.05)
        assert worker.submit("s", "t2") is False
    finally:
        release.set()
        assert worker.drain(5)


def test_submit_queues_when_the_in_flight_review_is_within_budget():
    started = threading.Event()
    release = threading.Event()

    def run(session_id, turn_id):
        started.set()
        release.wait(5)

    worker = Worker(run, timeout=30)
    try:
        assert worker.submit("s", "t1") is True
        assert started.wait(2)
        assert worker.submit("s", "t2") is True
    finally:
        release.set()
        assert worker.drain(5)


def test_an_overdue_review_does_not_refuse_its_own_turn():
    started = threading.Event()
    release = threading.Event()

    def run(session_id, turn_id):
        started.set()
        release.wait(5)

    worker = Worker(run, timeout=0.01)
    try:
        assert worker.submit("s", "t1") is True
        assert started.wait(2)
        time.sleep(0.05)
        assert worker.submit("s", "t1") is True
    finally:
        release.set()
        assert worker.drain(5)
