/**
 * Research tools. Ledger tools map one-to-one onto service methods; their
 * descriptions and specs come from the DSH plugin's tools.js. Engine tools
 * (dispatch, wait, finish, conclude) drive the autonomous loop instead, and
 * the delegate tools run node specialists.
 */
import type { Spec } from "./schema"

export type Role = "main" | "node_core" | "specialist"
export type EngineAction = "dispatch" | "wait" | "finish" | "conclude" | "delegate" | "delegate_batch"

export interface ToolDefinition {
  name: string
  description: string
  parameters: Record<string, Spec>
  roles: readonly Role[]
  /** Service method for ledger tools. */
  method?: string
  /** Engine action for loop-control tools. */
  engine?: EngineAction
  writes: boolean
  fields: (args: Record<string, unknown>) => Record<string, unknown>
}

const same = (args: Record<string, unknown>) => args
const BOTH: readonly Role[] = ["main", "node_core"]

export const TOOLS: ToolDefinition[] = [
  {
    name: "research_query",
    roles: [...BOTH, "specialist"],
    method: "query",
    writes: false,
    description:
      'Read a bounded research summary, retrieve a fixed reference, search knowledge, or page a collection. Use the returned cursor to continue. collection="hints" lists current-state structural candidates; use offset/limit to page and kind to filter the hint class. It does not replay historical boundaries.',
    parameters: {
      ref: { type: "string" }, query: { type: "string" }, collection: { type: "string" },
      node_id: { type: "string" }, kind: { type: "string" }, after: { type: "number" },
      status: { type: "string" }, revision: { type: "number" },
      conditions: { type: "object", additionalProperties: true, properties: {} },
      upper_id: { type: "number" }, offset: { type: "number" }, limit: { type: "number" },
    },
    fields: same,
  },
  {
    name: "research_propose",
    roles: BOTH,
    method: "propose",
    writes: true,
    description:
      "Atomically propose a node as an independent root with root_reason or a derived node with typed predecessors and fixed input_refs. A real predecessor must be declared; a handoff error does not make the successor a root. depends_on controls scheduling; branches_from and revises are scientific lineage only and may have empty input_refs; depends_on requires material inputs.",
    parameters: {
      question: { type: "string", required: true },
      why_now: { type: "string", required: true },
      plan: { type: "string", required: true },
      inputs: { type: "array", items: { type: "string" } },
      purpose: { type: "string" },
      strategy: { type: "string", enum: ["continue", "redirect", "anchor"] },
      anchor_ref: { type: "string" },
      question_ref: { type: "string" },
      root_reason: { type: "string" },
      predecessors: {
        type: "array",
        items: {
          type: "object",
          additionalProperties: false,
          properties: {
            node_id: { type: "string", required: true },
            relation_type: { type: "string", required: true, enum: ["depends_on", "branches_from", "revises"] },
            rationale: { type: "string", required: true },
            input_refs: { type: "array", required: true, items: { type: "string" } },
          },
        },
      },
    },
    fields: same,
  },
  {
    name: "research_consume",
    roles: ["node_core"],
    method: "consume",
    writes: true,
    description:
      "Append an idempotent record that the current node work segment adopted a later immutable research reference. This never rewrites the node proposal or its fixed inputs. Only node executors adopt materials.",
    parameters: {
      source_ref: { type: "string", required: true },
      use: { type: "string", required: true },
      relation_type: { type: "string", required: true, enum: ["adopts", "supports", "contradicts", "context"] },
    },
    fields: same,
  },
  {
    name: "research_memory",
    roles: BOTH,
    method: "memory_write",
    writes: true,
    description:
      'Field checks compare only declared fields with a frozen file and do not prove the conclusion. A consistent result does not prove the claim. On revise use changes.checks; omitting checks inherits the declarations. Record or revise sourced knowledge, checkpoint the current node, dispose of an impact, or narrow a scope. Revise never inherits execution_refs: omitting it stores []; supply the references again even when the result sources are unchanged. For record, omitted node_id defaults to the current work segment node. Project-wide placement requires visibility="project" explicitly; without a current node supply node_id or project visibility. Placement is retrieval context, not access isolation; keep scientific applicability in conditions/scope. Claims, observations, and lessons require evidence_refs. S-xxx#path identifies an entry inside a frozen snapshot. Bare S-xxx evidence on claims or observations produces an advisory candidate hint. Retracting requires change_kind="retract" together with affected_scope_mode="versions" naming the version withdrawn. Every revise must declare affected_scope_mode: "versions" with the exact versions it invalidates, "none" if it invalidates nothing, or "unknown" if you cannot tell. Use relations[] with grounded_in for what a statement rests on (it becomes the basis and propagates), and motivated_by for what merely prompted the work (it does not propagate); for an open question, a prior finding is grounded_in when the question presupposes it and motivated_by when it only explains why this node was chosen now.',
    parameters: {
      action: { type: "string", required: true, enum: ["record", "revise", "checkpoint", "dispose", "narrow_scope"] },
      kind: { type: "string", enum: ["observation", "hypothesis", "lesson", "decision", "open_question", "claim"] },
      statement: { type: "string" }, node_id: { type: "string" }, status: { type: "string" },
      visibility: { type: "string", enum: ["node", "project"] },
      scope: { type: "object", additionalProperties: true, properties: {} },
      conditions: { type: "object", additionalProperties: true, properties: {} },
      evidence_refs: { type: "array", items: { type: "string" } },
      dependencies: { type: "array", items: { type: "string" } },
      checks: {
        type: "array",
        items: {
          type: "object",
          additionalProperties: false,
          properties: {
            ref: { type: "string", required: true },
            path: { type: "string", required: true },
            op: { type: "string", required: true, enum: ["eq", "approx", "lt", "le", "gt", "ge"] },
            value: { required: true },
            tolerance: { type: "number" },
          },
        },
      },
      execution_refs: {
        type: "array",
        items: { type: "string" },
        description:
          "Optional execution references for this write: session:, attempt:, event:, or frozen object references. Unresolved references are retained as unlinked. On revise, omission stores an empty list, never inherited; explicitly supply execution_refs again if the result sources are unchanged. asserted_at is server-managed.",
      },
      motivated_by: { type: "array", items: { type: "string" } },
      relations: {
        type: "array",
        items: {
          type: "object",
          properties: {
            type: { type: "string", enum: ["grounded_in", "answers", "supersedes", "complements", "challenges"] },
            target: { type: "string" },
          },
        },
      },
      affected_scope_mode: { type: "string", enum: ["versions", "none", "unknown"] },
      affected_scope: { type: "array", items: { type: "string" } },
      change_kind: { type: "string", enum: ["retract", "correct", "narrow", "reword"] },
      change_id: { type: "string" }, affected_version: { type: "string" },
      disposition_kind: { type: "string", enum: ["unresolved", "retained_with_evidence", "revised", "retracted"] },
      replacement_ref: { type: "string" },
      ref: { type: "string" }, expected_revision: { type: "number" }, reason: { type: "string" },
      changes: { type: "object", additionalProperties: true, properties: {} },
      state: { type: "object", additionalProperties: true, properties: {} },
    },
    fields: (args) => ({
      action: args.action,
      fields: Object.fromEntries(
        Object.entries(args).filter(([key, value]) => key !== "action" && value !== undefined),
      ),
    }),
  },
  {
    name: "research_note",
    roles: BOTH,
    method: "note",
    writes: true,
    description: "Record progress, conditions, gaps, or a human correction in the current research work segment.",
    parameters: {
      body: { type: "string", required: true },
      kind: { type: "string", enum: ["progress", "condition", "gap", "correction"] },
    },
    fields: same,
  },
  {
    name: "research_snapshot",
    roles: BOTH,
    method: "snapshot",
    writes: true,
    description:
      "Freeze selected project files or directories as a handoff snapshot. Runtime control files, credentials, and Git metadata are rejected.",
    parameters: { paths: { type: "array", required: true, items: { type: "string" } } },
    fields: same,
  },
  {
    name: "research_publish",
    roles: BOTH,
    method: "publish",
    writes: true,
    description:
      "Publish an immutable partial or complete stage. Zero-experiment and empty-findings publications are valid. When possible include display: a short title, overview and grouped points in the research goal language; distinguish findings, untested hypotheses and limitations. Keep the detailed account in summary/report; do not call another model to format it. Display is optional for compatibility.",
    parameters: {
      status: { type: "string", required: true, enum: ["partial", "complete"] },
      summary: { type: "string", required: true },
      display: {
        type: "object",
        properties: {
          title: { type: "string", required: true, description: "Short plain-text title, at most 80 characters." },
          overview: { type: "string", required: true, description: "Brief plain-text summary, at most 400 characters." },
          sections: {
            type: "array",
            description: "At most 4 groups; separate results, hypotheses and limitations as appropriate.",
            items: {
              type: "object",
              properties: {
                heading: { type: "string", required: true, description: "At most 80 characters." },
                items: { type: "array", required: true, description: "1–4 plain-text points, each at most 240 characters.", items: { type: "string" } },
              },
            },
          },
          primary_item_id: {
            type: "string",
            description: "Optional item_id of a file report in this publication; never an inferred prose path.",
          },
        },
      },
      gaps: { type: "array", items: { type: "string" } },
      items: {
        type: "array",
        items: {
          type: "object",
          additionalProperties: true,
          properties: {
            item_id: { type: "string", required: true },
            kind: { type: "string" },
            content: { type: "object", additionalProperties: true, properties: {} },
            source_path: { type: "string" },
            knowledge_refs: { type: "array", items: { type: "string" } },
          },
        },
      },
      knowledge_refs: { type: "array", items: { type: "string" } },
    },
    fields: (args) => ({ ...args, gaps: args.gaps ?? [], items: args.items ?? [], knowledge_refs: args.knowledge_refs ?? [] }),
  },
  {
    name: "research_relate",
    roles: BOTH,
    method: "relate",
    writes: true,
    description:
      "Record a research relation between nodes or publications, or a revision rationale. This stores evidence structure and does not judge scientific truth. The reserved knowledge labels grounded_in, answers, supersedes, complements and challenges are refused here when both sides are knowledge versions: write those through research_memory relations[] so they are stored with the revision.",
    parameters: {
      source_ref: { type: "string", required: true },
      target_ref: { type: "string", required: true },
      label: { type: "string", required: true },
      note: { type: "string", required: true },
    },
    fields: same,
  },
  {
    name: "research_close_node",
    roles: BOTH,
    method: "close_node",
    writes: true,
    description: "Explicitly close a research node after its active work segment has ended. A successful publication is not required.",
    parameters: { node_id: { type: "string", required: true } },
    fields: same,
  },
  {
    name: "research_read_input",
    roles: ["specialist"],
    method: "specialist_read_input",
    writes: false,
    description:
      "Read an assigned frozen input by opaque input_id. Offset and limit count Unicode characters; follow next_offset until null.",
    parameters: { input_id: { type: "string", required: true }, offset: { type: "number" }, limit: { type: "number" } },
    fields: same,
  },
]

