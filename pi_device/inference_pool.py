"""Bounded, adaptive multiprocessing scheduler for YOLOv5n/NCNN frames.

The camera/libcamera controller stays in the parent process. Each worker owns
one independent NCNN network and receives at most one frame at a time. Frames
are dropped when every worker is busy rather than accumulating stale work.
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import os
import queue
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class WorkerEstimate:
    latency_seconds: float
    assignments: int = 0
    busy: bool = False


class AdaptiveWorkerScheduler:
    """Earliest-estimated-finish scheduler with EWMA latency and fair tie breaks."""

    def __init__(self, worker_count: int, initial_latency_seconds: float = 0.25):
        if worker_count < 1:
            raise ValueError("worker_count must be at least one")
        self.workers = [
            WorkerEstimate(max(0.01, float(initial_latency_seconds)))
            for _ in range(worker_count)
        ]

    def choose(self, available: list[int]) -> int | None:
        candidates = [
            index for index in available
            if 0 <= index < len(self.workers) and not self.workers[index].busy
        ]
        if not candidates:
            return None
        # Faster workers receive more work naturally, but equal workers rotate
        # by assignment count instead of starving all but worker zero.
        return min(
            candidates,
            key=lambda index: (
                self.workers[index].latency_seconds,
                self.workers[index].assignments,
                index,
            ),
        )

    def mark_assigned(self, worker_id: int) -> None:
        worker = self.workers[worker_id]
        if worker.busy:
            raise RuntimeError(f"Worker {worker_id} already has an in-flight frame")
        worker.busy = True
        worker.assignments += 1

    def mark_completed(self, worker_id: int, elapsed_seconds: float) -> None:
        worker = self.workers[worker_id]
        sample = max(0.001, float(elapsed_seconds))
        # EWMA reacts to thermal throttling and per-worker performance changes
        # without overreacting to one unusually slow frame.
        worker.latency_seconds = 0.25 * sample + 0.75 * worker.latency_seconds
        worker.busy = False

    def mark_failed(self, worker_id: int) -> None:
        self.workers[worker_id].busy = False

    def snapshot(self) -> dict:
        latencies = [worker.latency_seconds for worker in self.workers]
        return {
            "workers": len(self.workers),
            "busy_workers": sum(worker.busy for worker in self.workers),
            "worker_latency_ms": [round(value * 1000.0, 1) for value in latencies],
            "average_latency_ms": round(sum(latencies) * 1000.0 / len(latencies), 1),
            # Independent workers run concurrently, so their measured service
            # rates add to the current pool capacity estimate.
            "estimated_capacity_fps": round(sum(1.0 / value for value in latencies), 2),
        }


def _inference_worker(
    worker_id: int,
    task_queue,
    result_queue,
    model_dir: str | None,
    confidence: float,
    ncnn_threads: int,
) -> None:
    """Child entry point. Never imports or opens Picamera2/libcamera."""
    try:
        # Keep nested native thread pools from oversubscribing the Pi's CPUs.
        os.environ["OMP_NUM_THREADS"] = str(ncnn_threads)
        os.environ["OPENBLAS_NUM_THREADS"] = "1"
        import cv2
        # Spawned Python re-imports the application entry module, so cv2 may
        # already be loaded before this worker function begins. Explicitly cap
        # its native pool here to avoid multiplying OpenCV threads by workers.
        cv2.setNumThreads(ncnn_threads)
        from .detector import YoloV5nDetector

        detector = YoloV5nDetector(
            model_dir=model_dir,
            confidence=confidence,
            num_threads=ncnn_threads,
        )
        result_queue.put({"kind": "ready", "worker_id": worker_id}, timeout=10)
    except Exception as exc:
        try:
            result_queue.put({
                "kind": "init_error",
                "worker_id": worker_id,
                "error": f"{type(exc).__name__}: {exc}",
            }, timeout=5)
        except Exception:
            pass
        return

    while True:
        task = task_queue.get()
        if task is None:
            return
        frame_sequence, frame_bgr, frame_timestamp, threshold = task
        started = time.monotonic()
        try:
            detector.confidence = float(threshold)
            frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            detections = detector.detect(frame_rgb)
            result_queue.put({
                "kind": "result",
                "worker_id": worker_id,
                "frame_sequence": frame_sequence,
                "frame_timestamp": frame_timestamp,
                "elapsed_seconds": time.monotonic() - started,
                "detections": detections,
                "error": "",
            }, timeout=10)
        except Exception as exc:
            try:
                result_queue.put({
                    "kind": "result",
                    "worker_id": worker_id,
                    "frame_sequence": frame_sequence,
                    "frame_timestamp": frame_timestamp,
                    "elapsed_seconds": time.monotonic() - started,
                    "detections": [],
                    "error": f"{type(exc).__name__}: {exc}",
                }, timeout=5)
            except Exception:
                log.exception("YOLO worker %s could not report an inference error", worker_id)


class AdaptiveInferencePool:
    """Spawn-based, bounded inference pool with per-worker latency estimates."""

    def __init__(
        self,
        worker_count: int = 2,
        model_dir: str | None = None,
        confidence: float = 0.45,
        ncnn_threads: int = 1,
    ):
        if not 1 <= int(worker_count) <= 3:
            raise ValueError("inference worker count must be between 1 and 3")
        if int(ncnn_threads) < 1:
            raise ValueError("NCNN threads per worker must be at least one")
        self.worker_count = int(worker_count)
        self.model_dir = model_dir
        self.confidence = float(confidence)
        self.ncnn_threads = int(ncnn_threads)
        self.context = mp.get_context("spawn")
        self.result_queue = None
        self.task_queues = []
        self.processes = []
        self.scheduler = AdaptiveWorkerScheduler(self.worker_count)
        self.ready_workers: set[int] = set()
        self.last_error = ""
        self.started = False
        self._restart_at = [0.0 for _ in range(self.worker_count)]
        self._restart_delay = [1.0 for _ in range(self.worker_count)]
        self._inflight_by_worker: dict[int, int] = {}
        self._submitted_at_by_worker: dict[int, float] = {}
        self._pending_failures: list[dict] = []

    def _spawn_worker(self, worker_id: int) -> None:
        process = self.context.Process(
            target=_inference_worker,
            name=f"yolo-ncnn-{worker_id}",
            args=(
                worker_id,
                self.task_queues[worker_id],
                self.result_queue,
                self.model_dir,
                self.confidence,
                self.ncnn_threads,
            ),
            daemon=True,
        )
        process.start()
        if worker_id < len(self.processes):
            self.processes[worker_id] = process
        else:
            self.processes.append(process)

    def _restart_dead_workers(self) -> None:
        if not self.started:
            return
        now = time.monotonic()
        for worker_id, process in enumerate(self.processes):
            if process.is_alive() or now < self._restart_at[worker_id]:
                continue

            self.ready_workers.discard(worker_id)
            self.scheduler.mark_failed(worker_id)
            failed_sequence = self._inflight_by_worker.pop(worker_id, None)
            self._submitted_at_by_worker.pop(worker_id, None)
            if failed_sequence is not None:
                self._pending_failures.append({
                    "kind": "result",
                    "worker_id": worker_id,
                    "frame_sequence": failed_sequence,
                    "frame_timestamp": 0.0,
                    "elapsed_seconds": 0.0,
                    "detections": [],
                    "error": "YOLO worker process exited unexpectedly; frame was discarded",
                    "worker_failed": True,
                })
            try:
                process.join(timeout=0)
            except Exception:
                pass

            # The old process may have died before consuming its one queued
            # frame. Drop that stale task before reusing the bounded queue.
            tasks = self.task_queues[worker_id]
            while True:
                try:
                    tasks.get_nowait()
                except queue.Empty:
                    break
                except (ValueError, OSError):
                    break

            delay = self._restart_delay[worker_id]
            try:
                self._spawn_worker(worker_id)
                self._restart_at[worker_id] = now + delay
                self._restart_delay[worker_id] = min(30.0, delay * 2.0)
                log.warning(
                    "YOLO worker %s exited; respawned with %.1fs restart backoff",
                    worker_id, delay,
                )
            except Exception:
                self._restart_at[worker_id] = now + delay
                self.last_error = f"Could not restart YOLO worker {worker_id}"
                log.exception("Could not respawn YOLO worker %s", worker_id)

    def start(self, timeout: float = 75.0) -> None:
        if self.started:
            return
        self.result_queue = self.context.Queue(maxsize=max(4, self.worker_count * 2))
        self.task_queues = [
            self.context.Queue(maxsize=1) for _ in range(self.worker_count)
        ]
        for worker_id in range(self.worker_count):
            self._spawn_worker(worker_id)

        deadline = time.monotonic() + timeout
        while len(self.ready_workers) < self.worker_count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Only {len(self.ready_workers)}/{self.worker_count} YOLO workers became ready"
                )
            try:
                message = self.result_queue.get(timeout=min(1.0, remaining))
            except queue.Empty:
                dead = [
                    index for index, process in enumerate(self.processes)
                    if not process.is_alive() and index not in self.ready_workers
                ]
                if dead:
                    raise RuntimeError(f"YOLO worker(s) exited during startup: {dead}")
                continue
            if message.get("kind") == "ready":
                self.ready_workers.add(int(message["worker_id"]))
                log.info("YOLO NCNN worker %s is ready", message["worker_id"])
            elif message.get("kind") == "init_error":
                self.last_error = str(message.get("error", "worker model initialization failed"))
                raise RuntimeError(
                    f"YOLO worker {message.get('worker_id')} failed to load the model: {self.last_error}"
                )
        self.started = True
        log.info(
            "Adaptive YOLO pool ready: workers=%s, NCNN threads/worker=%s",
            self.worker_count,
            self.ncnn_threads,
        )

    def submit(self, frame, frame_sequence: int, frame_timestamp: float, confidence: float) -> int | None:
        if not self.started:
            return None
        available = [
            index for index, process in enumerate(self.processes)
            if index in self.ready_workers
            and process.is_alive()
            and not self.scheduler.workers[index].busy
        ]
        worker_id = self.scheduler.choose(available)
        if worker_id is None:
            return None
        self.scheduler.mark_assigned(worker_id)
        try:
            self.task_queues[worker_id].put_nowait((
                int(frame_sequence),
                frame,
                float(frame_timestamp),
                float(confidence),
            ))
        except (queue.Full, ValueError, OSError):
            self.scheduler.mark_failed(worker_id)
            return None
        self._inflight_by_worker[worker_id] = int(frame_sequence)
        self._submitted_at_by_worker[worker_id] = time.monotonic()
        return worker_id

    def collect(self, timeout: float = 0.0) -> list[dict]:
        if not self.started or self.result_queue is None:
            return []
        results = []
        messages = list(self._pending_failures)
        self._pending_failures.clear()
        try:
            first = self.result_queue.get(timeout=max(0.0, timeout)) if timeout else self.result_queue.get_nowait()
            messages.append(first)
        except queue.Empty:
            pass

        while True:
            try:
                messages.append(self.result_queue.get_nowait())
            except queue.Empty:
                break

        for message in messages:
            kind = message.get("kind")
            worker_id = int(message.get("worker_id", -1))
            if kind == "ready" and 0 <= worker_id < self.worker_count:
                self.ready_workers.add(worker_id)
                self._restart_at[worker_id] = 0.0
                self._restart_delay[worker_id] = 1.0
                continue
            if kind == "init_error" and 0 <= worker_id < self.worker_count:
                self.last_error = str(message.get("error", "worker initialization failed"))
                log.error("YOLO worker %s initialization failed: %s", worker_id, self.last_error)
                continue
            if kind != "result" or not 0 <= worker_id < self.worker_count:
                continue

            message_sequence = int(message.get("frame_sequence", -1))
            current_sequence = self._inflight_by_worker.get(worker_id)
            # A queue feeder can deliver an old worker's final result after
            # that worker has died and been replaced. Never let that stale
            # result complete or clear the replacement worker's in-flight job.
            if current_sequence is not None and current_sequence != message_sequence:
                log.debug(
                    "Ignoring stale YOLO result from worker %s for frame %s",
                    worker_id, message_sequence,
                )
                continue
            if current_sequence is None and not message.get("worker_failed"):
                continue
            self._inflight_by_worker.pop(worker_id, None)
            submitted_at = self._submitted_at_by_worker.pop(worker_id, None)
            if message.get("worker_failed"):
                results.append(message)
                continue
            # Schedule by end-to-end turnaround (IPC + worker execution), not
            # just the detector's inner compute time. The worker's own elapsed
            # field is still retained for the pure model-latency metric.
            turnaround = (
                max(0.001, time.monotonic() - submitted_at)
                if submitted_at is not None
                else max(0.001, float(message.get("elapsed_seconds", 0.0)))
            )
            self.scheduler.mark_completed(worker_id, turnaround)
            error = str(message.get("error", ""))
            if error:
                self.last_error = error
            results.append(message)

        self._restart_dead_workers()
        return results

    def snapshot(self) -> dict:
        state = self.scheduler.snapshot()
        ready_indices = [
            index for index, process in enumerate(self.processes)
            if index in self.ready_workers and process.is_alive()
        ]
        ready_latencies = [
            self.scheduler.workers[index].latency_seconds
            for index in ready_indices
        ]
        state.update({
            "ready_workers": len(ready_indices),
            "estimated_capacity_fps": round(
                sum(1.0 / value for value in ready_latencies), 2
            ),
            "ready_worker_latency_ms": [
                round(value * 1000.0, 1) for value in ready_latencies
            ],
            "last_error": self.last_error,
            "ncnn_threads_per_worker": self.ncnn_threads,
        })
        return state

    def stop(self, timeout: float = 3.0) -> None:
        if self.result_queue is None and not self.processes:
            self.started = False
            return

        for tasks in self.task_queues:
            try:
                tasks.put_nowait(None)
            except (queue.Full, ValueError, OSError):
                pass
        for process in self.processes:
            process.join(timeout=timeout)
        for process in self.processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=1)

        for tasks in self.task_queues:
            try:
                tasks.close()
                tasks.cancel_join_thread()
            except Exception:
                pass
        if self.result_queue is not None:
            try:
                self.result_queue.close()
                self.result_queue.cancel_join_thread()
            except Exception:
                pass

        self.task_queues.clear()
        self.processes.clear()
        self.ready_workers.clear()
        self._inflight_by_worker.clear()
        self._submitted_at_by_worker.clear()
        self._pending_failures.clear()
        self.result_queue = None
        self.started = False
