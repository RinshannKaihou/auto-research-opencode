/**
 * Full-screen, read-only research board. Every view comes from the server's
 * `board` RPC, which reads the ledger through a read-only store; opening or
 * browsing the board never writes the ledger or starts a model.
 */
import type { Context } from "@opencode/plugin/tui/context"
import type { ScrollBoxRenderable } from "@opentui/core"
import { For, Show, createMemo, createSignal, onCleanup, onMount } from "solid-js"
import { ResearchRpc } from "../../rpc"
import { errorText } from "../format"
import {
  TABS,
  decodeChunk,
  knowledgeDetail,
  knowledgeRows,
  materialRows,
  nodeDetail,
  nodeRows,
  overviewText,
  publicationDetail,
  runtimeText,
  sessionRows,
  type Row,
  type Tab,
} from "./model"

const POLL_MS = 5000
const CHUNK = 32 * 1024

interface Reader {
  ref: string
  text: string
  nextOffset: number | null
}

export function ResearchBoard(props: {
  context: Context
  sessionID?: string
  onClose(): void
  openSession?(sessionID: string): void
}) {
  const context = props.context
  const theme = context.theme
  const rpc = context.client.rpc(ResearchRpc)
  const location = () => context.location ?? context.data.location.default()
  const [tab, setTab] = createSignal<Tab>("overview")
  const [summary, setSummary] = createSignal<any>(null)
  const [presentation, setPresentation] = createSignal<any>(null)
  const [rows, setRows] = createSignal<Row[]>([])
  const [selected, setSelected] = createSignal(0)
  const [detail, setDetail] = createSignal("")
  const [reader, setReader] = createSignal<Reader | null>(null)
  const [error, setError] = createSignal("")
  const [loading, setLoading] = createSignal(false)
  const [kindFilter, setKindFilter] = createSignal("")
  const [statusFilter, setStatusFilter] = createSignal("")
  let dependencies: any[] = []
  let attempts: any[] = []
  let tasks: any[] = []
  let list: ScrollBoxRenderable | undefined
  let generation = 0

  async function board(view: string, fields: Record<string, unknown> = {}): Promise<any> {
    const value: any = await rpc.board({ view, sessionID: props.sessionID, fields }, { location: location() })
    if (value && typeof value.error === "string") throw new Error(value.error)
    return value
  }

  async function load(target: Tab = tab()) {
    const run = ++generation
    setLoading(true)
    setError("")
    try {
      const nextSummary = await board("summary")
      if (run !== generation) return
      setSummary(nextSummary)
      let nextRows: Row[] = []
      if (!nextSummary?.project) {
        nextRows = []
      } else if (target === "overview") {
        setPresentation(await board("presentation"))
      } else if (target === "process") {
        const [nodes, edges, work] = await Promise.all([
          board("page", { collection: "nodes", limit: 100 }),
          board("page", { collection: "dependencies", limit: 100 }),
          board("page", { collection: "attempts", limit: 100 }),
        ])
        dependencies = edges?.items ?? []
        attempts = work?.items ?? []
        nextRows = nodeRows(nodes)
      } else if (target === "knowledge") {
        nextRows = knowledgeRows(
          await board("knowledge", {
            limit: 100,
            ...(kindFilter() ? { kind: kindFilter() } : {}),
            ...(statusFilter() ? { status: statusFilter() } : {}),
          }),
        )
      } else if (target === "materials") {
        nextRows = materialRows(await board("page", { collection: "publications", limit: 100 }))
      } else {
        const [sessions, taskPage] = await Promise.all([
          board("page", { collection: "sessions", limit: 100 }),
          board("page", { collection: "tasks", limit: 100 }),
        ])
        tasks = taskPage?.items ?? []
        nextRows = sessionRows(sessions)
      }
      if (run !== generation) return
      setRows(nextRows)
      setSelected((index) => Math.min(index, Math.max(0, nextRows.length - 1)))
      await describe()
    } catch (failure) {
      if (run === generation) setError(errorText(failure))
    } finally {
      if (run === generation) setLoading(false)
    }
  }

  async function describe() {
    const row = rows()[selected()]
    if (tab() === "overview") return setDetail(overviewText(summary(), presentation()))
    if (tab() === "runtime") return setDetail(runtimeText(summary(), row?.data, tasks))
    if (!row) return setDetail(summary()?.project ? "（没有条目）" : overviewText(summary(), null))
    if (tab() === "process") {
      const node = await board("reference_get", { ref: row.id }).catch(() => null)
      return setDetail(nodeDetail(node?.value ?? row.data, dependencies, attempts))
    }
    if (tab() === "knowledge") return setDetail(knowledgeDetail(row.data))
    return setDetail(publicationDetail(row.data))
  }

  async function openReader(offset = 0) {
    const row = rows()[selected()]
    const ref = offset ? reader()?.ref : row?.ref
    if (!ref) return
    try {
      const content = await board("reference_content", { ref, offset, limit: CHUNK })
      const text = decodeChunk(content)
      setReader({
        ref,
        text: offset ? `${reader()?.text ?? ""}${text}` : text,
        nextOffset: content?.next_offset ?? null,
      })
    } catch (failure) {
      setError(errorText(failure))
    }
  }

  function switchTab(next: Tab) {
    setReader(null)
    setSelected(0)
    setTab(next)
    void load(next)
  }

  function move(delta: number) {
    const count = rows().length
    if (!count) return
    const next = Math.max(0, Math.min(count - 1, selected() + delta))
    setSelected(next)
    list?.scrollChildIntoView(`research-row-${next}`)
    void describe()
  }

  function cycle(values: readonly string[], current: string, set: (value: string) => void) {
    const options = ["", ...values]
    set(options[(options.indexOf(current) + 1) % options.length]!)
    void load("knowledge")
  }

  context.keymap.layer(() => ({
    commands: [
      ...TABS.map((item) => ({ bind: item.key, run: () => switchTab(item.id) })),
      { bind: "j", run: () => move(1) },
      { bind: "down", run: () => move(1) },
      { bind: "k", run: () => move(-1) },
      { bind: "up", run: () => move(-1) },
      {
        bind: "return",
        run: () => {
          const row = rows()[selected()]
          if (tab() === "runtime" && row) props.openSession?.(row.id)
          else void openReader()
        },
      },
      { bind: "n", enabled: () => reader()?.nextOffset != null, run: () => void openReader(reader()!.nextOffset!) },
      { bind: "r", run: () => void load() },
      {
        bind: "f",
        enabled: () => tab() === "knowledge",
        run: () => cycle(summary()?.knowledge_kinds ?? [], kindFilter(), setKindFilter),
      },
      {
        bind: "s",
        enabled: () => tab() === "knowledge",
        run: () => cycle(summary()?.knowledge_statuses ?? [], statusFilter(), setStatusFilter),
      },
      {
        title: "Close research board",
        bind: "escape",
        run: () => {
          if (reader()) setReader(null)
          else props.onClose()
        },
      },
    ],
  }))

  onMount(() => {
    void load()
    const unsubscribe = rpc.events.on("changed", () => void load())
    let revision = -1
    const timer = setInterval(async () => {
      const next = await board("summary").catch(() => null)
      if (next && revision >= 0 && next.revision !== revision) void load()
      if (next) revision = next.revision
    }, POLL_MS)
    onCleanup(() => {
      clearInterval(timer)
      unsubscribe()
    })
  })

  const tabsLine = createMemo(() =>
    TABS.map((item) => (item.id === tab() ? `[${item.key} ${item.title}]` : ` ${item.key} ${item.title} `)).join(" "),
  )
  const filterLine = createMemo(() =>
    tab() === "knowledge" ? `类型：${kindFilter() || "全部"} · 状态：${statusFilter() || "全部"}（f / s 切换）` : "",
  )
  const footer = createMemo(() =>
    reader()
      ? `Esc 返回${reader()?.nextOffset != null ? " · n 读取下一段" : ""}`
      : tab() === "runtime"
        ? "1-5 切换视图 · j/k 移动 · Enter 打开会话 · r 刷新 · Esc 关闭"
        : "1-5 切换视图 · j/k 移动 · Enter 打开文件 · r 刷新 · Esc 关闭",
  )

  return (
    <box flexDirection="column" width="100%" height="100%" paddingX={1} backgroundColor={theme.background.base}>
      <text fg={theme.text.base} wrapMode="none" flexShrink={0}>
        {`Research · ${summary()?.project?.goal ?? "研究看板"}${loading() ? " · 读取中…" : ""}`}
      </text>
      <text fg={theme.text.muted} wrapMode="none" flexShrink={0}>
        {tabsLine()}
      </text>
      <Show when={filterLine()}>
        <text fg={theme.text.muted} wrapMode="none" flexShrink={0}>
          {filterLine()}
        </text>
      </Show>
      <Show when={error()}>
        <text fg={theme.text.feedback.error.base} flexShrink={0}>
          {error()}
        </text>
      </Show>
      <Show
        when={!reader()}
        fallback={
          <scrollbox flexGrow={1} minHeight={0} focused scrollX={false} border borderColor={theme.border.base}>
            <text fg={theme.text.base}>{`${reader()?.ref}\n\n${reader()?.text}`}</text>
          </scrollbox>
        }
      >
        <box flexDirection="row" flexGrow={1} minHeight={0} columnGap={1}>
          <Show when={tab() !== "overview"}>
            <scrollbox
              ref={list}
              width="45%"
              minHeight={0}
              scrollX={false}
              border
              borderColor={theme.border.base}
            >
              <For each={rows()} fallback={<text fg={theme.text.muted}>（没有条目）</text>}>
                {(row, index) => (
                  <text
                    id={`research-row-${index()}`}
                    wrapMode="none"
                    fg={index() === selected() ? theme.text.base : theme.text.muted}
                  >
                    {`${index() === selected() ? "› " : "  "}${row.label}`}
                  </text>
                )}
              </For>
            </scrollbox>
          </Show>
          <scrollbox flexGrow={1} minHeight={0} scrollX={false} border borderColor={theme.border.base}>
            <text fg={theme.text.base}>{detail()}</text>
          </scrollbox>
        </box>
      </Show>
      <text fg={theme.text.muted} wrapMode="none" flexShrink={0}>
        {footer()}
      </text>
    </box>
  )
}
