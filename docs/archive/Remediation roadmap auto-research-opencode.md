> **已废止的历史规划。** 2026-11-01 归档；下文保留原时间、基线和交付描述，均不作为当前开发要求。当前以 [探索设计](<../Exploration and multi-route management design.md>)、[新路线图](<../Remediation roadmap auto-research-opencode.md>) 和 [新实施计划](<../Remediation implementation plan auto-research-opencode.md>) 为准。归档时只增加此说明并调整跨目录链接。

# Remediation roadmap: reduce coordination overhead before adding controls

Oct 31, 2026 · A repair direction derived from the rigidity audit and local run analysis

## Purpose and relationship to the other documents

This roadmap changes the delivery order of `Improvement roadmap auto-research-opencode.md`; it does not replace its evidence, risk or review semantics.

The sequence is now:

1. **Reduce routine coordination overhead and restore the visibility of findings.**
2. **Simplify writes while preserving concurrency and evidence semantics.**
3. **Move audit work to the decision points where it can change an answer.**
4. **Offer strict confirmatory validation as an opt-in path for selected experiments and claims.**

The governing rule is: **automate the machine's bookkeeping, preserve the agent's evidence judgments, and do not turn structural labels into research-quality scores.**

## Evidence from actual coordinator contexts

The rigidity audit's synthetic probe is directionally confirmed by the saved contexts of the eight current-protocol local projects. The exact synthetic percentage is not universal.

Across 1,055 actual coordinator context requests:

- The median `pending_review` share was 18.3% of the context characters.
- The median `pending_review + structure_hints` share was 32.8%.
- The median `knowledge` share was 20.2%.
- The median visible knowledge selection contained five entries, while the median pending-review selection represented 12 items.
- In the latest context of each project, process blocks occupied 27.9–42.7%, while knowledge occupied 14.2–23.4%.

The structural problem is therefore not only the 12k-character cap:

- Review candidates are queued on publications, finished attempts, knowledge revisions and correction notes.
- The coordinator selection emphasizes disputed, revised, lesson and project-level entries; ordinary node claims may not appear.
- Pending audit work is placed before knowledge in the context blocks.
- The backend has review-todo state transitions, but the plugin does not expose a normal clearing path. All 161 review todos in the local current-protocol snapshot remained pending.

Successful mutation records also identify where write-side measurement should start: 344 `research_memory` writes, including 83 checkpoints, and 155 `research_consume` calls. The `requests` table does not include all reads, failed calls or native shell work, so these counts must not be presented as total agent workload or refusal rates.

## Non-negotiable guardrails

- Current, decision-relevant risks must remain visible. Overhead reduction must not hide a retracted basis, an unresolved impact or an inconsistent check needed by the current task or final report.
- `unknown` never becomes a pass, and an empty citation or review set never proves that nothing needs review.
- Authorship is not execution provenance. Automatically recording the current session or attempt must not imply that it produced the evidence.
- Concurrency control may be hidden from the model, but not removed.
- Common-case presets must expand into explicit stored semantics; the server must not silently guess scientific invalidation or lineage.
- Exploratory and diagnostic research remains legitimate without confirmatory predictions, data isolation or execution-linked evidence.
- Existing projects and tool calls require compatible migration. Historical labels must not be silently upgraded.
- Fewer calls, more relations and more reviews are not themselves research-quality objectives.

## Target context model

Replace the undifferentiated audit feed with four distinct views.

### 1. Critical now

Keep only risks relevant to the current node, the material being synthesized or the final report:

- retracted or affected bases;
- unresolved impacts requiring a decision;
- inconsistent field checks and relevant not-checkable results;
- publication-reference risks for the current deliverable.

These items stay ahead of optional background information.

### 2. Findings for the current decision

Select knowledge using the current question, recently completed nodes, newly published materials and declared dependencies. Include ordinary claims and observations, not only disputed, revised, lesson or project-level entries.

Show scope and conditions with the finding. Use targeted expansion through `research_query` rather than placing the entire project history in every request.

Before final synthesis, load complete records for every cited entry as specified in the improvement roadmap.

### 3. Backlog summary

Show routine review pressure as a count and, at most, a small number of decision-relevant examples. Keep the full queue available through queries and the board.

Historical backlog unrelated to the current answer must not consume more context than the findings and must not automatically block conclusion.

### 4. Clerical work on demand

Move prose-mention, lineage-completion and similar relation-bookkeeping hints to the board or a consolidation action. Keep publication-risk and failed-check hints in `critical now` when they affect a current decision.

## Phase 1. Subtract routine overhead before adding protocol

### 1.1 Split audit signals by urgency and scope

In the context builder:

- separate critical risks from routine review candidates;
- replace routine `pending_review` details with a backlog summary;
- split clerical structure hints from safety-relevant hints;
- do not include an ordinary historical backlog item in every turn merely because it is old.

