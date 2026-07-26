from __future__ import annotations

from reeloom.adapters.tmdb import TmdbHttpAdapter
from reeloom.ports.tmdb import TmdbProvider


class TmdbHttpLease:
    def __init__(self, api_key: str) -> None:
        self._provider = TmdbHttpAdapter(api_key=api_key)

    @property
    def provider(self) -> TmdbProvider:
        return self._provider

    async def close(self) -> None:
        await self._provider.aclose()
