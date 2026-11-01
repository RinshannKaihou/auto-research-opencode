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
  kindChips,
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
import { REVIEW_COLLECTIONS, reviewCard, reviewRows, reviewFilterLabel } from "./review"
import { ReviewDetail, ReviewRow } from "./ReviewView"

const POLL_MS = 5000
const CHUNK = 32 * 1024
const ROUTE_STATUS: Record<string,string> = {
  untried:'待试', investigating:'试探中', deferred:'暂缓', excluded:'已排除', adopted:'已采用', combination_proposed:'建议组合',
}

interface Reader {
  ref: string
  path?: string
  markdown: boolean
  text: string
  nextOffset: number | null
  byteChunks?: Buffer[]
  truncated?: boolean
  directory?: { items: { ref: string; name: string; kind: string }[]; cursor: Record<string, unknown> | null; selected: number }
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
  const activeTab = () => TABS.find((group) => group.views.some((view) => view.id === tab()))!
  const subviews = () => activeTab().views
  const [summary, setSummary] = createSignal<any>(null)
  const [presentation, setPresentation] = createSignal<any>(null)
  const [rows, setRows] = createSignal<Row[]>([])
  const [pageCursor, setPageCursor] = createSignal<Record<string, unknown> | null>(null)
  const [nextCursor, setNextCursor] = createSignal<Record<string, unknown> | null>(null)
  const [pageHistory, setPageHistory] = createSignal<(Record<string, unknown> | null)[]>([])
  const [reviewCollection, setReviewCollection] = createSignal("critical_now")
  const [reviewSource, setReviewSource] = createSignal<any>(null)
  const [reviewLink, setReviewLink] = createSignal(0)
  const [reviewRaw, setReviewRaw] = createSignal(false)
  const [reviewNode, setReviewNode] = createSignal("")
  const [reviewState, setReviewState] = createSignal("")
  const [reviewKind, setReviewKind] = createSignal("")
  const [reviewTotal, setReviewTotal] = createSignal(0)
  const [reviewKinds, setReviewKinds] = createSignal<string[]>([])
  const [knowledgeDetail, setKnowledgeDetail] = createSignal<any>(null)
  const [selected, setSelected] = createSignal(0)
  const [reader, setReader] = createSignal<Reader | null>(null)
  const [readerHistory, setReaderHistory] = createSignal<Reader[]>([])
  const [error, setError] = createSignal("")
  const [loading, setLoading] = createSignal(false)
  const [kindFilter, setKindFilter] = createSignal("")
  const [statusFilter, setStatusFilter] = createSignal("")
  const [graph, setGraph] = createSignal<ProcessView | null>(null)
  const [processNode, setProcessNode] = createSignal<any>(null)
  const [routeMode, setRouteMode] = createSignal(false)
  const [tasks, setTasks] = createSignal<any[]>([])
  const [knowledgeTotals, setKnowledgeTotals] = createSignal<{ kind?: string; total: number }[]>([])
  /** How many entries match the current filters across all pages. */
  const [knowledgeListed, setKnowledgeListed] = createSignal(0)
  const chips = createMemo(() => kindChips(knowledgeTotals()))
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
  let detailGeneration = 0
  let readerGeneration = 0

  async function board(view: string, fields: Record<string, unknown> = {}): Promise<any> {
    const value: any = await rpc.board({ view, sessionID: props.sessionID, fields }, { location: location() })
    if (value && typeof value.error === "string") throw new Error(value.error)
    return value
  }

  async function fullReference(ref: string): Promise<any> {
    const chunks: Buffer[] = []
    let offset: number | null = 0
    let digest: string | undefined
    do {
      const part: any = await board("reference_chunk", { ref, offset, limit: CHUNK, ...(digest ? { expected_digest: digest } : {}) })
      chunks.push(Buffer.from(part.chunk, "base64"))
      digest = part.content_sha256
      offset = part.next_offset
    } while (offset !== null)
    const record = JSON.parse(Buffer.concat(chunks).toString("utf8"))
    if (record.version_context) record.value = { ...record.value, ...record.version_context }
    return record
  }

