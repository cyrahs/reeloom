import { z } from "zod";

export const workTypeSchema = z.enum(["anime", "tv", "movie"]);

export const sessionSchema = z
  .object({
    api_version: z.literal("1.0.0"),
    role: z.literal("admin"),
  })
  .strict();

export const healthSchema = z
  .object({
    status: z.literal("ok"),
    postgres_major: z.number().int(),
    schema_version: z.number().int(),
    notification_pending: z.number().int().nonnegative().default(0),
    notification_dead: z.number().int().nonnegative().default(0),
    telegram_configured: z.boolean().default(false),
  })
  .strict();

export const runSummarySchema = z
  .object({
    run_id: z.string(),
    status: z.string(),
    work_type: workTypeSchema,
    created_at: z.string(),
    phase: z.string().nullable(),
    plan_hash: z.string().nullable(),
    source_folder: z.string().nullable(),
    available_actions: z.array(z.literal("delete_run")),
  })
  .strict();

export const runsSchema = z
  .object({ items: z.array(runSummarySchema) })
  .strict();

export const discoverySchema = z
  .object({
    discovery_id: z.string(),
    watch_id: z.string(),
    work_type: workTypeSchema,
    discovered_at: z.string(),
    run_id: z.string().nullable(),
    run_status: z.string().nullable(),
    source_folder: z.string().nullable(),
  })
  .strict();

export const discoveriesSchema = z
  .object({ items: z.array(discoverySchema) })
  .strict();

export const foldersSchema = z
  .object({
    items: z.array(
      z
        .object({
          watch_id: z.string(),
          source_folder: z.string(),
          status: z.enum(["settling", "active", "blocked", "settled"]),
          reason_code: z.string().nullable(),
          stable_at: z.string().nullable(),
          run_id: z.string().nullable(),
          retry_count: z.number().int().min(0).max(3),
        })
        .strict(),
    ),
  })
  .strict();

export const runSchema = z
  .object({
    run_id: z.string(),
    status: z.string(),
    work_type: workTypeSchema,
    phase: z.string().nullable(),
    runtime_status: z.string().nullable(),
    event_sequence: z.number().int().nonnegative(),
    model_turns: z.number().int().nonnegative(),
    model_tokens: z.number().int().nonnegative(),
    tool_calls: z.number().int().nonnegative(),
    failures: z.number().int().nonnegative(),
    plan_hash: z.string().nullable(),
    recovery_approval_id: z.string().nullable(),
    apply_policy: z.enum(["plan_only", "manual", "automatic"]),
    available_actions: z.array(
      z.enum([
        "question",
        "revision",
        "approve_apply",
        "reapply",
        "recover",
        "settle_folder",
        "dispose_failed_folder",
        "recover_folder_disposition",
        "delete_run",
      ]),
    ),
    settlement: z
      .object({
        approval_id: z.string(),
        plan_hash: z.string(),
        transaction_id: z.string(),
        status: z.enum(["completed", "rolled_back"]),
        applied_count: z.number().int().nonnegative(),
        rolled_back_count: z.number().int().nonnegative(),
        failure_code: z.string().nullable(),
        settled_at: z.string(),
      })
      .strict()
      .nullable(),
    source_folder: z.string().nullable(),
    folder_disposition: z
      .object({
        plan_hash: z.string(),
        action: z.enum(["archive", "fail", "remove_empty"]),
        target_relative: z.string().nullable(),
        file_count: z.number().int().nonnegative(),
        reason_code: z.string(),
        status: z.enum([
          "planned",
          "prepared",
          "renamed",
          "completed",
          "blocked",
          "recovery_required",
        ]),
        recovery_approval_id: z.string().nullable(),
        failure_code: z.string().nullable(),
        move_backend: z.enum(["native", "clouddrive_webdav"]),
      })
      .strict()
      .nullable(),
    archive_report: z
      .object({
        status: z.enum(["checked", "incomplete"]),
        work_type: workTypeSchema,
        tmdb_id: z.number().int().positive(),
        searches: z.array(
          z
            .object({
              mode: z.enum(["selected_tmdb_id", "name"]),
              match_count: z.number().int().min(0).max(50),
              complete: z.boolean(),
            })
            .strict(),
        ).max(100),
        entries: z.array(
          z
            .object({
              entry_id: z.number().int().positive(),
              parent_entry_id: z.number().int().positive().nullable(),
              kind: z.enum(["directory", "video"]),
              name: z.string().min(1).max(255),
              depth: z.number().int().min(1).max(4),
              listed: z.boolean(),
            })
            .strict(),
        ).max(200),
        possible_existing_archive: z.boolean(),
        advisory_only: z.literal(true),
        observed_at: z.string(),
      })
      .strict()
      .nullable(),
  })
  .strict();

