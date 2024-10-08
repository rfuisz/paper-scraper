from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import re
import time
import urllib.parse
from collections.abc import Collection
from logging import Logger
from typing import Literal, TypeVar, cast, overload
from uuid import UUID

import aiohttp
import fitz

from paperscraper.exceptions import NoPDFLinkError

logger = logging.getLogger(__name__)


class ThrottledClientSession(aiohttp.ClientSession):
    """
    Rate-throttled client session.

    Original source: https://stackoverflow.com/a/60357775
    """

    MAX_WAIT_FOR_CLOSE = 6.0  # sec
    TIME_BASE = MAX_WAIT_FOR_CLOSE - 1  # sec

    def __init__(
        self, rate_limit: float | None = None, retry_count: int = 5, *args, **kwargs
    ) -> None:
        """
        Initialize.

        Args:
            rate_limit: Optional number of requests per second to throttle. If left as
                None, no throttling is applied.
            retry_count: Number of retries to attempt on service limit status codes, set
                to 0 to disable retries.
            *args: Positional arguments to pass to aiohttp.ClientSession.__init__.
            **kwargs: Keyword arguments to pass to aiohttp.ClientSession.__init__.
        """
        super().__init__(*args, **kwargs)
        self._rate_limit = rate_limit
        self._retry_count = retry_count
        self._start_time = time.time()
        if rate_limit is not None:
            queue_size = int(rate_limit * self.TIME_BASE)
            if queue_size < 1:
                raise ValueError(
                    f"Rate limit {rate_limit} is too low for a responsive close, please"
                    f" increase to at least {1 / self.TIME_BASE} requests/sec."
                )
            self._queue: asyncio.Queue | None = asyncio.Queue(maxsize=queue_size)
            self._fillerTask: asyncio.Task | None = asyncio.create_task(self._filler())
        else:
            self._queue = None
            self._fillerTask = None

    async def close(self) -> None:
        """Close rate-limiter's "bucket filler" task."""
        if self._fillerTask is not None:
            # There exists an edge case where an empty session gets closed
            # before the filler task even starts. In this edge case, we employ
            # a small asyncio sleep to give a chance to start the filler task.
            await asyncio.sleep(delay=1e-3)
            self._fillerTask.cancel()
            await asyncio.wait_for(self._fillerTask, timeout=self.MAX_WAIT_FOR_CLOSE)
        await super().close()

    async def _filler(self) -> None:
        """Filler task to fill the leaky bucket algo."""
        if self._rate_limit is None:
            return
        queue = cast(asyncio.Queue, self._queue)
        # This sleep interval (sec) is enough to enqueue at least 1 request
        # - If 1 / rate_limit is above 1e-3, we should add on average 1 request
        #   to the queue per below loop iteration
        # - Otherwise, we'll add on average above 1 request to the queue per
        #   below loop iteration
        sleep_interval = max(1 / self._rate_limit, 1e-3)  # sec
        ts_before_sleep = time.perf_counter()
        try:
            while True:
                ts_after_sleep = time.perf_counter()
                # Calculate how many requests to add to the bucket based on elapsed time.
                num_requests_to_add = int(
                    (ts_after_sleep - ts_before_sleep) * self._rate_limit
                )
                # Calculate available space in the queue to avoid overfilling it.
                available_space = queue.maxsize - queue.qsize()
                # Only add as many requests as there is space.
                for _ in range(min(num_requests_to_add, available_space)):
                    # Insert a request (represented as None) into the queue
                    queue.put_nowait(None)

                ts_before_sleep = ts_after_sleep
                await asyncio.sleep(sleep_interval)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Unexpected failure in queue filling.")

    async def _wait_can_make_request(self) -> None:
        if self._queue is not None:
            await self._queue.get()
            self._queue.task_done()

    SERVICE_LIMIT_REACHED_STATUS_CODES: Collection[int] = {429, 503}

    async def _request(self, *args, **kwargs) -> aiohttp.ClientResponse:
        for retry_num in range(self._retry_count + 1):
            await self._wait_can_make_request()
            response = await super()._request(*args, **kwargs)
            if response.status not in self.SERVICE_LIMIT_REACHED_STATUS_CODES:
                break
            if retry_num < self._retry_count:
                exp_backoff_with_jitter = 3 + 0.1 * (2**retry_num + random.random())
                logger.warning(
                    f"Hit a service limit per status {response.status} with message"
                    f" {await response.text()}, sleeping"
                    f" {exp_backoff_with_jitter:.2f}-sec before retry {retry_num + 1}."
                )
                await asyncio.sleep(exp_backoff_with_jitter)
                # NOTE: on next iteration, we have to wait again, which ensures
                # the rate_limit is adhered to
                continue
        else:
            raise RuntimeError(
                f"Failed to avoid a service limit across {self._retry_count} retries."
            )
        return response


def check_pdf(path: str | os.PathLike, verbose: bool | Logger = False) -> bool:
    path = str(path)
    if not os.path.exists(path):
        return False

    try:
        # Open the PDF file using fitz
        with fitz.open(path):
            pass  # For now, just opening the file is our basic check

    except fitz.FileDataError as e:
        if verbose and isinstance(verbose, bool):
            print(f"PDF at {path} is corrupt or unreadable: {e}")
        elif verbose:
            verbose.exception(f"PDF at {path} is corrupt or unreadable.", exc_info=e)
        return False

    return True


# SEE: https://www.crossref.org/blog/dois-and-matching-regular-expressions/
# Test cases: https://regex101.com/r/xtI5bS/10
pattern = r"\/(10.\d{4,9}(?:[\/\.][a-z-().]*(?:[-<>()\/;:\w]*\d+[-<>();:\w]*)+)+)"
compiled_pattern = re.compile(pattern, re.IGNORECASE)


@overload
def find_doi(text: str, disallow_no_match: Literal[True]) -> str: ...
@overload
def find_doi(text: str, disallow_no_match: Literal[False] = False) -> str | None: ...
def find_doi(text: str, disallow_no_match: bool = False) -> str | None:
    match = compiled_pattern.search(urllib.parse.unquote(text))
    if not match:
        if disallow_no_match:
            raise ValueError(f"Failed to find DOI in {text!r}.")
        return None
    return match.group(1)


def encode_id(value: str | bytes | UUID, maxsize: int | None = 16) -> str:
    """Encode a value (e.g. a DOI) optionally with a max length."""
    if isinstance(value, UUID):
        value = str(value)
    if isinstance(value, str):
        value = value.lower().encode()
    return hashlib.md5(value).hexdigest()[:maxsize]  # noqa: S324


def get_scheme_hostname(url: str) -> str:
    parsed_url = urllib.parse.urlparse(url)
    return urllib.parse.ParseResult(
        scheme=parsed_url.scheme,
        netloc=parsed_url.netloc,
        path="",
        params="",
        query="",
        fragment="",
    ).geturl()


def search_pdf_link(text: str, epdf: bool = False) -> str:
    if epdf:
        epdf_link = re.search(r'href="(\S+\.epdf)"', text)
        if epdf_link:
            return epdf_link.group(1).replace("epdf", "pdf")
    else:
        pdf_link = re.search(r'href="(\S+\.pdf)"', text)
        if pdf_link:
            return pdf_link.group(1)
    raise NoPDFLinkError("No PDF link found.")


def crossref_headers():
    """Crossref API key if available, otherwise nothing."""
    if api_key := os.environ.get("CROSSREF_API_KEY"):
        return {"Crossref-Plus-API-Token": f"Bearer {api_key}"}
    return {}


T = TypeVar("T")


async def aidentity_fn(x: T) -> T:
    return x
