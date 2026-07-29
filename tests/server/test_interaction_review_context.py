from reeloom.server.interaction_executor import AgentInteractionExecutor


class _Queries:
    def __init__(self) -> None:
        self.preview_calls = 0

    def get_plan(
        self,
        *,
        run_id: str,
        version: int | None,
    ) -> dict[str, object]:
        assert run_id == "run-1"
        assert version is None
        return {"version": 1, "plan_hash": "sha256:head"}

    def get_plan_preview(
        self,
        *,
        run_id: str,
        version: int,
        after: int,
        limit: int,
    ) -> dict[str, object]:
        assert (run_id, version) == ("run-1", 1)
        self.preview_calls += 1
        return {
            "counts": {"move": 1, "unmapped": 1, "unchanged": 0},
            "review": {
                "status": "system_only",
                "agent_summary": None,
                "advisory_only": True,
                "coverage": {
                    "total_unmapped": 1,
                    "agent_explained": 0,
                    "system_verified": 1,
                    "fallback": 0,
                },
            },
            "items": [
                {
                    "candidate_id": "video:13",
                    "disposition": "unmapped",
                    "source": "/absolute/must-not-enter-context.mkv",
                    "explanation": {
                        "reason_code": "existing_episode",
                        "agent_detail": None,
                        "verification": "verified",
                        "season": 0,
                        "episode": 3,
                        "related_video_id": None,
                    },
                }
            ],
        }


def test_interaction_context_contains_only_bounded_review_fields() -> None:
    queries = _Queries()
    executor = AgentInteractionExecutor(
        scheduler=object(),  # type: ignore[arg-type]
        definitions=object(),  # type: ignore[arg-type]
        configs=object(),  # type: ignore[arg-type]
        sessions=object(),  # type: ignore[arg-type]
        layouts=object(),  # type: ignore[arg-type]
        secrets=object(),  # type: ignore[arg-type]
        plans=object(),  # type: ignore[arg-type]
        model_factory=object(),  # type: ignore[arg-type]
        tmdb_factory=object(),  # type: ignore[arg-type]
        queries=queries,  # type: ignore[arg-type]
    )

    context = executor._review_context(
        run_id="run-1",
        plan_hash="sha256:head",
    )

    assert "video:13" in context
    assert "existing_episode" in context
    assert "/absolute/" not in context
    assert queries.preview_calls == 1
