from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from reeloom.kernel.candidates import CandidateId
from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.subtitles import MAX_SUBTITLE_SAMPLE_BYTES


@dataclass(frozen=True, slots=True)
class SubtitleSample:
    display_name: str
    content: bytes

    def __post_init__(self) -> None:
        if (
            not isinstance(self.display_name, str)
            or not isinstance(self.content, bytes)
            or len(self.content) > MAX_SUBTITLE_SAMPLE_BYTES
        ):
            raise DomainError(ErrorCode.INVALID_SUBTITLE_VARIANT)


class SubtitleSampleProvider(Protocol):
    @property
    def snapshot_id(self) -> str: ...

    @property
    def candidate_count(self) -> int: ...

    async def sample(
        self,
        subtitle_id: CandidateId,
        *,
        max_bytes: int,
    ) -> SubtitleSample: ...
