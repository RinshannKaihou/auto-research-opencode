import { expect, test } from "bun:test"
import type { Context } from "@opencode/plugin/tui/context"
import { RGBA } from "@opentui/core"
import { testRender } from "@opentui/solid"
import { mkdirSync, writeFileSync } from "node:fs"
import { createHash } from "node:crypto"
import { ResearchBoard } from "../src/board/Board"
import { recordFields, reviewCard, reviewRows } from "../src/board/review"

const target = "pub/P-007#final_report_v2"
const risk = { kind: "publication", target, risk: { status: "coverage unknown", versions: {}, targets_complete: false }, relevance: "related", relevance_reason: ["final"], expand: { ref: target } }
const file = { ref: target, publication_id: "P-007", item_id: "final_report_v2", source_path: "REPORT_V2.md", kind: "report", knowledge_refs: [] }
const knowledge = (i: number) => ({ ref: `knowledge/K-${String(i).padStart(3, "0")}@1`, kind: "claim", status: "working", node_id: "X-001", statement: `发现 ${i} under controlled conditions`, scope: { dataset: "fixture-v1" }, conditions: { boundary: "完整条件。".repeat(80) }, evidence_refs: [target], dependencies: [], supersedes: [], field_checks: [] })

type Command = { bind: string; enabled?: () => boolean; run(): void }
async function mounted(width = 132, folder = false, beforeRead?: (view: string, fields: any) => Promise<void>) {
  let layer!: () => { commands: Command[] }
  const calls: any[] = []
  const opened: string[] = []
  let closed = false
  let changed!: () => void
  const color = RGBA.fromHex("#cad3df")
  const scale = Object.fromEntries(Array.from({ length: 11 }, (_, i) => [i * 100, color]))
  const theme = {
    text: { base: color, muted: color, feedback: Object.fromEntries(["info", "success", "warning", "error"].map((v) => [v, { base: color }])) },
    background: { base: RGBA.fromHex("#101620"), raised: { high: color } }, border: { base: color },
    hue: { accent: scale, interactive: scale }, categorical: [scale],
    markdown: Object.fromEntries(["heading", "text", "codeBlock", "listItem", "strong", "emphasis", "code", "blockQuote", "link", "linkText"].map((v) => [v, color])),
    syntax: Object.fromEntries(["comment", "keyword", "function", "variable", "string", "number", "type", "operator", "punctuation"].map((v) => [v, color])),
  }
  const fakeBoard = async ({ view, fields: f = {} }: any) => {
    calls.push({ view, ...f })
    await beforeRead?.(view, f)
    if (view === "summary") return { project: { goal: "测试完整研究上下文" }, run: { state: "complete" }, counts: { nodes: 101, knowledge: 101 }, revision: 1, node_ids: ["X-001"], knowledge_kinds: ["claim"], knowledge_statuses: ["working"], review_queue: { pending_total: 101 },runtime_budget:{available:true,turns:3,total_nodes:101,unconfirmed_reservations:1,limits:{max_session_turns:8,max_total_nodes:120,max_tokens:2000},tokens_observed:420,cost_observed:null,deadline_at:'2026-10-31T12:00:00Z'} }
    if (view === "presentation") return { status: "missing" }
    if (view === "reference_chunk") {
      const value = f.ref === "X-001" ? { node_id: "X-001", question: "Q" } : f.ref.startsWith("knowledge/") ? knowledge(Number(/K-(\d+)/.exec(f.ref)![1])) : { ...file, ref: f.ref, ...(folder ? { source_path: "materials", object_kind: "directory" } : {}) }
      if (f.ref === "knowledge/K-101@1") value.conditions = { boundary: "完整长条件".repeat(9000) }
      const bytes = Buffer.from(JSON.stringify({ kind: "record", value }))
      const end = Math.min(f.offset + f.limit, bytes.length)
      return { chunk: bytes.subarray(f.offset, end).toString("base64"), next_offset: end < bytes.length ? end : null, content_sha256: createHash("sha256").update(bytes).digest("hex") }
    }
    if (view === "reference_get") return { kind: f.ref.startsWith("knowledge/") ? "knowledge" : "publication-item", value: f.ref.startsWith("knowledge/") ? knowledge(Number(/K-(\d+)/.exec(f.ref)![1])) : { ...file, ref: f.ref } }
    if (view === "reference_entries") {
      const after = f.after ?? 0
      return { items: Array.from({ length: Math.min(f.limit, 101 - after) }, (_, i) => ({ ref: `${target}/entry-${after + i}.md`, name: `entry-${after + i}.md`, kind: "file" })), cursor: after + f.limit < 101 ? { after: after + f.limit } : null }
    }
    if (view === "reference_content" && folder && f.ref === target) return { kind: "directory" }
    if (view === "reference_content") return { kind: "text", chunk: Buffer.from(f.offset ? "SECOND_CHUNK" : "# REPORT_OPENED\nThe original frozen report.").toString("base64"), next_offset: f.offset ? null : 42 }
    if (view === "knowledge") {
      const after = f.after ?? 0, count = f.limit ?? 100
      return { items: Array.from({ length: Math.min(count, 101 - after) }, (_, i) => knowledge(after + i + 1)), total: 101, cursor: after + count < 101 ? { after: after + count, upper_id: 101 } : null }
    }
    if (view === "page") {
      const after = f.cursor?.after ?? f.after ?? 0, count = f.limit ?? 100
      if (f.collection === "nodes") return { items: [{ node_id: "X-001", question: "Q" }], cursor: null }
      if (f.collection === "sessions") return { items: [{ session_id: "main", role: "main", detached: false }], cursor: null }
      if (f.collection === "critical_now") return {
        items: Array.from({ length: Math.min(count, 101 - after) }, (_, i) => ({ ...risk, target: after + i === 0 ? target : `pub/P-${after + i + 10}`, expand: { ref: after + i === 0 ? target : `pub/P-${after + i + 10}` } })),
        total: 101, kinds: ["publication"], cursor: after + count < 101 ? { after: after + count, fingerprint: "fixture" } : null,
      }
      if (f.collection === "review_todos") return { items: [{ todo_id: "todo-1", node_id: "X-001", state: "pending", trigger_kind: "publication", trigger_ref: target }], total: 1, trigger_types: ["publication"], cursor: null }
      if (f.collection === 'review_obligations') return {items:[{obligation_id:'RO-fixture',generation:1,scope_owner:'project',target_ref:'knowledge/K-001@1',change_id:'CH-001',state:'completed',risk_disposition:'unresolved',risk:{needs_action:true},events:[{action:'completed',reason:'已核对精确版本，报告提出反例',evidence_refs:[target],scope_snapshot:{result:'challenges'}}]}],total:1,cursor:null}
      if (f.collection === "hints") return { items: [{ class: "whole_snapshot_evidence", target: "knowledge/K-001@1", evidence: { ref: "S-001" } }], total: 1, kinds: ["whole_snapshot_evidence"], cursor: null }
      return { items: [], total: 0, cursor: null }
    }
    throw new Error(`Unexpected board view ${view}`)
  }
  const context = { theme, themeMode: "dark", renderer: {}, location: { directory: "/fixture" }, keymap: { layer: (value: any) => { layer = value } }, client: { rpc: () => ({ board: fakeBoard, events: { on: (_name: string, handler: () => void) => { changed = handler; return () => {} } } }) } } as unknown as Context
  const screen = await testRender(() => <ResearchBoard context={context} onClose={() => { closed = true }} openSession={(id) => opened.push(id)} />, { width, height: 40 })
  const flush = async () => {
    await new Promise((resolve) => setTimeout(resolve, 0))
    await screen.renderOnce()
    await screen.renderOnce()
  }
  const press = async (key: string) => {
    const cmd = layer().commands.find((v) => v.bind === key && (!v.enabled || v.enabled()))
    if (!cmd) throw new Error(`Key ${key} is not enabled`)
    cmd.run(); await flush()
  }
  await flush()
  return { screen, press, flush, calls, opened, changed: () => changed(), closed: () => closed, keys: () => layer().commands.map((v) => v.bind), frame: () => screen.captureCharFrame() }
}

