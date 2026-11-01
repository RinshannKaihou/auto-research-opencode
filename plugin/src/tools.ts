/**
 * Research tools. Ledger tools map one-to-one onto service methods; their
 * descriptions and specs come from the DSH plugin's tools.js. Engine tools
 * (dispatch, wait, finish, conclude) drive the autonomous loop instead, and
 * the delegate tools run node specialists.
 */
import type { Spec } from "./schema"
import { coordinatorMayUseNative } from "./coordinator-boundary"

export type Role = "main" | "node_core" | "specialist"
export type EngineAction = "dispatch" | "wait" | "finish" | "conclude" | "delegate" | "delegate_batch" | "exploration_decide"

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
      'Read research state and fixed references. collection=evidence with refs opens a consistent paged evidence view. Read frozen text separately with collection=evidence and material_ref, following every cursor; material pages can be delivered across model requests. Evidence view offsets count characters; material and draft decision offsets count UTF-8 bytes. Return each cursor unchanged. Query the evidence view again after reading, then prepare a conclusion. Read every page of its draft decision package in the concluding request. exploration_scopes/routes/decisions/results/intents contain choices and history; exploration_budget pages common allocations and resource leases. A read proves delivery, not scientific understanding.',
    parameters: {
      expected_digest: { type: "string" },
      scope_id:{type:'string',description:'For a local exploration choice, query evidence with this scope ID; omit for project-wide conclusion evidence.'},
      scope_owner:{type:'string'},generation:{type:'number'},target_ref:{type:'string'},change_id:{type:'string'},obligation_id:{type:'string'},
      state: { type: "string" }, trigger_kind: { type: "string" },
      cursor: { type: "object", additionalProperties: true, properties: {} },
      decision_refs: { type: "array", items: { type: "string" } }, final_ref: { type: "string" },
      ref: { type: "string" }, refs: { type: "array", items: { type: "string" } },
      material_ref: { type: "string" },
      draft_id: { type: "string" },
      snapshot_id: { type: "string" }, query: { type: "string" }, collection: { type: "string" },
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
      "Record adoption before a structured citation exists. Evidence/dependency citations already derive adoption, so do not consume them again. Same attempt/ref/relation/exact use is deduplicated; different purposes remain distinct. This never rewrites proposal inputs. Only node executors use this tool.",
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
      'Field checks compare only declared fields with a frozen file and do not prove the conclusion. A consistent result does not prove the claim. On revise use changes.checks; omitting checks inherits the declarations. Record or revise sourced knowledge, checkpoint the current work owner, dispose of an impact, or narrow a scope. Checkpoint node_id defaults to the current node (or project for a main session without node work); another owner is rejected. Use checkpoint_recovery.head_revision for checkpoint expected_revision. Revise never inherits execution_refs: omitting it stores []; supply the references again even when the result sources are unchanged. For record, omitted node_id defaults to the current work segment node. Project-wide placement requires visibility="project" explicitly; without a current node supply node_id or project visibility. Placement is retrieval context, not access isolation; keep scientific applicability in conditions/scope. Claims, observations, and lessons require evidence_refs. S-xxx#path identifies an entry inside a frozen snapshot. Bare S-xxx evidence on claims or observations produces an advisory candidate hint. Retracting requires change_kind="retract" together with affected_scope_mode="versions" naming the version withdrawn. Every revise must declare affected_scope_mode: "versions" with the exact versions it invalidates, "none" if it invalidates nothing, or "unknown" if you cannot tell. Use relations[] with grounded_in for what a statement rests on (it becomes the basis and propagates), and motivated_by for what merely prompted the work (it does not propagate); for an open question, a prior finding is grounded_in when the question presupposes it and motivated_by when it only explains why this node was chosen now.',
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
      base: { type: "string", description: "Checkpoint base returned by research_query collection=checkpoint or the last successful checkpoint next_base. Return it unchanged." },
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
      "Publish an immutable partial or complete stage. For an evidence-v1 final report supply draft_id, report_item_id, and exact core claim knowledge_refs; the evidence appendix is generated from the draft. Zero-experiment and empty-findings publications are valid. Display is optional; distinguish findings, hypotheses and limitations.",
    parameters: {
      status: { type: "string", required: true, enum: ["partial", "complete"] },
      summary: { type: "string", required: true },
      draft_id: { type: 'string', description: 'For an evidence-v1 final report, bind the prepared conclusion draft and generate its evidence appendix.' },
      report_item_id: { type: 'string', description: 'The item_id of the final report when draft_id is supplied.' },
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
      "Record a typed relation. Node source is the successor; a target publication identifies its predecessor and material. depends_on requires material inputs and cannot change dispatched nodes. Knowledge grounded_in/motivated_by/answers/supersedes/complements/challenges creates a new revision using explicit revision scope fields. reviews requires obligation_id, result and complete; it closes only that review task, never scientific risk.",
    parameters: {
      source_ref: { type: "string", required: true },
      target_ref: { type: "string", required: true },
      label: { type: "string", required: true },
      note: { type: "string", required: true },
      input_refs:{type:'array',items:{type:'string'}},revision:{type:'object',additionalProperties:true,properties:{}},
      obligation_id:{type:'string'},change_id:{type:'string'},result:{type:'string',enum:['supports','challenges','inconclusive']},
      complete:{type:'boolean'},evidence_refs:{type:'array',items:{type:'string'}},
      review_type:{type:'string',enum:['citation_scope','semantic_inference','recompute','reimplementation','adversarial_test']},
      specialist_task_id:{type:'string'},limitations:{type:'array',items:{type:'string'}},
    },
    fields: args => ({...args,contract_version:'r2-v1'}),
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

const memory = TOOLS.find(tool => tool.name === 'research_memory')!
function narrow(action:string, description:string, keys:string[], required:string[], extra:Record<string,Spec> = {}): ToolDefinition {
  const parameters = Object.fromEntries(keys.map(key => [key,{...memory.parameters[key],required:required.includes(key)}]))
  return {name:`research_${action}`,roles:BOTH,method:'memory_write',writes:true,description,
    parameters:{...parameters,...extra},fields: args => ({action,fields:args,contract_version:'r2-v1'})}
}
TOOLS.push(
  narrow('record','Record sourced knowledge. Claims, observations and lessons require evidence_refs. Placement defaults to the current node; explicitly choose project visibility for project knowledge. Scope/conditions describe scientific applicability.',
    ['kind','statement','node_id','visibility','status','scope','conditions','evidence_refs','dependencies','motivated_by','relations','execution_refs','checks'],['kind','statement']),
  narrow('revise','Revise an exact knowledge version. editorial changes wording/author only; correct_previous corrects only this version; custom requires explicit change_kind and affected scope. preserve_sources and no_new_evidence together retain old sources only when evidence/support/checks are unchanged. Omitted execution_refs otherwise stay empty. Returned field_checks compare declared fields, not scientific truth.',
    ['ref','changes','reason','change_kind','affected_scope_mode','affected_scope','relations','execution_refs'],['ref','changes','reason'],{
      preset:{type:'string',required:true,enum:['editorial','correct_previous','custom']},
      preserve_sources:{type:'boolean'},no_new_evidence:{type:'boolean'},
    }),
  narrow('checkpoint','Save current work state using the base delivered to this model request. No current base: read research_query collection=checkpoint first. Return explicit base/next_base unchanged when needed. Project decision_refs optionally replace the project review scope; scope_reason is required for that change.',
    ['state','base'],['state'],{decision_refs:{type:'array',items:{type:'string'}},scope_reason:{type:'string'}}),
  narrow('dispose','Dispose exactly one change_id/affected_version or one review_event/affected_version challenge. A handled challenge requires evidence_refs; revised requires replacement_ref. Task completion, scope exit and a new review never dispose a challenge.',
    ['change_id','affected_version','disposition_kind','reason','evidence_refs','replacement_ref'],['affected_version','disposition_kind','reason'],{review_event:{type:'number'}}),
  narrow('narrow_scope','Narrow a previously unknown affected scope to explicit fixed versions with a reason. This does not itself resolve scientific risk.',
    ['change_id','affected_scope','reason'],['change_id','affected_scope','reason']),
)
TOOLS.push({name:'research_review_scope',roles:['main'],method:'review_scope_set',writes:true,
  description:'Explicitly replace the project synthesis review scope with fixed adopted references and a reason. Empty refs clears the scope; it does not dispose scientific risks. No checkpoint is needed.',
  parameters:{refs:{type:'array',required:true,items:{type:'string'}},reason:{type:'string',required:true}},fields:same})

const EXPLORATION_WORK: Spec = {type:'object',additionalProperties:false,properties:{
  key:{type:'string',description:'Stable identity of one work allocation; shared probes bind multiple routes once.'},
  budget_amount:{type:'number',description:'Planned units from the configured common work budget; actual usage remains separately observed or unknown.'},
  resources:{type:'object',additionalProperties:true,properties:{},description:'Named positive integer units to hold while executing, for example {"gpu:0":1}.'},
  route_refs:{type:'array',required:true,items:{type:'string'}},question:{type:'string',required:true},
  why_now:{type:'string',required:true},plan:{type:'string',required:true},root_reason:{type:'string'},
  predecessors:{type:'array',items:{type:'object',additionalProperties:true,properties:{}}},
  inputs:{type:'array',items:{type:'string'}},work_kind:{type:'string',enum:['scout','probe','deepen','combine','restore']},
  strategy:{type:'string',enum:['continue','redirect','anchor']},anchor_ref:{type:'string'},question_ref:{type:'string'},
}}

TOOLS.push(
  { name:'research_prepare_conclusion', roles:['main'], method:'conclusion_prepare', writes:true,
    description:'Prepare an evidence-v1 conclusion from a current evidence snapshot. Declare exact core claim versions, outcome, risk judgments, review requirements and why exploration stops. Read every page of the returned draft before publishing its final report.',
    parameters:{delivery_mode:{type:'string',enum:['paged'],description:'Opt into an immutable paged decision package when the complete package exceeds the legacy 32000-byte limit. All pages must still reach the concluding request.'},snapshot_id:{type:'string',required:true},core_claim_refs:{type:'array',required:true,items:{type:'string'}},
      outcome:{type:'string',required:true,enum:['answered','partial','unresolved']},gaps:{type:'array',required:true,items:{type:'string'}},
      risk_judgments:{type:'array',required:true,items:{type:'object',additionalProperties:false,properties:{
        risk_id:{type:'string'},risk_ids:{type:'array',items:{type:'string'}},core_impact:{type:'string',required:true,enum:['yes','no','unknown']},
        disposition:{type:'string',required:true,enum:['handled','unresolved']},reason:{type:'string',required:true},
        evidence_refs:{type:'array',items:{type:'string'}},gap:{type:'string'},
      }}},
      review_requirements:{type:'array',required:true,items:{type:'object',additionalProperties:false,properties:{
        target_ref:{type:'string',required:true},types:{type:'array',required:true,items:{type:'string',enum:['citation_scope','semantic_inference','recompute','reimplementation','adversarial_test']}},
        independent:{type:'boolean'},reason:{type:'string'},
      }}},
      scope_change:{type:'object',additionalProperties:false,description:'Required when removing a previously declared core claim.',properties:{
        reason:{type:'string',required:true},excluded_impact:{type:'string',required:true},
      }},
      exploration_stop:{type:'object',required:true,additionalProperties:true,properties:{
        reason_kind:{type:'string',required:true,enum:['goal_met','sufficient_for_current_choice','low_expected_value','resource_limited','user_requested']},
        reason:{type:'string',required:true},unprocessed_results:{type:'array',items:{type:'string'}},
        scope_judgments:{type:'array',description:'Required for multiple scopes: cover every exact scope version, including deferred and closed scopes.',items:{type:'object',additionalProperties:false,properties:{
          scope_refs:{type:'array',required:true,items:{type:'string'}},reason:{type:'string',required:true},
          remaining_work:{type:'string',required:true},residual_uncertainty:{type:'string',required:true},
          reactivation_condition:{type:'string',required:true},
        }}},
        route_judgments:{type:'array',items:{type:'object',additionalProperties:false,properties:{
          route_refs:{type:'array',required:true,items:{type:'string'}},reason:{type:'string',required:true},
          reactivation_condition:{type:'string',required:true},
        }}},
      }}},
    fields:args=>({fields:args}) },
  { name:'research_scope', roles:['main'], method:'exploration_scope', writes:true,
    description:'Create or revise an exploration scope; multiple scopes may stay active. On revision supply its current scope_ref and reason. Deferred or closed scopes retain all evidence and risks; reactivation must explain changed conditions. Objective or constraints changes create a new goal version.',
    parameters:{scope_ref:{type:'string'},reason:{type:'string'},changed_conditions:{type:'string',description:'Required to reactivate a deferred or closed scope.'},
      changes:{type:'object',required:true,additionalProperties:false,properties:{
        objective:{type:'string'},task_kind:{type:'string',enum:['delivery','selection','discovery']},
        question_refs:{type:'array',items:{type:'string'}},constraints:{},comparison_basis:{},
        baseline_refs:{type:'array',items:{type:'string'}},budget_policy:{},
        parent_scope_ref:{type:'string'},state:{type:'string',enum:['active','deferred','closed']},reactivation_condition:{type:'string'},
      }}}, fields:args=>({fields:args}) },
  { name:'research_route', roles:['main'], method:'exploration_route', writes:true,
    description:'Save a candidate route without creating a node. Supply scope_ref; for edits also supply the current route_ref and a reason. Disposition is set by a decision.',
    parameters:{scope_ref:{type:'string',required:true},route_ref:{type:'string'},reason:{type:'string'},
      changes:{type:'object',required:true,additionalProperties:false,properties:{
        title:{type:'string'},rationale:{type:'string'},critical_assumption:{type:'string'},
        candidate_key:{type:'string',description:'Optional stable mechanism identity within this scope; duplicate keys identify the existing route.'},
        hypothesis_refs:{type:'array',items:{type:'string'}},baseline_refs:{type:'array',items:{type:'string'}},
        evidence_refs:{type:'array',items:{type:'string'}},next_probe:{},cost_estimate:{},reactivation_condition:{},
        component_refs:{type:'array',items:{type:'string'}},
        relations:{type:'array',items:{type:'object',additionalProperties:false,properties:{
          kind:{type:'string',required:true,enum:['competes_with','depends_on','complements','combines']},route_ref:{type:'string',required:true},
        }}},
      }}}, fields:args=>({fields:args}) },
  { name:'research_decide', roles:['main'], engine:'exploration_decide', writes:true,
    description:'Record a local decision after reading collection=evidence with scope_id. Cite current scope/route revisions and actually received result IDs. Schedule next_work or work_batch, sharing a probe by binding multiple routes to one work. Expected intent IDs, provisional choices and later revises_decision_id preserve asynchronous evidence. Unresolved expected work carries forward automatically; defer or stop records a reason without erasing the missing result. Failed/missing work is not refutation. Combinations require a new experiment with frozen inputs; restore from a fixed baseline.',
    parameters:{scope_ref:{type:'string',required:true},snapshot_id:{type:'string',required:true},
      route_refs:{type:'array',required:true,items:{type:'string'}},result_ids:{type:'array',items:{type:'string'}},
      action:{type:'string',required:true,enum:['probe','deepen','redirect','defer','recommend_combine','combine','restore','propose_stop']},
      reason:{type:'string',required:true},route_changes:{type:'array',items:{type:'object',additionalProperties:false,properties:{
        route_ref:{type:'string',required:true},disposition:{type:'string',required:true,enum:['untried','investigating','deferred','excluded','adopted','combination_proposed']},
        reason:{type:'string',required:true},reactivation_condition:{type:'string'},evidence_refs:{type:'array',items:{type:'string'}},
      }}},
      missing_results:{type:'array',items:{}},remaining_options:{type:'array',items:{}},
      provisional:{type:'boolean'},expected_results:{type:'array',items:{type:'string'},description:'Stable intent IDs whose results have not all arrived; explain missing results and keep the decision provisional.'},
      revises_decision_id:{type:'string'},
      comparisons:{type:'array',items:{type:'object',additionalProperties:false,properties:{
        result_refs:{type:'array',required:true,items:{type:'string'}},ranking:{type:'boolean',required:true},reason:{type:'string',required:true},
        conditions:{type:'array',required:true,items:{type:'object',additionalProperties:false,properties:{
          baseline_ref:{type:'string',required:true},workload:{required:true},metric:{type:'string',required:true},unit:{type:'string',required:true},
          environment:{required:true},repeats:{type:'number',required:true},uncertainty:{required:true},
        }}},
      }},description:'One declared condition record per fixed result ref; differing or unknown baseline/workload/metric/unit/environment cannot be ranked.'},
      reactivation_evidence:{type:'object',additionalProperties:true,properties:{},description:'Map deferred/excluded route_ref to {changed_conditions, evidence_refs:[fixed refs]}; preserve the prior negative result.'},
      supersede_intent_ids:{type:'array',items:{type:'string'}},
      next_work:EXPLORATION_WORK,work_batch:{type:'array',items:EXPLORATION_WORK,description:'Mutually exclusive with next_work. A finite batch shares one common project budget; each entry needs a unique key.'}}, fields:same },
)

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
      "End research execution after publishing a complete final report. For evidence-v1, provide draft_id and final_ref after reading the complete draft and publishing its bound report; outcome and review are derived from the draft. Legacy projects provide outcome, gaps and self-declared review. Refused while work is live.",
    parameters: {
      summary: { type: "string", required: true },
      final_ref: { type: "string", required: true, description: "Reference of the final report publication, for example pub/P-004 or pub/P-004#report." },
      draft_id: { type: "string" },
      outcome: { type: "string", enum: ["answered", "partial", "unresolved"] },
      gaps: { type: "array", items: { type: "string" } },
      review: {
        type: "object", additionalProperties: false,
        properties: {
          status: { type: "string", required: true, enum: ["unreviewed", "partial", "reviewed"] },
          refs: { type: "array", required: true, items: { type: "string" } },
          limitations: { type: "array", required: true, items: { type: "string" } },
        },
      },
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
  if (name === 'research_memory') return false
  if (role === "specialist") return specialistTools.includes(name)
  if (NATIVE_BLOCKED.has(name)) return false
  const own = TOOLS.find((tool) => tool.name === name)
  if (own) return own.roles.includes(role as Role)
  if (role === 'node_core') return true
  return coordinatorMayUseNative(name)
}
