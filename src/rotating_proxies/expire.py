from __future__ import annotations

import logging
import math
import random
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .utils import extract_proxy_hostport

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

logger = logging.getLogger(__name__)


class Proxies:
    """Expiring proxies container.

    A proxy can be in 3 states:

    * good;
    * dead;
    * unchecked.

    Initially, all proxies are in 'unchecked' state.
    When a request using a proxy is successful, this proxy moves to 'good'
    state. When a request using a proxy fails, proxy moves to 'dead' state.

    For crawling only 'good' and 'unchecked' proxies are used.

    'Dead' proxies move to 'unchecked' after a timeout (they are called
    'reanimated'). This timeout increases exponentially after each
    unsuccessful attempt to use a proxy.
    """

    def __init__(
        self,
        proxy_list: Iterable[str],
        backoff: Callable[..., float] | None = None,
    ) -> None:
        self.proxies: dict[str, ProxyState] = {url: ProxyState() for url in proxy_list}
        self.proxies_by_hostport: dict[str, str] = {
            extract_proxy_hostport(proxy): proxy for proxy in self.proxies
        }
        self.unchecked: set[str] = set(self.proxies)
        self.good: set[str] = set()
        self.dead: set[str] = set()

        if backoff is None:
            backoff = exp_backoff_full_jitter
        self.backoff = backoff

    def get_random(self) -> str | None:
        """Return a random available proxy (either good or unchecked)."""
        available = list(self.unchecked | self.good)
        if not available:
            return None
        return random.choice(available)  # noqa: S311

    def get_proxy(self, proxy_address: str | None) -> str | None:
        """Return complete proxy name associated with a hostport of a given
        ``proxy_address``. If ``proxy_address`` is unknown or empty,
        return None.
        """  # noqa: D205
        if not proxy_address:
            return None
        hostport = extract_proxy_hostport(proxy_address)
        return self.proxies_by_hostport.get(hostport, None)

    def mark_dead(self, proxy: str, _time: float | None = None) -> None:
        """Mark a proxy as dead."""
        if proxy not in self.proxies:
            logger.warning("Proxy <%s> was not found in proxies list", proxy)
            return

        if proxy in self.good:
            logger.debug("GOOD proxy became DEAD: <%s>", proxy)
        else:
            logger.debug("Proxy <%s> is DEAD", proxy)

        self.unchecked.discard(proxy)
        self.good.discard(proxy)
        self.dead.add(proxy)

        now = _time or time.time()
        state = self.proxies[proxy]
        state.backoff_time = self.backoff(state.failed_attempts)
        state.next_check = now + state.backoff_time
        state.failed_attempts += 1

    def mark_good(self, proxy: str) -> None:
        """Mark a proxy as good."""
        if proxy not in self.proxies:
            logger.warning("Proxy <%s> was not found in proxies list", proxy)
            return

        if proxy not in self.good:
            logger.debug("Proxy <%s> is GOOD", proxy)

        self.unchecked.discard(proxy)
        self.dead.discard(proxy)
        self.good.add(proxy)
        self.proxies[proxy].failed_attempts = 0

    def reanimate(self, _time: float | None = None) -> int:
        """Move dead proxies to unchecked if a backoff timeout passes."""
        n_reanimated = 0
        now = _time or time.time()
        for proxy in list(self.dead):
            state = self.proxies[proxy]
            assert state.next_check is not None  # noqa: S101
            if state.next_check <= now:
                self.dead.remove(proxy)
                self.unchecked.add(proxy)
                n_reanimated += 1
        return n_reanimated

    def reset(self) -> None:
        """Mark all dead proxies as unchecked."""
        for proxy in list(self.dead):
            self.dead.remove(proxy)
            self.unchecked.add(proxy)

    @property
    def mean_backoff_time(self) -> float:
        """Return mean backoff time for all dead proxies."""
        if not self.dead:
            return 0.0
        total_backoff = sum(self.proxies[p].backoff_time or 0.0 for p in self.dead)
        return float(total_backoff) / len(self.dead)

    @property
    def reanimated(self) -> list[str]:
        """Return list of reanimated proxies."""
        return [p for p in self.unchecked if self.proxies[p].failed_attempts]

    def __str__(self) -> str:
        """Return a string representation of the proxies container."""
        n_reanimated = len(self.reanimated)
        return (
            "Proxies("
            f"good: {len(self.good)}, "
            f"dead: {len(self.dead)}, "
            f"unchecked: {len(self.unchecked) - n_reanimated}, "
            f"reanimated: {n_reanimated}, "
            f"mean backoff time: {int(self.mean_backoff_time)}s"
            ")"
        )


@dataclass
class ProxyState:
    """State of a proxy."""

    failed_attempts: int = 0
    next_check: float | None = None
    backoff_time: float | None = None  # for debugging


def exp_backoff(attempt: int, cap: float = 3600, base: float = 300) -> float:
    """Exponential backoff time."""
    # this is a numerically stable version of `min(cap, base * 2**attempt)`
    max_attempts = math.log2(cap / base)
    if attempt <= max_attempts:
        return base * 2**attempt
    return cap


def exp_backoff_full_jitter(
    attempt: int, cap: float = 3600, base: float = 300
) -> float:
    """Exponential backoff time with Full Jitter."""
    return random.uniform(0, exp_backoff(attempt, cap, base))  # noqa: S311