test.each([80, 132])("risk view at %i columns explains unknown coverage and opens original report", async (width) => {
  const ui = await mounted(width)
  try {
    await ui.press("3")
    await ui.press("v")
    await ui.press("v")
    const frame = ui.frame()
    expect(frame).toContain("检查覆盖未知")
    expect(frame).toContain("结构化知识引用")
    expect(frame).not.toContain('"targets_complete"')
    expect(frame).toContain("101 条")
    expect(frame).toContain("o 决策范围")
    if (process.env.R1_CAPTURE_TUI) {
      const dir = new URL("../../docs/evidence/r1-context/ui/", import.meta.url)
      mkdirSync(dir, { recursive: true }); writeFileSync(new URL(`review-${width}.txt`, dir), frame.replace(/[ \t]+$/gm, "").trimEnd() + "\n")
    }
    await ui.press("return")
    for (let i = 0; i < 30 && !ui.frame().includes("REPORT_OPENED"); i++) { await new Promise((r) => setTimeout(r, 10)); await ui.flush() }
    expect(ui.frame()).toContain("REPORT_OPENED")
    await ui.press("n")
    for (let i = 0; i < 30 && !ui.frame().includes("SECOND_CHUNK"); i++) { await new Promise((r) => setTimeout(r, 10)); await ui.flush() }
    expect(ui.frame()).toContain("SECOND_CHUNK")
    await ui.press("escape")
    await ui.press("o")
    expect(ui.frame()).toContain("决策范围 X-001")
    expect(ui.frame()).toContain("保留相关性未知项")
    await ui.press("]")
    expect(ui.frame()).toContain("第 2 页")
    await ui.press("]")
    expect(ui.frame()).toContain("第 3 页")
    expect(ui.calls.filter((v) => v.collection === "critical_now").at(-1).cursor.after).toBe(100)
    await ui.press("[")
    expect(ui.frame()).toContain("第 2 页")
    await ui.press("f")
    expect(ui.frame()).toContain('当前审阅义务')
    await ui.press('f')
    expect(ui.frame()).toContain('审阅候选')
    await ui.press('f')
    expect(ui.frame()).toContain("历史审阅队列")
    expect(ui.frame()).toContain("不是继续研究或结项")
    await ui.press("f")
    expect(ui.frame()).toContain("文书提示")
    expect(ui.calls.filter((v) => v.collection === "hints").at(-1).kind).toBe("clerical")
  } finally { ui.screen.renderer.destroy() }
})