  // Graphs and ownership need the full index; each RPC remains paginated.
  const pages = (collections: string[]) => Promise.all(collections.map(async (collection) => {
    const items: any[] = []
    let cursor: Record<string, unknown> | null = null
    do {
      const page: any = collection === "knowledge"
        ? await board("knowledge", { limit: 100, ...(cursor ?? {}) })
        : await board("page", { collection, limit: 100, ...(cursor ?? {}) })
      items.push(...page.items)
      cursor = page.cursor
    } while (cursor)
    return items
  }))

  async function load(target: Tab = tab()) {
    const run = ++generation
    detailGeneration++
    setLoading(true)
    setError("")
    try {
      const nextSummary = await board("summary")
      if (run !== generation) return
      setSummary(nextSummary)
      let nextRows: Row[] = []
      setNextCursor(null)
      if (!nextSummary?.project) {
        nextRows = []
      } else if (target === "overview") {
        setPresentation(await board("presentation"))
      } else if (target === "process" && routeMode()) {
        const page = await board('page', { collection:'exploration_routes', limit:50, cursor:pageCursor() })
        if (run !== generation) return
        setGraph(null)
        setNextCursor(page.cursor)
        nextRows = (page.items ?? []).map((route:any) => ({id:route.ref,code:route.route_id,title:`${route.scope_id ?? ''} · ${route.title}`,data:route}))
      } else if (target === "process") {
        const [nodes, dependencies, attempts, taskPage, sessions, publications, snapshots, knowledge] = await pages([
          "nodes",
          "dependencies",
          "attempts",
          "tasks",
          "sessions",
          "publications",
          "snapshots",
          "knowledge",
        ])
        const view = processView({
          nodes: nodes!,
          dependencies: dependencies!,
          attempts: attempts!,
          tasks: taskPage!,
          sessions: sessions!,
          publications: publications!,
          snapshots: snapshots!,
          knowledge: knowledge!,
        })
        if (run !== generation) return
        setGraph(view)
        nextRows = view.nodes.map((node) => ({ id: node.id, code: node.id, title: node.title, data: node }))
      } else if (target === "knowledge") {
        // Rows are the first page; chip counts come from one count query per
        // kind under the same status filter, so they cover every entry.
        const status = statusFilter() ? { status: statusFilter() } : {}
        const kinds: string[] = nextSummary.knowledge_kinds ?? []
        const [shown, totals, [nodes, publications, snapshots, attempts]] = await Promise.all([
          board("knowledge", { limit: 100, ...(pageCursor() ?? {}), ...(kindFilter() ? { kind: kindFilter() } : {}), ...status }),
          Promise.all(
            [undefined, ...kinds].map((kind) =>
              board("knowledge", { limit: 1, ...(kind ? { kind } : {}), ...status }).then((page) => ({
                kind,
                total: Number(page?.total ?? 0),
              })),
            ),
          ),
          pages(["nodes", "publications", "snapshots", "attempts"]),
        ])
        if (run !== generation) return
        setKnowledgeTotals(totals)
        setKnowledgeListed(Number(shown?.total ?? 0))
        const resolve = knowledgeOwners({ publications: publications!, snapshots: snapshots!, attempts: attempts! })
        setOwners(() => resolve)
        setTitles(nodeTitles(nodes!, publications!))
        setNextCursor(shown.cursor)
        nextRows = knowledgeRows(shown)
      } else if (target === "materials") {
        const page = await board("page", { collection: "publications", limit: 100, ...(pageCursor() ?? {}) })
        if (run !== generation) return
        setNextCursor(page.cursor)
        nextRows = materialRows(page)
      } else if (target === "review") {
        const collection = reviewCollection()
        const page = await board("page", { collection, limit: 50,
          ...(reviewNode() ? collection==='review_obligations' ? {scope_owner:`node:${reviewNode()}`} : { node_id: reviewNode() } : {}),
          ...(reviewKind() ? collection==='review_candidates' ? {trigger_kind:reviewKind()} : { kind: reviewKind(), trigger_kind: reviewKind() } : collection === "hints" ? { kind: "clerical" } : {}),
          ...(["review_todos",'review_obligations'].includes(collection) && reviewState() ? { state: reviewState() } : {}),
          cursor: pageCursor() })
        if (page.available===false) throw new Error('该旧项目尚未迁移审阅生命周期；历史记录仍可在历史审阅队列查看。')
        if (run !== generation) return
        setNextCursor(page.cursor)
        setReviewTotal(page.total)
        setReviewKinds(page.kinds ?? page.trigger_types ?? [])
        nextRows = reviewRows(page.items)
      } else {
        const [sessionPage, [taskPage]] = await Promise.all([
          board("page", { collection: "sessions", limit: 100, ...(pageCursor() ?? {}) }), pages(["tasks"]),
        ])
        if (run !== generation) return
        setNextCursor(sessionPage.cursor)
        setTasks(taskPage!)
        nextRows = sessionRows(sessionPage)
      }
      if (run !== generation) return
      setRows(nextRows)
      setSelected((index) => Math.min(index, Math.max(0, nextRows.length - 1)))
      await describe()
    } catch (failure) {
      if (run === generation) {
        const message = errorText(failure)
        if (message.includes("read_view_changed")) {
          setRows([])
          setReviewSource(null)
          setKnowledgeDetail(null)
          setError("研究记录已更新，当前分页已失效。按 r 从第一页刷新。")
        } else setError(message)
      }
    } finally {
      if (run === generation) setLoading(false)
    }
  }