Do not delete or mark items complete to improve the display. The change is in selection and presentation, not historical records.

### 1.2 Make findings visible to the coordinator

Revise coordinator knowledge selection to include:

- claims and observations from recently completed nodes;
- knowledge pinned by new publications;
- entries related to the current question or planned synthesis;
- their scope, conditions and current risk state.

Keep independent bounded selections for disputes and revisions, but do not let them replace ordinary findings.

### 1.3 Change review todos from automatic debt to candidates

Use a two-stage lifecycle:

1. A publication, finish, revision or correction creates a **candidate**, either as a new explicit state or as a derived view.
2. Only a load-bearing, affected or explicitly promoted candidate becomes a pending obligation.

Automatically close an obligation when a suitable review is linked, a disposition resolves it, or it leaves the relevant claim scope. Preserve an exceptional manual transition API, but do not require the model to clear every historical event by hand.

Prefer concrete `(change_id, affected_version)` obligations over an undifferentiated list of everything that happened.

### 1.4 Establish the before/after measurements

Use saved `context_requests` and `context_packs` to measure:

- routine audit characters per coordinator request;
- visible findings per completed node;
- presence of scope and conditions with those findings;
- current critical-risk recall;
- repeated exposure to the same unchanged backlog.

Do not optimize the process share toward zero. The target is less routine debt with complete recall of current critical risks.

**Done when:** routine audit detail no longer dominates coordinator context; ordinary findings from every completed node are discoverable; the attention and retracted-basis fixtures remain visible at the decision point; and no historical item is silently discarded.

## Phase 2. Simplify writes without weakening their meaning

### 2.1 Split tools and keep compatibility

Split `research_memory` into record, revise, checkpoint, dispose and narrow-scope tools. Keep a versioned adapter for the existing action-based interface during migration.

Provide one relation-write route. Validate reserved labels from their type and target rather than requiring the model to remember which of two tools accepts them.

### 2.2 Replace revision boilerplate with explicit presets

Offer a small set of model-selected presets, for example:

- `editorial`: reword, invalidates nothing;
- `correct_previous`: correction, invalidates the previous version;
- `custom`: full change kind, affected-scope mode and exact versions.

Expand presets into the existing explicit revision fields and return the expanded result. Retraction, narrowing and ambiguous corrections continue to require explicit scope. Do not infer invalidation solely from which text fields changed.

### 2.3 Hide checkpoint version copying, not the concurrency check

The plugin should carry the checkpoint base revision from the context or another server-managed snapshot into the write. The backend continues its compare-and-swap check and returns a conflict when the base is stale.

The model submits the new state, not a manually copied `head_revision`. A stale checkpoint must never be appended as though it were based on the latest state.

### 2.4 Separate authorship from execution provenance

Continue to derive `asserted_at` from the server-managed session and attempt. Do not auto-fill those identities as `execution_refs`.

Allow an explicit `preserve_sources` path only when the reviser declares that no new experiment or evidence source was introduced. New or changed evidence requires its own execution link; otherwise preserve `unknown` or empty provenance rather than manufacturing certainty.

### 2.5 Derive only unambiguous lineage and adoption

From an input publication, derive its owner node and material reference. Ask for relation type and rationale when the scientific relationship is not structurally determined; do not guess `depends_on` versus `branches_from` or `revises`.

When a later evidence or dependency reference already records adoption unambiguously, avoid a duplicate consume write. Keep explicit consume for adopted material that is not yet represented by another relation.

### 2.6 Measure actual protocol burden

Join ledger requests with session transcripts to measure successful, refused and retried calls by tool and reason. Include native shell and other research work before claiming a bookkeeping-versus-research ratio.

**Done when:** common revisions and checkpoints require fewer model-supplied fields; stale checkpoints still conflict; no revision acquires false execution provenance; duplicate consume writes decrease without losing adopted-but-uncited material; and existing calls continue to work through the compatibility path.

## Phase 3. Concentrate audit work at decision points

### 3.1 Use a focused final-review step

At final synthesis and conclusion, compute the declared load-bearing set and its known risks. Do not require the coordinator to clear every historical candidate first.

Run the current publication check, gather cited field-check outcomes and pending impacts, and require a decision for every relevant flagged item. Keep the conclusion-check snapshot and its sequence bound.

### 3.2 Separate answer outcome from evidence closure

Avoid converting every open research question into the same project-level penalty.

Use two dimensions:

- `outcome`: answered, partial or unresolved within the declared research scope;
- `evidence_closure`: resolved, qualified or unknown, with explicit risks and gaps.

