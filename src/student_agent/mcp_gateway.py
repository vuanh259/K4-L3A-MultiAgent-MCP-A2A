from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .contracts import Contracts


class EvidenceGateway:
    def __init__(
        self,
        endpoint: str,
        team_api_key: str,
        contracts: Contracts,
    ) -> None:
        self._endpoint = endpoint
        self._team_api_key = team_api_key
        self._contracts = contracts
        self._session: ClientSession | None = None
        self._exit_stack: AsyncExitStack | None = None
        self._lock = asyncio.Lock()

    async def _ensure_session(self) -> ClientSession:
        async with self._lock:
            if self._session is not None:
                return self._session
            self._exit_stack = AsyncExitStack()
            headers = {"Authorization": f"Bearer {self._team_api_key}"}
            timeout = httpx2.Timeout(300.0, connect=30.0, read=300.0, write=30.0, pool=30.0)
            http_client = await self._exit_stack.enter_async_context(
                httpx2.AsyncClient(headers=headers, timeout=timeout)
            )
            read_stream, write_stream = await self._exit_stack.enter_async_context(
                streamable_http_client(self._endpoint, http_client=http_client)
            )
            session = await self._exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await session.initialize()
            self._session = session
            return self._session

    async def _reset_session(self) -> None:
        async with self._lock:
            if self._exit_stack:
                with suppress(Exception):
                    await self._exit_stack.aclose()
            self._exit_stack = None
            self._session = None

    async def close(self) -> None:
        await self._reset_session()

    async def list_tools(self) -> list[str]:
        for attempt in range(3):
            try:
                session = await self._ensure_session()
                response = await session.list_tools()
                return sorted(tool.name for tool in response.tools)
            except (Exception, asyncio.CancelledError, BaseExceptionGroup):
                await self._reset_session()
                if attempt == 2:
                    raise
                await asyncio.sleep(1.0 * (attempt + 1))
        return []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        payload = {"case_id": case_id, **arguments}
        result = None
        # The competition gateway drops connections for tens of seconds at a time.
        backoffs = (2.0, 4.0, 8.0, 16.0, 30.0)
        for attempt in range(len(backoffs) + 1):
            try:
                session = await self._ensure_session()
                result = await session.call_tool(tool_name, arguments=payload)
                break
            except (Exception, asyncio.CancelledError, BaseExceptionGroup) as exc:
                await self._reset_session()
                if attempt == len(backoffs):
                    raise RuntimeError(
                        f"MCP tool {tool_name} failed after retries: {exc}"
                    ) from exc
                await asyncio.sleep(backoffs[attempt])

        if result is None:
            raise RuntimeError(f"MCP tool {tool_name} returned no result")

        is_error = getattr(result, "is_error", getattr(result, "isError", False))
        if is_error:
            message = " ".join(
                block.text for block in result.content if getattr(block, "text", None)
            )
            raise RuntimeError(f"MCP tool {tool_name} failed: {message or 'unknown error'}")

        evidence = getattr(result, "structuredContent", None)
        if evidence is None:
            evidence = getattr(result, "structured_content", None)
        if evidence is None:
            text_blocks = [block.text for block in result.content if getattr(block, "text", None)]
            if len(text_blocks) != 1:
                raise ValueError(f"MCP tool {tool_name} did not return one evidence object")
            evidence = json.loads(text_blocks[0])

        self._contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
        return evidence


@asynccontextmanager
async def connect_gateway(
    endpoint: str, team_api_key: str, contracts: Contracts
) -> AsyncIterator[EvidenceGateway]:
    gateway = EvidenceGateway(endpoint, team_api_key, contracts)
    try:
        await gateway._ensure_session()
        yield gateway
    finally:
        await gateway.close()
