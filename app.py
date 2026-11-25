"""BuildForge ARM64/x86-64 desktop workbench. No third-party Python packages required."""
import ctypes
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from cache_compat import GIB, atomic_json, available_ram
from recovery import inventory, recover
from resource_control import hardware
from targeting import format_overrides, parse_overrides

HOME = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'BuildForgeARM64'
BASE = Path(__file__).resolve().parent


def discover_ndk():
    candidates = []
    for key in ('ANDROID_NDK_HOME', 'ANDROID_NDK_ROOT', 'NDK_HOME'):
        if os.environ.get(key):
            candidates.append(Path(os.environ[key]))
    sdk = Path(
        os.environ.get('ANDROID_SDK_ROOT')
        or os.environ.get('ANDROID_HOME')
        or Path.home() / 'AppData' / 'Local' / 'Android' / 'Sdk'
    )
    if (sdk / 'ndk').is_dir():
        candidates.extend(sorted((sdk / 'ndk').iterdir(), reverse=True))
    candidates.append(sdk / 'ndk-bundle')
    for pf in {os.environ.get('ProgramFiles', r'C:\Program Files'), os.environ.get('ProgramFiles(x86)', '')}:
        if not pf:
            continue
        hub = Path(pf) / 'Unity' / 'Hub' / 'Editor'
        if hub.is_dir():
            for ed in sorted(hub.iterdir(), reverse=True):
                candidates.append(ed / 'Editor' / 'Data' / 'PlaybackEngines' / 'AndroidPlayer' / 'NDK')
    for p in candidates:
        if (p / 'toolchains' / 'llvm' / 'prebuilt').is_dir():
            return str(p)
    return ''


def defaults():
    return dict(
        name='New Android project',
        source='',
        includes=[],
        ndk=discover_ndk(),
        output='',
        workers=min(32, os.cpu_count() or 2),
        api=21,
        opt='1',
        reserve=4,
        headroom=2,
        gos=False,
        recursive=False,
        defines=[],
        arch='aarch64',
        overrides={},
        slow_seconds=900,
        link=False,
        link_name='liboutput.so',
        link_system_libs=[],
        link_libraries=[],
        freestanding=False,
    )


def cpu_times():
    if os.name == 'nt':
        import ctypes.wintypes as w

        idle, kernel, user = w.FILETIME(), w.FILETIME(), w.FILETIME()
        if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return None
        val = lambda x: (x.dwHighDateTime << 32) + x.dwLowDateTime
        return val(idle), val(kernel) + val(user)
    if sys.platform.startswith('linux'):
        values = list(map(int, Path('/proc/stat').read_text(encoding='utf-8').splitlines()[0].split()[1:9]))
        return values[3] + values[4], sum(values)
    return None


