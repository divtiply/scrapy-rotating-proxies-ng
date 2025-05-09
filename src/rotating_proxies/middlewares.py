from __future__ import annotations

import codecs
import logging
from functools import partial
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit

from scrapy import signals
from scrapy.exceptions import CloseSpider, NotConfigured
from scrapy.statscollectors import StatsCollector
from scrapy.utils.misc import load_object
from scrapy.utils.url import add_http_if_no_scheme
from twisted.internet import task
from typing_extensions import Self

from .expire import Proxies, exp_backoff_full_jitter

if TYPE_CHECKING:
    from collections.abc import Iterable

    from scrapy.crawler import Crawler
    from scrapy.http.request import Request
    from scrapy.http.response import Response
    from scrapy.spiders import Spider

    from .policy import BanDetectionPolicy

logger = logging.getLogger(__name__)


class RotatingProxyMiddleware:
    """Scrapy downloader middleware which chooses a random proxy for each request.

    To enable it, add it and BanDetectionMiddleware
    to DOWNLOADER_MIDDLEWARES option::

        DOWNLOADER_MIDDLEWARES = {
            # ...
            "rotating_proxies.middlewares.RotatingProxyMiddleware": 610,
            "rotating_proxies.middlewares.BanDetectionMiddleware": 620,
            # ...
        }

    It keeps track of dead and alive proxies and avoids using dead proxies.
    Proxy is considered dead if request.meta['_ban'] is True, and alive
    if request.meta['_ban'] is False; to set this meta key use
    BanDetectionMiddleware.

    Dead proxies are re-checked with a randomized exponential backoff.

    By default, all default Scrapy concurrency options (DOWNLOAD_DELAY,
    AUTOTHROTTLE_..., CONCURRENT_REQUESTS_PER_DOMAIN, etc) become per-proxy
    for proxied requests when RotatingProxyMiddleware is enabled.
    For example, if you set CONCURRENT_REQUESTS_PER_DOMAIN=2 then
    spider will be making at most 2 concurrent connections to each proxy.

    Settings:

    * ``ROTATING_PROXY_LIST``  - a list of proxies to choose from;
    * ``ROTATING_PROXY_LIST_PATH``  - path to a file with a list of proxies;
    * ``ROTATING_PROXY_LOGSTATS_INTERVAL`` - stats logging interval in seconds,
      30 by default;
    * ``ROTATING_PROXY_CLOSE_SPIDER`` - When True, spider is stopped if
      there are no alive proxies. If False (default), then when there is no
      alive proxies all dead proxies are re-checked.
    * ``ROTATING_PROXY_PAGE_RETRY_TIMES`` - a number of times to retry
      downloading a page using a different proxy. After this amount of retries
      failure is considered a page failure, not a proxy failure.
      Think of it this way: every improperly detected ban cost you
      ``ROTATING_PROXY_PAGE_RETRY_TIMES`` alive proxies. Default: 5.
    * ``ROTATING_PROXY_BACKOFF_BASE`` - base backoff time, in seconds.
      Default is 300 (i.e. 5 min).
    * ``ROTATING_PROXY_BACKOFF_CAP`` - backoff time cap, in seconds.
      Default is 3600 (i.e. 60 min).
    """

    def __init__(  # noqa: PLR0913
        self,
        proxy_list: Iterable[str],
        logstats_interval: float,
        stop_if_no_proxies: bool,  # noqa: FBT001
        max_proxies_to_try: int,
        backoff_base: float,
        backoff_cap: float,
        crawler: Crawler,
    ) -> None:
        backoff = partial(exp_backoff_full_jitter, base=backoff_base, cap=backoff_cap)
        self.proxies = Proxies(self.cleanup_proxy_list(proxy_list), backoff=backoff)
        self.logstats_interval = logstats_interval
        self.reanimate_interval = 5
        self.stop_if_no_proxies = stop_if_no_proxies
        self.max_proxies_to_try = max_proxies_to_try
        self.stats = cast(StatsCollector, crawler.stats)

        self.log_task: task.LoopingCall | None = None
        self.reanimate_task: task.LoopingCall | None = None

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        s = crawler.settings
        proxy_path = s.get("ROTATING_PROXY_LIST_PATH", None)
        if proxy_path is not None:
            with codecs.open(proxy_path, "r", encoding="utf8") as f:
                proxy_list = [line.strip() for line in f if line.strip()]
        else:
            proxy_list = s.getlist("ROTATING_PROXY_LIST")
        if not proxy_list:
            raise NotConfigured
        mw = cls(
            proxy_list=proxy_list,
            logstats_interval=s.getfloat("ROTATING_PROXY_LOGSTATS_INTERVAL", 30),
            stop_if_no_proxies=s.getbool("ROTATING_PROXY_CLOSE_SPIDER", False),  # noqa: FBT003
            max_proxies_to_try=s.getint("ROTATING_PROXY_PAGE_RETRY_TIMES", 5),
            backoff_base=s.getfloat("ROTATING_PROXY_BACKOFF_BASE", 300),
            backoff_cap=s.getfloat("ROTATING_PROXY_BACKOFF_CAP", 3600),
            crawler=crawler,
        )
        crawler.signals.connect(mw.engine_started, signal=signals.engine_started)
        crawler.signals.connect(mw.engine_stopped, signal=signals.engine_stopped)
        return mw

    def engine_started(self) -> None:
        if self.logstats_interval:
            self.log_task = task.LoopingCall(self.log_stats)
            self.log_task.start(self.logstats_interval, now=True)

        if self.reanimate_interval:
            self.reanimate_task = task.LoopingCall(self.reanimate_proxies)
            self.reanimate_task.start(self.reanimate_interval, now=False)

    def reanimate_proxies(self) -> None:
        n_reanimated = self.proxies.reanimate()
        if n_reanimated:
            logger.debug("%s proxies moved from 'dead' to 'reanimated'", n_reanimated)

    def engine_stopped(self) -> None:
        if self.log_task and self.log_task.running:
            self.log_task.stop()

        if self.reanimate_task and self.reanimate_task.running:
            self.reanimate_task.stop()

    def process_request(
        self,
        request: Request,
        spider: Spider,  # noqa: ARG002
    ) -> None:
        if "proxy" in request.meta and not request.meta.get("_rotating_proxy"):
            return
        proxy = self.proxies.get_random()
        if not proxy:
            if self.stop_if_no_proxies:
                raise CloseSpider("no_proxies")  # noqa: EM101
            logger.warning("No proxies available; marking all proxies as unchecked")
            self.proxies.reset()
            proxy = self.proxies.get_random()
            if proxy is None:
                logger.error("No proxies available even after a reset.")
                raise CloseSpider("no_proxies_after_reset")  # noqa: EM101

        request.meta["proxy"] = proxy
        request.meta["download_slot"] = self.get_proxy_slot(proxy)
        request.meta["_rotating_proxy"] = True

    def get_proxy_slot(self, proxy: str) -> str:
        """Return downloader slot for a proxy.

        By default it doesn't take port in account, i.e. all proxies with
        the same hostname / ip address share the same slot.
        """
        # FIXME: an option to use website address as a part of slot as well?  # noqa
        return cast(str, urlsplit(proxy).hostname)

    def process_exception(
        self,
        request: Request,
        exception: Exception,  # noqa: ARG002
        spider: Spider,
    ) -> Request | None:
        return self._handle_result(request, spider)

    def process_response(
        self, request: Request, response: Response, spider: Spider
    ) -> Response | Request:
        return self._handle_result(request, spider) or response

    def _handle_result(self, request: Request, spider: Spider) -> Request | None:
        proxy = self.proxies.get_proxy(request.meta.get("proxy", None))
        if not (proxy and request.meta.get("_rotating_proxy")):
            return None
        self.stats.set_value(
            "proxies/unchecked",
            len(self.proxies.unchecked) - len(self.proxies.reanimated),
        )
        self.stats.set_value("proxies/reanimated", len(self.proxies.reanimated))
        self.stats.set_value("proxies/mean_backoff", self.proxies.mean_backoff_time)
        ban = request.meta.get("_ban", None)
        if ban is True:
            self.proxies.mark_dead(proxy)
            self.stats.set_value("proxies/dead", len(self.proxies.dead))
            return self._retry(request, spider)
        if ban is False:
            self.proxies.mark_good(proxy)
            self.stats.set_value("proxies/good", len(self.proxies.good))
        return None

    def _retry(self, request: Request, spider: Spider) -> Request | None:
        retries = request.meta.get("proxy_retry_times", 0) + 1
        max_proxies_to_try = request.meta.get(
            "max_proxies_to_try", self.max_proxies_to_try
        )

        if retries > max_proxies_to_try:
            logger.debug(
                "Gave up retrying %(request)s "
                "(failed %(retries)d times with different proxies)",
                {"request": request, "retries": retries},
                extra={"spider": spider},
            )
            return None

        logger.debug(
            "Retrying %(request)s with another proxy "
            "(failed %(retries)d times, max retries: %(max_proxies_to_try)d)",
            {
                "request": request,
                "retries": retries,
                "max_proxies_to_try": max_proxies_to_try,
            },
            extra={"spider": spider},
        )
        retry_req = request.copy()
        retry_req.meta["proxy_retry_times"] = retries
        retry_req.dont_filter = True
        return retry_req

    def log_stats(self) -> None:
        logger.info("%s", self.proxies)

    @classmethod
    def cleanup_proxy_list(cls, proxy_list: Iterable[str]) -> list[str]:
        lines = (line.strip() for line in proxy_list)
        return list(
            {
                add_http_if_no_scheme(url)
                for url in lines
                if url and not url.startswith("#")
            }
        )


