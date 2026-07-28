import { toPayload } from "../src/pages/ConfigPage";

test("round-trips existing capabilities through explicit retain modes", () => {
  const payload = toPayload({
    watches: [
      {
        watch_id: "primary",
        work_type: "anime",
        poll_interval_seconds: 30,
        settle_interval_seconds: 120,
        rootMode: "retain",
        rootPath: "",
        libraryRootMode: "retain",
        libraryRootPath: "",
      },
    ],
    base_url: "https://api.openai.com/v1",
    model: "gpt-5",
    reasoning_effort: "medium",
    verbosity: "medium",
    credentialMode: "retain",
    apiKey: "",
    apply_policy: "manual",
  });
  expect(payload.watches[0]?.root).toEqual({ mode: "retain" });
  expect(payload.watches[0]?.library_root).toEqual({ mode: "retain" });
  expect(payload.provider.credential).toEqual({ mode: "retain" });
  expect(JSON.stringify(payload)).not.toContain("api_key");
});

test("includes replacement values only after an explicit replace choice", () => {
  const payload = toPayload({
    watches: [
      {
        watch_id: "new",
        work_type: "tv",
        poll_interval_seconds: 60,
        settle_interval_seconds: 180,
        rootMode: "replace",
        rootPath: "/media/incoming",
        libraryRootMode: "replace",
        libraryRootPath: "/media/archive",
      },
    ],
    base_url: "https://provider.example/v1",
    model: "model-1",
    reasoning_effort: "low",
    verbosity: "low",
    credentialMode: "replace",
    apiKey: "new-key",
    apply_policy: "plan_only",
  });
  expect(payload.watches[0]?.root).toEqual({
    mode: "replace",
    path: "/media/incoming",
  });
  expect(payload.watches[0]?.library_root).toEqual({
    mode: "replace",
    path: "/media/archive",
  });
  expect(payload.provider.credential).toEqual({
    mode: "replace",
    api_key: "new-key",
  });
});

test("cannot retain a capability after changing its exact identity", () => {
  const payload = toPayload(
    {
      watches: [
        {
          watch_id: "renamed",
          work_type: "anime",
          poll_interval_seconds: 30,
          settle_interval_seconds: 120,
          rootMode: "retain",
          rootPath: "/media/new-incoming",
          libraryRootMode: "retain",
          libraryRootPath: "/media/new-archive",
        },
      ],
      base_url: "https://api.openai.com/v1",
      model: "gpt-5",
      reasoning_effort: "medium",
      verbosity: "medium",
      credentialMode: "retain",
      apiKey: "",
      apply_policy: "manual",
    },
    {
      revision: 4,
      revision_id: "revision-4",
      watches: [
        {
          watch_id: "primary",
          work_type: "anime",
          poll_interval_seconds: 30,
          settle_interval_seconds: 120,
          root: "/media/incoming",
          library_root: "/media/library",
        },
      ],
      provider: {
        base_url: "https://api.openai.com/v1",
        model: "gpt-5",
        reasoning_effort: "medium",
        verbosity: "medium",
        api_key_configured: true,
      },
      apply_policy: "manual",
    },
  );

  expect(payload.watches[0]?.root).toEqual({
    mode: "replace",
    path: "/media/new-incoming",
  });
  expect(payload.watches[0]?.library_root).toEqual({
    mode: "replace",
    path: "/media/new-archive",
  });
});
