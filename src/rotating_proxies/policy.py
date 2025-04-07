from typing import ClassVar

from scrapy.exceptions import IgnoreRequest
from scrapy.http.request import Request
from scrapy.http.response import Response


class BanDetectionPolicy:
    """Default ban detection rules."""

    NOT_BAN_STATUSES: ClassVar[set[int]] = {200, 301, 302}
    NOT_BAN_EXCEPTIONS: ClassVar[tuple[type[Exception]]] = (IgnoreRequest,)

    def response_is_ban(self, request: Request, response: Response) -> bool:  # noqa: ARG002
        if response.status not in self.NOT_BAN_STATUSES:
            return True
        if response.status == 200 and not len(response.body):  # noqa: SIM103, PLR2004
            return True
        return False

    def exception_is_ban(self, request: Request, exception: Exception) -> bool:  # noqa: ARG002
        return not isinstance(exception, self.NOT_BAN_EXCEPTIONS)
