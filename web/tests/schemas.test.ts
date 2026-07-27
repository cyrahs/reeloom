import { previewSchema, workTypeSchema } from "../src/schemas";
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
