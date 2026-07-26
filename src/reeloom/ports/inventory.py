from __future__ import annotations

from typing import Protocol

from reeloom.kernel.inventory import ExistingInventory
from reeloom.kernel.tmdb import TmdbWorkType


class ExistingInventoryProvider(Protocol):
    async def get_inventory(
        self,
        *,
        work_type: TmdbWorkType,
        tmdb_id: int,
    ) -> ExistingInventory: ...
