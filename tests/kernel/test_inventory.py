from __future__ import annotations

import pytest

from reeloom.kernel.candidates import Candidate, CandidateSnapshot
from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.inventory import ExistingInventory
from reeloom.kernel.mapping import EpisodeCatalog, MappingDraft
from reeloom.kernel.tmdb import TmdbWorkType


def _mapping(*, episode: int) -> MappingDraft:
    candidates = CandidateSnapshot.create(
        [
            Candidate.from_dict(
                {
                    "id": "video:1",
                    "kind": "video",
                    "display_name": "episode.mkv",
                }
            )
        ]
    )
    return MappingDraft.from_dict(
        {
            "videos": [
                {
                    "video_id": "video:1",
                    "season": 1,
                    "episode_start": episode,
                    "episode_end": episode,
                }
            ],
            "subtitles": [],
        },
        candidates=candidates,
        catalog=EpisodeCatalog.from_counts({1: 12}),
    )


def test_inventory_rejects_an_occupied_episode() -> None:
    inventory = ExistingInventory.from_episodes(
        work_type=TmdbWorkType.ANIME,
        tmdb_id=100,
        occupied=((1, 2),),
    )

    with pytest.raises(DomainError) as error:
        inventory.validate(_mapping(episode=2))

    assert error.value.code is ErrorCode.INVENTORY_CONFLICT
    assert error.value.context == {
        "season": 1,
        "episode": 2,
        "video_id": "video:1",
    }


def test_inventory_accepts_an_unoccupied_episode() -> None:
    inventory = ExistingInventory.from_episodes(
        work_type=TmdbWorkType.ANIME,
        tmdb_id=100,
        occupied=((1, 2),),
    )

    inventory.validate(_mapping(episode=3))
