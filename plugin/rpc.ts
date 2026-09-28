import { Rpc } from "@opencode/plugin"

/**
 * The only channel between the terminal UI and the server plugin.
 * `command` runs /research actions; `board` serves read-only project views.
 */
export const ResearchRpc = Rpc.define({
  id: "auto-research",
  methods: {
    command: {
      input: {
        type: "object",
        properties: {
          action: { type: "string" },
          sessionID: { type: "string" },
          directory: { type: "string" },
          args: { type: "object" },
        },
        required: ["action"],
      },
      output: {},
    },
    session: {
      input: {
        type: "object",
        properties: { sessionID: { type: "string" } },
        required: ["sessionID"],
      },
      output: {},
    },
    board: {
      input: {
        type: "object",
        properties: {
          view: { type: "string" },
          sessionID: { type: "string" },
          directory: { type: "string" },
          fields: { type: "object" },
        },
        required: ["view"],
      },
      output: {},
    },
  },
  events: {
    changed: { schema: { type: "object", properties: { root: { type: "string" } } } },
  },
})
