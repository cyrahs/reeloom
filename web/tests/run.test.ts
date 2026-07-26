import { canApproveCurrentPlan } from "../src/pages/RunPage";

test("only the current durable plan can be approved", () => {
  expect(canApproveCurrentPlan("sha256:head", "sha256:head")).toBe(true);
  expect(canApproveCurrentPlan("sha256:head", "sha256:old")).toBe(false);
  expect(canApproveCurrentPlan(null, "sha256:head")).toBe(false);
});
