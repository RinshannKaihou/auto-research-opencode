import { parentPort } from 'node:worker_threads'
import { createHash } from 'node:crypto'
import { closeSync, constants, existsSync, fstatSync, lstatSync, openSync, opendirSync, readSync } from 'node:fs'
import { basename, join, relative } from 'node:path'
import type { SourceResult } from './workspace-probe'

const SUFFIXES = new Set(['py','pyi','ipynb','c','cc','cpp','h','hpp','cu','cuh','rs','go','ts','tsx','js','jsx','sh','r','jl','yaml','yml','toml'])
const SKIP = new Set(['inputs','node_modules','__pycache__','workspaces','venv','site-packages','dist-packages'])
let cachedRoot: string | undefined
let cache = new Map<string, { stamp: string; digest: string }>()
const stamp = (stat: ReturnType<typeof fstatSync>): string =>
  [stat.dev,stat.ino,stat.size,stat.mtimeMs,stat.ctimeMs].join(':')

parentPort!.on('message', ({ cwd, limits }) => {
  const result: SourceResult = { digest: null, entries: 0, files: 0, filesHashed: 0, bytesRead: 0 }
  const next = new Map<string, { stamp: string; digest: string }>()
  const files: [string,string][] = []
  const started = performance.now()
  const exhausted = (): boolean => performance.now() - started >= limits.timeoutMs
  if (cachedRoot !== cwd) { cache.clear(); cachedRoot = cwd }
  try {
    const stack = [cwd]
    while (stack.length && !result.reason) {
      const directory = stack.pop()!
      if (exhausted()) { result.reason = 'time_limit'; break }
      if (SKIP.has(basename(directory)) || existsSync(join(directory,'pyvenv.cfg')) || existsSync(join(directory,'conda-meta'))) continue
      const reader = opendirSync(directory)
      try {
        let entry
        while ((entry = reader.readSync())) {
          if (++result.entries > limits.entries) { result.reason = 'entry_limit'; break }
          if (exhausted()) { result.reason = 'time_limit'; break }
          if (entry.name.startsWith('.') || entry.isSymbolicLink()) continue
          const path = join(directory,entry.name)
          if (entry.isDirectory()) { if (!SKIP.has(entry.name)) stack.push(path); continue }
          if (!entry.isFile() || !SUFFIXES.has(entry.name.split('.').at(-1)!.toLowerCase())) continue
          if (++result.files > limits.files) { result.reason = 'file_limit'; break }
          const before = lstatSync(path)
          if (!before.isFile() || before.isSymbolicLink()) { result.reason = 'changed_during_scan'; break }
          const key = relative(cwd,path)
          const fingerprint = stamp(before)
          let value = cache.get(key)
          if (value?.stamp !== fingerprint) {
            if (before.size > limits.bytes - result.bytesRead) { result.reason = 'byte_limit'; break }
            const fd = openSync(path,constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
            try {
              const hash = createHash('sha256')
              const buffer = Buffer.alloc(64 * 1024)
              let count
              while ((count = readSync(fd,buffer,0,Math.min(buffer.length,limits.bytes-result.bytesRead+1),null))) {
                result.bytesRead += count
                if (result.bytesRead > limits.bytes) { result.reason = 'byte_limit'; break }
                if (exhausted()) { result.reason = 'time_limit'; break }
                hash.update(buffer.subarray(0,count))
              }
              if (result.reason) break
              if (stamp(fstatSync(fd)) !== fingerprint) { result.reason = 'changed_during_scan'; break }
              value = { stamp: fingerprint, digest: hash.digest('hex') }
              result.filesHashed++
            } finally { closeSync(fd) }
          }
          next.set(key,value!)
          files.push([key,value!.digest])
        }
      } finally { reader.closeSync() }
    }
    if (!result.reason) {
      files.sort((a,b) => a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0)
      result.digest = createHash('sha256').update(JSON.stringify(files)).digest('hex')
      cache = next
    }
  } catch { result.reason = 'io_error' }
  parentPort!.postMessage(result)
})
