/** Plain-text summaries of ledger state for /research dialogs. */

const RUN_STATES: Record<string, string> = {
  manual: "手动",
  running: "自主推进中",
  paused: "已暂停",
  stopping: "停止中",
  stopped: "已停止",
  complete: "执行已结束",
  cold: "重启后待恢复",
  unverified: "停止待核实",
}

const ROLES: Record<string, string> = {
  main: "研究主会话",
  node_core: "节点执行",
  exploration: "探索",
  discussion: "讨论",
  handoff: "接手",
  specialist: "专家",
  legacy: "历史关联",
}

const PAUSE_REASONS: Record<string, string> = {
  wait: "等待节点结果",
  segment_complete: "节点工作已结束",
  native_stop: "你中断了这一轮",
  fault: "连续出错",
  no_progress: "连续几轮没有研究推进",
  budget:'自主调度预算到限，须显式提额后恢复',
  cold: "OpenCode 重启后暂停",
  project: "项目已暂停",
  detached: "已移出项目",
  finished: "已结束",
  complete: "已结束",
  stop: "已停止",
}

export function pauseReason(reason: string | null | undefined): string {
  return reason ? PAUSE_REASONS[reason] ?? reason : ""
}

export function runState(state: string | undefined): string {
  return state ? RUN_STATES[state] ?? state : "未知"
}

export function roleName(role: string | undefined): string {
  return role ? ROLES[role] ?? role : "未登记"
}

export const OUTCOMES: Record<string, string> = { answered: "已回答", partial: "部分回答", unresolved: "未解决" }
export const REVIEWS: Record<string, string> = { unreviewed: "未审阅", partial: "部分审阅", reviewed: "已审阅" }

export function conclusionLines(conclusion: any, state?: string): string[] {
  if (!conclusion) return state === "complete" ? ["结项记录：未记录；审阅情况未知。"] : []
  // Counts only: these lists have no bound, and the board overview shows every entry.
  const lists: [string, string[] | undefined][] = [
    ["未解决项", conclusion.gaps],
    ["审阅限制", conclusion.review.limitations],
    ["审阅材料", conclusion.review.refs],
  ]
  const counted = lists.filter(([, items]) => items?.length).map(([label, items]) => `${label} ${items!.length} 条`)
  return [
    `结项结果：${OUTCOMES[conclusion.outcome] ?? conclusion.outcome}`,
    `最终报告：${conclusion.final_ref}`,
    `审阅声明：${REVIEWS[conclusion.review.status] ?? conclusion.review.status}（不表示结论通过）`,
    ...(counted.length ? [`${counted.join(" · ")}，逐条见 /research board 概览`] : []),
  ]
}

function countsLine(counts: Record<string, number> | undefined): string {
  if (!counts) return ""
  return `节点 ${counts.nodes ?? 0} · 发布 ${counts.publications ?? 0} · 知识 ${counts.knowledge ?? 0} · 检查点 ${counts.checkpoints ?? 0}`
}

/** Status of a session that belongs to a project (control_state). */
export function associatedStatus(state: any): string {
  const attempt = state.attempt
  const work = attempt
    ? attempt.node_id
      ? `正在节点 ${attempt.node_id} 上工作（执行批次 ${attempt.attempt_id}）`
      : `规划执行批次 ${attempt.attempt_id}（未关联节点）`
    : "当前没有正在进行的执行批次"
  const pending = state.review_queue?.pending_total
  return [
    `目标：${state.project?.goal ?? "未记录"}`,
    `项目：${state.project_root ?? ""}`,
    `本会话：${roleName(state.workflow?.session?.role)} · 运行状态：${runState(state.workflow?.run?.state)}`,
    state.schema_version>=11 ? `写入契约：R2 · schema ${state.schema_version}` : '',
    work,
    ...conclusionLines(state.workflow?.conclusion, state.workflow?.run?.state),
    countsLine(state.counts),
    state.review_queue?.semantics==='task_completion_is_not_risk_resolution'
      ? `当前审阅义务 ${pending} 项 · 候选 ${state.review_queue.candidate_total} 项（按需查看）`
      : pending ? `历史待整理 ${pending} 项` : "",
  ]
    .filter(Boolean)
    .join("\n")
}

/** Status of a session outside any project, given what lies in its directory. */
export function unassociatedStatus(root: string | null, summary: any, inSession = true): string {
  const who = inSession ? "本会话没有关联研究项目" : "当前不在会话里"
  if (!root || !summary?.project) {
    return `${who}，当前目录下也没有研究项目。\n用 /research auto <研究目标> 开始自主研究。`
  }
  return [
    `${who}。`,
    `目录 ${root} 里有一个研究项目：${summary.project.goal ?? ""}`,
    `运行状态：${runState(summary.run?.state)} · ${countsLine(summary.counts)}`,
    ...conclusionLines(summary.conclusion, summary.run?.state),
    "用 /research auto 继续自主研究，用 /research takeover 接管它，或 /research board 只读浏览。",
  ].join("\n")
}

export const USAGE = [
  "/research auto [目标或指令]   建立项目并开始自主研究；已有项目时文字作为补充指令，目标不变",
  "/research pause               暂停自主研究（当前这一轮会做完）",
  "/research resume              恢复自主研究",
  "/research stop                停止自主研究，取消节点任务（需确认）",
  "/research status              查看项目和本会话状态",
  "/research budget [turns|nodes|seconds|tokens|cost <正数|off> <原因>]  查看或改额；仅 cost 允许小数",
  "/research board               打开研究看板（只读）",
  "/research init <研究目标>     只建立项目，不开始（想先和模型讨论时用）",
  "/research guidance [路径 [--version v]]  查看或登记科研指导文档",
  "/research context-policy [legacy|r1]  查看或切换上下文读取策略",
  "/research takeover            接管目录里已有的项目（单向，需确认）",
  "/research detach              把本会话移出项目，保留对话和材料",
].join("\n")

/** Readable text for anything thrown across the RPC boundary. */
export function errorText(error: unknown): string {
  if (error instanceof Error && error.message) return error.message
  if (error && typeof error === "object") {
    const record = error as Record<string, any>
    for (const candidate of [record.message, record.error, record.data?.message, record.cause?.message]) {
      if (typeof candidate === "string" && candidate) return candidate
    }
    try {
      return JSON.stringify(error)
    } catch {}
  }
  return String(error)
}