  /** Only the process detail needs more than its row: the page view shortens long fields. */
  async function describe() {
    const run = ++detailGeneration
    const target = tab()
    const row = current()
    if (!row) return
    const active = () => run === detailGeneration && tab() === target && current()?.id === row.id
    if (target === "knowledge") {
      setKnowledgeDetail(null)
      setReviewLink(0)
      const value = await fullReference(row.data.ref)
      if (active()) setKnowledgeDetail(value.value)
      return
    }
    if (target === "review") {
      setReviewSource(null)
      setReviewLink(0)
      const ref = row.data.target_ref ?? row.data.expand?.ref ?? row.data.trigger_ref ?? row.data.ref ?? row.data.node_id
      if (ref) {
        const value = await fullReference(ref)
        if (active()) setReviewSource({ ...value.value, ref })
      }
      return
    }
    if (target !== "process") return
    if (routeMode()) { setProcessNode(row.data); return }
    const node = await fullReference(row.id)
    if (active()) setProcessNode(node?.value ?? null)
  }

  async function openReviewLink() {
    const row = current()
    if (!row) return
    const target = tab()
    const run = ++detailGeneration
    const source = target === "knowledge" ? knowledgeDetail() : reviewSource()
    const link = reviewCard(row.data, source).links[reviewLink()]
    if (!link) return
    if (link.file) await openReader(0, link.ref, link.path)
    else {
      const value = await fullReference(link.ref)
      if (run !== detailGeneration || target !== tab() || current()?.id !== row.id) return
      if (target === "knowledge") setKnowledgeDetail({ ...value.value, ref: link.ref })
      else setReviewSource({ ...value.value, ref: link.ref })
      setReviewLink(0)
      detailBox?.scrollTo(0)
    }
  }

  async function openReader(offset = 0, reference?: string, sourcePath?: string) {
    const row = current()
    const previous = reader()
    const target = tab()
    const run = ++readerGeneration
    const active = () => run === readerGeneration && target === tab() && current()?.id === row?.id
    const ref = offset ? previous!.ref : reference ?? row?.ref
    if (!ref) return
    const path = offset ? previous!.path : sourcePath ?? row?.data?.item?.source_path
    try {
      const content = await board("reference_content", { ref, offset, limit: CHUNK })
      if (!active()) return
      if (content.kind === "directory") {
        const entries = await board("reference_entries", { ref, limit: 100 })
        if (!active()) return
        setReader({ ref, path, markdown: false, text: "", nextOffset: null,
          directory: { items: entries.items, cursor: entries.cursor, selected: 0 } })
        return
      }
      const byteChunks = content.kind === "text"
        ? [...(offset ? previous!.byteChunks! : []), Buffer.from(content.chunk, "base64")] : []
      const text = content.kind === "text"
        ? new TextDecoder().decode(Buffer.concat(byteChunks), { stream: content.next_offset !== null })
        : decodeChunk(content)
      setReader({
        ref,
        path,
        markdown: content?.kind === "text" && isMarkdown(path ?? ref),
        text, byteChunks, truncated: content.truncated,
        nextOffset: content?.next_offset ?? null,
      })
    } catch (failure) {
      if (active()) setError(errorText(failure))
    }
  }