test.each([80,132])('R2 review at %i columns separates completed tasks from unresolved risk',async width=>{
  const ui=await mounted(width)
  try {
    await ui.press('3'); await ui.press('v'); await ui.press('v'); await ui.press('f')
    const frame=ui.frame()
    expect(frame).toContain('任务已完成')
    expect(frame).toContain('尚未解决')
    expect(frame).toContain('尚未评估')
    expect(frame).toContain('CH-001')
    if(process.env.R2_CAPTURE_TUI){
      const dir=new URL('../../docs/evidence/r2-runtime/ui/',import.meta.url)
      mkdirSync(dir,{recursive:true});writeFileSync(new URL(`review-${width}.txt`,dir),frame.replace(/[ \t]+$/gm,'').trimEnd()+'\n')
    }
    await ui.press('2');await ui.press('v')
    const runtime=ui.frame()
    expect(runtime).toContain('自主调度预算')
    expect(runtime).toContain('420 / 2000')
    expect(runtime).toContain('cost 未知')
    if(process.env.R2_CAPTURE_TUI) writeFileSync(new URL(`../../docs/evidence/r2-runtime/ui/runtime-${width}.txt`,import.meta.url),runtime.replace(/[ \t]+$/gm,'').trimEnd()+'\n')
  }finally{ui.screen.renderer.destroy()}
})

