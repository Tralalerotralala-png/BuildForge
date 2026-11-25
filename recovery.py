"""Windows-only targeted process discovery/recovery. Never terminate by image name."""
import base64, ctypes, json, os, subprocess, time
from pathlib import Path

class RecoveryError(RuntimeError): pass

def ps(script):
    if os.name != 'nt': raise RecoveryError('Process recovery is available on Windows only.')
    cmd=['powershell.exe','-NoLogo','-NoProfile','-NonInteractive','-EncodedCommand',base64.b64encode(('$ErrorActionPreference="Stop"; '+script).encode('utf-16le')).decode()]
    try:
        r=subprocess.run(cmd,capture_output=True,encoding='utf-8',errors='replace',timeout=35,creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError,subprocess.TimeoutExpired) as e: raise RecoveryError('Windows process query failed; do not clear the lock.') from e
    if r.returncode: raise RecoveryError('Windows process query failed; do not clear the lock. '+r.stderr[:500])
    try: return json.loads(r.stdout.strip() or 'null')
    except ValueError as e: raise RecoveryError('Invalid process-query response; do not clear the lock.') from e

def argv_windows(s):
    if not s: return []
    import ctypes.wintypes as w
    n=ctypes.c_int(); fn=ctypes.windll.shell32.CommandLineToArgvW
    fn.argtypes=[w.LPCWSTR,ctypes.POINTER(ctypes.c_int)]; fn.restype=ctypes.POINTER(w.LPWSTR)
    ptr=fn(s,ctypes.byref(n))
    if not ptr: raise RecoveryError('Cannot parse process command line.')
    try:return [ptr[i] for i in range(n.value)]
    finally:
        f=ctypes.windll.kernel32.LocalFree; f.argtypes=[ctypes.c_void_p]; f.restype=ctypes.c_void_p; f(ptr)

def norm(s):
    # Match Windows paths case-insensitively; no substring matches.
    import ntpath
    return ntpath.normcase(ntpath.normpath(s))

def classify(name,args,out):
    import ntpath
    exe=name.lower(); dest=norm(str(out))
    def opt(flag):
        for i,v in enumerate(args):
            if v==flag and i+1<len(args): return args[i+1]
            if v.startswith(flag+'='):return v[len(flag)+1:]
        return None
    if exe.startswith(('python','py.','buildforge')):
        target=opt('--out')
        if target and norm(target)==dest:
            scripts={ntpath.basename(v).lower() for v in args if v.lower().endswith('.py')}
            if scripts & {'compile_m2_objects.py','engine.py'} or (exe.startswith('buildforge') and '--engine' in args):return 'Build controller'
            return 'Unrecognized process using this output'
    if exe.startswith(('clang','cc1')):
        target=opt('-o')
        if target and norm(ntpath.dirname(target))==dest:return 'Compiler for this output'
    return None

def inventory(out):
    data=ps('''[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); $p=@(Get-CimInstance Win32_Process | Where-Object {$_.Name -match '^(python|py\\.|clang|cc1|buildforge)'} | ForEach-Object { [pscustomobject]@{pid=[int]$_.ProcessId; name=$_.Name; cmd=$_.CommandLine; born=$_.CreationDate.ToString('o')} }); ConvertTo-Json -InputObject $p -Compress -Depth 4''')
    matches=[]; unknown=[]
    for p in data or []:
        if not p.get('cmd'):
            unknown.append(p['pid']);continue
        role=classify(p['name'],argv_windows(p['cmd']),out)
        if role: matches.append(dict(p,role=role))
    return matches,unknown

def terminate_exact(p):
    # Recheck both creation time and command line immediately before stopping.
    payload=base64.b64encode(json.dumps({k:p[k] for k in ('pid','born','cmd','name')},ensure_ascii=False).encode()).decode()
    script=f'''$v=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}')) | ConvertFrom-Json; $p=Get-CimInstance Win32_Process -Filter ('ProcessId='+$v.pid); if ($null -eq $p) {{ConvertTo-Json @{{stopped=$false; gone=$true}} -Compress; exit}}; if ($p.CreationDate.ToString('o') -cne $v.born -or $p.CommandLine -cne $v.cmd -or $p.Name -cne $v.name) {{throw 'Process identity changed; refused'}}; Stop-Process -Id $v.pid -Force -ErrorAction Stop; ConvertTo-Json @{{stopped=$true}} -Compress'''
    return ps(script)

def lock_snapshot(lock):
    try:
        s=lock.stat();return (s.st_ino,s.st_size,s.st_mtime_ns,lock.read_bytes())
    except FileNotFoundError:return None

def recover(out, approved, scan=inventory, stop=terminate_exact):
    """Only stop exact approved identities. Archive lock only after two clear scans."""
    out=Path(out); lock=out/'.compile.lock'; original=lock_snapshot(lock)
    allowed={(p['pid'],p['born'],p['cmd']) for p in approved}
    current,unknown=scan(out)
    if unknown:raise RecoveryError('Some compiler/Python command lines are inaccessible. Close the original build or use the same Windows account; lock kept.')
    if any(p['role'].startswith('Unrecognized') for p in current):raise RecoveryError('Unrecognized process uses this output; lock kept.')
    if any((p['pid'],p['born'],p['cmd']) not in allowed for p in current):raise RecoveryError('Build process list changed; scan and confirm again.')
    for p in sorted(current,key=lambda p:p['role']!='Build controller'):stop(p)
    # A child started between scan and stopping may survive. Never silently kill it.
    for _ in range(2):
        time.sleep(0.8); remaining,unknown=scan(out)
        if remaining or unknown:raise RecoveryError('Processes remain or appeared. Scan again; lock kept.')
    if lock_snapshot(lock)!=original:raise RecoveryError('Lock changed during recovery; scan again.')
    if original is not None:
        import uuid
        archived=out/('.compile.lock.recovered-'+uuid.uuid4().hex[:10]); lock.rename(archived)
        return f'Lock archived as {archived.name}. Objects and stamps preserved.'
    return 'No lock remains. Objects and stamps preserved.'
