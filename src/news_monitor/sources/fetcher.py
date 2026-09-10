"""HTTP fetching of listing and article pages."""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

MAX_PAGE_BYTES = 4 * 1024 * 1024


class FetchError(RuntimeError):
    """Raised when a page cannot be retrieved."""


class HttpHtmlFetcher:
    """Fetches HTML over HTTP with a bounded timeout and response size."""

    def __init__(
        self,
        *,
        user_agent: str,
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client = client
        self._owns_client = client is None
        self._user_agent = user_agent
        self._timeout = timeout_seconds

    async def __aenter__(self) -> "HttpHtmlFetcher":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
                headers={
                    "User-Agent": self._user_agent,
                    "Accept": "text/html,application/xhtml+xml",
                },
            )
            self._owns_client = True
        return self._client

    async def fetch(self, url: str) -> str:
        """Return the decoded HTML body of the given url."""
        client = self._get_client()
        try:
            response = await client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # Only the exception type is reported so that no header can leak.
            raise FetchError(f"cannot fetch page: {type(exc).__name__}") from exc
        if len(response.content) > MAX_PAGE_BYTES:
            raise FetchError("page exceeds the allowed size")
        return response.text

    async def aclose(self) -> None:
        """Close the client when this fetcher created it."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