export const planSchema = z
  .object({
    run_id: z.string(),
    version: z.number().int().positive(),
    plan_hash: z.string(),
    parent_plan_hash: z.string().nullable(),
    plan_kind: z.enum(["initial", "amendment"]),
    created_at: z.string(),
  })
  .strict();

export const lineageSchema = z
  .object({ items: z.array(planSchema) })
  .strict();

const previewItemFields = {
  index: z.number().int().nonnegative(),
  candidate_id: z.string(),
  kind: z.enum(["video", "subtitle"]),
  source: z.string(),
};
const previewExplanationSchema = z
  .object({
    reason_code: z.enum([
      "existing_episode",
      "possible_existing_movie",
      "extra_video",
      "ambiguous_mapping",
      "unsupported_content",
      "duplicate_candidate",
      "not_selected",
      "other",
    ]),
    agent_detail: z.string().max(1024).nullable(),
    verification: z.enum(["verified", "advisory", "fallback"]),
    season: z.number().int().min(0).max(999).nullable(),
    episode: z.number().int().min(1).max(100_000).nullable(),
    related_video_id: z.string().nullable(),
  })
  .strict();
const previewItemSchema = z.discriminatedUnion("disposition", [
  z
    .object({
      ...previewItemFields,
      disposition: z.literal("move"),
      destination: z.string(),
      explanation: z.null(),
    })
    .strict(),
  z
    .object({
      ...previewItemFields,
      disposition: z.literal("unmapped"),
      destination: z.null(),
      explanation: previewExplanationSchema,
    })
    .strict(),
  z
    .object({
      ...previewItemFields,
      disposition: z.literal("unchanged"),
      destination: z.null(),
      explanation: z.null(),
    })
    .strict(),
]);

export const previewSchema = z
  .object({
    run_id: z.string(),
    version: z.number().int().positive(),
    plan_hash: z.string(),
    plan_kind: z.enum(["initial", "amendment"]),
    counts: z
      .object({
        move: z.number().int().nonnegative(),
        unmapped: z.number().int().nonnegative(),
        unchanged: z.number().int().nonnegative(),
      })
      .strict(),
    review: z
      .object({
        status: z.enum([
          "agent_and_system",
          "system_only",
          "unavailable",
        ]),
        agent_summary: z.string().max(4096).nullable(),
        advisory_only: z.literal(true),
        coverage: z
          .object({
            total_unmapped: z.number().int().nonnegative(),
            agent_explained: z.number().int().nonnegative(),
            system_verified: z.number().int().nonnegative(),
            fallback: z.number().int().nonnegative(),
          })
          .strict(),
      })
      .strict(),
    items: z.array(previewItemSchema),
    next_after: z.number().int().nonnegative().nullable(),
  })
  .strict();

export const interactionSchema = z
  .object({
    interaction_id: z.string(),
    kind: z.enum(["question", "revision", "reapply"]),
    status: z.enum(["active", "completed", "failed"]),
    request_message: z.string().nullable(),
    assistant_reply: z.string().nullable(),
    content_available: z.boolean(),
    plan_hash: z.string().nullable(),
    created_at: z.string(),
    finished_at: z.string().nullable(),
  })
  .strict();

export const interactionsSchema = z
  .object({ items: z.array(interactionSchema) })
  .strict();

const eventSchema = z
  .object({
    event_id: z.number().int().nonnegative(),
    event_type: z.string(),
    data: z.record(z.string(), z.unknown()),
  })
  .strict();

export const eventsSchema = z
  .object({ items: z.array(eventSchema) })
  .strict();

const watchSchema = z
  .object({
    watch_id: z.string(),
    work_type: workTypeSchema,
    poll_interval_seconds: z.number().int(),
    settle_interval_seconds: z.number().int(),
    root: z.string(),
    library_root: z.string(),
  })
  .strict();