  async function nextDirectoryPage() {
    const value = reader()!
    const run = ++readerGeneration
    const entries = await board("reference_entries", { ref: value.ref, limit: 100, ...value.directory!.cursor })
    if (run !== readerGeneration || reader() !== value) return
    setReader({ ...value, directory: { items: entries.items, cursor: entries.cursor, selected: 0 } })
  }

  function resetPage() {
    readerGeneration++
    setPageCursor(null)
    setNextCursor(null)
    setPageHistory([])
  }

  function turnPage(forward: boolean) {
    if (forward) {
      if (!nextCursor()) return
      setPageHistory([...pageHistory(), pageCursor()])
      setPageCursor(nextCursor())
    } else {
      if (!pageHistory().length) return
      setPageCursor(pageHistory().at(-1)!)
      setPageHistory(pageHistory().slice(0, -1))
    }
    setSelected(0)
    void load()
  }

  function switchTab(next: Tab) {
    resetPage()
    setReader(null)
    setReaderHistory([])
    setReviewRaw(false)
    // Each tab renders its own row shape; never show another tab's rows while loading.
    setRows([])
    setSelected(0)
    setTab(next)
    detailBox?.scrollTo(0)
    void load(next)
  }

  function move(delta: number) {
    if (reader()) {
      const value = reader()!
      if (value.directory && value.directory.items.length) {
        readerGeneration++
        const selected = Math.max(0, Math.min(value.directory.items.length - 1, value.directory.selected + delta))
        setReader({ ...value, directory: { ...value.directory, selected } })
        readerBox?.scrollChildIntoView(`reader-entry-${selected}`)
      }
      return
    }
    const count = rows().length
    if (!count) return
    const next = Math.max(0, Math.min(count - 1, selected() + delta))
    readerGeneration++
    setSelected(next)
    list?.scrollChildIntoView(`research-row-${next}`)
    // A refresh keeps the reading position; only a new selection starts from the top.
    detailBox?.scrollTo(0)
    void describe().catch((failure) => setError(errorText(failure)))
  }

  function cycle(values: readonly string[], current: string, set: (value: string) => void) {
    const options = ["", ...values]
    set(options[(options.indexOf(current) + 1) % options.length]!)
    setSelected(0)
    resetPage()
    void load("knowledge")
  }