class BanDetectionMiddleware:
    """Downloader middleware for detecting bans.

    It adds '_ban': True to request.meta if the response was a ban.

    To enable it, add it to DOWNLOADER_MIDDLEWARES option::

        DOWNLOADER_MIDDLEWARES = {
            # ... "rotating_proxies.middlewares.BanDetectionMiddleware": 620, #
            ...
        }

    By default, client is considered banned if a request failed, and alive if a
    response was received. You can override ban detection method by passing a
    path to a custom BanDetectionPolicy in ``ROTATING_PROXY_BAN_POLICY``, e.g.::

    ROTATING_PROXY_BAN_POLICY = "myproject.policy.MyBanPolicy"

    The policy must be a class with ``response_is_ban`` and ``exception_is_ban``
    methods. These methods can return True (ban detected), False (not a ban) or
    None (unknown). It can be convenient to subclass and modify default
    BanDetectionPolicy::

        # myproject/policy.py
        from rotating_proxies.policy import BanDetectionPolicy


        class MyPolicy(BanDetectionPolicy):
            def response_is_ban(self, request, response):
                # use default rules, but also consider HTTP 200 responses # a
                ban if there is 'captcha' word in response body. ban =
                super().response_is_ban(request, response) ban = ban or
                b"captcha" in response.body return ban

            def exception_is_ban(self, request, exception):
                # override method completely: don't take exceptions in account
                return None

    Instead of creating a policy you can also implement ``response_is_ban`` and
    ``exception_is_ban`` methods as spider methods, for example::

        class MySpider(scrapy.Spider):
            # ...

            def response_is_ban(self, request, response):
                return b"banned" in response.body

            def exception_is_ban(self, request, exception):
                return None

    """

    def __init__(self, stats: StatsCollector, policy: BanDetectionPolicy) -> None:
        self.stats = stats
        self.policy = policy

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        return cls(cast(StatsCollector, crawler.stats), cls._load_policy(crawler))

    @classmethod
    def _load_policy(cls, crawler: Crawler) -> BanDetectionPolicy:
        policy_path = crawler.settings.get(
            "ROTATING_PROXY_BAN_POLICY", "rotating_proxies.policy.BanDetectionPolicy"
        )
        policy_cls = load_object(policy_path)
        if hasattr(policy_cls, "from_crawler"):
            return policy_cls.from_crawler(crawler)
        return policy_cls()

    def process_response(
        self, request: Request, response: Response, spider: Spider
    ) -> Response:
        is_ban = getattr(spider, "response_is_ban", self.policy.response_is_ban)
        ban = is_ban(request, response)
        request.meta["_ban"] = ban
        if ban:
            self.stats.inc_value(f"bans/status/{response.status}")
            if not len(response.body):
                self.stats.inc_value("bans/empty")
        return response

    def process_exception(
        self, request: Request, exception: Exception, spider: Spider
    ) -> None:
        is_ban = getattr(spider, "exception_is_ban", self.policy.exception_is_ban)
        ban = is_ban(request, exception)
        if ban:
            ex_class = (
                f"{exception.__class__.__module__}.{exception.__class__.__name__}"
            )
            self.stats.inc_value(f"bans/error/{ex_class}")
        request.meta["_ban"] = ban