An unresolved item restricts `answered` only when it undermines the answer's core claim. The coordinator records that core-impact decision and its evidence; the backend enforces the resulting contract rather than judging scientific materiality itself. A project may legitimately answer its main question while retaining qualified evidence and open mechanisms, as in the local `sdc_agent_188` case.

### 3.3 Choose review by expected information value

Use read-only specialists for citation, scope, wording, alternatives and counterexamples. Use executable verification nodes for independent recomputation, reimplementation and adversarial experiments.

Review the claims where a mistake would change the conclusion, where evidence is controversial or where independent work is feasible and informative. Do not require one uniform review ritual for every entry or every ancestor.

### 3.4 Preserve semantic judgment

An automatically generated evidence appendix can preserve scope and conditions, but cannot fix an overbroad headline. Keep semantic review for claim strength, especially the difference between a finite zero-hit upper bound and impossibility.

**Done when:** the `sdc_agent_222` attention case requires explicit dispositions; an empty-reference final report cannot become vacuously reviewed; an `sdc_agent_188`-style qualified answer remains representable; and a P-018-style executable review can change a conclusion without clearing unrelated historical backlog.

## Phase 4. Offer confirmatory rigor as an opt-in track

### 4.1 Keep exploration lightweight by default

Exploratory reading, failed experiments, changing hypotheses and post-hoc explanations remain allowed without a prediction registry or complete execution provenance. Automatically capture what the system can observe and avoid duplicate manual logging.

These results may be valuable findings. They simply cannot claim confirmatory status that their provenance does not support.

### 4.2 Require strict structure only for confirmatory claims

When an experiment or claim is promoted to confirmatory evaluation, then require the applicable conditions:

- frozen prediction and candidate identity;
- dataset and data-consumption lineage;
- append-only protocol amendments with post-hoc status;
- execution-linked output where the research type requires it;
- appropriate uncertainty metadata and independent review.

No recorded consumption is not proof of no prior exposure outside a mediated boundary. Confirmatory eligibility describes evidence conditions, not truth or overall research quality.

### 4.3 Use research-type profiles

Numeric experiments, deterministic reproduction, theoretical arguments and hardware fault studies need different verification artifacts. Do not force every project through a `metric ge threshold` template or require seeds and intervals where they have no meaning.

Allow the normal cycle of exploration, protocol correction and later confirmation on new evidence. A post-hoc amendment is not misconduct; presenting it as an untouched pre-registration is the problem.

**Done when:** an exploratory project can conclude without confirmatory ceremony; a claim cannot acquire confirmatory eligibility merely because its node had a prediction; and post-hoc, diagnostic and confirmatory evidence remain distinguishable in the final report.

## Rollout and regression strategy

### Release 1: read-model and context relief

Ship context triage, findings-first selection and backlog summaries in shadow-compatible form. Replay saved contexts and local risk fixtures before changing write behavior.

### Release 2: lifecycle and write simplification

Introduce review candidates, automatic closure, split tools, revision presets and server-carried checkpoint bases. Preserve compatibility and measure protocol burden and conflict rates.

### Release 3: decision-point closure

Add focused final risk disposition, the two-dimensional outcome/evidence-closure contract, and risk-based read-only or executable review. Test against the local attention, empty-reference and qualified-answer cases.

### Release 4: optional confirmatory validation

Add native predictions, candidates, data-consumption lineage and research-type profiles only after the earlier phases show that they reduce overhead without losing critical-risk recall.

## Cross-release acceptance criteria

- **Visibility:** ordinary findings from completed nodes reach the coordinator; routine backlog does not displace them.
- **Safety:** every current critical-risk fixture is still surfaced and handled; no empty set passes by vacuity.
- **Semantics:** no authorship-to-execution laundering, no silent invalidation defaults, no stale checkpoint acceptance and no retroactive historical relabeling.
- **Burden:** bookkeeping calls, duplicated inputs and repeated audit exposure decrease across comparable runs.
- **Research outcomes:** independent grading shows no regression in claim accuracy, trap detection, appropriate negative results or cost.
- **Proportionality:** exploration remains possible without confirmatory fields, and qualified answered outcomes remain possible when open issues do not undermine the core answer.

Stop or roll back a phase if it reduces routine overhead by hiding decision-relevant risk, or if it merely moves the same clerical burden into the conclusion step.

## First implementation slice

Start with a small, reversible change:

1. Partition context hints into critical and clerical classes.
2. Replace routine pending-review detail with a count plus on-demand expansion.
3. Add recent-node and publication-pinned findings to coordinator knowledge selection.
4. Replay the 1,055 saved coordinator contexts and the local critical-risk cases.
5. Measure both reclaimed context and the completeness of visible findings and risks.

Do not begin this repair direction by adding predictions, candidates, data-consumption forms or more required revision fields. Those belong later, after the coordinator can see its findings without paying a recurring audit tax.
