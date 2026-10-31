import { expect, test } from "bun:test"
import { mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { Observations } from "../src/observations"

test("rejected operations retain independent observations and never serialize arguments", async () => {
  const root = mkdtempSync(join(tmpdir(), "r2-observation-"))
  try {
    const log = new Observations()
    const rejected = Object.assign(new Error("private request text"), { code: "checkpoint_conflict" })
    await expect(log.invoke(root, { invocation_id: "a", operation_id: "op", session_id: "session",
      tool: "checkpoint", contract: "legacy", input_fields: 1, input_chars: 20 }, async () => {
      throw rejected
    })).rejects.toBe(rejected)
    await log.invoke(root, { invocation_id: "b", operation_id: "op2", session_id: "session",
      tool: "query", contract: "legacy", input_fields: 0, input_chars: 2 }, async () => 42)
    const directory = join(root, ".research", "observations")
    expect(readdirSync(directory)).toEqual([`${log.instanceID}.jsonl`])
    const text = readFileSync(join(directory, `${log.instanceID}.jsonl`), "utf8")
    const rows = text.trim().split("\n").map(line => JSON.parse(line))
    expect(rows.map(row => row.phase)).toEqual(["start", "end", "start", "end"])
    expect(rows[1].error_code).toBe("checkpoint_conflict")
    expect(rows[3].outcome).toBe("succeeded")
    expect(text).not.toContain("private request text")
  } finally { rmSync(root, { recursive: true, force: true }) }
})

test("observation I/O failure cannot change a successful domain result", async () => {
  const root = mkdtempSync(join(tmpdir(), "r2-observation-"))
  try {
    writeFileSync(join(root, ".research"), "blocks the log directory")
    const log = new Observations()
    const result = await log.invoke(root, { invocation_id: "a", operation_id: "op", session_id: "s",
      tool: "record", contract: "legacy", input_fields: 0, input_chars: 2 }, async () => ({ revision: 1 }))
    expect(result).toEqual({ revision: 1 })
    expect(log.coverage(root).complete).toBe(false)
  } finally { rmSync(root, { recursive: true, force: true }) }
})

test('transport metrics count source snapshots and omit undefined wire fields',async()=>{
  const root=mkdtempSync(join(tmpdir(),'r2-wire-'))
  try{
    const log=new Observations()
    await log.invoke(root,{invocation_id:'i',operation_id:'o',session_id:'s',tool:'dispatch',contract:'r2-v1',input_fields:1,input_chars:2},async()=>{
      log.transportObserver('o','runtime_reserve',{host_id:'opencode',session_id:'s',source_snapshot:{digest:null},checkpoint_base:undefined})!.attempt(1)
    })
    const rows=readFileSync(join(root,'.research','observations',`${log.instanceID}.jsonl`),'utf8').trim().split('\n').map(line=>JSON.parse(line))
    const transport=rows.find(row=>row.phase==='transport')
    expect(transport.host_fields).toBe(3)
    expect(transport.wire_fields).toBe(6)
  }finally{rmSync(root,{recursive:true,force:true})}
})
