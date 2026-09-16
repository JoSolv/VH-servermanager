"""Network throughput accounting.

Linux has no per-process byte counters (``/proc/<pid>/net`` is per *namespace*,
not per process), so per-instance traffic needs help from the kernel firewall.
Two providers are tried in order:

``NftablesProvider``
    Installs a named nftables counter per instance port range. Exact, but
    needs CAP_NET_ADMIN -- i.e. the manager must run as root.
``HostProvider``
    Always available. Machine-wide totals, reported separately so the UI can
    label them honestly rather than pretending they belong to one instance.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any

import psutil

TABLE = "vhsm"


@dataclass(slots=True)
class NetSample:
    rx_bytes: int = 0
    tx_bytes: int = 0
    rx_rate: float = 0.0   # bytes/second
    tx_rate: float = 0.0
    source: str = "unavailable"

    def to_dict(self) -> dict[str, Any]:
        return {
            "rx_bytes": self.rx_bytes,
            "tx_bytes": self.tx_bytes,
            "rx_rate": round(self.rx_rate, 1),
            "tx_rate": round(self.tx_rate, 1),
            "source": self.source,
        }


class _RateTracker:
    """Turns monotonically increasing counters into per-second rates."""

    def __init__(self) -> None:
        self._previous: dict[str, tuple[float, int, int]] = {}

    def rate(self, key: str, rx: int, tx: int) -> tuple[float, float]:
        now = time.monotonic()
        previous = self._previous.get(key)
        self._previous[key] = (now, rx, tx)
        if previous is None:
            return 0.0, 0.0
        elapsed = now - previous[0]
        if elapsed <= 0:
            return 0.0, 0.0
        # max(0, ...) guards against counters being reset underneath us.
        return max(0, rx - previous[1]) / elapsed, max(0, tx - previous[2]) / elapsed

    def forget(self, key: str) -> None:
        self._previous.pop(key, None)


class NftablesProvider:
    """Per-instance counters using nftables named counters."""

    def __init__(self) -> None:
        self.available = False
        self.reason = "nft binary not found"
        self._registered: set[int] = set()
        self._rates = _RateTracker()
        self._probe()

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["nft", *args], capture_output=True, text=True, timeout=5, check=check
        )

    def _probe(self) -> None:
        if shutil.which("nft") is None:
            return
        try:
            self._run("add", "table", "inet", TABLE)
            self._run(
                "add", "chain", "inet", TABLE, "input",
                "{ type filter hook input priority 0 ; policy accept ; }",
            )
            self._run(
                "add", "chain", "inet", TABLE, "output",
                "{ type filter hook output priority 0 ; policy accept ; }",
            )
            self.available = True
            self.reason = ""
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            detail = getattr(exc, "stderr", "") or str(exc)
            self.reason = f"nftables unavailable (needs root): {detail.strip()[:120]}"

    def register(self, port: int) -> None:
        """Add counters for an instance's UDP port range (game, query, +1)."""
        if not self.available or port in self._registered:
            return
        span = f"{port}-{port + 2}"
        try:
            for direction, chain, match in (
                ("rx", "input", "dport"),
                ("tx", "output", "sport"),
            ):
                counter = f"{direction}_{port}"
                self._run("add", "counter", "inet", TABLE, counter)
                self._run(
                    "add", "rule", "inet", TABLE, chain,
                    "udp", match, span, "counter", "name", f'"{counter}"',
                )
            self._registered.add(port)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            self.available = False
            self.reason = f"failed to register counters: {exc}"

    def read_all(self) -> dict[int, NetSample]:
        if not self.available or not self._registered:
            return {}
        try:
            result = self._run("-j", "list", "counters", "table", "inet", TABLE)
            payload = json.loads(result.stdout)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                json.JSONDecodeError, OSError):
            return {}

        totals: dict[int, list[int]] = {}
        for item in payload.get("nftables", []):
            counter = item.get("counter")
            if not counter:
                continue
            name = counter.get("name", "")
            direction, _, port_text = name.partition("_")
            if direction not in ("rx", "tx") or not port_text.isdigit():
                continue
            entry = totals.setdefault(int(port_text), [0, 0])
            entry[0 if direction == "rx" else 1] = int(counter.get("bytes", 0))

        samples: dict[int, NetSample] = {}
        for port, (rx, tx) in totals.items():
            rx_rate, tx_rate = self._rates.rate(f"nft:{port}", rx, tx)
            samples[port] = NetSample(rx, tx, rx_rate, tx_rate, "nftables")
        return samples

    def unregister(self, port: int) -> None:
        self._registered.discard(port)
        self._rates.forget(f"nft:{port}")


class NetworkMonitor:
    """Facade over the providers, plus machine-wide totals."""

    def __init__(self) -> None:
        self.nft = NftablesProvider()
        self._rates = _RateTracker()
        self._per_port: dict[int, NetSample] = {}

    @property
    def per_instance_available(self) -> bool:
        return self.nft.available

    @property
    def per_instance_reason(self) -> str:
        return self.nft.reason

    def watch(self, port: int) -> None:
        self.nft.register(port)

    def unwatch(self, port: int) -> None:
        self.nft.unregister(port)

    def refresh(self) -> None:
        """Read every counter once per tick rather than once per instance."""
        self._per_port = self.nft.read_all()

    def for_port(self, port: int) -> NetSample:
        sample = self._per_port.get(port)
        if sample is not None:
            return sample
        return NetSample(source="unavailable" if not self.nft.available else "pending")

    def host(self) -> NetSample:
        counters = psutil.net_io_counters()
        rx_rate, tx_rate = self._rates.rate("host", counters.bytes_recv, counters.bytes_sent)
        return NetSample(
            counters.bytes_recv, counters.bytes_sent, rx_rate, tx_rate, "host"
        )
