"""Bounded local hand-off shared by the Momentum study and production gateway."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import errno
import json
import multiprocessing
import os
from pathlib import Path
import socket
import tempfile
import time
from typing import Mapping
import uuid


@dataclass(frozen=True)
class ParsedTradeFrame:
    received_ns: int
    trades: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class HandoffCounters:
    input_frames: int
    forwarded_frames: int
    dropped_frames: int
    parse_failures: int


@dataclass(frozen=True)
class SocketWriterCounters:
    sent_frames: int
    would_block_drops: int
    unavailable_drops: int = 0
    oversized_drops: int = 0


@dataclass(frozen=True)
class ConsumerProcessResult:
    producer_pid: int
    consumer_pid: int
    consumed_frames: int
    handoff_lags_ms: tuple[float, ...]
    raw_samples: str


def parse_massive_trade_frame(
    raw: bytes | str | Mapping[str, object],
) -> tuple[dict[str, object], ...]:
    """Parse one Massive websocket frame and retain its `T.*` trade events."""

    decoded: object
    if isinstance(raw, Mapping):
        decoded = dict(raw)
    else:
        decoded = json.loads(raw)
    rows = decoded if isinstance(decoded, list) else [decoded]
    if not rows or any(not isinstance(row, Mapping) for row in rows):
        raise ValueError("Massive frame must contain an object or a non-empty object list")
    trades: list[dict[str, object]] = []
    for item in rows:
        row = dict(item)
        event = str(row.get("ev", ""))
        if event == "T" or event.startswith("T."):
            trades.append(row)
    return tuple(trades)


class BoundedPaperHandoff:
    """Non-awaiting producer side of a drop-oldest queue.

    ``offer`` contains no ``await`` and uses only ``asyncio.Queue`` non-blocking
    operations.  A dead paper consumer therefore sheds the oldest queued frame
    and increments a counter instead of applying backpressure to the producer.
    """

    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._queue: asyncio.Queue[ParsedTradeFrame] = asyncio.Queue(maxsize=capacity)
        self._input_frames = 0
        self._forwarded_frames = 0
        self._dropped_frames = 0
        self._parse_failures = 0

    def offer(
        self,
        raw: bytes | str | Mapping[str, object],
        *,
        received_ns: int,
    ) -> bool:
        self._input_frames += 1
        try:
            trades = parse_massive_trade_frame(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            self._parse_failures += 1
            return False
        if not trades:
            return True
        frame = ParsedTradeFrame(received_ns=int(received_ns), trades=trades)
        if self._queue.full():
            self._queue.get_nowait()
            self._queue.task_done()
            self._dropped_frames += 1
        self._queue.put_nowait(frame)
        self._forwarded_frames += 1
        return True

    async def get(self) -> ParsedTradeFrame:
        return await self._queue.get()

    def get_nowait(self) -> ParsedTradeFrame:
        return self._queue.get_nowait()

    def task_done(self) -> None:
        self._queue.task_done()

    @property
    def size(self) -> int:
        return self._queue.qsize()

    @property
    def counters(self) -> HandoffCounters:
        return HandoffCounters(
            input_frames=self._input_frames,
            forwarded_frames=self._forwarded_frames,
            dropped_frames=self._dropped_frames,
            parse_failures=self._parse_failures,
        )


def _consumer_process_main(
    socket_path: str,
    raw_samples_path: str,
    mode: str,
    stop_event: object,
    control: object,
) -> None:
    consumer = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        Path(socket_path).unlink(missing_ok=True)
        consumer.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8_192)
        consumer.bind(socket_path)
        consumer.settimeout(0.05)
        control.send(
            {
                "kind": "ready",
                "pid": os.getpid(),
                "receive_buffer_bytes": consumer.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF),
            }
        )
        consumed = 0
        lags_ms: list[float] = []
        output = Path(raw_samples_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8") as handle:
            if mode == "dead":
                stop_event.wait()
            else:
                while True:
                    try:
                        raw = consumer.recv(1_048_576)
                    except TimeoutError:
                        if stop_event.is_set():
                            break
                        continue
                    received_ns = time.time_ns()
                    payload = json.loads(raw)
                    producer_received_ns = int(payload["received_ns"])
                    lag_ms = (received_ns - producer_received_ns) / 1_000_000
                    consumed += 1
                    lags_ms.append(lag_ms)
                    handle.write(
                        json.dumps(
                            {
                                "consumer_received_ns": received_ns,
                                "producer_received_ns": producer_received_ns,
                                "lag_ms": lag_ms,
                                "trades": payload["trades"],
                            },
                            sort_keys=True,
                        )
                        + "\n"
                    )
        control.send(
            {
                "kind": "result",
                "pid": os.getpid(),
                "consumed_frames": consumed,
                "handoff_lags_ms": lags_ms,
            }
        )
    except BaseException as exc:
        try:
            control.send({"kind": "error", "error": f"{type(exc).__name__}: {exc}"})
        except BaseException:
            pass
        raise
    finally:
        consumer.close()
        Path(socket_path).unlink(missing_ok=True)
        control.close()


class CrossProcessPaperConsumer:
    """Separate-process Unix-datagram consumer used by the Step 2 harness."""

    def __init__(self, *, mode: str, raw_samples_path: Path) -> None:
        if mode not in {"active", "dead"}:
            raise ValueError("consumer mode must be active or dead")
        context = multiprocessing.get_context("spawn")
        self.mode = mode
        self.raw_samples_path = raw_samples_path
        self.socket_path = str(
            Path(tempfile.gettempdir()) / f"mai-tai-mgw-{os.getpid()}-{uuid.uuid4().hex[:10]}.sock"
        )
        self._stop_event = context.Event()
        self._control, child_control = context.Pipe(duplex=True)
        self._child_control = child_control
        self._process = context.Process(
            target=_consumer_process_main,
            args=(
                self.socket_path,
                str(raw_samples_path),
                mode,
                self._stop_event,
                child_control,
            ),
            name=f"momentum-step2-{mode}-consumer",
        )
        self._consumer_pid: int | None = None
        self._receive_buffer_bytes: int | None = None

    @property
    def pid(self) -> int:
        if self._consumer_pid is None:
            raise RuntimeError("consumer has not started")
        return self._consumer_pid

    @property
    def receive_buffer_bytes(self) -> int:
        if self._receive_buffer_bytes is None:
            raise RuntimeError("consumer has not started")
        return self._receive_buffer_bytes

    def start(self, *, timeout_seconds: float = 10.0) -> int:
        self.raw_samples_path.parent.mkdir(parents=True, exist_ok=True)
        self._process.start()
        self._child_control.close()
        if not self._control.poll(timeout_seconds):
            self._process.terminate()
            self._process.join(timeout=2)
            raise RuntimeError("separate Momentum consumer did not become ready")
        ready = self._control.recv()
        if ready.get("kind") != "ready":
            raise RuntimeError(f"separate Momentum consumer failed to start: {ready}")
        self._consumer_pid = int(ready["pid"])
        self._receive_buffer_bytes = int(ready["receive_buffer_bytes"])
        if self._consumer_pid == os.getpid():
            raise RuntimeError("Momentum consumer must not run in the producer process")
        return self._consumer_pid

    def stop(self, *, timeout_seconds: float = 10.0) -> ConsumerProcessResult:
        self._stop_event.set()
        self._process.join(timeout_seconds)
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=2)
            raise RuntimeError("separate Momentum consumer did not stop")
        if not self._control.poll(1):
            raise RuntimeError(
                f"separate Momentum consumer exited {self._process.exitcode} without a result"
            )
        result = self._control.recv()
        if result.get("kind") != "result":
            raise RuntimeError(f"separate Momentum consumer failed: {result}")
        return ConsumerProcessResult(
            producer_pid=os.getpid(),
            consumer_pid=int(result["pid"]),
            consumed_frames=int(result["consumed_frames"]),
            handoff_lags_ms=tuple(float(value) for value in result["handoff_lags_ms"]),
            raw_samples=str(self.raw_samples_path),
        )

    def close(self) -> None:
        if self._process.is_alive():
            self._stop_event.set()
            self._process.terminate()
            self._process.join(timeout=2)
        self._control.close()
        Path(self.socket_path).unlink(missing_ok=True)


def connect_consumer_socket(socket_path: str) -> socket.socket:
    producer_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        producer_socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8_192)
        producer_socket.setblocking(False)
        producer_socket.connect(socket_path)
    except BaseException:
        producer_socket.close()
        raise
    return producer_socket


def encode_trade_frame(frame: ParsedTradeFrame) -> bytes:
    return json.dumps(
        {"received_ns": frame.received_ns, "trades": frame.trades},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def decode_trade_frame(raw: bytes) -> ParsedTradeFrame:
    payload = json.loads(raw)
    trades = payload.get("trades")
    if not isinstance(trades, list) or any(not isinstance(row, dict) for row in trades):
        raise ValueError("Momentum gateway frame has invalid trades")
    return ParsedTradeFrame(
        received_ns=int(payload["received_ns"]),
        trades=tuple(dict(row) for row in trades),
    )


class UnixDatagramPaperReceiver:
    """Production-side local receiver shared by no other service."""

    def __init__(self, socket_path: str) -> None:
        self.socket_path = str(Path(socket_path).expanduser())
        self._socket: socket.socket | None = None

    def open(self) -> None:
        path = Path(self.socket_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        receiver.setblocking(False)
        receiver.bind(self.socket_path)
        self._socket = receiver

    async def receive(self) -> ParsedTradeFrame:
        if self._socket is None:
            raise RuntimeError("Momentum gateway receiver is not open")
        raw = await asyncio.get_running_loop().sock_recv(self._socket, 1_048_576)
        return decode_trade_frame(raw)

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        Path(self.socket_path).unlink(missing_ok=True)


async def drain_handoff_to_socket(
    handoff: BoundedPaperHandoff,
    producer_socket: socket.socket,
    producer_done: asyncio.Event,
) -> SocketWriterCounters:
    sent_frames = 0
    would_block_drops = 0
    oversized_drops = 0
    while not producer_done.is_set() or handoff.size:
        try:
            frame = await asyncio.wait_for(handoff.get(), timeout=0.05)
        except TimeoutError:
            continue
        try:
            payload = encode_trade_frame(frame)
            try:
                sent = producer_socket.send(payload)
            except OSError as exc:
                if exc.errno == errno.EMSGSIZE:
                    oversized_drops += 1
                elif exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK, errno.ENOBUFS}:
                    would_block_drops += 1
                else:
                    raise
            else:
                if sent != len(payload):
                    raise RuntimeError("Unix datagram write was unexpectedly partial")
                sent_frames += 1
        finally:
            handoff.task_done()
    return SocketWriterCounters(
        sent_frames=sent_frames,
        would_block_drops=would_block_drops,
        oversized_drops=oversized_drops,
    )
