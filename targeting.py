"""Explicit per-unit flags and cache contexts; unchanged units keep legacy keys."""
import hashlib,json
from pathlib import Path


def overrides_for(sources, raw):
    if not isinstance(raw,dict): raise ValueError('Per-file overrides must be a mapping.')
    names={p.name:p for p in sources}; result={}
    for name, value in raw.items():
        if name not in names: raise ValueError('Unknown target file: '+str(name))
        if not isinstance(value,dict) or set(value)-{'opt','timing'}: raise ValueError('Invalid override for '+name)
        opt=value.get('opt'); timing=value.get('timing',False)
        if opt is not None and str(opt) not in ('0','1','2'): raise ValueError('Optimization must be 0, 1 or 2.')
        if type(timing) is not bool: raise ValueError('Timing must be boolean.')
        result[name]={'opt':str(opt) if opt is not None else None,'timing':timing}
    return result


def parse_overrides(text):
    result={}
    for n,line in enumerate(text.splitlines(),1):
        line=line.strip()
        if not line or line.startswith('#'): continue
        parts=line.split('|'); name=parts[0].strip()
        if len(parts) not in (2,3) or not name or name in result: raise ValueError(f'Invalid or duplicate target at line {n}. Use filename | 0 | timing')
        opt=parts[1].strip(); timing=len(parts)==3 and parts[2].strip().lower()=='timing'
        if opt not in ('0','1','2','same') or (len(parts)==3 and not timing): raise ValueError(f'Invalid target at line {n}.')
        result[name]={'opt':None if opt=='same' else opt,'timing':timing}
    return result


def format_overrides(mapping):
    return '\n'.join(f'{name} | {v.get("opt") if v.get("opt") is not None else "same"}'+(' | timing' if v.get('timing') else '') for name,v in sorted(mapping.items()))


def flags_for(flags, override):
    if not override: return list(flags)
    opt=override.get('opt'); result=[('-O'+opt if opt is not None and f in ('-O0','-O1','-O2') else f) for f in flags]
    if override.get('timing'): result.append('-ftime-report')
    return result


def fingerprint(base, flags):
    return hashlib.sha256(json.dumps(dict(base,flags=flags),sort_keys=True).encode()).hexdigest()