  context.keymap.layer(() => ({
    mode: "global",
    priority: 1,
    commands: [
      ...TABS.map((item) => ({ bind: item.key, run: () => switchTab(item.id) })),
      { bind: "v", enabled: () => !reader() && subviews().length > 1, run: () => {
        const views = subviews()
        switchTab(views[(views.findIndex((view) => view.id === tab()) + 1) % views.length]!.id)
      } },
      { bind: "j", run: () => move(1) },
      { bind: "down", run: () => move(1) },
      { bind: "k", run: () => move(-1) },
      { bind: "up", run: () => move(-1) },
      {
        bind: "return",
        run: () => {
          if (reader()) {
            const value = reader()!
            const entry = value.directory?.items[value.directory.selected]
            if (entry) { setReaderHistory([...readerHistory(), value]); void openReader(0, entry.ref, entry.name) }
            return
          }
          const row = current()
          if (tab() === "runtime" && row) props.openSession?.(row.id)
          else if (tab() === "review" || tab() === "knowledge") void openReviewLink().catch((failure) => setError(errorText(failure)))
          else if (tab() === 'process' && routeMode()) return
          else void openReader()
        },
      },
      { bind: "home", run: () => (reader() ? readerBox : detailBox)?.scrollTo(0) },
      { bind: "end", run: () => { const box = reader() ? readerBox : detailBox; box?.scrollTo(box.scrollHeight) } },
      { bind: "pagedown", run: () => (reader() ? readerBox : detailBox)?.scrollBy(0.8, "viewport") },
      { bind: "pageup", run: () => (reader() ? readerBox : detailBox)?.scrollBy(-0.8, "viewport") },
      { bind: "n", enabled: () => reader()?.nextOffset != null || !!reader()?.directory?.cursor, run: () => {
        if (reader()?.directory) void nextDirectoryPage().catch((failure) => setError(errorText(failure)))
        else void openReader(reader()!.nextOffset!)
      } },
      { bind: "]", enabled: () => !reader() && nextCursor() !== null, run: () => turnPage(true) },
      { bind: "[", enabled: () => !reader() && pageHistory().length > 0, run: () => turnPage(false) },
      { bind: "r", run: () => {
        if (reader()) void openReader(0, reader()!.ref, reader()!.path)
        else { resetPage(); void load() }
      } },
      { bind: "b", enabled: () => !reader() && tab() === 'process' && !!summary()?.exploration, run: () => {
        setRouteMode(!routeMode()); setRows([]); setSelected(0); resetPage(); void load('process')
      } },
      { bind: "x", enabled: () => !reader() && (tab() === "review" || tab() === "knowledge"), run: () => setReviewRaw(!reviewRaw()) },
      { bind: "l", enabled: () => !reader() && (tab() === "review" || tab() === "knowledge"), run: () => {
        const count = current() ? reviewCard(current()!.data, tab() === "knowledge" ? knowledgeDetail() : reviewSource()).links.length : 0
        if (count) setReviewLink((reviewLink() + 1) % count)
        detailBox?.scrollTo(0)
      } },
      { bind: "backspace", enabled: () => reader() ? readerHistory().length > 0 : tab() === "review" || tab() === "knowledge", run: () => {
        if (reader()) { readerGeneration++; setReader(readerHistory().at(-1)!); setReaderHistory(readerHistory().slice(0, -1)) }
        else void describe().catch((failure) => setError(errorText(failure)))
      } },
      {
        bind: "f",
        enabled: () => !reader() && (tab() === "knowledge" || tab() === "review"),
        run: () => {
          if (tab() === "review") {
            const collections = REVIEW_COLLECTIONS.map((v) => v.id)
            setReviewCollection(collections[(collections.indexOf(reviewCollection() as any) + 1) % collections.length]!)
            setReviewKind("")
            setReviewState("")
            resetPage()
            void load()
            return
          }
          const kinds = chips().kinds.map((entry) => entry.kind)
          cycle(kinds.length ? kinds : summary()?.knowledge_kinds ?? [], kindFilter(), setKindFilter)
        },
      },
      {
        bind: "s",
        enabled: () => !reader() && (tab() === "knowledge" || tab() === "review" && ['review_todos','review_obligations'].includes(reviewCollection())),
        run: () => {
          if (tab() === "knowledge") cycle(summary()?.knowledge_statuses ?? [], statusFilter(), setStatusFilter)
          else { const values = reviewCollection()==='review_obligations' ? ['', 'pending','running','completed','out_of_scope','waived'] : ["", "pending", "running", "proposal_ready", "completed"]; setReviewState(values[(values.indexOf(reviewState()) + 1) % values.length]!); resetPage(); void load() }
        },
      },
      { bind: "g", enabled: () => !reader() && tab() === "review", run: () => {
        const values = ["", ...reviewKinds()]
        setReviewKind(values[(values.indexOf(reviewKind()) + 1) % values.length]!)
        resetPage(); void load()
      } },
      { bind: "o", enabled: () => !reader() && tab() === "review", run: () => {
        const values = ["", ...(graph()?.nodes.map((v) => v.id) ?? summary()?.node_ids ?? [])]
        setReviewNode(values[(values.indexOf(reviewNode()) + 1) % values.length]!)
        resetPage(); void load()
      } },
      {
        title: "Close research board",
        bind: "escape",
        run: () => {
          if (reader()) { readerGeneration++; setReader(null); setReaderHistory([]) }
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
    if (reader()?.directory) return [["j/k", "选择"], ["Enter", "打开"], ["n", "下一页"], ["r", "目录首页"], ["Backspace", "上级"], ["Esc", "返回看板"]]
    if (reader()) return [["Esc", "返回"], ["PgUp/PgDn", "翻页"], ...(reader()?.nextOffset != null ? [["n", "读取下一段"] as [string, string]] : [])]
    if (tab() === "review") return [["v", "成果内容"], ["f", "检查类别"], ["j/k", "移动"], ["[/]", "页"], ["l/Enter", "依据"], ["r", "刷新"], ["Esc", "关闭"]]
    if (tab() === "overview") return [["1-3", "导航"], ["PgUp/PgDn", "滚动"], ["r", "刷新"], ["Esc", "关闭"]]
    if (tab() === 'process' && summary()?.exploration) return [["b", routeMode() ? "节点" : "路线"], ["j/k", "移动"],
      ["[/]", "列表翻页"], ["PgUp/PgDn", "滚动详情"], ["r", "刷新"], ["Esc", "关闭"]]
    return [
      ["v", "切换内容"],
      ["j/k", "移动"],
      ["[/]", "列表翻页"],
      ["PgUp/PgDn", "滚动详情"],
      [tab() === "knowledge" ? "l/Enter" : "Enter", tab() === "runtime" ? "打开会话" : tab() === "knowledge" ? "依据" : "打开文件"],
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
        {/* The overview shows the full goal; elsewhere one line keeps the context. */}
        <span style={{ fg: colors.base }}>
          {tab() === "overview" && summary()?.project ? "" : `  ${fit(summary()?.project?.goal ?? "研究看板", dimensions().width - 24)}`}
        </span>
        <span style={{ fg: colors.muted }}>{loading() ? "  读取中…" : ""}</span>
      </text>
      <text wrapMode="none" flexShrink={0} marginTop={1}>
        <For each={TABS}>
          {(item) => (
            <>
              <span
                style={{
                  fg: item.id === activeTab().id ? colors.base : colors.muted,
                  bg: item.id === activeTab().id ? colors.tab : colors.none,
                  attributes: item.id === activeTab().id ? TextAttributes.BOLD : TextAttributes.NONE,
                }}
              >
                {` ${item.key} ${item.title} `}
              </span>
              <span>{" "}</span>
            </>
          )}
        </For>
      </text>
      <Show when={subviews().length > 1 && !reader()}>
        <text flexShrink={0}>
          <For each={subviews()}>{(view) => <span style={{ fg: view.id === tab() ? colors.base : colors.muted,
            attributes: view.id === tab() ? TextAttributes.BOLD : TextAttributes.NONE }}>{` ${view.title} `}</span>}</For>
          <span style={{ fg: colors.muted }}> · v 切换</span>
        </text>
      </Show>
      <Show when={tab() === "knowledge" && !reader() && knowledgeTotals().length}>
        <box marginTop={1} flexShrink={0}>
          <KnowledgeFilter
            context={context}
            counts={chips().kinds}
            total={chips().total}
            kind={kindFilter()}
            status={statusFilter()}
          />
        </box>
      </Show>
      <Show when={tab() === "review" && !reader()}>
        <box flexDirection="column" flexShrink={0}>
        <text fg={colors.base} flexShrink={0}>{`${REVIEW_COLLECTIONS.find((v) => v.id === reviewCollection())!.title} · 共 ${reviewTotal()} 条 · 第 ${pageHistory().length + 1} 页${reviewNode() ? ` · ${reviewCollection() === "critical_now" ? "决策范围 " : "节点 "}${reviewNode()}` : " · 全项目"}${reviewKind() ? ` · ${reviewFilterLabel(reviewKind())}` : ""}${reviewState() ? ` · ${reviewFilterLabel(reviewState())}` : ""}`}</text>
        <text fg={colors.muted} flexShrink={0}>{REVIEW_COLLECTIONS.find((v) => v.id === reviewCollection())!.note}</text>
        <text fg={colors.muted} flexShrink={0}>{reviewCollection() === "critical_now" ? "g 类型 · o 决策范围（保留相关性未知项） · x 原始详情" : "g 类型 · s 审阅状态 · o 节点 · x 原始详情"}</text>
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
              <Show when={reader()?.truncated}><text fg={colors.warning}>文件超出内置预览范围，以下内容未显示完整。</text></Show>
              <box marginTop={1} flexDirection="column">
                <Show when={reader()?.directory} fallback={<Show when={reader()?.markdown} fallback={<text fg={colors.base}>{reader()?.text}</text>}>
                  <Markdown style={markdown} content={reader()?.text ?? ""} />
                </Show>}>
                  {(directory) => <For each={directory().items} fallback={<text fg={colors.muted}>空目录</text>}>
                    {(entry, index) => <text id={`reader-entry-${index()}`} fg={index() === directory().selected ? colors.brand : colors.base}>
                      {`${index() === directory().selected ? "▸" : " "} ${entry.kind === "directory" ? "目录" : "文件"}  ${entry.name}`}
                    </text>}
                  </For>}
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
                    <Match when={tab() === 'process' && routeMode() && rows().length}>
                      <For each={rows()}>{(row,index) => <text id={`research-row-${index()}`} fg={index() === selected() ? colors.brand : colors.base}>
                        {`${index() === selected() ? '▸' : ' '} ${row.code}  ${row.title}`}
                      </text>}</For>
                    </Match>
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
                    <Match when={rows().length && tab() === "review"}>
                      <For each={rows()}>{(row, index) => <ReviewRow context={context} row={row} index={index()} selected={index() === selected()} width={listWidth() - 4} />}</For>
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
              {/* Outside the scrolling list, so it stays visible: the list is one page, the counts are not. */}
              <Show when={tab() === "knowledge" && knowledgeListed() > rows().length}>
                <text fg={colors.muted} wrapMode="none" flexShrink={0}>
                  {` 本页 ${rows().length} 条，共 ${knowledgeListed()} 条；[/] 翻页`}
                </text>
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
                <Match when={tab() === 'process' && routeMode()}>
                  <Show when={current()?.data} fallback={<text fg={colors.muted}>尚无路线卡。</text>}>
                    {(route) => <box flexDirection="column">
                      <text fg={colors.base}>{route().title}</text>
                      <text fg={colors.muted}>{`状态  ${ROUTE_STATUS[route().disposition] ?? route().disposition} · 版本 ${route().revision}`}</text>
                      <text fg={colors.muted}>{`范围  ${route().scope_id} · 目标版本 ${route().goal_version}`}</text>
                      <text fg={colors.base}>{`理由  ${route().rationale}`}</text>
                      <For each={route().relations ?? []}>{relation => <text fg={colors.link}>{`关系  ${relation.kind} → ${relation.route_ref}`}</text>}</For>
                      <For each={route().component_refs ?? []}>{ref => <text fg={colors.link}>{`组合组件  ${ref}`}</text>}</For>
                      <For each={route().baseline_refs ?? []}>{ref => <text fg={colors.link}>{`冻结基线  ${ref}`}</text>}</For>
                      <text fg={colors.base}>{`关键假设  ${route().critical_assumption}`}</text>
                      <Show when={route().disposition_reason}><text fg={colors.base}>{`处置理由  ${route().disposition_reason}`}</text></Show>
                      <Show when={route().reactivation_condition}><text fg={colors.base}>{`恢复条件  ${route().reactivation_condition}`}</text></Show>
                      <Show when={route().next_probe}><text fg={colors.base}>{`下一探针  ${typeof route().next_probe === 'string' ? route().next_probe : route().next_probe?.question ?? '已登记'}`}</text></Show>
                      <For each={route().evidence_refs ?? []}>{(ref) => <text fg={colors.link}>{`依据  ${ref}`}</text>}</For>
                    </box>}
                  </Show>
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
                  <Show when={knowledgeDetail()?.ref && !knowledgeDetail().ref.startsWith("knowledge/")} fallback={<>
                    <Show when={current()}>
                      <text fg={colors.link}>{(() => {
                        const links = reviewCard(current()!.data, knowledgeDetail()).links
                        const link = links[reviewLink()]
                        return link ? `l/Enter ${link.label}：${link.ref} · Backspace 返回 · x 原始详情` : ""
                      })()}</text>
                    </Show>
                    <KnowledgeDetail context={context} item={knowledgeDetail() ?? current()?.data} owners={owners()} titles={titles()} />
                    <Show when={reviewRaw()}><text fg={colors.muted}>{JSON.stringify(knowledgeDetail(), null, 2)}</text></Show>
                  </>}>
                    <ReviewDetail context={context} item={current()?.data} source={knowledgeDetail()} selectedLink={reviewLink()} raw={reviewRaw()} />
                  </Show>
                </Match>
                <Match when={tab() === "materials"}>
                  <PublicationDetail context={context} row={current()} markdown={markdown} width={detailWidth() - 4} />
                </Match>
                <Match when={tab() === "review"}>
                  <ReviewDetail context={context} item={current()?.data} source={reviewSource()} selectedLink={reviewLink()} raw={reviewRaw()} />
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