/** One specialist task; the names follow the DSH plugin's research_delegate. */
const SPECIALIST_TASK: Record<string, Spec> = {
  label: { type: "string", required: true, description: "Short name shown in the session list." },
  question: { type: "string", required: true },
  purpose: { type: "string", required: true },
  inputs: {
    type: "array",
    items: { type: "string" },
    description: "Frozen research references the specialist reads, for example pub/P-001#report or S-001#path.",
  },
  tool_scope: {
    type: "array",
    items: { type: "string", enum: ["read", "glob", "grep", "research_query", "research_read_input"] },
    description: "Optional narrower subset of the specialist's read-only tools.",
  },
  node_id: { type: "string" },
  context_mode: {
    type: "string",
    enum: ["research", "blind"],
    description:
      "research (default): the specialist also reads your workspace and the research records. blind: it sees only the inputs, as input-1, input-2…, read with research_read_input; no workspace, records, project background or conclusions. Blind inputs must be published files (pub/P-…#item).",
  },
  deliverable: { type: "string", required: true },
  completion_criteria: { type: "string", required: true },
  report_requirements: { type: "string", required: true },
}

TOOLS.push(
  {
    name: "research_dispatch",
    roles: ["main"],
    engine: "dispatch",
    writes: true,
    description:
      "Dispatch an existing research node to its own node executor session. The engine queues the task durably and starts it when an execution slot is free. Returns the task; use research_wait to wait for it.",
    parameters: { node_id: { type: "string", required: true } },
    fields: same,
  },
  {
    name: "research_wait",
    roles: ["main"],
    engine: "wait",
    writes: true,
    description:
      "Wait for your dispatched tasks. Your continuation pauses after this turn; when a listed task finishes, its result arrives as a 【Research 自动推进】 message and you continue. No polling is needed.",
    parameters: { task_ids: { type: "array", required: true, items: { type: "string" } } },
    fields: same,
  },
  {
    name: "research_finish",
    roles: ["node_core"],
    engine: "finish",
    writes: true,
    description:
      "End your node work segment after this turn. Use state finished when the node question is answered and stopped when you are blocked. The summary states the evidence, gaps and suggested next steps; the coordinator receives it together with your publications.",
    parameters: {
      state: { type: "string", required: true, enum: ["finished", "stopped"] },
      summary: { type: "string", required: true },
    },
    fields: same,
  },
  {
    name: "research_conclude",
    roles: ["main"],
    engine: "conclude",
    writes: true,
    description:
      "Declare the research project complete after publishing the final report. Autonomous work stops. Refused while node tasks are still running.",
    parameters: {
      summary: { type: "string", required: true },
      final_ref: { type: "string", required: true, description: "Reference of the final report publication, for example pub/P-004 or pub/P-004#report." },
    },
    fields: same,
  },
  {
    name: "research_delegate",
    roles: ["node_core"],
    engine: "delegate",
    writes: true,
    description:
      "Start one bounded read-only specialist in the current node and wait for its report. By default it reads your workspace and the research records; with context_mode blind it sees only its assigned inputs. It cannot run commands, change files or research records, or delegate further. The report is archived in the ledger; judge it on its evidence.",
    parameters: SPECIALIST_TASK,
    fields: same,
  },
  {
    name: "research_delegate_batch",
    roles: ["node_core"],
    engine: "delegate_batch",
    writes: true,
    description:
      "Run a bounded batch of independent read-only specialists in parallel and wait for all of them. A batch larger than the node's specialist limit starts nothing. Returns each report.",
    parameters: {
      tasks: { type: "array", required: true, items: { type: "object", additionalProperties: false, properties: SPECIALIST_TASK } },
    },
    fields: same,
  },
)

export const TOOL_NAMES: readonly string[] = TOOLS.map((tool) => tool.name)

/**
 * Built-in tools no research session gets: a background subagent runs outside
 * the engine's view and a question stalls autonomous work until someone answers.
 */
const NATIVE_BLOCKED = new Set(["subagent", "question"])

/**
 * Whether one model request keeps a tool. Sessions outside a project lose only
 * this plugin's tools; a specialist keeps exactly the tools it was given.
 */
export function keepsTool(
  name: string,
  associated: boolean,
  role: string | undefined,
  specialistTools: readonly string[] = [],
): boolean {
  if (!associated) return !TOOL_NAMES.includes(name)
  if (role === "specialist") return specialistTools.includes(name)
  if (NATIVE_BLOCKED.has(name)) return false
  const own = TOOLS.find((tool) => tool.name === name)
  return !own || own.roles.includes(role as Role)
}
