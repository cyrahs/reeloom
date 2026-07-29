import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createElement } from "react";

import {
  ArchiveReportCard,
  PlanReviewCard,
  canApproveCurrentPlan,
  shouldShowArchiveReport,
} from "../src/pages/RunPage";
import { DeleteRunDialog } from "../src/components/RunDeletion";
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

test("permission failures have actionable text", () => {
  expect(errorMessage("permission_denied")).toMatch(/修复权限/);
});

test("run deletion requires explicit acknowledgement", async () => {
  const onConfirm = vi.fn();
  render(createElement(DeleteRunDialog, {
    runId: "run-1",
    pending: false,
    onCancel: () => undefined,
    onConfirm,
  }));

  const confirm = screen.getByRole("button", {
    name: "确认删除记录",
  });
  expect(confirm).toBeDisabled();
  expect(screen.getByText(/媒体文件不会改变/)).toBeInTheDocument();

  await userEvent.click(screen.getByRole("checkbox"));
  await userEvent.click(confirm);
  expect(onConfirm).toHaveBeenCalledOnce();
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
