from __future__ import annotations

from typing import TypeAlias

from reeloom.kernel.errors import DomainError
from reeloom.kernel.forward_execution import RenamePlanV2
from reeloom.kernel.movie_plan import MovieRenamePlan
from reeloom.kernel.movie_forward_execution import MovieRenamePlanV2
from reeloom.kernel.rename_plan import RenamePlan

InitialPlan: TypeAlias = (
    RenamePlan | MovieRenamePlan | RenamePlanV2 | MovieRenamePlanV2
)


def parse_initial_plan(content: bytes, *, plan_hash: str) -> InitialPlan:
    errors: list[DomainError] = []
    for plan_type in (
        RenamePlan,
        MovieRenamePlan,
        RenamePlanV2,
        MovieRenamePlanV2,
    ):
        try:
            return plan_type.from_canonical_bytes(
                content,
                plan_hash=plan_hash,
            )
        except DomainError as error:
            errors.append(error)
    raise errors[-1]


def verify_initial_plan_bytes(content: bytes, plan_hash: str) -> bool:
    try:
        parse_initial_plan(content, plan_hash=plan_hash)
    except (DomainError, ValueError):
        return False
    return True
