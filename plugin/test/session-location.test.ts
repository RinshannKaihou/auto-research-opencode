import {afterEach, beforeEach, expect, test} from 'bun:test'
import {mkdirSync, mkdtempSync, rmSync, symlinkSync} from 'node:fs'
import {tmpdir} from 'node:os'
import {join} from 'node:path'
import {assertResearchTools, isSessionEntryError, isSessionMove, locationError, sameDirectory, toolEntryError} from '../src/session-location'

let root: string
beforeEach(() => { root = mkdtempSync(join(tmpdir(), 'aro-location-unit-')) })
afterEach(() => rmSync(root, {recursive: true, force: true}))

test('directory equality resolves symlinks without granting identity to descendants', () => {
  mkdirSync(join(root, 'project'))
  mkdirSync(join(root, 'project', 'child'))
  symlinkSync(join(root, 'project'), join(root, 'alias'))
  expect(sameDirectory(join(root, 'alias'), join(root, 'project'))).toBe(true)
  expect(sameDirectory(join(root, 'project', 'child'), join(root, 'project'))).toBe(false)
  expect(sameDirectory(join(root, 'missing', '..', 'future'), join(root, 'future'))).toBe(true)
})

test('location diagnostics distinguish research root, host directory and plugin scope', () => {
  const error = locationError('/research', '/host-workspace', '/plugin-instance')
  for (const path of ['/research', '/host-workspace', '/plugin-instance']) expect(error.message).toContain(path)
  expect(isSessionEntryError(error)).toBe(true)
  expect(isSessionEntryError(toolEntryError('/research', ['research_finish']))).toBe(true)
  expect(isSessionEntryError(new Error('unrelated failure'))).toBe(false)
  expect(isSessionEntryError(null)).toBe(false)
})

test('move detection targets the host effective name without matching execute or arbitrary names', () => {
  expect(isSessionMove('opencode_session_move')).toBe(true)
  for (const name of ['execute', 'shell', 'session_move', 'opencode.session_move', 'custom_session_move']) expect(isSessionMove(name)).toBe(false)
})

test('working-tool requirements follow the registered role and identify the missing tool', () => {
  const tools = new Set(['research_query', 'research_publish', 'research_checkpoint', 'research_finish'])
  expect(() => assertResearchTools(root, 'node_core', tools)).not.toThrow()
  tools.delete('research_finish')
  expect(() => assertResearchTools(root, 'node_core', tools)).toThrow('research_finish')
  expect(() => assertResearchTools(root, 'main', new Set(['research_query', 'research_dispatch']))).not.toThrow()
  expect(() => assertResearchTools(root, 'main', tools)).toThrow('research_dispatch')
  expect(() => assertResearchTools(root, 'specialist', new Set())).not.toThrow()
  expect(() => assertResearchTools(root, undefined, new Set())).not.toThrow()
})
