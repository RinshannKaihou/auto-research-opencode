import { test, expect, afterAll } from 'bun:test'
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, utimesSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { WorkspaceProbe, SOURCE_LIMITS } from '../src/workspace-probe'

const captured:Record<string,unknown>={limits:SOURCE_LIMITS}
afterAll(()=>{
  if(process.env.R2_CAPTURE_PROBE)writeFileSync(new URL('../../docs/evidence/r2-runtime/workspace-probe.json',import.meta.url),JSON.stringify(captured,null,2)+'\n')
})

test('workspace worker excludes environments and hashes only changed source files',async()=>{
  const root=mkdtempSync(join(tmpdir(),'r2-source-'))
  const probe=new WorkspaceProbe({...SOURCE_LIMITS,timeoutMs:2000})
  try{
    writeFileSync(join(root,'experiment.py'),'print(1)\n')
    for(const [name,marker] of [['env','pyvenv.cfg'],['conda','conda-meta']]){
      const path=join(root,name!);mkdirSync(path)
      if(marker==='conda-meta')mkdirSync(join(path,marker))
      else writeFileSync(join(path,marker!),'environment')
      writeFileSync(join(path,'ignored.py'),'private environment code')
    }
    mkdirSync(join(root,'site-packages'));writeFileSync(join(root,'site-packages','ignored.py'),'ignored')
    mkdirSync(join(root,'inputs'));writeFileSync(join(root,'inputs','frozen.py'),'immutable handoff source')
    const first=await probe.sample(root)
    expect(first.digest).toBeString();expect(first.files).toBe(1);expect(first.filesHashed).toBe(1)
    const cached=await probe.sample(root)
    expect(cached.digest).toBe(first.digest);expect(cached.filesHashed).toBe(0);expect(cached.bytesRead).toBe(0)
    writeFileSync(join(root,'experiment.py'),'print(100)\n')
    const changed=await probe.sample(root)
    expect(changed.digest).not.toBe(first.digest);expect(changed.filesHashed).toBe(1)
    utimesSync(join(root,'experiment.py'),new Date(),new Date(Date.now()+1000))
    const touched=await probe.sample(root)
    expect(touched.digest).toBe(changed.digest)
    captured.incremental={first,cached,changed,touchRetainsDigest:touched.digest===changed.digest}
    writeFileSync(join(root,'notes.md'),'bookkeeping')
    expect((await probe.sample(root)).digest).toBe(changed.digest)
  }finally{probe.close();rmSync(root,{recursive:true,force:true})}
})

test.each(['entries','files','bytes'] as const)('workspace %s limit returns unknown, never a partial digest',async limit=>{
  const root=mkdtempSync(join(tmpdir(),'r2-source-limit-'))
  const probe=new WorkspaceProbe({...SOURCE_LIMITS,timeoutMs:2000,[limit]:1})
  try{
    writeFileSync(join(root,'a.py'),'print(1)\n');writeFileSync(join(root,'b.py'),'print(2)\n')
    const result=await probe.sample(root)
    expect(result.digest).toBeNull()
    expect(result.reason).toBe(({entries:'entry_limit',files:'file_limit',bytes:'byte_limit'})[limit])
    captured[limit]=result
  }finally{probe.close();rmSync(root,{recursive:true,force:true})}
})

test('a blocked worker has a host deadline and cannot block host timers or queue more work',async()=>{
  const root=mkdtempSync(join(tmpdir(),'r2-source-deadline-'))
  writeFileSync(join(root,'blocked.ts'),'import {parentPort} from "node:worker_threads";parentPort.on("message",()=>{});')
  const probe=new WorkspaceProbe({...SOURCE_LIMITS,timeoutMs:80},pathToFileURL(join(root,'blocked.ts')))
  let ticks=0
  const timer=setInterval(()=>ticks++,5)
  try{
    const pending=probe.sample(root)
    expect((await probe.sample(root)).reason).toBe('busy')
    const result=await pending
    expect(result.digest).toBeNull();expect(result.reason).toBe('time_limit');expect(ticks).toBeGreaterThan(0)
    captured.blockedWorker={...result,hostTimersResponsive:ticks>0}
  }finally{clearInterval(timer);probe.close();rmSync(root,{recursive:true,force:true})}
})
