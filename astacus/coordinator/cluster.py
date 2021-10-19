"""
Copyright (c) 2021 Aiven Ltd
See LICENSE for details
"""
from astacus.common import ipc, op, utils
from astacus.common.magic import LockCall
from astacus.common.progress import Progress
from astacus.common.statsd import StatsClient
from astacus.common.utils import AsyncSleeper
from astacus.coordinator.config import CoordinatorNode, PollConfig
from enum import Enum
from typing import Callable, cast, Dict, List, Optional, Sequence, Type, TypeVar, Union

import asyncio
import httpx
import json
import logging

logger = logging.getLogger(__name__)

T = TypeVar("T")
NR = TypeVar("NR", bound=ipc.NodeResult)
Result = Union[BaseException, httpx.Response, Dict]


class LockResult(Enum):
    ok = "ok"
    failure = "failure"
    exception = "exception"


class Cluster:
    def __init__(
        self,
        *,
        nodes: List[CoordinatorNode],
        poll_config: Optional[PollConfig] = None,
        subresult_url: Optional[str] = None,
        subresult_sleeper: Optional[AsyncSleeper] = None,
        stats: Optional[StatsClient] = None
    ):
        self.nodes = nodes
        self.poll_config = PollConfig() if poll_config is None else poll_config
        self.subresult_url = subresult_url
        self.subresult_sleeper = subresult_sleeper
        self.stats = stats
        self.progress_handler: Optional[Callable[[Progress], None]] = None

    def set_progress_handler(self, progress_handler: Optional[Callable[[Progress], None]]):
        self.progress_handler = progress_handler

    async def request_lock(self, *, locker: str, ttl: int) -> LockResult:
        return await self._request_lock_call_from_nodes(call=LockCall.lock, locker=locker, ttl=ttl, nodes=self.nodes)

    async def request_unlock(self, *, locker: str) -> LockResult:
        return await self._request_lock_call_from_nodes(call=LockCall.unlock, locker=locker, nodes=self.nodes)

    async def request_relock(self, *, node: CoordinatorNode, locker: str, ttl: int) -> LockResult:
        assert node in self.nodes
        return await self._request_lock_call_from_nodes(call=LockCall.relock, locker=locker, ttl=ttl, nodes=[node])

    async def request_from_nodes(
        self,
        url,
        *,
        caller: str,
        req: Optional[ipc.NodeRequest] = None,
        nodes: Optional[List[CoordinatorNode]] = None,
        **kw
    ) -> Sequence[Optional[Result]]:
        if nodes is None:
            nodes = self.nodes
        if req is not None:
            assert isinstance(req, ipc.NodeRequest)
            if self.subresult_url is not None:
                req.result_url = self.subresult_url
            kw["data"] = req.json()
        urls = [f"{node.url}/{url}" for node in nodes]
        aws = [utils.httpx_request(url, caller=caller, **kw) for url in urls]
        results = await asyncio.gather(*aws, return_exceptions=True)
        logger.info("request_from_nodes %r => %r", urls, results)
        return results

    async def _request_lock_call_from_nodes(
        self, *, call: LockCall, locker: str, ttl: int = 0, nodes: List[CoordinatorNode]
    ) -> LockResult:
        results = await self.request_from_nodes(
            f"{call}?locker={locker}&ttl={ttl}",
            method="post",
            ignore_status_code=True,
            json=False,
            nodes=nodes,
            caller="Cluster._request_lock_call_from_nodes"
        )
        logger.debug("%s results: %r", call, results)
        if call in [LockCall.lock, LockCall.relock]:
            expected_result = {"locked": True}
        elif call in [LockCall.unlock]:
            expected_result = {"locked": False}
        else:
            raise NotImplementedError(f"Unknown lock call: {call!r}")
        rv = LockResult.ok
        for node, result in zip(nodes, results):
            # This assert helps mypy handle request_from_nodes return type dependent on its json parameter
            assert not isinstance(result, dict)
            if result is None or isinstance(result, BaseException):
                logger.info("Exception occurred when talking with node %r: %r", node, result)
                if rv != LockResult.failure:
                    # failures mean that we're done, so don't override them
                    rv = LockResult.exception
            elif result.is_error:
                logger.info("%s of %s failed - unexpected result %r %r", call, node, result.status_code, result)
                rv = LockResult.failure
            else:
                try:
                    decoded_result = result.json()
                except json.JSONDecodeError:
                    decoded_result = None
                if decoded_result != expected_result:
                    logger.info("%s of %s failed - unexpected result %r", call, node, decoded_result)
                    rv = LockResult.failure
        if rv == LockResult.failure and self.stats is not None:
            self.stats.increase("astacus_lock_call_failure", tags={
                "call": call,
                "locker": locker,
            })
        return rv

    async def wait_successful_results(
        self,
        *,
        start_results: Sequence[Optional[Result]],
        result_class: Type[NR],
        required_successes: Optional[int] = None
    ) -> List[NR]:
        urls = []

        for i, start_result in enumerate(start_results, 1):
            if not start_result or isinstance(start_result, BaseException):
                logger.info(
                    "wait_successful_results: Incorrect start result for #%d/%d: %r", i, len(start_results), start_result
                )
                raise WaitResultError(f"incorrect start result for #{i}/{len(start_results)}: {start_result!r}")
            parsed_start_result = op.Op.StartResult.parse_obj(start_result)
            urls.append(parsed_start_result.status_url)
        if required_successes is not None and len(urls) != required_successes:
            raise WaitResultError(f"incorrect number of results: {len(urls)} vs {required_successes}")
        results: List[Optional[NR]] = [None] * len(urls)
        # Note that we don't have timeout mechanism here as such,
        # however, if re-locking times out, we will bail out. TBD if
        # we need timeout mechanism here anyway.
        failures = {i: 0 for i in range(len(results))}

        async for _ in utils.exponential_backoff(
            initial=self.poll_config.delay_start,
            multiplier=self.poll_config.delay_multiplier,
            maximum=self.poll_config.delay_max,
            duration=self.poll_config.duration,
            async_sleeper=self.subresult_sleeper,
        ):
            for i, (url, result) in enumerate(zip(urls, results)):
                # TBD: This could be done in parallel too
                if result is not None and result.progress.final:
                    continue
                r = await utils.httpx_request(
                    url, caller="Nodes.wait_successful_results", timeout=self.poll_config.result_timeout
                )
                if r is None:
                    failures[i] += 1
                    if failures[i] >= self.poll_config.maximum_failures:
                        raise WaitResultError("too many failures")
                    continue
                # We got something -> decode the result
                result = result_class.parse_obj(r)
                results[i] = result
                failures[i] = 0
                if self.progress_handler is not None:
                    self.progress_handler(Progress.merge(r.progress for r in results if r is not None))
                if result.progress.finished_failed:
                    raise WaitResultError
            if not any(True for result in results if result is None or not result.progress.final):
                break
        else:
            logger.debug("wait_successful_results timed out")
            raise WaitResultError("timed out")
        # The case is valid because we get there when all results are not None
        return cast(List[NR], results)


class WaitResultError(Exception):
    pass