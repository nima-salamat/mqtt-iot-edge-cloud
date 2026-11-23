"""Unit tests for the adaptive multi-process YOLO scheduler."""
from __future__ import annotations

import unittest

from pi_device.inference_pool import AdaptiveWorkerScheduler


class AdaptiveWorkerSchedulerTests(unittest.TestCase):
    def test_busy_worker_is_skipped_and_work_goes_to_other_worker(self):
        scheduler = AdaptiveWorkerScheduler(2, initial_latency_seconds=0.25)

        first = scheduler.choose([0, 1])
        self.assertEqual(first, 0)
        scheduler.mark_assigned(first)

        second = scheduler.choose([0, 1])
        self.assertEqual(second, 1)
        scheduler.mark_assigned(second)

        self.assertIsNone(scheduler.choose([0, 1]))
        self.assertEqual(scheduler.snapshot()["busy_workers"], 2)

    def test_latency_estimate_is_updated_after_every_completion(self):
        scheduler = AdaptiveWorkerScheduler(2, initial_latency_seconds=0.25)
        scheduler.mark_assigned(0)
        scheduler.mark_completed(0, 0.10)

        # EWMA alpha=0.25: 25% new sample + 75% previous estimate.
        self.assertAlmostEqual(scheduler.workers[0].latency_seconds, 0.2125)
        self.assertFalse(scheduler.workers[0].busy)

        scheduler.mark_assigned(0)
        scheduler.mark_completed(0, 0.50)
        expected = 0.25 * 0.50 + 0.75 * 0.2125
        self.assertAlmostEqual(scheduler.workers[0].latency_seconds, expected)

    def test_aggregate_capacity_is_sum_of_worker_service_rates(self):
        scheduler = AdaptiveWorkerScheduler(2, initial_latency_seconds=0.5)
        self.assertAlmostEqual(
            scheduler.snapshot()["estimated_capacity_fps"], 4.0, places=2
        )

        scheduler.mark_assigned(0)
        scheduler.mark_completed(0, 0.25)
        self.assertGreater(
            scheduler.snapshot()["estimated_capacity_fps"], 4.0
        )

    def test_tie_breaking_rotates_equal_speed_workers(self):
        scheduler = AdaptiveWorkerScheduler(2, initial_latency_seconds=0.2)
        first = scheduler.choose([0, 1])
        scheduler.mark_assigned(first)
        scheduler.mark_completed(first, 0.2)

        second = scheduler.choose([0, 1])
        self.assertEqual(first, 0)
        self.assertEqual(second, 1)

    def test_no_available_worker_returns_none(self):
        scheduler = AdaptiveWorkerScheduler(2)
        self.assertIsNone(scheduler.choose([]))

    def test_invalid_worker_count_is_rejected(self):
        with self.assertRaises(ValueError):
            AdaptiveWorkerScheduler(0)


if __name__ == "__main__":
    unittest.main()
