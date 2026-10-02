"""Finding the socket a server actually listens on.

The Steam query port is conventionally ``game port + 1`` bound to every
interface, and probing ``127.0.0.1`` at that port is the obvious check. Both
halves of that are assumptions, and either one being wrong looks identical
from outside: the probe times out and the port is reported closed while the
server is plainly running and accepting players.

Since the manager owns the process, it can ask the kernel which UDP sockets
that pid holds instead of guessing -- and, just as importantly, which address
each one is bound to. A socket bound to a single interface is unreachable over
loopback, so the probe has to follow the binding rather than assume it.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from typing import Any

import psutil

#: Addresses meaning "every interface"; reach these over loopback.
WILDCARD = {"0.0.0.0", "::", ""}


@dataclass(frozen=True, slots=True)
class Endpoint:
    """A UDP socket the server holds, and where to reach it."""

    ip: str
    port: int
    #: Address to actually send to (loopback when the socket is a wildcard).
    probe_ip: str

    @property
    def wildcard(self) -> bool:
        return self.ip in WILDCARD

    def to_dict(self) -> dict[str, Any]:
        return {
            "ip": self.ip,
            "port": self.port,
            "probe_ip": self.probe_ip,
            "wildcard": self.wildcard,
        }


def primary_host_ip() -> str:
    """The address this host would use to reach the outside world.

    No packet is sent: connecting a UDP socket only sets its route.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))   # TEST-NET-1, never routed
            return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def bound_udp_sockets(pid: int | None) -> list[Endpoint]:
    """Every UDP socket held by *pid*, as reported by the kernel."""
    if not pid:
        return []
    try:
        connections = psutil.Process(pid).net_connections(kind="udp")
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return []

    endpoints: list[Endpoint] = []
    for conn in connections:
        if not conn.laddr:
            continue
        ip = getattr(conn.laddr, "ip", "") or ""
        port = getattr(conn.laddr, "port", 0) or 0
        if not port:
            continue
        probe_ip = "127.0.0.1" if ip in WILDCARD else ip
        endpoint = Endpoint(ip=ip, port=port, probe_ip=probe_ip)
        if endpoint not in endpoints:
            endpoints.append(endpoint)
    return sorted(endpoints, key=lambda e: e.port)


def query_candidates(pid: int | None, game_port: int) -> list[Endpoint]:
    """Ordered guesses for the Steam query socket, best first.

    Sockets the process actually holds come first, ranked by how close they
    sit to the conventional ``game port + 1``. The conventional address is
    appended as a fallback for when socket enumeration is unavailable.
    """
    candidates: list[Endpoint] = []
    for endpoint in bound_udp_sockets(pid):
        # The game port carries Valheim's own protocol, not Source queries.
        if endpoint.port == game_port:
            continue
        candidates.append(endpoint)
    candidates.sort(key=lambda e: abs(e.port - (game_port + 1)))

    for fallback in (
        Endpoint("0.0.0.0", game_port + 1, "127.0.0.1"),
        Endpoint(primary_host_ip(), game_port + 1, primary_host_ip()),
    ):
        if not any(c.port == fallback.port and c.probe_ip == fallback.probe_ip
                   for c in candidates):
            candidates.append(fallback)
    return candidates
