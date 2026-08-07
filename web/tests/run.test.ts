import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createElement } from "react";

import {
  ArchiveReportCard,
  PlanReviewCard,
  RunIdentity,
  canApproveCurrentPlan,
  compactRunId,
  runStatusForDisplay,
  shouldShowArchiveReport,
} from "../src/pages/RunPage";
import { errorMessage } from "../src/components/Status";

test("only the current durable plan can be approved", () => {
  expect(canApproveCurrentPlan("sha256:head", "sha256:head")).toBe(true);
  expect(canApproveCurrentPlan("sha256:head", "sha256:old")).toBe(false);
  expect(canApproveCurrentPlan(null, "sha256:head")).toBe(false);
});

test("archive reference is only shown for the exact current plan", () => {
  expect(shouldShowArchiveReport("sha256:head", "sha256:head")).toBe(true);
  expect(shouldShowArchiveReport("sha256:old", "sha256:head")).toBe(false);
  expect(shouldShowArchiveReport(null, "sha256:head")).toBe(false);
});

test("claimed recovery is not presented as a new approval", () => {
  expect(
    runStatusForDisplay(
      "awaiting_approval",
      "approval-v1-recovery",
      ["recover"],
    ),
  ).toBe("recovery_required");
  expect(
    runStatusForDisplay("awaiting_approval", null, ["approve_apply"]),
  ).toBe("awaiting_approval");
});

test("run identity is compact, accessible, and copies the full value", async () => {
  const runId = "run-0dae51fc45a8db238e0b901e8f420272";
  const writeText = vi.fn().mockResolvedValue(undefined);
  vi.stubGlobal("navigator", {
    clipboard: { writeText },
  });

  expect(compactRunId(runId)).toBe("run-0dae51fc…8f420272");
  expect(compactRunId("run-short")).toBe("run-short");
  expect(compactRunId("运行-一二三四五六七八九十一二三四五六七八九十二三四五六七")).toContain(
    "…",
  );

  render(createElement(RunIdentity, { runId }));
  expect(
    screen.getByLabelText(`完整运行 ID：${runId}`),
  ).toHaveAttribute("title", runId);
  expect(screen.getByText("run-0dae51fc…8f420272")).toBeVisible();

  await userEvent.click(screen.getByRole("button", { name: "复制" }));
  expect(writeText).toHaveBeenCalledWith(runId);
  expect(screen.getByRole("button", { name: "已复制" })).toBeVisible();
});

test("permission failures have actionable text", () => {
  expect(errorMessage("permission_denied")).toMatch(/修复权限/);
});

test("interaction budget failures distinguish retryable timeouts", () => {
  expect(errorMessage("interaction_budget_exhausted")).toMatch(
    /单次超时可重试/,
  );
});

test("archive reference renders untrusted names as plain text", () => {
  const malicious = "<img src=x onerror=alert(1)>";
  const { container } = render(createElement(ArchiveReportCard, {
    report: {
      status: "incomplete",
      work_type: "movie",
      tmdb_id: 42,
      searches: [
        {
          mode: "name",
          match_count: 1,
          complete: false,
        },
      ],
      entries: [
        {
          entry_id: 1,
          parent_entry_id: null,
          kind: "directory",
          name: malicious,
          depth: 1,
          listed: false,
        },
      ],
      possible_existing_archive: true,
      advisory_only: true,
      observed_at: "2026-07-28T00:00:00+00:00",
    },
  }));

  expect(screen.getByText(malicious)).toBeInTheDocument();
  expect(container.querySelector("img")).toBeNull();
  expect(screen.getByText(/不能视为完整库存/)).toBeInTheDocument();
});

test("plan review labels Agent text as advisory and renders it literally", () => {
  const malicious = "<script>alert(1)</script>";
  const { container } = render(createElement(PlanReviewCard, {
    review: {
      status: "agent_and_system",
      agent_summary: malicious,
      advisory_only: true,
      coverage: {
        total_unmapped: 1,
        agent_explained: 1,
        system_verified: 1,
        fallback: 0,
      },
    },
  }));

  expect(screen.getByText(malicious)).toBeInTheDocument();
  expect(screen.getByText(/Agent 判断（仅供审查）/)).toBeInTheDocument();
  expect(container.querySelector("script")).toBeNull();
});
