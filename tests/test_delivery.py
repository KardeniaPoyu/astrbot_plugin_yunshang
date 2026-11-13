"""Real SQLite tests; all identifiers and payloads are synthetic."""

import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone

from delivery import DeliveryStore, completed_window


SHANGHAI = timezone(timedelta(hours=8))
BOOT = datetime(2026, 11, 13, 9, 0, tzinfo=SHANGHAI)
START = datetime(2026, 11, 13, 8, 0, tzinfo=SHANGHAI)
END = datetime(2026, 11, 13, 12, 0, tzinfo=SHANGHAI)
KEY = "2026-11-13T12:00:00+08:00"


def step(target="test-group-a", page=1):
    return {"target": target, "page": page,
            "payload": {"text": "synthetic page", "page": page}}


class WindowTests(unittest.TestCase):
    def test_exact_boundary_closes_preceding_window(self):
        start, end = completed_window(END)
        self.assertEqual(start.isoformat(), "2026-11-13T08:00:00+08:00")
        self.assertEqual(end.isoformat(), KEY)

    def test_before_boundary_excludes_still_open_window(self):
        start, end = completed_window(END - timedelta(microseconds=1))
        self.assertEqual(start.isoformat(), "2026-11-13T04:00:00+08:00")
        self.assertEqual(end.isoformat(), "2026-11-13T08:00:00+08:00")

    def test_all_six_daily_boundaries(self):
        for hour in (0, 4, 8, 12, 16, 20):
            with self.subTest(hour=hour):
                boundary = END.replace(hour=hour)
                start, end = completed_window(boundary)
                self.assertEqual(end, boundary)
                self.assertEqual(start, boundary - timedelta(hours=4))

    def test_utc_and_negative_offset_normalize_to_shanghai(self):
        for now in (datetime(2026, 11, 13, 4, 17, tzinfo=timezone.utc),
                    datetime(2026, 11, 12, 20, 17,
                             tzinfo=timezone(timedelta(hours=-8)))):
            with self.subTest(now=now):
                start, end = completed_window(now)
                self.assertEqual(start.isoformat(), "2026-11-13T08:00:00+08:00")
                self.assertEqual(end.isoformat(), KEY)

    def test_midnight_rolls_back_date(self):
        start, end = completed_window(END.replace(hour=0))
        self.assertEqual(start.isoformat(), "2026-11-12T20:00:00+08:00")
        self.assertEqual(end.isoformat(), "2026-11-13T00:00:00+08:00")

    def test_other_divisors_use_fixed_midnight_anchor(self):
        for interval, start_hour, end_hour in ((1, 11, 12), (3, 9, 12), (6, 6, 12)):
            with self.subTest(interval=interval):
                start, end = completed_window(END, interval)
                self.assertEqual((start.hour, end.hour), (start_hour, end_hour))
        start, end = completed_window(END, 24)
        self.assertEqual(start.isoformat(), "2026-11-12T00:00:00+08:00")
        self.assertEqual(end.isoformat(), "2026-11-13T00:00:00+08:00")

    def test_invalid_intervals_are_rejected(self):
        for interval in (0, -4, 5, 25, 1.5, 4.0, True, "4", None):
            with self.subTest(interval=interval):
                with self.assertRaises(ValueError):
                    completed_window(END, interval)

    def test_naive_input_is_rejected(self):
        with self.assertRaises(ValueError):
            completed_window(datetime(2026, 11, 13, 12))


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "ledger.sqlite3"
        self.store = self.open_store()
        self.store.bootstrap(BOOT)

    def open_store(self):
        store = DeliveryStore(self.path)
        self.addCleanup(store.close)
        return store

    def prepare(self, steps=None, now=END):
        ticket = self.store.claim_window(START, END, now)
        self.assertIsNotNone(ticket)
        self.assertTrue(self.store.finish_window(
            ticket, {"title": "synthetic digest"}, [step()] if steps is None else steps))
        return ticket

    def test_first_boot_skips_current_closed_window_and_all_history(self):
        self.assertIsNone(self.store.claim_window(
            START - timedelta(hours=4), START, BOOT))
        self.assertIsNone(self.store.claim_window(
            START - timedelta(hours=8), START - timedelta(hours=4), BOOT))
        status = self.store.status()
        self.assertEqual(status["baseline"], "2026-11-13T08:00:00+08:00")
        self.assertIsNone(status["latest_window"])
        self.assertEqual(sum(status["counts"]["windows"].values()), 0)
        self.assertIsNotNone(self.store.claim_window(START, END, END))

    def test_bootstrap_does_not_create_candidates_or_move_persistent_baseline(self):
        self.store.close()
        restarted = self.open_store()
        restarted.bootstrap(END + timedelta(days=7))
        self.assertEqual(restarted.status()["baseline"], "2026-11-13T08:00:00+08:00")
        self.assertEqual(sum(restarted.status()["counts"]["windows"].values()), 0)
        self.assertIsNotNone(restarted.claim_window(START, END, END))

    def test_uninitialized_store_requires_bootstrap(self):
        fresh = DeliveryStore(Path(self.temp.name) / "fresh.sqlite3")
        self.addCleanup(fresh.close)
        with self.assertRaises(RuntimeError):
            fresh.claim_window(START, END, END)

    def test_restart_only_stores_explicit_latest_candidate(self):
        now = END + timedelta(days=10)
        latest_start, latest_end = completed_window(now)
        self.store.bootstrap(now)
        ticket = self.store.claim_window(latest_start, latest_end, now)
        self.assertIsNotNone(ticket)
        self.assertEqual(sum(self.store.status()["counts"]["windows"].values()), 1)

    def test_future_window_is_not_stored(self):
        self.assertIsNone(self.store.claim_window(START, END, END - timedelta(seconds=1)))
        self.assertIsNone(self.store.status()["latest_window"])

    def test_window_keys_deduplicate_equivalent_offsets(self):
        ticket = self.store.claim_window(START, END, END)
        self.assertEqual(ticket["window_key"], KEY)
        self.assertIsNone(self.store.claim_window(
            START.astimezone(timezone.utc), END.astimezone(timezone.utc), END))

    def test_all_datetime_entrypoints_reject_naive_inputs(self):
        naive = END.replace(tzinfo=None)
        ticket = self.store.claim_window(START, END, END)
        for call in (lambda: self.store.bootstrap(naive),
                     lambda: self.store.claim_window(naive, END, END),
                     lambda: self.store.claim_window(START, naive, END),
                     lambda: self.store.claim_window(START, END, naive),
                     lambda: self.store.claim_delivery(naive),
                     lambda: self.store.fail_window(ticket, naive, "TimeoutError"),
                     lambda: self.store.fail_delivery({}, naive, "TimeoutError")):
            with self.subTest(call=call):
                with self.assertRaises(ValueError):
                    call()

    def test_reversed_or_conflicting_window_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.claim_window(END, START, END)
        self.store.claim_window(START, END, END)
        with self.assertRaises(ValueError):
            self.store.claim_window(START - timedelta(hours=4), END, END)

    def test_prepared_digest_and_steps_survive_restart_without_repreparing(self):
        digest = {"title": "合成数据", "items": [{"id": "test-id"}]}
        ticket = self.store.claim_window(START, END, END)
        self.assertTrue(self.store.finish_window(ticket, digest, [step()]))
        self.store.close()
        restarted = self.open_store()
        restarted.bootstrap(END)
        self.assertIsNone(restarted.claim_window(START, END, END))
        delivery = restarted.claim_delivery(END)
        self.assertEqual(delivery["payload"], step()["payload"])
        self.assertEqual(delivery["window_key"], KEY)
        self.assertEqual(delivery["target"], "test-group-a")
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            saved = db.execute("SELECT digest FROM windows WHERE window_key = ?", (KEY,)).fetchone()[0]
        self.assertEqual(json.loads(saved), digest)

    def test_empty_batch_completes_once_without_deliveries(self):
        ticket = self.prepare([])
        self.assertIsNone(self.store.claim_delivery(END))
        self.assertIsNone(self.store.claim_window(START, END, END + timedelta(days=1)))
        self.assertFalse(self.store.finish_window(ticket, {}, [step()]))
        self.assertEqual(self.store.status()["counts"]["windows"]["completed"], 1)

    def test_preparation_failure_is_retryable_and_not_success(self):
        ticket = self.store.claim_window(START, END, END)
        self.assertTrue(self.store.fail_window(ticket, END, "TimeoutError"))
        self.assertIsNone(self.store.claim_delivery(END))
        self.assertFalse(self.store.finish_window(ticket, {}, [step()]))
        self.assertEqual(self.store.status()["counts"]["windows"]["completed"], 0)
        self.assertIsNone(self.store.claim_window(START, END, END + timedelta(seconds=59)))
        retry = self.store.claim_window(START, END, END + timedelta(seconds=60))
        self.assertEqual(retry["attempts"], 2)
        self.assertTrue(self.store.finish_window(retry, {}, [step()]))

    def test_rollover_retires_older_preparation_retry_before_backoff_is_due(self):
        newer_end = END + timedelta(hours=4)
        failure_time = newer_end - timedelta(seconds=10)
        old = self.store.claim_window(START, END, failure_time)
        self.assertTrue(self.store.fail_window(old, failure_time, "TimeoutError"))
        newer = self.store.claim_window(END, newer_end, newer_end)
        self.assertIsNotNone(newer)
        self.assertEqual(self.store.status()["errors"], {"WindowSuperseded": 1})
        self.assertEqual(self.store.status()["counts"]["windows"]["failed"], 1)
        self.store.close()
        restarted = self.open_store()
        self.assertIsNone(restarted.claim_window(START, END, newer_end + timedelta(days=1)))
        self.assertFalse(restarted.finish_window(old, {}, [step()]))
        self.assertTrue(restarted.finish_window(newer, {}, []))
        self.assertIsNone(restarted.claim_delivery(newer_end + timedelta(days=1)))

    def test_rollover_does_not_fence_an_older_active_preparation(self):
        newer_end = END + timedelta(hours=4)
        old = self.store.claim_window(START, END, newer_end - timedelta(seconds=30))
        other = self.open_store()
        newer = other.claim_window(END, newer_end, newer_end)
        self.assertTrue(other.finish_window(newer, {}, []))
        self.assertTrue(self.store.finish_window(old, {}, [step()]))
        self.assertEqual(self.store.status()["errors"], {})
        job = other.claim_delivery(newer_end + timedelta(seconds=1))
        self.assertEqual(job["window_key"], KEY)
        self.assertTrue(other.finish_delivery(job))

    def test_rollover_retires_expired_older_lease_even_if_latest_is_completed(self):
        newer_end = END + timedelta(hours=4)
        old = self.store.claim_window(START, END, newer_end - timedelta(seconds=30))
        newer = self.store.claim_window(END, newer_end, newer_end)
        self.assertTrue(self.store.finish_window(newer, {}, []))
        self.assertIsNone(self.store.claim_window(END, newer_end, newer_end + timedelta(seconds=149)))
        self.assertEqual(self.store.status()["counts"]["windows"]["preparing"], 1)
        self.assertIsNone(self.store.claim_window(END, newer_end, newer_end + timedelta(seconds=150)))
        self.assertEqual(self.store.status()["errors"], {"WindowSuperseded": 1})
        self.assertEqual(self.store.status()["counts"]["windows"]["preparing"], 0)
        self.assertFalse(self.store.finish_window(old, {}, [step()]))
        self.assertFalse(self.store.fail_window(old, newer_end + timedelta(seconds=151), "OldError"))
        self.assertIsNone(self.store.claim_window(START, END, newer_end + timedelta(days=1)))

    def test_rollover_ignores_future_candidate_and_preserves_same_window_retry(self):
        old = self.store.claim_window(START, END, END)
        self.assertTrue(self.store.fail_window(old, END, "TimeoutError"))
        self.assertIsNone(self.store.claim_window(END, END + timedelta(hours=4), END))
        self.assertEqual(self.store.status()["errors"], {"TimeoutError": 1})
        retry = self.store.claim_window(START, END, END + timedelta(seconds=60))
        self.assertEqual(retry["attempts"], 2)
        self.assertTrue(self.store.finish_window(retry, {}, []))

    def test_preparation_stops_after_three_attempts_with_backoff(self):
        now = END
        for attempt, delay in ((1, 60), (2, 300), (3, 1200)):
            ticket = self.store.claim_window(START, END, now)
            self.assertEqual(ticket["attempts"], attempt)
            self.assertTrue(self.store.fail_window(ticket, now, "PrepareError"))
            self.assertIsNone(self.store.claim_window(START, END, now + timedelta(seconds=delay - 1)))
            now += timedelta(seconds=delay)
        self.assertIsNone(self.store.claim_window(START, END, now + timedelta(days=100)))
        self.assertEqual(self.store.status()["counts"]["windows"]["failed"], 1)
        self.assertIsNone(self.store.claim_delivery(now))

    def test_window_lease_survives_restart_and_fences_stale_token(self):
        stale = self.store.claim_window(START, END, END)
        other = self.open_store()
        self.assertIsNone(other.claim_window(START, END, END + timedelta(seconds=179)))
        current = other.claim_window(START, END, END + timedelta(seconds=180))
        self.assertEqual(current["attempts"], 2)
        self.assertNotEqual(current["token"], stale["token"])
        self.assertFalse(self.store.finish_window(stale, {}, [step()]))
        self.assertFalse(self.store.fail_window(stale, END + timedelta(seconds=181), "OldError"))
        self.assertTrue(other.finish_window(current, {}, [step()]))

    def test_crashed_preparations_are_bounded_too(self):
        for attempt in range(3):
            ticket = self.store.claim_window(START, END, END + timedelta(seconds=180 * attempt))
            self.assertEqual(ticket["attempts"], attempt + 1)
        self.assertIsNone(self.store.claim_window(START, END, END + timedelta(seconds=540)))
        self.assertFalse(self.store.finish_window(ticket, {}, [step()]))
        self.assertEqual(self.store.status()["counts"]["windows"]["failed"], 1)

    def test_invalid_preparation_is_atomic_and_can_be_corrected(self):
        ticket = self.store.claim_window(START, END, END)
        invalid_batches = ([step(), step()], [step(), step(page=-1)],
                           [step(), {"target": "", "page": 2, "payload": {}}],
                           [step(), {"target": "b", "page": True, "payload": {}}],
                           [step(), {"target": "b", "page": 2, "payload": object()}])
        for batch in invalid_batches:
            with self.subTest(batch=batch):
                with self.assertRaises((ValueError, TypeError)):
                    self.store.finish_window(ticket, {}, batch)
                self.assertIsNone(self.store.claim_delivery(END))
        with self.assertRaises((TypeError, ValueError)):
            self.store.finish_window(ticket, {"bad": float("nan")}, [step()])
        self.assertTrue(self.store.finish_window(ticket, {}, [step()]))

    def test_database_failure_rolls_back_digest_and_every_step(self):
        ticket = self.store.claim_window(START, END, END)
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            db.execute("""CREATE TRIGGER fail_second_step BEFORE INSERT ON deliveries
                          WHEN NEW.page = 2 BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.finish_window(ticket, {"title": "not committed"}, [step(), step(page=2)])
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM deliveries").fetchone()[0], 0)
            self.assertIsNone(db.execute("SELECT digest FROM windows").fetchone()[0])
            db.execute("DROP TRIGGER fail_second_step")
        self.assertTrue(self.store.finish_window(ticket, {}, [step()]))

    def test_three_targets_partial_success_retries_only_failed_target(self):
        self.prepare([step("test-group-a"), step("test-group-b"), step("test-group-c")])
        successes = set()
        for _ in range(3):
            ticket = self.store.claim_delivery(END)
            if ticket["target"] == "test-group-b":
                self.assertTrue(self.store.fail_delivery(ticket, END, "TimeoutError"))
            else:
                successes.add(ticket["target"])
                self.assertTrue(self.store.finish_delivery(ticket))
        self.assertEqual(successes, {"test-group-a", "test-group-c"})
        self.store.close()
        restarted = self.open_store()
        self.assertIsNone(restarted.claim_delivery(END + timedelta(seconds=59)))
        retry = restarted.claim_delivery(END + timedelta(seconds=60))
        self.assertEqual(retry["target"], "test-group-b")
        self.assertTrue(restarted.finish_delivery(retry))
        self.assertFalse(restarted.finish_delivery(retry))
        self.assertIsNone(restarted.claim_delivery(END + timedelta(days=10)))
        self.assertEqual(restarted.status()["counts"]["deliveries"]["sent"], 3)
        self.assertEqual(restarted.status()["counts"]["windows"]["completed"], 1)

    def test_pages_are_ordered_per_target_and_not_by_insertion(self):
        self.prepare([step(page=2), step(page=1), step("test-group-b", 1)])
        claimed = [self.store.claim_delivery(END), self.store.claim_delivery(END)]
        self.assertEqual({(t["target"], t["page"]) for t in claimed},
                         {("test-group-a", 1), ("test-group-b", 1)})
        self.assertIsNone(self.store.claim_delivery(END))
        first = next(t for t in claimed if t["target"] == "test-group-a")
        other = next(t for t in claimed if t["target"] == "test-group-b")
        self.assertTrue(self.store.fail_delivery(first, END, "SendError"))
        self.assertTrue(self.store.finish_delivery(other))
        self.assertIsNone(self.store.claim_delivery(END + timedelta(seconds=59)))
        retry = self.store.claim_delivery(END + timedelta(seconds=60))
        self.assertEqual(retry["page"], 1)
        self.assertIsNone(self.store.claim_delivery(END + timedelta(seconds=60)))
        self.assertTrue(self.store.finish_delivery(retry))
        second = self.store.claim_delivery(END + timedelta(seconds=60))
        self.assertEqual((second["target"], second["page"]), ("test-group-a", 2))

    def test_zero_based_pages_work(self):
        self.prepare([step(page=1), step(page=0)])
        ticket = self.store.claim_delivery(END)
        self.assertEqual(ticket["page"], 0)
        self.assertIsNone(self.store.claim_delivery(END))
        self.assertTrue(self.store.finish_delivery(ticket))
        self.assertEqual(self.store.claim_delivery(END)["page"], 1)

    def test_page_two_retry_does_not_repeat_page_one(self):
        self.prepare([step(), step(page=2)])
        first = self.store.claim_delivery(END)
        self.assertTrue(self.store.finish_delivery(first))
        second = self.store.claim_delivery(END)
        self.assertTrue(self.store.fail_delivery(second, END, "SendError"))
        retry = self.store.claim_delivery(END + timedelta(seconds=60))
        self.assertEqual(retry["id"], second["id"])
        self.assertEqual(retry["page"], 2)
        self.assertEqual(retry["attempts"], 2)
        self.assertTrue(self.store.finish_delivery(retry))
        self.assertIsNone(self.store.claim_delivery(END + timedelta(days=1)))

    def test_delivery_lease_expiry_and_stale_acknowledgements(self):
        self.prepare()
        stale = self.store.claim_delivery(END)
        other = self.open_store()
        self.assertIsNone(other.claim_delivery(END + timedelta(seconds=179)))
        current = other.claim_delivery(END + timedelta(seconds=180))
        self.assertEqual(current["id"], stale["id"])
        self.assertNotEqual(current["token"], stale["token"])
        self.assertFalse(self.store.finish_delivery(stale))
        self.assertFalse(self.store.fail_delivery(stale, END + timedelta(seconds=181), "OldError"))
        self.assertTrue(other.finish_delivery(current))
        self.assertFalse(other.fail_delivery(current, END + timedelta(seconds=182), "OldError"))

    def test_terminal_page_failure_blocks_later_pages_but_not_other_targets(self):
        self.prepare([step(), step(page=2)])
        now = END
        for attempt, delay in ((1, 60), (2, 300), (3, 1200)):
            ticket = self.store.claim_delivery(now)
            self.assertEqual((ticket["page"], ticket["attempts"]), (1, attempt))
            self.assertTrue(self.store.fail_delivery(ticket, now, "SendError"))
            self.assertIsNone(self.store.claim_delivery(now + timedelta(seconds=delay - 1)))
            now += timedelta(seconds=delay)
        self.assertIsNone(self.store.claim_delivery(now + timedelta(days=100)))
        counts = self.store.status()["counts"]
        self.assertEqual(counts["deliveries"]["failed"], 1)
        self.assertEqual(counts["deliveries"]["blocked"], 1)
        self.assertEqual(counts["windows"]["failed"], 1)
        next_start, next_end = END, END + timedelta(hours=4)
        window = self.store.claim_window(next_start, next_end, next_end)
        self.assertTrue(self.store.finish_window(window, {}, [step()]))
        self.assertIsNotNone(self.store.claim_delivery(next_end))

    def test_crashed_sends_exhaust_attempts_and_unblock_other_target(self):
        self.prepare([step(), step(page=2), step("test-group-b")])
        first = self.store.claim_delivery(END)
        other = self.store.claim_delivery(END)
        self.assertEqual(other["target"], "test-group-b")
        self.assertTrue(self.store.finish_delivery(other))
        for attempt in (2, 3):
            current = self.store.claim_delivery(END + timedelta(seconds=180 * (attempt - 1)))
            self.assertEqual(current["attempts"], attempt)
        self.assertIsNone(self.store.claim_delivery(END + timedelta(seconds=540)))
        self.assertFalse(self.store.finish_delivery(first))
        self.assertFalse(self.store.finish_delivery(current))
        self.assertEqual(self.store.status()["counts"]["deliveries"]["sent"], 1)
        self.assertEqual(self.store.status()["counts"]["deliveries"]["blocked"], 1)

    def test_already_created_old_delivery_can_retry_after_newer_window(self):
        self.prepare()
        ticket = self.store.claim_delivery(END)
        self.assertTrue(self.store.fail_delivery(ticket, END, "SendError"))
        newer = END + timedelta(days=2)
        start, end = completed_window(newer)
        window = self.store.claim_window(start, end, newer)
        self.assertTrue(self.store.finish_window(window, {}, []))
        retry = self.store.claim_delivery(newer)
        self.assertEqual(retry["id"], ticket["id"])
        self.assertEqual(retry["attempts"], 2)
        self.assertEqual(self.store.status()["counts"]["windows"]["prepared"], 1)
        self.assertEqual(self.store.status()["errors"], {"SendError": 1})
        self.assertTrue(self.store.finish_delivery(retry))
        self.assertEqual(self.store.status()["counts"]["windows"]["completed"], 2)

    def test_unknown_or_forged_tickets_cannot_finish_or_release_work(self):
        window = self.prepare()
        delivery = self.store.claim_delivery(END)
        for ticket in ({}, {**window, "token": "wrong-token"}, delivery):
            self.assertFalse(self.store.finish_window(ticket, {}, []))
            self.assertFalse(self.store.fail_window(ticket, END, "Error"))
        for ticket in ({}, {**delivery, "token": "wrong-token"}, window):
            self.assertFalse(self.store.finish_delivery(ticket))
            self.assertFalse(self.store.fail_delivery(ticket, END, "Error"))
        self.assertTrue(self.store.finish_delivery(delivery))

    def test_cancel_delivery_persists_and_does_not_block_other_targets(self):
        self.prepare([step(), step(page=2), step("test-group-b")])
        removed = self.store.claim_delivery(END)
        self.assertEqual(removed["target"], "test-group-a")
        self.assertTrue(self.store.cancel_delivery(removed))
        self.assertFalse(self.store.cancel_delivery(removed))
        self.assertFalse(self.store.finish_delivery(removed))
        self.assertFalse(self.store.fail_delivery(removed, END, "TimeoutError"))
        status = self.store.status()
        self.assertEqual(status["counts"]["deliveries"]["failed"], 1)
        self.assertEqual(status["counts"]["deliveries"]["blocked"], 1)
        self.assertEqual(status["counts"]["windows"]["failed"], 1)
        self.assertEqual(status["errors"], {"TargetRemoved": 2})
        self.assertNotIn("test-group-a", json.dumps(status))
        self.assertNotIn(removed["token"], json.dumps(status))
        self.store.close()
        restarted = self.open_store()
        remaining = restarted.claim_delivery(END + timedelta(days=1))
        self.assertEqual(remaining["target"], "test-group-b")
        self.assertTrue(restarted.finish_delivery(remaining))
        self.assertIsNone(restarted.claim_delivery(END + timedelta(days=100)))
        self.assertIsNone(restarted.claim_window(START, END, END + timedelta(days=100)))

    def test_cancel_delivery_preserves_sent_pages_and_other_windows(self):
        self.prepare([step(), step(page=2), step(page=3)])
        sent = self.store.claim_delivery(END)
        self.assertTrue(self.store.finish_delivery(sent))
        self.assertFalse(self.store.cancel_delivery(sent))
        removed = self.store.claim_delivery(END)
        self.assertEqual(removed["page"], 2)
        later_end = END + timedelta(hours=4)
        later = self.store.claim_window(END, later_end, later_end)
        self.assertTrue(self.store.finish_window(later, {}, [step()]))
        # Scope comes from the persisted row, never mutable fields in a ticket.
        altered = {**removed, "target": "test-group-b", "page": 0,
                   "window_key": later_end.isoformat()}
        self.assertTrue(self.store.cancel_delivery(altered))
        self.assertEqual(self.store.status()["counts"]["deliveries"]["sent"], 1)
        self.assertEqual(self.store.status()["errors"], {"TargetRemoved": 2})
        available = self.store.claim_delivery(later_end)
        self.assertEqual(available["window_key"], later_end.isoformat())
        self.assertEqual((available["target"], available["page"]), ("test-group-a", 1))
        self.assertTrue(self.store.finish_delivery(available))
        self.assertIsNone(self.store.claim_delivery(later_end + timedelta(days=1)))

    def test_cancel_delivery_rejects_forged_and_superseded_tokens(self):
        window = self.prepare([step(), step(page=2)])
        stale = self.store.claim_delivery(END)
        for ticket in ({}, window, {**stale, "token": "wrong-token"}):
            self.assertFalse(self.store.cancel_delivery(ticket))
        other = self.open_store()
        current = other.claim_delivery(END + timedelta(seconds=180))
        self.assertFalse(self.store.cancel_delivery(stale))
        self.assertEqual(self.store.status()["counts"]["deliveries"]["sending"], 1)
        self.assertTrue(other.cancel_delivery(current))
        self.assertFalse(self.store.finish_delivery(current))
        self.assertIsNone(self.store.claim_delivery(END + timedelta(days=1)))

    def test_cancel_delivery_requires_a_current_claim_and_cancels_retries(self):
        self.prepare([step(), step(page=2)])
        failed = self.store.claim_delivery(END)
        self.assertTrue(self.store.fail_delivery(failed, END, "SendError"))
        self.assertFalse(self.store.cancel_delivery(failed))
        retry = self.store.claim_delivery(END + timedelta(seconds=60))
        self.assertEqual(retry["attempts"], 2)
        self.assertTrue(self.store.cancel_delivery(retry))
        self.assertEqual(self.store.status()["errors"], {"TargetRemoved": 2})
        self.assertIsNone(self.store.claim_delivery(END + timedelta(days=100)))

    def test_cancel_delivery_rolls_back_every_page_if_database_write_fails(self):
        self.prepare([step(), step(page=2)])
        removed = self.store.claim_delivery(END)
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            db.execute("""CREATE TRIGGER fail_cancel BEFORE UPDATE ON deliveries
                WHEN NEW.page = 2 AND NEW.error_type = 'TargetRemoved'
                BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.cancel_delivery(removed)
        self.assertEqual(self.store.status()["counts"]["windows"]["prepared"], 1)
        self.assertEqual(self.store.status()["counts"]["deliveries"]["sending"], 1)
        self.assertEqual(self.store.status()["counts"]["deliveries"]["pending"], 1)
        self.assertEqual(self.store.status()["errors"], {})
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            db.execute("DROP TRIGGER fail_cancel")
        self.assertTrue(self.store.cancel_delivery(removed))
        self.assertEqual(self.store.status()["errors"], {"TargetRemoved": 2})
        self.assertIsNone(self.store.claim_delivery(END + timedelta(days=1)))

    def test_status_contains_only_safe_primitives_and_error_types(self):
        secret = "PRIVATE text with Cookie=synthetic-secret"
        window = self.store.claim_window(START, END, END)
        self.assertTrue(self.store.finish_window(window, {"private": secret},
            [{"target": "private-target-id", "page": 1, "payload": {"private": secret}}]))
        ticket = self.store.claim_delivery(END)
        self.assertTrue(self.store.fail_delivery(ticket, END, TimeoutError(secret)))
        status = self.store.status()
        serialized = json.dumps(status, allow_nan=False)
        for value in (secret, "private-target-id", ticket["token"], str(self.path), "Cookie"):
            self.assertNotIn(value, serialized)
        self.assertEqual(status["latest_window"], KEY)
        self.assertEqual(status["errors"], {"TimeoutError": 1})
        retry = self.store.claim_delivery(END + timedelta(seconds=60))
        self.assertTrue(self.store.fail_delivery(retry, END + timedelta(seconds=60), secret))
        self.assertNotIn(secret, json.dumps(self.store.status()))

    def test_two_connections_atomically_claim_each_window_and_delivery_once(self):
        def race(operation):
            barrier = threading.Barrier(2)
            def worker():
                store = DeliveryStore(self.path)
                try:
                    barrier.wait(timeout=10)
                    return operation(store)
                finally:
                    store.close()
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(worker) for _ in range(2)]
                return [future.result(timeout=15) for future in futures]
        claimed = race(lambda store: store.claim_window(START, END, END))
        winners = [ticket for ticket in claimed if ticket is not None]
        self.assertEqual(len(winners), 1)
        self.assertTrue(self.store.finish_window(winners[0], {}, [step()]))
        claimed = race(lambda store: store.claim_delivery(END))
        winners = [ticket for ticket in claimed if ticket is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0]["attempts"], 1)
        self.assertTrue(self.store.finish_delivery(winners[0]))

    def test_two_connections_preserve_first_bootstrap(self):
        other = self.open_store()
        other.bootstrap(END + timedelta(days=1))
        self.assertEqual(other.status()["baseline"], "2026-11-13T08:00:00+08:00")

    def test_wal_and_private_file_permissions(self):
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
