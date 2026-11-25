"""Local Android ARM64 object compiler, compatible with the legacy object cache."""
import argparse, concurrent.futures as cf, hashlib, json, os, subprocess, sys, time, uuid
from collections import deque
from resource_control import LiveControl, ProcessRegistry, hardware
from pathlib import Path
from targeting import overrides_for, flags_for, fingerprint
import link_stage
from cache_compat import digest, private, units, header_digest, compile_one, available_ram, can_launch, atomic_json, GIB

def event(kind, **fields):print(json.dumps({'event':kind,**fields},ensure_ascii=True),flush=True)

def plan(c):
    src=Path(c['source']).resolve(); out=private(Path(c['output'])); ndk=Path(c['ndk']).resolve()
    includes=[Path(s).resolve() for s in c.get('includes',[]) if s.strip()]; gos=bool(c.get('gos'))
    if not src.is_dir() or not ndk.is_dir():raise ValueError('Source or NDK folder does not exist.')
    if any(not p.is_dir() for p in includes):raise ValueError('One or more include folders do not exist.')
    if any(out==p or p in out.parents or out in p.parents for p in [src,ndk,*includes]):raise ValueError('Output must be separate from input and NDK folders.')
    if gos:
        if len(includes)!=1:raise ValueError('Legacy profile needs exactly the runtime include directory.')
        sources=units(src)
        for h in ('recomp_ctx.h','recomp_bridge.h','recomp_eh.h'):
            if not (includes[0]/h).is_file():raise ValueError('Missing runtime header: '+h)
        for h in ('recomp_decls.h','recomp_simd.h'):
            if not (src/h).is_file():raise ValueError('Missing generated header: '+h)
    else:
        sources=sorted({p for ext in ('*.cpp','*.cc','*.cxx','*.c') for p in (src.rglob(ext) if c.get('recursive') else src.glob(ext)) if p.is_file()})
        if not sources:raise ValueError('No C or C++ sources found.')
        if len({p.stem.casefold() for p in sources})!=len(sources):raise ValueError('Duplicate source basenames. Choose a narrower source folder or rename conflicting units in your own project.')
    host='windows-x86_64' if os.name=='nt' else 'linux-x86_64' if sys.platform.startswith('linux') else 'darwin-x86_64'
    tool=ndk/'toolchains'/'llvm'/'prebuilt'/host; compiler=tool/'bin'/('clang++.exe' if os.name=='nt' else 'clang++'); sysroot=tool/'sysroot'
    if not compiler.is_file() or not sysroot.is_dir():raise ValueError('NDK compiler/sysroot not found.')
    api=int(c['api']); opt=str(c['opt']); workers=int(c['workers']); reserve=float(c['reserve']); headroom=float(c['headroom'])
    if not 21<=api<=35 or opt not in ('0','1','2') or not 1<=workers<=64 or not 2<=reserve<=64 or not .5<=headroom<=8:raise ValueError('Invalid API, optimization, workers or memory settings.')
    arch=c.get('arch','aarch64')
    if arch not in ('aarch64','x86_64') or (gos and arch!='aarch64'):raise ValueError('Unsupported architecture for this profile.')
    flags=[f'--target={arch}-linux-android{api}',f'--sysroot={sysroot}','-stdlib=libc++','-std=c++17',f'-O{opt}','-fPIC','-ffunction-sections','-fdata-sections','-DNDEBUG','-DANDROID',f'-I{src}',*[f'-I{p}' for p in includes]]
    # Extra definitions and include folders only. No output overrides or shell commands.
    for d in c.get('defines',[]):
        if not d or any(x in d for x in '\r\n\0'):raise ValueError('Invalid preprocessor definition.')
        flags.append('-D'+d)
    overrides_for(sources,c.get('overrides',{}));link_stage.validate_config(c)
    threshold=int(c.get('slow_seconds',900))
    if not 1<=threshold<=86400:raise ValueError('Slow-file threshold must be 1–86400 seconds.')
    roots=[src,*includes,sysroot/'usr'/'include']
    return sources,out,ndk,compiler,roots,flags,workers,reserve,headroom