test("knowledge list reaches item 101 and requests complete conditions", async () => {
  const ui = await mounted()
  try {
    await ui.press("3")
    await ui.press("]")
    expect(ui.calls.some((v) => v.view === "reference_chunk" && v.ref === "knowledge/K-101@1")).toBe(true)
    for (let i = 0; i < 10; i++) await ui.flush()
    expect(ui.frame()).toContain("K-101")
    const chunks = ui.calls.filter((v) => v.view === "reference_chunk" && v.ref === "knowledge/K-101@1")
    expect(chunks.length).toBeGreaterThan(1)
    expect(chunks[1].expected_digest).toBeDefined()
    expect(ui.calls.find((v) => v.view === "knowledge" && v.after === 100)).toBeDefined()
  } finally { ui.screen.renderer.destroy() }
})

test("risk identities and explanations preserve distinct checks and uncertainty", () => {
  const items = [0, 1].map((index) => ({ kind: "field_check", target: "knowledge/K-001@1", relevance: "unknown", relevance_reason: ["decision_scope_not_declared_for_target"], risk: { check_index: index, result: "not_checkable", reason: "missing_value", input_ref: target } }))
  expect(new Set(reviewRows(items).map((v) => v.id)).size).toBe(2)
  expect(reviewCard(items[0]).summary).toContain("不是对科学结论的判定")
  expect(reviewCard(items[0]).relevance).toContain("尚不明确")
  expect(reviewCard({ ...risk, risk: { status: "coverage partial", versions: {} } }).title).toContain("部分依据")
  const card = reviewCard(risk, file)
  expect(card.links[0].file).toBe(true)
  expect(card.links[0].ref).toBe(target)
  expect(recordFields(false)).toEqual([["", "false"]])
  expect(recordFields(0)).toEqual([["", "0"]])
  const versions = reviewCard(knowledge(1), { ...knowledge(1), current_ref: "knowledge/K-001@2", published_refs: ["knowledge/K-001@1"] })
  expect(versions.links.some((v) => v.ref === "knowledge/K-001@2")).toBe(true)
})

test("process knowledge counts include entries beyond the first page", async () => {
  const ui = await mounted()
  try {
    await ui.press("2")
    expect(ui.calls.some((v) => v.view === "knowledge" && v.after === 100)).toBe(true)
    expect(ui.frame()).toContain("+96")
  } finally { ui.screen.renderer.destroy() }
})

test("directory materials page to file 101, open it, and return to the same directory", async () => {
  const ui = await mounted(132, true)
  try {
    await ui.press("3")
    await ui.press("v")
    await ui.press("v")
    await ui.press("return")
    expect(ui.frame()).toContain("entry-0.md")
    await ui.press("n")
    expect(ui.frame()).toContain("entry-100.md")
    await ui.press("return")
    for (let i = 0; i < 30 && !ui.frame().includes("REPORT_OPENED"); i++) { await new Promise((r) => setTimeout(r, 10)); await ui.flush() }
    expect(ui.frame()).toContain("REPORT_OPENED")
    await ui.press("backspace")
    expect(ui.frame()).toContain("entry-100.md")
    await ui.press("r")
    expect(ui.frame()).toContain("entry-0.md")
  } finally { ui.screen.renderer.destroy() }
})

