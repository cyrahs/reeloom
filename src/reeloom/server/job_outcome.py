from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class FailureStage(StrEnum):
    AGENT_SETUP = "agent_setup"
    AGENT_LOOP = "agent_loop"
    MEDIA_PLAN = "media_plan"
    SUBTITLE_PLAN = "subtitle_plan"
    EFFECT = "effect"
    SCAN = "scan"
    INTERNAL = "internal"


class RetryMode(StrEnum):
    NONE = "none"
    SAME_OPERATION = "same_operation"
    FRESH_SCAN = "fresh_scan"


class SourceDisposition(StrEnum):
    PRESERVE = "preserve"
    ARCHIVE = "archive"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class FailureEnvelope:
    """Closed failure contract between a worker and the control plane."""

    code: str
    stage: FailureStage
    retry: RetryMode = RetryMode.NONE
    source_disposition: SourceDisposition = SourceDisposition.PRESERVE

    def __post_init__(self) -> None:
        if (
            not isinstance(self.code, str)
            or not self.code
            or len(self.code.encode("utf-8")) > 128
            or not isinstance(self.stage, FailureStage)
            or not isinstance(self.retry, RetryMode)
            or not isinstance(self.source_disposition, SourceDisposition)
        ):
            raise ValueError("invalid failure envelope")


class AgentWorkFailure(RuntimeError):
    def __init__(self, failure: FailureEnvelope) -> None:
        if not isinstance(failure, FailureEnvelope):
            raise TypeError("failure must be a FailureEnvelope")
        self.failure = failure
        super().__init__(failure.code)
