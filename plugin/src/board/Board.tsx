/**
 * Full-screen, read-only research board. Every view comes from the server's
 * `board` RPC, which reads the ledger through a read-only store; opening or
 * browsing the board never writes the ledger or starts a model.
 */
import type { Context } from "@opencode/plugin/tui/context"
import { TextAttributes, type ScrollBoxRenderable } from "@opentui/core"
import { useTerminalDimensions } from "@opentui/solid"
import { For, Match, Show, Switch, createMemo, createSignal, onCleanup, onMount } from "solid-js"
import { ResearchRpc } from "../../rpc"
import { errorText } from "../format"
import { KnowledgeDetail, KnowledgeFilter, KnowledgeRow } from "./KnowledgeView"
import { MaterialRow, PublicationDetail } from "./MaterialsView"
import {
  TABS,
  decodeChunk,
  isMarkdown,
  knowledgeCounts,
  knowledgeRows,
  materialRows,
  overviewModel,
  sessionRows,
  type Row,
  type Tab,
} from "./model"
import { OverviewView } from "./OverviewView"
import { markdownStyle, palette } from "./palette"
import { fit, knowledgeOwners, nodeTitles, processView, type ProcessView } from "./process"
import { ProcessDetail, ProcessLegend, ProcessList } from "./ProcessView"
import { RuntimeDetail, SessionRow } from "./RuntimeView"
import { KeyHints, Markdown } from "./ui"

const POLL_MS = 5000
const CHUNK = 32 * 1024

interface Reader {
  ref: string
  path?: string
  markdown: boolean
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
  const colors = palette(context)
  const markdown = markdownStyle(context)
  onCleanup(() => markdown.destroy())
  const rpc = context.client.rpc(ResearchRpc)
  const location = () => context.location ?? context.data.location.default()
  const [tab, setTab] = createSignal<Tab>("overview")
  const [summary, setSummary] = createSignal<any>(null)
  const [presentation, setPresentation] = createSignal<any>(null)
  const [rows, setRows] = createSignal<Row[]>([])
  const [selected, setSelected] = createSignal(0)
  const [reader, setReader] = createSignal<Reader | null>(null)
  const [error, setError] = createSignal("")
  const [loading, setLoading] = createSignal(false)
  const [kindFilter, setKindFilter] = createSignal("")
  const [statusFilter, setStatusFilter] = createSignal("")
  const [graph, setGraph] = createSignal<ProcessView | null>(null)
  const [processNode, setProcessNode] = createSignal<any>(null)
  const [tasks, setTasks] = createSignal<any[]>([])
  const [knowledgeAll, setKnowledgeAll] = createSignal<any[]>([])
  const [owners, setOwners] = createSignal<(item: any) => { node: string; via?: string }[]>(() => [])
  const [titles, setTitles] = createSignal(new Map<string, string>())
  // Integer pane widths: a percentage can leave a fractional column for wrapped text.
  const dimensions = useTerminalDimensions()
  const listWidth = () => Math.max(28, Math.floor((dimensions().width - 5) * 0.45))
  const detailWidth = () => dimensions().width - 5 - listWidth()
  const current = () => rows()[selected()]
  let list: ScrollBoxRenderable | undefined
  let detailBox: ScrollBoxRenderable | undefined
  let readerBox: ScrollBoxRenderable | undefined
  let generation = 0

  async function board(view: string, fields: Record<string, unknown> = {}): Promise<any> {
    const value: any = await rpc.board({ view, sessionID: props.sessionID, fields }, { location: location() })
    if (value && typeof value.error === "string") throw new Error(value.error)
    return value
  }