test.each([80, 132])("three primary destinations retain results, checks and sessions at %i columns", async (width) => {
  const ui = await mounted(width)
  try {
    const navigation = ui.frame()
    expect(navigation).toContain("1 概览")
    expect(navigation).toContain("2 研究")
    expect(navigation).toContain("3 成果")
    expect(navigation).not.toContain("4 材料")
    expect(navigation).not.toContain("7 模型")
    expect(navigation).not.toContain("诊断")
    expect(ui.keys()).not.toContain("d")
    expect(ui.keys()).not.toContain("p")
    expect(ui.keys()).not.toContain("c")
    expect(ui.keys()).not.toContain("t")
    await ui.press("2")
    await ui.press("v")
    expect(ui.frame()).toContain("运行会话")
    await ui.press("return")
    expect(ui.opened).toEqual(["main"])
    await ui.press("v")
    expect(ui.frame()).toContain("+96")
    await ui.press("3")
    await ui.press("v")
    expect(ui.calls.at(-1).collection).toBe("publications")
    expect(ui.calls.every((v) => v.view !== "context_preview")).toBe(true)
    await ui.press("v")
    expect(ui.frame()).toContain("当前风险")
    await ui.press("f")
    await ui.press("f")
    await ui.press("f")
    await ui.press("f")
    await ui.press("f")
    expect(ui.frame()).toContain("当前风险")
    expect(ui.calls.filter((v) => v.view === "page").some((v) => ["findings", "completed_findings"].includes(v.collection))).toBe(false)
    await ui.press("v")
    expect(ui.calls.at(-1).view).toBe("reference_chunk")
    expect(ui.calls.at(-1).ref).toBe("knowledge/K-001@1")
    if (process.env.R1_CAPTURE_TUI) writeFileSync(new URL(`../../docs/evidence/r1-context/ui/navigation-${width}.txt`, import.meta.url), ui.frame().replace(/[ \t]+$/gm, "").trimEnd() + "\n")
    await ui.press("escape")
    expect(ui.closed()).toBe(true)
  } finally { ui.screen.renderer.destroy() }
})

test.each(["j", "2"])("late evidence responses cannot overwrite a new selection or view (%s)", async (key) => {
  let block = false
  let release!: () => void
  const ui = await mounted(132, false, async (view, fields) => {
    if (block && view === "reference_chunk" && fields.ref === "knowledge/K-001@1") {
      await new Promise<void>((resolve) => { release = resolve })
    }
  })
  const right = () => ui.frame().split("\n").map((line) => line.split("│")[3] ?? "").join("\n")
  try {
    await ui.press("3")
    block = true
    await ui.press("return")
    block = false
    await ui.press(key)
    const before = right()
    if (key === "j") expect(before).toContain("发现 2 under controlled conditions")
    release()
    await ui.flush()
    expect(right()).toBe(before)
  } finally { ui.screen.renderer.destroy() }
})

test("closing a file discards its pending continuation", async () => {
  let block = false
  let release!: () => void
  const ui = await mounted(132, false, async (view, fields) => {
    if (block && view === "reference_content" && fields.offset) {
      await new Promise<void>((resolve) => { release = resolve })
    }
  })
  try {
    await ui.press("3"); await ui.press("v"); await ui.press("v"); await ui.press("return")
    for (let i = 0; i < 30 && !ui.frame().includes("REPORT_OPENED"); i++) { await new Promise((r) => setTimeout(r, 10)); await ui.flush() }
    expect(ui.frame()).toContain("REPORT_OPENED")
    block = true
    await ui.press("n")
    await ui.press("escape")
    release()
    await ui.flush()
    expect(ui.frame()).not.toContain("SECOND_CHUNK")
    expect(ui.frame()).not.toContain("REPORT_OPENED")
    expect(ui.frame()).toContain("当前风险")
  } finally { ui.screen.renderer.destroy() }
})

test("changed risk pages ask for explicit refresh without retaining stale rows or retrying", async () => {
  let changed = false
  const ui = await mounted(132, false, async (view, fields) => {
    if (changed && view === "page" && fields.collection === "critical_now" && fields.cursor) {
      throw new Error("read_view_changed: refresh the collection")
    }
  })
  try {
    await ui.press("3"); await ui.press("v"); await ui.press("v"); await ui.press("]")
    expect(ui.frame()).toContain("第 2 页")
    changed = true
    ui.changed()
    await ui.flush()
    expect(ui.frame()).toContain("当前分页已失效")
    expect(ui.frame()).toContain("按 r 从第一页刷新")
    expect(ui.frame()).not.toContain("报告的检查覆盖未知")
    const calls = ui.calls.length
    await ui.flush()
    expect(ui.calls.length).toBe(calls)
    await ui.press("r")
    expect(ui.frame()).toContain("第 1 页")
    expect(ui.frame()).toContain("报告的检查覆盖未知")
    expect(ui.frame()).not.toContain("当前分页已失效")
  } finally { ui.screen.renderer.destroy() }
})