class App:
    def __init__(self, root):
        self.root = root
        root.title('BuildForge | Local C++ build workbench · 0.5 beta')
        root.geometry('1120x860')
        root.minsize(880, 720)
        self.q = queue.Queue()
        self.proc = None
        self.stopfile = None
        self.cancelfile = None
        self.busy = False
        self.closing = False
        self.matches = []
        self.unknown = []
        self.scan_output = None
        self.last_cpu = None
        self.log_path = None
        self.active_output = None
        self.controlfile = None
        HOME.mkdir(parents=True, exist_ok=True)
        self.profiles_path = HOME / 'profiles.json'
        self.state_path = HOME / 'state.json'
        self.profiles = {}
        try:
            loaded = json.loads(self.profiles_path.read_text(encoding='utf-8'))
            if isinstance(loaded, dict):
                self.profiles = loaded
        except (OSError, ValueError):
            pass
        self.profiles[defaults()['name']] = defaults()
        initial_name = defaults()['name']
        try:
            state = json.loads(self.state_path.read_text(encoding='utf-8'))
            if isinstance(state, dict) and state.get('last_profile') in self.profiles:
                initial_name = state['last_profile']
        except (OSError, ValueError):
            pass
        self.v = {}
        self.status = tk.StringVar(value='Ready. Choose a profile or configure a new project.')
        self.progress = tk.DoubleVar()
        self.resource = tk.StringVar(value='Reading local CPU / RAM…')
        self.summary = tk.StringVar(value='No build started')
        self.recovery_status = tk.StringVar(value='Scan the selected output folder before attempting recovery.')
        self.profile = tk.StringVar(value=initial_name)
        self.mode = tk.StringVar(value='Generic Android ARM64 C++')
        self.budget = tk.IntVar(value=100)
        self.budget_text = tk.StringVar(value='100% · Maximum')
        self.effective = tk.StringVar(value='Target workers: —')
        self.hardware_info = hardware()
        self.make_ui()
        self.load(self.profiles[initial_name])
        self.root.protocol('WM_DELETE_WINDOW', self.on_close)
        self.tick()
        self.monitor()

    def make_ui(self):
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('Title.TLabel', font=('Segoe UI', 20, 'bold'))
        style.configure('Sub.TLabel', foreground='#53657b', font=('Segoe UI', 10))
        style.configure('Metric.TLabel', font=('Segoe UI', 11, 'bold'))
        style.configure('Accent.TButton', font=('Segoe UI', 10, 'bold'), padding=(12, 7))
        style.configure('TButton', padding=(8, 5))
        style.configure('TFrame', background='#f3f6fa')
        style.configure('TLabel', background='#f3f6fa')
        style.configure('TLabelframe', background='#f3f6fa')
        style.configure('TLabelframe.Label', background='#f3f6fa', font=('Segoe UI', 10, 'bold'))
        self.root.configure(bg='#f3f6fa')
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill='both', expand=True)

        top = ttk.Frame(outer)
        top.pack(fill='x')
        ttk.Label(top, text='BuildForge  0.5 beta', style='Title.TLabel').pack(side='left')
        ttk.Label(top, text='LOCAL • PRIVATE • RESUMABLE', style='Sub.TLabel').pack(side='right')
        ttk.Label(
            outer, text='Android NDK C++ object compiler · no cloud uploads or dependencies', style='Sub.TLabel'
        ).pack(anchor='w', pady=(2, 10))

        bar = ttk.Frame(outer)
        bar.pack(fill='x', pady=(0, 10))
        ttk.Label(bar, text='Profile').pack(side='left')
        self.combo = ttk.Combobox(bar, textvariable=self.profile, values=sorted(self.profiles), state='readonly', width=36)
        self.combo.pack(side='left', padx=8)
        self.combo.bind('<<ComboboxSelected>>', lambda e: self.load(self.profiles[self.profile.get()]))
        self.profile_controls = [self.combo]
        for label, command, side in [
            ('Save as…', self.save, 'left'),
            ('Delete', self.delete_profile, 'left'),
            ('New project', self.new, 'left'),
            ('Import profile…', self.import_profile, 'right'),
        ]:
            b = ttk.Button(bar, text=label, command=command)
            b.pack(side=side, padx=4)
            self.profile_controls.append(b)

        self.tabs = ttk.Notebook(outer)
        self.tabs.pack(fill='both', expand=True)
        self.config = ttk.Frame(self.tabs, padding=12)
        self.run_tab = ttk.Frame(self.tabs, padding=12)
        self.recovery_tab = ttk.Frame(self.tabs, padding=12)
        self.target_tab = ttk.Frame(self.tabs, padding=12)
        self.link_tab = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(self.config, text='  Project & performance  ')
        self.tabs.add(self.run_tab, text='  Live build  ')
        self.tabs.add(self.recovery_tab, text='  Inspect / recover  ')
        self.tabs.add(self.target_tab, text='  Target & diagnose  ')
        self.tabs.add(self.link_tab, text='  Link  ')

        f = self.config
        f.columnconfigure(1, weight=1)
        self.inputs = []
        ttk.Label(f, text='Build type').grid(row=0, column=0, sticky='w', pady=5)
        c = ttk.Combobox(
            f,
            textvariable=self.mode,
            values=['Legacy fixed-count profile', 'Generic Android ARM64 C++', 'Generic Android x86-64 C/C++'],
            state='readonly',
        )
        c.grid(row=0, column=1, columnspan=2, sticky='ew', pady=5)
        self.inputs.append(c)
        for row, (key, label) in enumerate(
            [('source', 'C++ source folder'), ('ndk', 'Android NDK folder'), ('output', 'Private object output')], 1
        ):
            self.v[key] = tk.StringVar()
            ttk.Label(f, text=label).grid(row=row, column=0, sticky='w', pady=5)
            e = ttk.Entry(f, textvariable=self.v[key])
            e.grid(row=row, column=1, sticky='ew', padx=8, pady=5)
            self.inputs.append(e)
            b = ttk.Button(f, text='Browse…', command=lambda k=key: self.browse(k))
            b.grid(row=row, column=2, sticky='e')
            self.inputs.append(b)
        ttk.Label(f, text='Include directories\n(one per line)').grid(row=4, column=0, sticky='nw', pady=5)
        self.includes = tk.Text(f, height=3, wrap='none', font=('Consolas', 10))
        self.includes.grid(row=4, column=1, sticky='ew', padx=8, pady=5)
        b = ttk.Button(f, text='Add folder…', command=self.add_include)
        b.grid(row=4, column=2, sticky='ne')
        self.inputs.extend([self.includes, b])
        ttk.Label(f, text='Extra C++ definitions\n(one NAME=VALUE per line)').grid(row=5, column=0, sticky='nw', pady=5)
        self.defines = tk.Text(f, height=2, font=('Consolas', 10))
        self.defines.grid(row=5, column=1, columnspan=2, sticky='ew', padx=8, pady=5)
        self.inputs.append(self.defines)
        self.v['recursive'] = tk.BooleanVar()
        b = ttk.Checkbutton(f, text='Search subfolders (generic projects only)', variable=self.v['recursive'])
        b.grid(row=6, column=1, sticky='w', padx=8)
        self.inputs.append(b)

        perf = ttk.LabelFrame(f, text='Performance', padding=12)
        perf.grid(row=7, column=0, columnspan=3, sticky='ew', pady=(14, 8))
        for i, (k, label, lo, hi, step) in enumerate(
            [
                ('workers', 'Workers', 1, 64, 1),
                ('reserve', 'RAM reserve (GiB)', 2, 64, 1),
                ('headroom', 'Headroom / worker (GiB)', 0.5, 8, 0.5),
                ('api', 'Android API', 21, 35, 1),
                ('opt', 'Optimization', 0, 2, 1),
            ]
        ):
            self.v[k] = tk.StringVar()
            ttk.Label(perf, text=label).grid(row=0, column=i, sticky='w', padx=5)
            spin = ttk.Spinbox(perf, textvariable=self.v[k], from_=lo, to=hi, increment=step, width=12)
            spin.grid(row=1, column=i, sticky='ew', padx=5, pady=(4, 8))
            perf.columnconfigure(i, weight=1)
            self.inputs.append(spin)
        presets = ttk.Frame(perf)
        presets.grid(row=2, column=0, columnspan=5, sticky='w')
        for label, n in [('Quiet', 4), ('Balanced', 12), ('Max 16', 16)]:
            b = ttk.Button(
                presets, text=label, command=lambda n=n: self.v['workers'].set(str(min(n, os.cpu_count() or n)))
            )
            b.pack(side='left', padx=(0, 6))
            self.inputs.append(b)
        ttk.Label(
            f,
            text='Keep API, optimization, include order and paths unchanged to reuse legacy cached objects.\n'
            'Memory throttling is automatic; worker presets do not guarantee an optimal sustained speed.',
            style='Sub.TLabel',
            wraplength=790,
        ).grid(row=8, column=0, columnspan=3, sticky='w', pady=8)

        rt = self.run_tab
        ttk.Label(rt, textvariable=self.summary, style='Metric.TLabel').pack(anchor='w', pady=(0, 8))
        ttk.Progressbar(rt, variable=self.progress, maximum=100).pack(fill='x', pady=(0, 8))
        ttk.Label(rt, textvariable=self.resource, style='Sub.TLabel').pack(anchor='w', pady=(0, 8))
        live = ttk.LabelFrame(rt, text='Live hardware budget', padding=10)
        live.pack(fill='x', pady=(0, 8))
        r = ttk.Frame(live)
        r.pack(fill='x')
        ttk.Label(r, textvariable=self.budget_text, style='Metric.TLabel').pack(side='left')
        ttk.Label(r, textvariable=self.effective).pack(side='right')
        self.live_slider = ttk.Scale(
            live, from_=0, to=100, orient='horizontal', variable=self.budget, command=self.change_budget
        )
        self.live_slider.pack(fill='x', pady=4)
        modes = ttk.Frame(live)
        modes.pack(fill='x')
        for name, value in [('Pause', 0), ('Eco', 25), ('Balanced', 50), ('Performance', 75), ('Maximum', 100)]:
            ttk.Button(modes, text=name, command=lambda v=value: self.change_budget(v)).pack(side='left', padx=(0, 5))
        ttk.Label(
            live,
            text='0% drains active jobs, then pauses. Lowering the budget never kills an active compiler. This is not an exact CPU quota.',
            style='Sub.TLabel',
            wraplength=900,
        ).pack(anchor='w', pady=(5, 0))

        units_frame = ttk.Frame(rt)
        units_frame.pack(fill='x', pady=(0, 8))
        self.live_units = ttk.Treeview(
            units_frame, columns=('file', 'pid', 'elapsed', 'cpu', 'rss'), show='headings', height=4
        )
        self.live_units.heading('file', text='Active compiler')
        self.live_units.heading('pid', text='PID')
        self.live_units.heading('elapsed', text='Elapsed (s)')
        self.live_units.heading('cpu', text='CPU time (s)')
        self.live_units.heading('rss', text='RSS (MiB)')
        self.live_units.column('file', width=360)
        self.live_units.column('pid', width=100)
        self.live_units.column('elapsed', width=100)
        self.live_units.column('cpu', width=100)
        self.live_units.column('rss', width=100)
        unit_scroll = ttk.Scrollbar(units_frame, command=self.live_units.yview)
        self.live_units.configure(yscrollcommand=unit_scroll.set)
        unit_scroll.pack(side='right', fill='y')
        self.live_units.pack(side='left', fill='x', expand=True)

        ttk.Button(rt, text='Copy selected active filenames for next retry', command=self.copy_active).pack(
            anchor='w', pady=(0, 4)
        )
        logs = ttk.Frame(rt)
        logs.pack(fill='both', expand=True)
        self.logs = tk.Text(
            logs, wrap='word', font=('Consolas', 10), bg='#152338', fg='#e6eef9', insertbackground='white', state='disabled'
        )
        self.logs.tag_configure('COMPILED', foreground='#6ee7b7')
        self.logs.tag_configure('CACHED', foreground='#93c5fd')
        self.logs.tag_configure('FAILED', foreground='#fca5a5')
        self.logs.tag_configure('WARNING', foreground='#fcd34d')
        self.logs.tag_configure('PHASE', foreground='#67e8f9')
        scroll = ttk.Scrollbar(logs, command=self.logs.yview)
        self.logs.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.logs.pack(side='left', fill='both', expand=True)
        controls = ttk.Frame(rt)
        controls.pack(fill='x', pady=(8, 0))
        ttk.Button(controls, text='Open object folder', command=self.open_output).pack(side='left')
        ttk.Button(controls, text='Open session log', command=self.open_log).pack(side='left', padx=6)
        ttk.Button(controls, text='Clear log', command=self.clear_log).pack(side='left')

        rec = self.recovery_tab
        ttk.Label(rec, text='Recover an interrupted build', style='Metric.TLabel').pack(anchor='w')
        ttk.Label(
            rec,
            text='Scans Windows process command lines for the exact selected output. No global Python or Clang kills.\n'
            'A stale lock is archived, never used as permission to delete objects. Stop gracefully if possible.',
            wraplength=850,
            style='Sub.TLabel',
        ).pack(anchor='w', pady=(4, 10))
        ttk.Label(rec, textvariable=self.recovery_status, wraplength=850).pack(anchor='w', pady=(0, 8))
        rec_tree_frame = ttk.Frame(rec)
        rec_tree_frame.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(rec_tree_frame, columns=('pid', 'role', 'name'), show='headings', height=8)
        self.tree.heading('pid', text='PID')
        self.tree.heading('role', text='Matched build role')
        self.tree.heading('name', text='Process')
        self.tree.column('pid', width=90)
        self.tree.column('role', width=370)
        self.tree.column('name', width=200)
        rec_scroll = ttk.Scrollbar(rec_tree_frame, command=self.tree.yview)
        self.tree.configure(yscrollcommand=rec_scroll.set)
        rec_scroll.pack(side='right', fill='y')
        self.tree.pack(side='left', fill='both', expand=True)
        row = ttk.Frame(rec)
        row.pack(fill='x', pady=8)
        ttk.Button(row, text='Scan selected output', command=self.scan).pack(side='left')
        self.recover_btn = ttk.Button(
            row, text='Stop matching build / clear stale lock…', command=self.do_recover, state='disabled'
        )
        self.recover_btn.pack(side='left', padx=8)
        ttk.Label(
            rec,
            text='Recovery does not delete .o, .json or .log files. If process access is denied or a new process appears, recovery refuses to clear the lock.',
            style='Sub.TLabel',
            wraplength=850,
        ).pack(anchor='w', pady=4)

        target = self.target_tab
        ttk.Label(target, text='Targeted compilation and diagnosis', style='Metric.TLabel').pack(anchor='w')
        ttk.Label(
            target,
            text='Keep the base optimization unchanged. Give only stubborn files a separate optimization or timing report. '
            'Targets run first on the next build; unchanged files keep their legacy cache keys. Never edit an active job.',
            wraplength=900,
        ).pack(anchor='w', pady=8)
        ttk.Label(target, text='One target per line: filename.cpp | 0   or   filename.cpp | same | timing').pack(anchor='w')
        self.overrides = tk.Text(target, height=9, font=('Consolas', 11), wrap='none')
        self.overrides.pack(fill='both', expand=True, pady=6)
        self.inputs.append(self.overrides)
        row = ttk.Frame(target)
        row.pack(fill='x')
        self.v['slow_seconds'] = tk.StringVar(value='900')
        ttk.Label(row, text='Slow-unit warning (seconds)').pack(side='left')
        spin = ttk.Spinbox(row, textvariable=self.v['slow_seconds'], from_=1, to=86400, width=8)
        spin.pack(side='left', padx=8)
        self.inputs.append(spin)
        b = ttk.Button(row, text='Analyze session log…', command=self.analyze_log)
        b.pack(side='left', padx=6)
        self.inputs.append(b)
        b_miss = ttk.Button(row, text='Find missing / failed units', command=self.find_missing)
        b_miss.pack(side='left', padx=6)
        self.inputs.append(b_miss)
        ttk.Label(
            target,
            text='A slow warning is not a hang diagnosis. Timing reports are written to per-file logs only when Clang exits. '
            'Mixed flags are recorded in object_manifest.json. A downstream legacy linker may require uniform flags.',
            wraplength=900,
            style='Sub.TLabel',
        ).pack(anchor='w', pady=8)

        lk = self.link_tab
        ttk.Label(lk, text='Strict Android shared-library linking', style='Metric.TLabel').pack(anchor='w')
        self.v['link'] = tk.BooleanVar(value=False)
        b = ttk.Checkbutton(lk, text='Link after all object fingerprints pass', variable=self.v['link'])
        b.pack(anchor='w', pady=6)
        self.inputs.append(b)
        self.v['link_name'] = tk.StringVar(value='liboutput.so')
        ttk.Label(lk, text='Output library filename').pack(anchor='w')
        e = ttk.Entry(lk, textvariable=self.v['link_name'])
        e.pack(fill='x')
        self.inputs.append(e)
        ttk.Label(lk, text='Additional archive/shared library paths (one per line)').pack(anchor='w', pady=(10, 0))
        self.link_libs = tk.Text(lk, height=4, wrap='none')
        self.link_libs.pack(fill='x')
        self.inputs.append(self.link_libs)
        ttk.Label(lk, text='NDK system libraries (one name per line, e.g. log)').pack(anchor='w', pady=(10, 0))
        self.link_system = tk.Text(lk, height=3)
        self.link_system.pack(fill='x')
        self.inputs.append(self.link_system)
        self.v['freestanding'] = tk.BooleanVar(value=False)
        b = ttk.Checkbutton(lk, text='Freestanding / no standard libraries (advanced)', variable=self.v['freestanding'])
        b.pack(anchor='w', pady=8)
        self.inputs.append(b)
        ttk.Label(
            lk,
            text='No unresolved symbols allowed. A failed link preserves any previous binary and writes link_status.json. '
            'Successful linking is not a device boot test. The linker is optional and is not a replacement for a project-specific build pipeline.',
            wraplength=900,
            style='Sub.TLabel',
        ).pack(anchor='w')

        footer = ttk.Frame(outer)
        footer.pack(fill='x', pady=(12, 0))
        self.start_btn = ttk.Button(footer, text='Start / resume build', style='Accent.TButton', command=self.start)
        self.start_btn.pack(side='right')
        self.stop_btn = ttk.Button(footer, text='Stop after current files', command=self.graceful, state='disabled')
        self.stop_btn.pack(side='right', padx=8)
        self.cancel_btn = ttk.Button(footer, text='Cancel active jobs…', command=self.cancel_active, state='disabled')
        self.cancel_btn.pack(side='right')
        ttk.Label(footer, textvariable=self.status, wraplength=480).pack(side='left', fill='x', expand=True)

    def _save_state(self, profile_name):
        try:
            atomic_json(self.state_path, {'last_profile': profile_name})
        except OSError:
            pass

    def get(self):
        return dict(
            name=self.profile.get(),
            source=self.v['source'].get().strip(),
            ndk=self.v['ndk'].get().strip(),
            output=self.v['output'].get().strip(),
            includes=[x.strip() for x in self.includes.get('1.0', 'end').splitlines() if x.strip()],
            defines=[x.strip() for x in self.defines.get('1.0', 'end').splitlines() if x.strip()],
            gos=self.mode.get().startswith('Legacy'),
            arch='x86_64' if 'x86-64' in self.mode.get() else 'aarch64',
            recursive=self.v['recursive'].get(),
            overrides=parse_overrides(self.overrides.get('1.0', 'end')),
            slow_seconds=self.v['slow_seconds'].get(),
            link=self.v['link'].get(),
            link_name=self.v['link_name'].get(),
            freestanding=self.v['freestanding'].get(),
            link_libraries=[x.strip() for x in self.link_libs.get('1.0', 'end').splitlines() if x.strip()],
            link_system_libs=[x.strip() for x in self.link_system.get('1.0', 'end').splitlines() if x.strip()],
            **{k: self.v[k].get() for k in ('workers', 'reserve', 'headroom', 'api', 'opt')},
        )

    def load(self, c):
        if self.proc or self.busy:
            return
        self.profile.set(c['name'])
        self._save_state(c['name'])
        self.mode.set(
            'Legacy fixed-count profile'
            if c.get('gos')
            else 'Generic Android x86-64 C/C++'
            if c.get('arch') == 'x86_64'
            else 'Generic Android ARM64 C++'
        )
        fallback = defaults()
        for k in self.v:
            self.v[k].set(c.get(k, fallback.get(k, '')))
        self.overrides.delete('1.0', 'end')
        self.overrides.insert('1.0', format_overrides(c.get('overrides', {})))
        for w, k in [(self.link_libs, 'link_libraries'), (self.link_system, 'link_system_libs')]:
            w.delete('1.0', 'end')
            w.insert('1.0', '\n'.join(c.get(k, [])))
        for w, k in [(self.includes, 'includes'), (self.defines, 'defines')]:
            w.delete('1.0', 'end')
            w.insert('1.0', '\n'.join(c.get(k, [])))
        self.scan_output = None
        self.recover_btn.configure(state='disabled')
        self.status.set('Profile loaded. Existing output files are untouched.')

    def new(self):
        c = defaults()
        c.update(name='New project', source='', output='', includes=[], gos=False, recursive=True)
        self.load(c)

    def save(self):
        from tkinter.simpledialog import askstring

        if self.proc or self.busy:
            return
        name = askstring('Save profile', 'Profile name:', initialvalue=self.profile.get(), parent=self.root)
        if not name:
            return
        if name == defaults()['name']:
            messagebox.showwarning('Preset', 'Choose a different name for your custom profile.')
            return
        try:
            c = self.get()
        except ValueError as e:
            messagebox.showerror('Invalid target', str(e))
            return
        c['name'] = name
        self.profiles[name] = c
        atomic_json(self.profiles_path, self.profiles)
        self.combo.configure(values=sorted(self.profiles))
        self.profile.set(name)
        self._save_state(name)
        self.status.set('Profile saved locally.')

    def delete_profile(self):
        if self.proc or self.busy:
            return
        name = self.profile.get()
        if name == defaults()['name']:
            messagebox.showinfo('Preset', 'The built-in default template cannot be deleted.')
            return
        if name not in self.profiles:
            return
        if not messagebox.askyesno('Delete profile', f'Remove saved profile "{name}"?\nOutput files are never deleted.'):
            return
        self.profiles.pop(name, None)
        atomic_json(self.profiles_path, self.profiles)
        self.combo.configure(values=sorted(self.profiles))
        self.load(self.profiles[defaults()['name']])
        self.status.set(f'Profile "{name}" removed.')

    def import_profile(self):
        if self.proc or self.busy:
            return
        path = filedialog.askopenfilename(filetypes=[('JSON profile', '*.json')])
        if not path:
            return
        try:
            d = json.loads(Path(path).read_text(encoding='utf-8'))
            if not isinstance(d, dict) or not all(k in d for k in ('source', 'output', 'ndk')):
                raise ValueError('Select a single saved profile or job JSON.')
            c = defaults()
            c.update({k: v for k, v in d.items() if k in c})
            c['name'] = str(d.get('name', 'Imported profile'))
            self.load(c)
        except (OSError, ValueError, TypeError) as e:
            messagebox.showerror('Import blocked', str(e))

    def change_budget(self, value):
        pct = max(0, min(100, round(float(value))))
        self.budget.set(pct)
        name = {0: 'Pause', 25: 'Eco', 50: 'Balanced', 75: 'Performance', 100: 'Maximum'}.get(pct, 'Custom')
        self.budget_text.set(f'{pct}% · {name}')
        if self.controlfile and self.proc:
            try:
                atomic_json(self.controlfile, {'percent': pct})
            except OSError as e:
                self.append('Resource update failed: ' + str(e), tag='WARNING')

    def browse(self, k):
        p = filedialog.askdirectory(initialdir=self.v[k].get() or str(Path.home()))
        if p:
            self.v[k].set(p)

    def add_include(self):
        p = filedialog.askdirectory()
        if p:
            self.includes.insert('end-1c', ('\n' if self.includes.get('1.0', 'end').strip() else '') + p)

    def append(self, s, tag=None):
        self.logs.configure(state='normal')
        if tag:
            self.logs.insert('end', s.rstrip() + '\n', tag)
        else:
            self.logs.insert('end', s.rstrip() + '\n')
        self.logs.see('end')
        self.logs.configure(state='disabled')

    def clear_log(self):
        self.logs.configure(state='normal')
        self.logs.delete('1.0', 'end')
        self.logs.configure(state='disabled')

    def set_busy(self, b):
        self.busy = b
        self.start_btn.configure(state='disabled' if b or self.proc else 'normal')
        self.freeze_inputs(bool(b or self.proc))

    def freeze_inputs(self, locked):
        for w in self.inputs + self.profile_controls:
            if isinstance(w, ttk.Combobox):
                w.configure(state='disabled' if locked else 'readonly')
            else:
                w.configure(state='disabled' if locked else 'normal')

    def scan(self):
        if self.busy:
            return
        raw = self.v['output'].get().strip()
        if not raw:
            messagebox.showerror('Output', 'Choose the output folder first.')
            return
        out = Path(raw).resolve()
        self.set_busy(True)
        self.recover_btn.configure(state='disabled')
        self.recovery_status.set('Inspecting Windows processes…')
        self.tabs.select(self.recovery_tab)

        def task():
            try:
                self.q.put(('scan', (out, *inventory(out))))
            except Exception as e:
                self.q.put(('recovery_error', str(e)))

        threading.Thread(target=task, daemon=True).start()

    def do_recover(self):
        if self.busy or self.proc:
            return
        out = Path(self.v['output'].get()).resolve()
        if out != self.scan_output:
            messagebox.showerror('Scan again', 'Output changed since inspection.')
            return
        if self.unknown or any(p['role'].startswith('Unrecognized') for p in self.matches):
            messagebox.showerror(
                'Recovery blocked', 'Process identification is incomplete. Stop the old build manually; do not delete the lock.'
            )
            return
        text = (
            f'Force-stop {len(self.matches)} exact matching process(es) and archive the stale lock only after they are gone?\n\n{out}\n\n'
            'In-flight files may need retrying. Completed objects are preserved.'
            if self.matches
            else f'No matching process was found. Recheck and archive the stale lock in:\n{out}\n\nDo not start the old launcher during recovery.'
        )
        if not messagebox.askyesno('Confirm targeted recovery', text, icon='warning'):
            return
        approved = list(self.matches)
        self.set_busy(True)
        self.recover_btn.configure(state='disabled')
        self.recovery_status.set('Rechecking process identities…')

        def task():
            try:
                self.q.put(('recovered', recover(out, approved)))
            except Exception as e:
                self.q.put(('recovery_error', str(e)))

        threading.Thread(target=task, daemon=True).start()

    def start(self):
        if self.proc or self.busy:
            return
        try:
            c = self.get()
        except ValueError as e:
            messagebox.showerror('Invalid target', str(e))
            return
        if not all(c[k] for k in ('source', 'ndk', 'output')):
            messagebox.showerror('Missing paths', 'Choose source, NDK and output folders.')
            return
        out = Path(c['output']).resolve()
        c['output'] = str(out)
        if (out / '.compile.lock').exists():
            self.status.set('Lock found. Inspect / recover before starting.')
            self.scan()
            return
        # Check for orphan compiler processes even if lock is missing. Fail closed on Windows.
        self.set_busy(True)
        self.status.set('Checking output and existing compiler processes…')

        def preflight():
            try:
                if os.name == 'nt':
                    m, u = inventory(out)
                    if m or u:
                        self.q.put(('scan', (out, m, u)))
                        return
                from engine import plan

                plan(c)
                self.q.put(('launch', c))
            except Exception as e:
                self.q.put(('recovery_error', str(e)))

        threading.Thread(target=preflight, daemon=True).start()

    def launch(self, c):
        self.set_busy(False)
        jobs = HOME / 'jobs'
        jobs.mkdir(exist_ok=True)
        ident = uuid.uuid4().hex
        job = jobs / (ident + '.json')
        self.stopfile = jobs / (ident + '.stop')
        self.cancelfile = jobs / (ident + '.cancel')
        c['stopfile'] = str(self.stopfile)
        c['cancelfile'] = str(self.cancelfile)
        self.controlfile = jobs / (ident + '.budget.json')
        c['controlfile'] = str(self.controlfile)
        c['budget'] = self.budget.get()
        atomic_json(self.controlfile, {'percent': self.budget.get()})
        atomic_json(job, c)
        self.log_path = jobs / (ident + '.log')
        self.progress.set(0)
        self.summary.set('Checking source and cache…')
        self.tabs.select(self.run_tab)
        self.append('--- New build / resume ---', tag='PHASE')
        self.status.set('Build running. Stop safely to drain current files.')
        exe = sys.executable
        if os.name == 'nt' and Path(exe).name.lower() == 'pythonw.exe' and Path(exe).with_name('python.exe').exists():
            exe = str(Path(exe).with_name('python.exe'))
        args = [exe, '--engine'] if getattr(sys, 'frozen', False) else [exe, '-u', str(BASE / 'engine.py')]
        try:
            self.proc = subprocess.Popen(
                [*args, '--job', str(job), '--out', c['output']],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace',
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            )
        except Exception as e:
            self.status.set(str(e))
            return
        self.active_output = c['output']
        self.freeze_inputs(True)
        self.start_btn.configure(state='disabled')
        self.stop_btn.configure(state='normal')
        self.cancel_btn.configure(state='normal')
        self.recover_btn.configure(state='disabled')
        proc = self.proc

        def read():
            with self.log_path.open('w', encoding='utf-8') as log:
                for line in proc.stdout:
                    log.write(line)
                    log.flush()
                    try:
                        self.q.put(('event', json.loads(line)))
                    except ValueError:
                        self.q.put(('line', line))
            self.q.put(('exit', proc.wait()))

        threading.Thread(target=read, daemon=True).start()

    def analyze_log(self):
        if self.proc or self.busy:
            return
        path = filedialog.askopenfilename(
            title='Select BuildForge JSONL session log', filetypes=[('Logs', '*.log *.jsonl *.txt'), ('All files', '*')]
        )
        if not path:
            return
        try:
            from diagnostics import analyze, expected_from_folder, target_lines

            source = self.v['source'].get().strip()
            expected = (
                expected_from_folder(source, self.v['recursive'].get() and not self.mode.get().startswith('Legacy'))
                if source and Path(source).is_dir()
                else None
            )
            with open(path, encoding='utf-8', errors='replace') as f:
                report = analyze(f, int(self.v['slow_seconds'].get()), self.v['opt'].get(), expected)
            targets = target_lines(report)
            if not targets:
                messagebox.showinfo(
                    'No candidates',
                    'No sufficiently observed slow units were identified. For older logs, select the exact original source folder first.',
                )
                return
            if messagebox.askyesno(
                'Review targeted retry',
                targets + '\n\nMerge these suggestions into the existing overrides? Only run after the previous build has stopped.',
            ):
                current = parse_overrides(self.overrides.get('1.0', 'end'))
                current.update(parse_overrides(targets))
                self.overrides.delete('1.0', 'end')
                self.overrides.insert('1.0', format_overrides(current))
        except Exception as e:
            messagebox.showerror('Log analysis failed', str(e))

    def find_missing(self):
        if self.proc or self.busy:
            return
        try:
            from engine import find_missing_units

            c = self.get()
            missing = find_missing_units(c)
        except Exception as e:
            messagebox.showerror('Check failed', str(e))
            return
        if not missing:
            messagebox.showinfo('Complete', 'All expected source units already have valid objects and stamps.')
            return
        preview = '\n'.join(missing[:20]) + (f'\n… and {len(missing) - 20} more' if len(missing) > 20 else '')
        opt = 'same' if str(c.get('opt', '1')) == '0' else '0'
        if messagebox.askyesno(
            'Missing / failed units found',
            f'Found {len(missing)} unit(s) without valid objects:\n\n{preview}\n\n'
            f'Add them to overrides with optimization={opt} and timing enabled?',
        ):
            current = parse_overrides(self.overrides.get('1.0', 'end'))
            for name in missing:
                current[name] = {'opt': None if opt == 'same' else opt, 'timing': True}
            self.overrides.delete('1.0', 'end')
            self.overrides.insert('1.0', format_overrides(current))

    def copy_active(self):
        selected = self.live_units.selection()
        if not selected:
            messagebox.showinfo('Target files', 'Select one or more active rows first.')
            return
        text = '\n'.join(str(self.live_units.item(i, 'values')[0]) + ' | 0' for i in selected)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status.set('Target lines copied. Paste in Target & diagnose after this build stops.')

    def graceful(self):
        if self.proc and self.stopfile:
            self.stopfile.touch()
            self.status.set('Stopping safely: finishing active compiler jobs, no new files queued.')
            self.stop_btn.configure(state='disabled')

    def cancel_active(self):
        if not (self.proc and self.cancelfile):
            return
        if not messagebox.askyesno(
            'Cancel active compiler jobs',
            'Terminate active compiler processes owned by this build session?\n\n'
            'Completed cached objects are preserved. Interrupted units will need to be resumed.',
            icon='warning',
        ):
            return
        self.cancelfile.touch()
        self.status.set('Cancelling active owned compiler processes…')
        self.stop_btn.configure(state='disabled')
        self.cancel_btn.configure(state='disabled')

    def open_output(self):
        p = Path(self.active_output or self.v['output'].get())
        if p.is_dir():
            self.open_path(p)
        else:
            messagebox.showinfo('Output', 'Output directory does not exist yet.')

    def open_log(self):
        if self.log_path and self.log_path.exists():
            self.open_path(self.log_path)

    def open_path(self, p):
        if os.name == 'nt':
            os.startfile(str(p))
        else:
            subprocess.Popen(['xdg-open', str(p)])

    def monitor(self):
        try:
            now = cpu_times()
            cpu = '—'
            if now and self.last_cpu and now[1] > self.last_cpu[1]:
                cpu = f'{max(0, min(100, 100 * (1 - (now[0] - self.last_cpu[0]) / (now[1] - self.last_cpu[1])))):.0f}%'
            self.last_cpu = now
            free = available_ram() / GIB
            raw = self.active_output or self.v['output'].get()
            disk = ''
            p = Path(raw) if raw else None
            if p and p.is_dir():
                disk = f' | Disk free {shutil.disk_usage(p).free / GIB:.0f} GiB'
            self.resource.set(
                f'Local CPU {cpu} | Available RAM {free:.1f} GiB | '
                f'Cores {self.hardware_info.get("physical") or "?"} physical / {self.hardware_info["logical"]} logical'
                + disk
            )
        except Exception:
            pass
        self.root.after(1500, self.monitor)

    def tick(self):
        # Yield to Tk even when the engine produces events faster than we render.
        try:
            for _ in range(100):
                kind, v = self.q.get_nowait()
                if kind == 'scan':
                    self.set_busy(False)
                    self.scan_output, self.matches, self.unknown = v
                    self.tabs.select(self.recovery_tab)
                    self.tree.delete(*self.tree.get_children())
                    for p in self.matches:
                        self.tree.insert('', 'end', values=(p['pid'], p['role'], p['name']))
                    locked = (self.scan_output / '.compile.lock').exists()
                    self.recovery_status.set(
                        f'{len(self.matches)} matching process(es) • Lock: {"present" if locked else "absent"} • '
                        f'{len(self.unknown)} inaccessible command line(s).'
                    )
                    self.recover_btn.configure(
                        state='normal' if not self.proc and (locked or self.matches) and not self.unknown else 'disabled'
                    )
                    self.status.set('Inspection complete. Review before stopping or recovering.')
                elif kind in ('recovery_error', 'recovered'):
                    self.set_busy(False)
                    self.recovery_status.set(v)
                    self.status.set(v)
                    self.recover_btn.configure(state='disabled')
                    self.scan_output = None
                    if kind == 'recovery_error':
                        messagebox.showerror('Operation blocked', v)
                    else:
                        messagebox.showinfo('Recovery complete', v)
                elif kind == 'launch':
                    self.launch(v)
                elif kind == 'line':
                    self.append(v)
                elif kind == 'event':
                    ev = v.get('event')
                    if ev == 'unit':
                        st = v['state'].upper()
                        sec = f' ({v["seconds"]:.1f}s)' if isinstance(v.get('seconds'), (int, float)) else ''
                        self.append(f'{st:8} {v["name"]}{sec}', tag=st if st in ('COMPILED', 'CACHED', 'FAILED') else None)
                        if v.get('log_tail'):
                            self.append('  ' + v['log_tail'].replace('\n', '\n  '), tag='FAILED')
                    elif ev == 'progress':
                        self.progress.set(100 * v['done'] / max(1, v['total']))
                        self.summary.set(
                            f'{v["done"]}/{v["total"]} processed  |  {max(0, v["total"] - v["done"])} remaining  |  {v["compiled"]} new  |  '
                            f'{v["cached"]} cached  |  {v["failed"]} failed  |  {v["active"]} active'
                        )
                        self.effective.set(
                            f'Target {v.get("target", "—")} workers · {v.get("priority", "scheduler only")} · '
                            f'RAM reserve {v.get("reserve", "—")} GiB'
                        )
                        selected = {str(self.live_units.item(i, 'values')[0]) for i in self.live_units.selection()}
                        self.live_units.delete(*self.live_units.get_children())
                        for unit in v.get('active_units', []):
                            item = self.live_units.insert(
                                '',
                                'end',
                                values=(
                                    unit['name'],
                                    unit['pid'],
                                    unit['seconds'],
                                    unit.get('cpu_seconds') if unit.get('cpu_seconds') is not None else '—',
                                    unit.get('rss_mib') if unit.get('rss_mib') is not None else '—',
                                ),
                            )
                            if unit['name'] in selected:
                                self.live_units.selection_add(item)
                        if v.get('cancelled'):
                            self.status.set('Cancelling active compiler jobs…')
                        elif v.get('stopping'):
                            self.status.set('Draining active compiler jobs…')
                        elif v.get('budget') == 0:
                            self.status.set('Paused or draining; raise the live slider to resume.')
                        else:
                            self.status.set('Build active. Live budget can be changed safely.')
                    elif ev in ('phase', 'error', 'warning'):
                        tag = 'PHASE' if ev == 'phase' else 'WARNING' if ev == 'warning' else 'FAILED'
                        self.append(v['message'], tag=tag)
                        self.status.set(v['message'])
                    elif ev == 'finished':
                        self.append(json.dumps(v, indent=2), tag='PHASE' if v['all_translated_objects_ready'] else 'WARNING')
                        self.status.set(
                            (
                                'Shared library linked; device test still required.'
                                if v.get('android_link_verified')
                                else 'Objects ready; requested link failed, inspect link.log.'
                                if v.get('link_requested')
                                else 'All translated objects ready. Linking is separate.'
                            )
                            if v['all_translated_objects_ready']
                            else 'Incomplete build; resume to retry remaining files.'
                        )
                elif kind == 'exit':
                    self.append(f'Engine exit code: {v}')
                    self.proc = None
                    self.active_output = None
                    self.controlfile = None
                    self.cancelfile = None
                    self.live_units.delete(*self.live_units.get_children())
                    self.freeze_inputs(False)
                    self.stop_btn.configure(state='disabled')
                    self.cancel_btn.configure(state='disabled')
                    self.start_btn.configure(state='normal')
                    if self.closing:
                        self.root.destroy()
                        return
        except queue.Empty:
            pass
        self.root.after(150 if self.q.empty() else 1, self.tick)

    def on_close(self):
        if self.busy:
            messagebox.showinfo('Operation in progress', 'Finish the current inspection first.')
            return
        if self.proc:
            if messagebox.askyesno(
                'Build running',
                'Stop scheduling new files and close after current jobs finish?\nChoose No to keep the app open.',
            ):
                self.closing = True
                self.graceful()
            return
        self.root.destroy()


if __name__ == '__main__':
    if '--engine' in sys.argv:
        sys.argv.remove('--engine')
        from engine import main

        raise SystemExit(main())
    root = tk.Tk()
    App(root)
    root.mainloop()
