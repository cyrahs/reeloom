import {
  previewSchema,
  moveCapabilitySchema,
  runDeletionSchema,
  runSchema,
  workTypeSchema,
} from "../src/schemas";
import { workTypeLabel } from "../src/workTypes";

const preview = {
  run_id: "run-1",
  version: 1,
  plan_hash: "sha256:head",
  plan_kind: "initial",
  counts: { move: 1, unmapped: 0, unchanged: 0 },
  next_after: null,
};

test("preview destination is bound to the move disposition", () => {
  expect(
    previewSchema.safeParse({
      ...preview,
      items: [
        {
          index: 0,
          disposition: "move",
          candidate_id: "candidate-1",
          kind: "video",
          source: "source.mkv",
          destination: null,
        },
      ],
    }).success,
  ).toBe(false);
  expect(
    previewSchema.safeParse({
      ...preview,
      items: [
        {
          index: 0,
          disposition: "unmapped",
          candidate_id: "candidate-1",
          kind: "video",
          source: "source.mkv",
          destination: "unexpected.mkv",
        },
      ],
    }).success,
  ).toBe(false);
});

test("Movie uses the shared read model and Chinese label", () => {
  expect(workTypeSchema.parse("movie")).toBe("movie");
  expect(workTypeLabel("movie")).toBe("电影");
});

test("run deletion and action schemas remain strict", () => {
  expect(
    runDeletionSchema.safeParse({
      run_id: "run-1",
      deleted_at: "2026-07-28T12:00:00Z",
    }).success,
  ).toBe(true);
  expect(
    runDeletionSchema.safeParse({
      run_id: "run-1",
      deleted_at: "2026-07-28T12:00:00Z",
      extra: true,
    }).success,
  ).toBe(false);
  const actions = runSchema.shape.available_actions;
  expect(actions.parse(["delete_run"])).toEqual(["delete_run"]);
});

test("move capability response is bounded and strict", () => {
  const value = {
    watch_id: "watch-1",
    move_backend: "native",
    folder_disposition: {
      status: "unsupported",
      failure_code: "atomic_move_unsupported",
    },
    media_apply: {
      status: "supported",
      failure_code: null,
    },
  };
  expect(moveCapabilitySchema.safeParse(value).success).toBe(true);
  expect(
    moveCapabilitySchema.safeParse({ ...value, absolute_path: "/private" })
      .success,
  ).toBe(false);
});