def run(c, output_arg):
    sources,out,ndk,compiler,roots,flags,workers,reserve,headroom=plan(c)
    if out!=Path(output_arg).resolve():raise ValueError('Job/output mismatch.')
    out.mkdir(parents=True,exist_ok=True); lock=out/'.compile.lock'; token=uuid.uuid4().hex
    stopfile=Path(c['stopfile']); fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    lock_data=json.dumps({'engine':'BuildForge','pid':os.getpid(),'token':token,'time':time.time()}).encode()
    try:
        with os.fdopen(fd,'wb') as f:f.write(lock_data);f.flush();os.fsync(f.fileno())
        for name in ('objects.rsp','safe_status.json','object_manifest.json','link_status.json'):(out/name).unlink(missing_ok=True)
        event('phase',message='Checking toolchain and hashing headers; existing cache format preserved.',total=len(sources))
        version=subprocess.check_output([str(compiler),'--version'],stderr=subprocess.STDOUT,timeout=30)
        resource=Path(subprocess.check_output([str(compiler),'-print-resource-dir'],text=True,timeout=30).strip())/'include'
        roots += [resource,ndk/'sources'/'cxx-stl'/'llvm-libc++'/'include']
        base={'compiler_sha256':digest(compiler),'version':version.decode(errors='replace'),'headers':header_digest(roots)}
        overrides=overrides_for(sources,c.get('overrides',{})); unit_flags={s.name:flags_for(flags,overrides.get(s.name)) for s in sources}
        contexts={name:fingerprint(base,f) for name,f in unit_flags.items()}
        # Explicit targets first, then largest source files. All other objects retain their original context.
        q=deque(sorted(sources,key=lambda p:(p.name not in overrides,-p.stat().st_size,p.name))); running=set(); results={}; t=time.monotonic(); idle=None; last=0; stopped=False
        threshold=int(c.get('slow_seconds',900)); warned=set(); timings={}; submitted={}
        control=LiveControl(c.get('controlfile'),int(c.get('budget',100))); registry=ProcessRegistry(); total_ram=hardware()['total_ram']; priority='scheduler only'; last_priority=0
        event('phase',message='Compiling or validating cached objects.',total=len(sources))
        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            while q or running:
                stopped=stopfile.exists()
                free=available_ram(); budget=control.read(); target=budget.workers(workers); effective_reserve=budget.reserve(reserve,total_ram)
                if time.monotonic()-last_priority>=1:
                    priority=registry.apply(budget.priority);last_priority=time.monotonic()
                while q and not stopped and len(running)<target and can_launch(free,len(running),effective_reserve,headroom):
                    s=q.popleft(); future=pool.submit(compile_one,s,out,compiler,unit_flags[s.name],contexts[s.name],True,registry);running.add(future);submitted[future]=time.monotonic();free=available_ram();stopped=stopfile.exists()
                    budget=control.read();target=budget.workers(workers);effective_reserve=budget.reserve(reserve,total_ram)
                if running:
                    idle=None; done,running=cf.wait(running,timeout=.5,return_when=cf.FIRST_COMPLETED)
                    for f in done:
                        name,state=f.result();results[name]=state;elapsed=round(time.monotonic()-submitted.pop(f),3);timings[name]={'state':state,'seconds':elapsed};event('unit',name=name,state=state,seconds=elapsed)
                elif stopped:break
                elif q and target==0:
                    idle=None;time.sleep(.25)
                elif q:
                    idle=idle or time.monotonic()
                    if time.monotonic()-idle>120:raise ValueError('Insufficient RAM headroom for 120 seconds. Resume with fewer workers or more free RAM.')
                    time.sleep(.5)
                if time.monotonic()-last>=.7 or not (q or running):
                    counts={k:sum(v==k for v in results.values()) for k in ('compiled','cached','failed')}
                    active_units=registry.snapshot()
                    for u in active_units:
                        if u['seconds']>=threshold and u['name'] not in warned:
                            warned.add(u['name']);event('warning',message=f"Slow unit: {u['name']} ({u['seconds']} s). Elapsed time alone does not establish a stall.")
                    event('progress',done=len(results),total=len(sources),active=len(running),queued=len(q),ram=round(free/GIB,1),seconds=round(time.monotonic()-t),stopping=stopped,budget=budget.percent,target=target,reserve=round(effective_reserve,1),priority=priority,active_units=active_units,**counts);last=time.monotonic()
        counts={k:sum(v==k for v in results.values()) for k in ('compiled','cached','failed')}; complete=len(results)==len(sources) and counts['failed']==0
        entries=[]; link_ok=False
        if complete:
            if header_digest(roots)!=base['headers'] or digest(compiler)!=base['compiler_sha256']:
                raise ValueError('Headers or toolchain changed during build; resume to revalidate. No ready manifest published.')
            for src in sources:
                obj=out/(src.stem+'.o'); stamp=json.loads(obj.with_suffix('.json').read_text()); expected_context=contexts[src.name]
                if src.suffix.lower()=='.c':expected_context=hashlib.sha256((expected_context+'|C17').encode()).hexdigest()
                key={'source_sha256':digest(src),'context_sha256':expected_context}
                if not __import__('cache_compat').is_elf_object(obj,62 if c.get('arch')=='x86_64' else 183) or stamp.get('key')!=key or stamp.get('object_sha256')!=digest(obj):raise ValueError('Object or source changed before manifest: '+src.name)
                entries.append({'source':src.name,'object':obj.name,'key':key,'object_sha256':stamp['object_sha256'],'flags':unit_flags[src.name],'language':'C17' if src.suffix.lower()=='.c' else 'C++17'})
            atomic_json(out/'object_manifest.json',{'schema':1,'base_context':fingerprint(base,flags),'overrides':overrides,'objects':entries})
            tmp=out/('objects.rsp.'+token+'.tmp'); tmp.write_text(''.join(f'"{(out/(s.stem+".o")).as_posix()}"\n' for s in sources),encoding='utf-8');os.replace(tmp,out/'objects.rsp')
            if c.get('link'):
                event('phase',message='Strict shared-library link; unresolved symbols fail the link.')
                try:link_ok=link_stage.link(c,out,compiler,flags,entries,registry)
                except Exception as e:event('warning',message='Link failed: '+str(e));atomic_json(out/'link_status.json',{'success':False,'error':str(e)})
        atomic_json(out/'unit_timings.json',{'units':timings,'slow_threshold_seconds':threshold,'slow_units':sorted(warned)})
        status={'expected_units':len(sources),**counts,'all_translated_objects_ready':complete,'android_link_verified':link_ok,'device_execution_verified':False,'stopped':stopped and not complete,'mixed_flags':any(unit_flags[n]!=flags for n in unit_flags),'link_requested':bool(c.get('link'))}
        atomic_json(out/'safe_status.json',status);event('finished',**status);return (4 if c.get('link') and not link_ok else 0) if complete else 3 if stopped else 1
    finally:
        try:
            if lock.read_bytes()==lock_data:lock.unlink()
        except FileNotFoundError:pass

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--job',required=True);ap.add_argument('--out',required=True);a=ap.parse_args()
    try:return run(json.loads(Path(a.job).read_text(encoding='utf-8')),a.out)
    except FileExistsError:event('error',message='Output is locked. Use Inspect / recover in the app.');return 2
    except Exception as e:event('error',message=str(e));return 2
if __name__=='__main__':raise SystemExit(main())