const agentBudgetSchema = z
  .object({
    max_model_turns: z.number().int().min(1).max(1024),
    max_tool_calls: z.number().int().min(1).max(4096),
    max_failures: z.number().int().min(1).max(100),
    max_total_tokens: z.number().int().min(1).max(10_000_000),
    max_elapsed_seconds: z.number().positive().max(3600),
  })
  .strict();

export const configSchema = z
  .object({
    revision: z.number().int().positive(),
    revision_id: z.string(),
    watches: z.array(watchSchema),
    provider: z
      .object({
        base_url: z.string(),
        model: z.string(),
        reasoning_effort: z.string().nullable(),
        verbosity: z.string().nullable(),
        api_key_configured: z.boolean(),
      })
      .strict(),
    telegram: z
      .object({
        enabled: z.boolean(),
        notification_types: z.array(
          z.enum([
            "plan_ready",
            "archive_completed",
            "attention_required",
          ]),
        ).min(1).max(3),
        destination_configured: z.boolean(),
      })
      .strict(),
    apply_policy: z.enum(["plan_only", "manual", "automatic"]),
    agent_budget: agentBudgetSchema,
  })
  .strict();

export const directoryListingSchema = z
  .object({
    path: z.string().max(4096),
    absolute_path: z.string().min(1).max(4096),
    parent: z.string().max(4096).nullable(),
    directories: z.array(
      z
        .object({
          name: z.string().min(1).max(255),
          path: z.string().min(1).max(4096),
        })
        .strict(),
    ).max(1000),
  })
  .strict();

const moveCapabilityCheckSchema = z
  .object({
    status: z.enum([
      "supported",
      "degraded",
      "unsupported",
      "cross_filesystem",
      "uncertain",
    ]),
    failure_code: z.string().nullable(),
  })
  .strict();

export const moveCapabilitySchema = z
  .object({
    watch_id: z.string(),
    move_backend: z.enum(["native", "fuse_checked_rename"]),
    folder_disposition: moveCapabilityCheckSchema,
    media_apply: moveCapabilityCheckSchema,
  })
  .strict();

export const interactionResultSchema = z
  .object({
    interaction_id: z.string(),
    kind: z.enum(["question", "revision"]),
    assistant_reply: z.string(),
    plan_hash: z.string().nullable(),
    model_tokens: z.number().int().nonnegative(),
  })
  .strict();

export const reapplyResultSchema = z
  .object({
    interaction_id: z.string(),
    assistant_reply: z.string(),
    plan_hash: z.string().nullable(),
    no_op: z.boolean(),
  })
  .strict();

export const applyResultSchema = z
  .object({
    transaction_id: z.string(),
    plan_hash: z.string(),
    approval_id: z.string(),
    status: z.string(),
    applied_count: z.number().int().nonnegative(),
    rolled_back_count: z.number().int().nonnegative(),
    folder_disposition: z
      .object({
        run_id: z.string(),
        plan_hash: z.string(),
        approval_id: z.string(),
        transaction_id: z.string(),
        action: z.enum(["archive", "fail", "remove_empty"]),
        target_relative: z.string().nullable(),
        status: z.literal("completed"),
      })
      .strict()
      .nullable(),
  })
  .strict();

export const folderDispositionResultSchema = z
  .object({
    run_id: z.string(),
    plan_hash: z.string(),
    approval_id: z.string(),
    transaction_id: z.string(),
    action: z.enum(["archive", "fail", "remove_empty"]),
    target_relative: z.string().nullable(),
    status: z.literal("completed"),
  })
  .strict();

export const recoveryResultSchema = z
  .object({
    transaction_id: z.string(),
    status: z.string(),
    applied_count: z.number().int().nonnegative(),
    rolled_back_count: z.number().int().nonnegative(),
  })
  .strict();

export const runDeletionSchema = z
  .object({
    run_id: z.string(),
    deleted_at: z.string(),
  })
  .strict();

export type Run = z.infer<typeof runSchema>;
export type Plan = z.infer<typeof planSchema>;
export type Preview = z.infer<typeof previewSchema>;
export type Config = z.infer<typeof configSchema>;
export type RunEvent = z.infer<typeof eventSchema>;