  const pages = (collections: string[]) =>
    Promise.all(collections.map((collection) => board("page", { collection, limit: 100 }).then((page) => page?.items ?? [])))

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
        const [nodes, dependencies, attempts, taskPage, sessions, publications, snapshots] = await pages([
          "nodes",
          "dependencies",
          "attempts",
          "tasks",
          "sessions",
          "publications",
          "snapshots",
        ])
        const knowledge = await board("knowledge", { limit: 100 })
        const view = processView({
          nodes: nodes!,
          dependencies: dependencies!,
          attempts: attempts!,
          tasks: taskPage!,
          sessions: sessions!,
          publications: publications!,
          snapshots: snapshots!,
          knowledge: knowledge?.items ?? [],
        })
        if (run !== generation) return
        setGraph(view)
        nextRows = view.nodes.map((node) => ({ id: node.id, code: node.id, title: node.title, data: node }))
      } else if (target === "knowledge") {
        const filtered = Boolean(kindFilter() || statusFilter())
        const [all, shown, [nodes, publications, snapshots, attempts]] = await Promise.all([
          board("knowledge", { limit: 100 }),
          filtered
            ? board("knowledge", {
                limit: 100,
                ...(kindFilter() ? { kind: kindFilter() } : {}),
                ...(statusFilter() ? { status: statusFilter() } : {}),
              })
            : null,
          pages(["nodes", "publications", "snapshots", "attempts"]),
        ])
        if (run !== generation) return
        setKnowledgeAll(all?.items ?? [])
        const resolve = knowledgeOwners({ publications: publications!, snapshots: snapshots!, attempts: attempts! })
        setOwners(() => resolve)
        setTitles(nodeTitles(nodes!, publications!))
        nextRows = knowledgeRows(filtered ? shown : all)
      } else if (target === "materials") {
        nextRows = materialRows(await board("page", { collection: "publications", limit: 100 }))
      } else {
        const [sessions, taskPage] = await pages(["sessions", "tasks"])
        if (run !== generation) return
        setTasks(taskPage!)
        nextRows = sessionRows({ items: sessions })
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

  /** Only the process detail needs more than its row: the page view shortens long fields. */
  async function describe() {
    const row = current()
    if (tab() !== "process" || !row) return
    const node = await board("reference_get", { ref: row.id }).catch(() => null)
    if (current()?.id === row.id) setProcessNode(node?.value ?? null)
  }

  async function openReader(offset = 0) {
    const row = current()
    const ref = offset ? reader()?.ref : row?.ref
    if (!ref) return
    const path = offset ? reader()?.path : row?.data?.item?.source_path
    try {
      const content = await board("reference_content", { ref, offset, limit: CHUNK })
      const text = decodeChunk(content)
      setReader({
        ref,
        path,
        markdown: content?.kind === "text" && isMarkdown(path ?? ref),
        text: offset ? `${reader()?.text ?? ""}${text}` : text,
        nextOffset: content?.next_offset ?? null,
      })
    } catch (failure) {
      setError(errorText(failure))
    }
  }

  function switchTab(next: Tab) {
    setReader(null)
    // Each tab renders its own row shape; never show another tab's rows while loading.
    setRows([])
    setSelected(0)
    setTab(next)
    detailBox?.scrollTo(0)
    void load(next)
  }

  function move(delta: number) {
    const count = rows().length
    if (!count) return
    const next = Math.max(0, Math.min(count - 1, selected() + delta))
    setSelected(next)
    list?.scrollChildIntoView(`research-row-${next}`)
    // A refresh keeps the reading position; only a new selection starts from the top.
    detailBox?.scrollTo(0)
    void describe()
  }

  function cycle(values: readonly string[], current: string, set: (value: string) => void) {
    const options = ["", ...values]
    set(options[(options.indexOf(current) + 1) % options.length]!)
    setSelected(0)
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
          const row = current()
          if (tab() === "runtime" && row) props.openSession?.(row.id)
          else void openReader()
        },
      },
      { bind: "pagedown", run: () => (reader() ? readerBox : detailBox)?.scrollBy(0.8, "viewport") },
      { bind: "pageup", run: () => (reader() ? readerBox : detailBox)?.scrollBy(-0.8, "viewport") },
      { bind: "n", enabled: () => reader()?.nextOffset != null, run: () => void openReader(reader()!.nextOffset!) },
      { bind: "r", run: () => void load() },
      {
        bind: "f",
        enabled: () => tab() === "knowledge",
        run: () => cycle(knowledgeCounts(knowledgeAll()).map((entry) => entry.kind), kindFilter(), setKindFilter),
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

  const hints = createMemo((): [string, string][] => {
    if (reader()) return [["Esc", "返回"], ["PgUp/PgDn", "翻页"], ...(reader()?.nextOffset != null ? [["n", "读取下一段"] as [string, string]] : [])]
    if (tab() === "overview") return [["1-5", "切换视图"], ["PgUp/PgDn", "滚动"], ["r", "刷新"], ["Esc", "关闭"]]
    return [
      ["1-5", "切换视图"],
      ["j/k", "移动"],
      ["PgUp/PgDn", "滚动详情"],
      ["Enter", tab() === "runtime" ? "打开会话" : "打开文件"],
      ["r", "刷新"],
      ["Esc", "关闭"],
    ]
  })
  const noProject = () => summary() !== null && !summary()?.project

  // Scrollbox content is padded: besides the breathing room, the padding keeps
  // wrapped text out of the column the vertical scrollbar draws over.
  return (
    <box flexDirection="column" width="100%" height="100%" paddingX={2} paddingTop={1} backgroundColor={theme.background.base}>
      <text wrapMode="none" flexShrink={0}>
        <span style={{ fg: colors.brand, attributes: TextAttributes.BOLD }}>Research</span>
        <span style={{ fg: colors.base }}>{`  ${fit(summary()?.project?.goal ?? "研究看板", dimensions().width - 24)}`}</span>
        <span style={{ fg: colors.muted }}>{loading() ? "  读取中…" : ""}</span>
      </text>
      <text wrapMode="none" flexShrink={0} marginTop={1}>
        <For each={TABS}>
          {(item) => (
            <>
              <span
                style={{
                  fg: item.id === tab() ? colors.base : colors.muted,
                  bg: item.id === tab() ? colors.tab : colors.none,
                  attributes: item.id === tab() ? TextAttributes.BOLD : TextAttributes.NONE,
                }}
              >
                {` ${item.key} ${item.title} `}
              </span>
              <span>{" "}</span>
            </>
          )}
        </For>
      </text>
      <Show when={tab() === "knowledge" && !reader() && knowledgeAll().length}>
        <box marginTop={1} flexShrink={0}>
          <KnowledgeFilter
            context={context}
            counts={knowledgeCounts(knowledgeAll())}
            total={knowledgeAll().length}
            kind={kindFilter()}
            status={statusFilter()}
          />
        </box>
      </Show>
      <Show when={error()}>
        <text fg={theme.text.feedback.error.base} flexShrink={0}>
          {error()}
        </text>
      </Show>
      <Show
        when={!reader()}
        fallback={
          <scrollbox ref={readerBox} flexGrow={1} minHeight={0} marginTop={1} focused scrollX={false} border borderColor={colors.rail}>
            <box paddingX={1} flexDirection="column">
              <text wrapMode="none">
                <span style={{ fg: colors.link }}>{reader()?.ref}</span>
                <span style={{ fg: colors.muted }}>{reader()?.path ? `  ${reader()?.path}` : ""}</span>
              </text>
              <box marginTop={1}>
                <Show when={reader()?.markdown} fallback={<text fg={colors.base}>{reader()?.text}</text>}>
                  <Markdown style={markdown} content={reader()?.text ?? ""} />
                </Show>
              </box>
            </box>
          </scrollbox>
        }
      >
        <box flexDirection="row" flexGrow={1} minHeight={0} marginTop={1} columnGap={1}>
          <Show when={tab() !== "overview" && !noProject()}>
            <box flexDirection="column" width={listWidth()} minHeight={0}>
              <scrollbox ref={list} flexGrow={1} minHeight={0} scrollX={false} border borderColor={colors.rail}>
                <box paddingX={1} flexDirection="column">
                  <Switch fallback={<text fg={colors.muted}>（没有条目）</text>}>
                    <Match when={tab() === "process" ? graph() : null}>
                      {(view) => <ProcessList context={context} view={view()} selected={selected()} width={listWidth() - 4} />}
                    </Match>
                    <Match when={rows().length && tab() === "knowledge"}>
                      <For each={rows()}>
                        {(row, index) => (
                          <KnowledgeRow context={context} row={row} index={index()} selected={index() === selected()} width={listWidth() - 4} />
                        )}
                      </For>
                    </Match>
                    <Match when={rows().length && tab() === "materials"}>
                      <For each={rows()}>
                        {(_, index) => (
                          <MaterialRow context={context} rows={rows()} index={index()} selected={index() === selected()} width={listWidth() - 4} />
                        )}
                      </For>
                    </Match>
                    <Match when={rows().length && tab() === "runtime"}>
                      <For each={rows()}>
                        {(row, index) => (
                          <SessionRow context={context} row={row} index={index()} selected={index() === selected()} width={listWidth() - 4} />
                        )}
                      </For>
                    </Match>
                  </Switch>
                </box>
              </scrollbox>
              <Show when={tab() === "process" && graph()}>
                <ProcessLegend context={context} />
              </Show>
            </box>
          </Show>
          <scrollbox ref={detailBox} flexGrow={1} minHeight={0} scrollX={false} border borderColor={colors.rail}>
            <box paddingX={1} flexDirection="column">
              <Switch>
                <Match when={tab() === "overview" || noProject()}>
                  <OverviewView
                    context={context}
                    model={overviewModel(summary(), presentation())}
                    width={dimensions().width - 8}
                    markdown={markdown}
                  />
                </Match>
                <Match when={tab() === "process" ? graph() : null}>
                  {(view) => (
                    <ProcessDetail
                      context={context}
                      view={view()}
                      selected={selected()}
                      node={processNode()}
                      width={detailWidth() - 4}
                      markdown={markdown}
                    />
                  )}
                </Match>
                <Match when={tab() === "knowledge"}>
                  <KnowledgeDetail context={context} item={current()?.data} owners={owners()} titles={titles()} />
                </Match>
                <Match when={tab() === "materials"}>
                  <PublicationDetail context={context} row={current()} markdown={markdown} width={detailWidth() - 4} />
                </Match>
                <Match when={tab() === "runtime"}>
                  <RuntimeDetail context={context} summary={summary()} session={current()?.data} tasks={tasks()} />
                </Match>
              </Switch>
            </box>
          </scrollbox>
        </box>
      </Show>
      <KeyHints context={context} hints={hints()} />
    </box>
  )
}
