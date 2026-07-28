import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createElement } from "react";

import {
  canApproveCurrentPlan,
  DeleteRunDialog,
} from "../src/pages/RunPage";

test("only the current durable plan can be approved", () => {
  expect(canApproveCurrentPlan("sha256:head", "sha256:head")).toBe(true);
  expect(canApproveCurrentPlan("sha256:head", "sha256:old")).toBe(false);
  expect(canApproveCurrentPlan(null, "sha256:head")).toBe(false);
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
