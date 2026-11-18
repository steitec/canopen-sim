"""Shared test helpers."""
import time


def wait_until(pred, timeout=2.0, interval=0.01):
    """Poll ``pred`` until it returns truthy; raise AssertionError on timeout."""
    deadline = time.monotonic() + timeout
    while True:
        if pred():
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        time.sleep(interval)
