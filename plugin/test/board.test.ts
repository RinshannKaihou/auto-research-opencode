import { describe, expect, test } from "bun:test"
import { fit, formatDuration, nodeTitle, processView, railLayout } from "../src/board/process"

const edge = (from: string, to: string, relation_type = "depends_on") => ({
  predecessor_node_id: from,
  successor_node_id: to,
  relation_type,
})

/** The dependency shape of a real seven-node project. */
const dependencies = [
  edge("X-1", "X-2"),
  edge("X-2", "X-3"),
  edge("X-3", "X-4"),
  edge("X-3", "X-5"),
  edge("X-5", "X-6"),
  edge("X-3", "X-6"),
  edge("X-4", "X-6"),
  edge("X-6", "X-7"),
  edge("X-3", "X-7", "branches_from"),
]
const nodes = ["X-1", "X-2", "X-3", "X-4", "X-5", "X-6", "X-7"].map((node_id) => ({ node_id, status: "open", question: `${node_id} 的问题是什么？后文` }))
const empty = { attempts: [], tasks: [], sessions: [], publications: [], snapshots: [], knowledge: [] }

describe("process rail", () => {
  test("draws a branch and a merge, leaving implied edges to the detail", () => {
    const view = processView({ nodes, dependencies, ...empty })
    expect(view.rail.map((line) => `${line.rail}|${line.kind === "node" ? view.nodes[line.node!]!.id : line.kind}`)).toEqual([
      "●  |X-1",
      "│  |meta",
      "●  |X-2",
      "│  |meta",
      "●  |X-3",
      "│  |meta",
      "├─╮|link",
      "● │|X-4",
      "│ │|meta",
      "│ ●|X-5",
      "│ │|meta",
      "├─╯|link",
      "●  |X-6",
      "│  |meta",
      "●  |X-7",
      "   |meta",
    ])
    // The rail drops X-3 → X-6 and X-3 → X-7; the node still lists every edge.
    const x3 = view.nodes.find((node) => node.id === "X-3")!
    expect(x3.downstream.map((edge) => edge.to)).toEqual(["X-4", "X-5", "X-6", "X-7"])
  })

  test("independent roots get their own lanes", () => {
    const { rail } = railLayout(["A", "B", "C"], new Map([["A", ["C"]], ["B", ["C"]], ["C", []]]))
    expect(rail.map((line) => line.rail)).toEqual(["●  ", "│  ", "│ ●", "│ │", "├─╯", "●  ", "   "])
  })
})

describe("process channels", () => {
  test("execution, delivery and knowledge are derived separately", () => {
    const view = processView(
      {
        nodes: nodes.slice(0, 3),
        dependencies: dependencies.slice(0, 2),
        attempts: [
          { attempt_id: "A-1", node_id: "X-1", state: "finished", started_at: "2026-09-29T11:00:00Z", ended_at: "2026-09-29T11:24:00Z" },
          { attempt_id: "A-2", node_id: "X-2", state: "open", started_at: "2026-09-29T12:00:00Z", ended_at: null },
        ],
        tasks: [{ task_id: "T-1", node_id: "X-3", state: "queued" }],
        sessions: [{ session_id: "s2", node_id: "X-2", role: "node_core", pause_reason: "fault" }],
        publications: [
          { publication_id: "P-1", node_id: "X-1", status: "complete", display: { title: "X-1：执行基础" }, items: [{ item_id: "a" }, { item_id: "__research_display" }] },
        ],
        snapshots: [{ snapshot_id: "S-9", attempt_id: "A-2" }],
        knowledge: [
          { ref: "knowledge/K-1@1", kind: "claim", node_id: null, evidence_refs: ["pub/P-1#a"] },
          { ref: "knowledge/K-2@1", kind: "observation", node_id: null, evidence_refs: ["S-9"] },
        ],
      },
      Date.parse("2026-09-29T13:30:00Z"),
    )
    const [x1, x2, x3] = view.nodes
    expect([x1!.execution, x2!.execution, x3!.execution]).toEqual(["finished", "paused", "queued"])
    expect(x1!.title).toBe("执行基础")
    expect(x1!.delivery).toEqual({ id: "P-1", status: "complete", items: 1 })
    expect(formatDuration(x1!.duration!)).toBe("24 分钟")
    expect(formatDuration(x2!.duration!)).toBe("1.5 小时")
    expect(x1!.knowledge.map((item) => item.ref)).toEqual(["knowledge/K-1@1"])
    expect(x2!.knowledge.map((item) => item.ref)).toEqual(["knowledge/K-2@1"])
    expect(x3!.delivery).toBeUndefined()
  })

  test("titles fall back to the question's first clause", () => {
    expect(nodeTitle({ node_id: "X-9", question: "如何在物理 GPU 6/7 上建立执行基础：设备绑定" })).toBe("如何在物理 GPU 6/7 上建立执行基础")
    expect(nodeTitle({ node_id: "X-9", question: "短？" })).toBe("短？")
  })

  test("fit counts wide characters as two cells", () => {
    expect(fit("误差结构与三命题判定", 9)).toBe("误差结构…")
    expect(fit("abc", 3)).toBe("abc")
  })
})
