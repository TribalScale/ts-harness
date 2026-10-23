"""One turn per session across runtime replicas: the session store's advisory lock."""

from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from tests.pg_support import PgTestCase, requires_pg

from ts_harness.agent.sessions import SessionStore


@requires_pg
class SessionAdvisoryLockTests(PgTestCase):
    def store(self) -> SessionStore:
        # A separate store per thread stands in for a separate runtime replica.
        return SessionStore(db_url=self.db.url)

    def test_second_replica_waits_for_the_first_turn(self):
        order: list[str] = []
        first_in = threading.Event()

        def first():
            with self.store().turn_lock("s-1"):
                order.append("first-start")
                first_in.set()
                time.sleep(0.3)
                order.append("first-end")

        def second():
            first_in.wait(5)
            with self.store().turn_lock("s-1"):
                order.append("second")

        threads = [threading.Thread(target=first), threading.Thread(target=second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(order, ["first-start", "first-end", "second"])

    def test_different_sessions_do_not_wait(self):
        with self.store().turn_lock("s-1"):
            started = time.monotonic()
            with self.store().turn_lock("s-2"):
                pass
            self.assertLess(time.monotonic() - started, 1)

    def test_released_when_the_turn_raises(self):
        with self.assertRaises(ValueError):
            with self.store().turn_lock("s-1"):
                raise ValueError("turn failed")
        with patch.dict("os.environ", {"SESSION_LOCK_WAIT_SECONDS": "1"}):
            with self.store().turn_lock("s-1"):
                pass

    def test_gives_up_after_the_wait(self):
        import psycopg

        with self.store().turn_lock("s-1"):
            with patch.dict("os.environ", {"SESSION_LOCK_WAIT_SECONDS": "0.2"}):
                with self.assertRaises(psycopg.errors.LockNotAvailable):
                    with self.store().turn_lock("s-1"):
                        self.fail("entered a held session")


class SessionLockFallbackTests(unittest.TestCase):
    def test_without_a_database_the_lock_is_a_no_op(self):
        with SessionStore().turn_lock("s-1"):
            pass

    def test_unreachable_database_falls_back_to_the_process_lock(self):
        store = SessionStore(db_url="postgresql://nobody@127.0.0.1:1/none")
        with store.turn_lock("s-1"):
            pass

    def test_runtime_takes_the_process_and_store_locks(self):
        from ts_harness.gateway.server import RuntimeState

        entered: list[str] = []

        class Store:
            def turn_lock(self, session_id):
                from contextlib import contextmanager

                @contextmanager
                def held():
                    entered.append(session_id)
                    yield

                return held()

        runtime = RuntimeState.__new__(RuntimeState)
        runtime._session_locks = {}
        runtime._session_locks_guard = threading.Lock()
        with patch("ts_harness.gateway.server.get_session_store", return_value=Store()):
            with runtime._session_lock("s-1"):
                self.assertTrue(runtime._session_locks["s-1"].locked())
        self.assertEqual(entered, ["s-1"])
        self.assertFalse(runtime._session_locks["s-1"].locked())


if __name__ == "__main__":
    unittest.main()
