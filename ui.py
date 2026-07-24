"""Week 3 tkinter UI for GGMod.

Full trainer interface: game selector, mod list, per-mod hotkey capture,
add-mod form, preview/apply/disable panel, and a docked log.

This module is UI-only. It calls into the already-tested TrainerEngine and
HotkeyManager and never touches their internals except the documented
read-only helpers (engine.is_attached / engine.is_mod_active) and, for the
capture slot display, the same read-only pattern the test scripts use.
"""

import glob
import json
import os
import queue
import re
import sys
import threading

import engine  # for the capstone-backed steal exceptions (Insufficient/Mid)
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, simpledialog, ttk

# ---------------------------------------------------------------------------
# Theme: "Vault" dark — slate surfaces with a blue brand accent, adapted from
# the Password Manager (Tailwind slate + brand-blue) design language.
# ---------------------------------------------------------------------------
BG = "#0f172a"          # slate-900  — main window / panels
BG2 = "#1e293b"         # slate-800  — raised: headings bar, hover, badges
BG3 = "#020617"         # slate-950  — deepest: log, inputs
FG = "#e2e8f0"          # slate-200  — primary text
MUTED = "#94a3b8"       # slate-400  — muted / secondary text
ACCENT = "#327dff"      # brand-500  — accent (blue): headings, focus, hover
ACCENT2 = "#1b5cf5"     # brand-600  — filled primary button base
ACCENT_SOFT = "#1d2b53" # brand tint on dark — selected/active surface
GREEN = "#22c55e"       # green-500  — success / active
RED = "#f43f5e"         # rose-500   — danger / error
AMBER = "#f59e0b"       # amber-500  — warning / multiple-match
BORDER = "#334155"      # slate-700  — borders
WHITE = "#ffffff"

# Font: Vault uses Inter, falling back to Segoe UI. We resolve the best
# available family once the Tk root exists (see _resolve_font).
FONT = "Segoe UI"       # sans-serif (reassigned to "Inter" if installed)
MONO = "Consolas"       # monospace for hex dumps / logs

# Resolve the games/ folder next to the executable (frozen .exe) or this
# source file, so configs load/save correctly regardless of the working
# directory the app was launched from.
if getattr(sys, "frozen", False):
    _APP_DIR = os.path.dirname(sys.executable)
else:
    _APP_DIR = os.path.dirname(os.path.abspath(__file__))
GAMES_DIR = os.path.join(_APP_DIR, "games")

GRIP = "⋮"   # ⋮ three-dot drag handle shown before each mod name

TEMPLATES = ["hard_freeze", "pointer_capture"]
FREEZE_MODES = ["value", "nop"]
POLL_MODES = ["never_decrease", "clamp_min", "set_once", "hard_set"]
REGISTERS = [
    "eax", "ebx", "ecx", "edx", "esi", "edi",
    "rax", "rbx", "rcx", "rdx", "rsi", "rdi",
]

# AOB tokens: pairs of hex digits or a "??" / "?" wildcard, space-separated.
_AOB_TOKEN = re.compile(r"^([0-9A-Fa-f]{2}|\?\?|\?)$")


class GGModUI:
    def __init__(self, engine, hotkeys):
        self.engine = engine
        self.hotkeys = hotkeys

        self.config_path = None       # path to the loaded JSON
        self.config_raw = {}          # full parsed config (so we can re-save)
        self.selected_mod = None      # currently selected mod dict
        self.preview_ready_for = None # name of mod whose last preview was ready

        self._log_queue = queue.Queue()
        # Separate queue for the verbose per-keydown hotkey diagnostics, so they
        # don't flood the main log; drained into a collapsible panel.
        self._key_log_queue = queue.Queue()
        # mod name -> hotkey string currently registered as a global hotkey.
        self._bound = {}
        # Note-column tooltip state.
        self._tooltip = None
        self._tip_key = None
        # Edit mode: index in engine.mods being edited (None = Add mode).
        self._editing_index = None
        self._editing_orig_name = None

        self.root = tk.Tk()
        self.root.title("GGMod")
        self._set_window_icon()
        self.root.configure(bg=BG)
        self.root.geometry("1040x720")
        self.root.minsize(900, 600)

        self._resolve_font()
        self._setup_style()
        self._build_topbar()
        self._build_main()
        self._build_log()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh_game_list()
        self._drain_log()             # start log pump
        self._refresh_form_fields()   # set initial add-form visibility

    # ==================================================================
    # Styling
    # ==================================================================
    def _resolve_font(self):
        """Use Inter if installed (Vault's font), else fall back to Segoe UI."""
        global FONT
        try:
            if "Inter" in set(tkfont.families(self.root)):
                FONT = "Inter"
        except tk.TclError:
            pass

    def _setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(
            "Treeview", background=BG3, foreground=FG, fieldbackground=BG3,
            bordercolor=BORDER, rowheight=26, font=(FONT, 9),
        )
        style.configure(
            "Treeview.Heading", background=BG2, foreground=MUTED,
            font=(FONT, 9, "bold"), relief="flat", padding=(6, 4),
        )
        style.map(
            "Treeview",
            background=[("selected", ACCENT2)],
            foreground=[("selected", WHITE)],
        )
        style.map("Treeview.Heading", background=[("active", BG2)])
        style.configure(
            "TCombobox", fieldbackground=BG3, background=BG2, foreground=FG,
            arrowcolor=ACCENT, bordercolor=BORDER, padding=3,
        )
        style.map("TCombobox", fieldbackground=[("readonly", BG3)],
                  foreground=[("readonly", FG)])
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=BG, foreground=MUTED,
                        padding=(14, 6), font=(FONT, 9, "bold"))
        style.map("TNotebook.Tab",
                  background=[("selected", BG2)],
                  foreground=[("selected", ACCENT)])
        # Dropdown list colours (option DB affects the popdown Listbox).
        self.root.option_add("*TCombobox*Listbox.background", BG3)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT2)
        self.root.option_add("*TCombobox*Listbox.selectForeground", WHITE)

    # ---- small widget factories -------------------------------------
    def _label(self, parent, text, fg=FG, font=None, **kw):
        return tk.Label(parent, text=text, bg=kw.pop("bg", BG), fg=fg,
                        font=font or (FONT, 9), **kw)

    def _button(self, parent, text, command, **kw):
        # Filled primary button (Vault brand-blue), matching the app's dominant
        # call-to-action style. Flat, rounded look approximated with padding.
        # bg / activebackground are overridable (e.g. a red Delete button).
        bg = kw.pop("bg", ACCENT2)
        active = kw.pop("activebackground", ACCENT)
        return tk.Button(
            parent, text=text, command=command,
            bg=bg, fg=WHITE, activebackground=active, activeforeground=WHITE,
            relief=tk.FLAT, bd=0, padx=12, pady=5,
            font=(FONT, 9, "bold"), cursor="hand2",
            highlightthickness=0, disabledforeground="#64748b",  # slate-500
            **kw
        )

    def _button_ghost(self, parent, text, command, **kw):
        # Secondary/ghost button: transparent with slate text, subtle hover.
        return tk.Button(
            parent, text=text, command=command,
            bg=BG2, fg=FG, activebackground=BORDER, activeforeground=FG,
            relief=tk.FLAT, bd=0, padx=12, pady=5,
            font=(FONT, 9, "bold"), cursor="hand2",
            highlightthickness=0, disabledforeground="#64748b",
            **kw
        )

    def _entry(self, parent, textvariable=None, width=24):
        return tk.Entry(
            parent, textvariable=textvariable, width=width,
            bg=BG3, fg=FG, insertbackground=ACCENT, relief=tk.FLAT, bd=0,
            highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT,
        )

    # ==================================================================
    # Top bar: game selector, attach/detach, status
    # ==================================================================
    def _set_window_icon(self):
        """Replace the default Tk feather in the title bar and taskbar with the
        GGMod icon. Looks for GGMod.ico next to the source/exe, or in the
        PyInstaller bundle (_MEIPASS) when frozen."""
        base = getattr(sys, "_MEIPASS", _APP_DIR)
        ico = os.path.join(base, "GGMod.ico")
        try:
            if os.path.exists(ico):
                self.root.iconbitmap(default=ico)
        except tk.TclError:
            pass

    def _build_topbar(self):
        bar = tk.Frame(self.root, bg=BG)
        bar.pack(fill=tk.X, padx=16, pady=(14, 8))

        # Brand wordmark.
        self._label(bar, "GGMod", fg=FG, font=(FONT, 16, "bold")).pack(side=tk.LEFT)

        self._label(bar, "Game", fg=MUTED, font=(FONT, 9, "bold")).pack(side=tk.LEFT, padx=(24, 6))
        self.game_var = tk.StringVar()
        self.game_combo = ttk.Combobox(
            bar, textvariable=self.game_var, state="readonly", width=24
        )
        self.game_combo.pack(side=tk.LEFT)
        self.game_combo.bind("<<ComboboxSelected>>", self.on_game_selected)

        self._button_ghost(bar, "Refresh", self.refresh_game_list).pack(side=tk.LEFT, padx=6)
        self._button_ghost(bar, "Browse", self.on_browse_config).pack(side=tk.LEFT, padx=(0, 4))
        self._button(bar, "New Game", self.on_new_game).pack(side=tk.LEFT, padx=(0, 4))

        self._button(bar, "Attach", self.on_attach).pack(side=tk.LEFT, padx=(16, 4))
        self.detach_btn = self._button_ghost(bar, "Detach", self.on_detach)
        self.detach_btn.pack(side=tk.LEFT, padx=4)

        self.status_var = tk.StringVar(value="● Not attached")
        self.status_label = self._label(
            bar, "", fg=RED, font=(FONT, 10, "bold"),
            textvariable=self.status_var
        )
        self.status_label.pack(side=tk.RIGHT)

        # Thin divider under the top bar (Vault's border-b).
        tk.Frame(self.root, bg=BORDER, height=1).pack(fill=tk.X)

    # ==================================================================
    # Main area: mod list (left) + notebook (right)
    # ==================================================================
    def _build_main(self):
        main = tk.Frame(self.root, bg=BG)
        main.pack(fill=tk.BOTH, expand=True, padx=12, pady=6)

        # ---- Left: mod list + per-mod action buttons ----
        left = tk.Frame(main, bg=BG)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._label(left, "Mods", fg=ACCENT, font=(FONT, 11, "bold")).pack(anchor="w")

        cols = ("template", "hotkey", "enabled", "status", "note")
        self.tree = ttk.Treeview(left, columns=cols, show="tree headings", height=14)
        self.tree.heading("#0", text="Name")
        self.tree.column("#0", width=170, anchor="w")
        self.tree.heading("template", text="Template")
        self.tree.column("template", width=104, anchor="w")
        self.tree.heading("hotkey", text="Hotkey")
        self.tree.column("hotkey", width=66, anchor="center")
        self.tree.heading("enabled", text="On")
        self.tree.column("enabled", width=36, anchor="center")
        self.tree.heading("status", text="Status")
        self.tree.column("status", width=86, anchor="center")
        # "note" column: shows "!" for mods with a non-empty notes field.
        self.tree.heading("note", text="")
        self.tree.column("note", width=26, anchor="center")
        self.tree.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        self.tree.bind("<<TreeviewSelect>>", self.on_mod_select)
        self.tree.bind("<Button-1>", self.on_tree_click)
        self.tree.bind("<Motion>", self._on_tree_motion)
        self.tree.bind("<Leave>", lambda e: self._hide_tip())
        # Drag a row up/down to reorder mods (persists to config on release).
        self.tree.bind("<ButtonPress-1>", self._on_drag_start, add="+")
        self.tree.bind("<B1-Motion>", self._on_drag_motion)
        self.tree.bind("<ButtonRelease-1>", self._on_drag_end)
        self._drag_index = None
        self._drag_moved = False

        # colour tags for the status column
        self.tree.tag_configure("active", foreground=GREEN)
        self.tree.tag_configure("error", foreground=RED)
        self.tree.tag_configure("idle", foreground=MUTED)

        btns = tk.Frame(left, bg=BG)
        btns.pack(fill=tk.X, pady=6)
        self.hotkey_btn = self._button(btns, "Set Hotkey", self.on_set_hotkey)
        self.hotkey_btn.pack(side=tk.LEFT, padx=2)
        self.preview_btn = self._button(btns, "Preview", self.on_preview)
        self.preview_btn.pack(side=tk.LEFT, padx=2)
        # Apply / Disable share a slot; text+command swap with mod state.
        self.action_btn = self._button(btns, "Apply", self.on_apply)
        self.action_btn.pack(side=tk.LEFT, padx=2)
        self._set_buttons_state(disabled=True)

        # ---- Live value editor (hard_set / clamp_min, only while active) ----
        # Hidden by default; shown by update_action_buttons() when applicable.
        self.value_frame = tk.Frame(left, bg=BG)
        self._label(self.value_frame, "Live value:", fg=MUTED).pack(side=tk.LEFT)
        self.value_var = tk.StringVar()
        self.value_entry = self._entry(self.value_frame, self.value_var, width=10)
        self.value_entry.pack(side=tk.LEFT, padx=(2, 4))
        self.set_value_btn = self._button(self.value_frame, "Set", self.on_set_value)
        self.set_value_btn.pack(side=tk.LEFT)
        # Save persists the CURRENT in-memory value (mod['value']) to JSON —
        # separate from Set (live tweak). Not auto-saved on Set.
        self.save_cfg_btn = self._button(
            self.value_frame, "Save to Config", self.on_save_to_config)
        self.save_cfg_btn.pack(side=tk.LEFT, padx=(6, 0))
        self.value_err_var = tk.StringVar()
        self._label(self.value_frame, "", fg=RED,
                    textvariable=self.value_err_var).pack(side=tk.LEFT, padx=6)

        # ---- Force Set (ANY active pointer_capture mod, any poll_mode) ----
        # One-time immediate write; does NOT change ongoing poll behaviour.
        self.force_frame = tk.Frame(left, bg=BG)
        self._label(self.force_frame, "Force set:", fg=MUTED).pack(side=tk.LEFT)
        self.force_var = tk.StringVar()
        self.force_entry = self._entry(self.force_frame, self.force_var, width=10)
        self.force_entry.pack(side=tk.LEFT, padx=(2, 4))
        self.force_btn = self._button(
            self.force_frame, "Force Set Now", self.on_force_set)
        self.force_btn.pack(side=tk.LEFT)
        self.force_err_var = tk.StringVar()
        self._label(self.force_frame, "", fg=RED,
                    textvariable=self.force_err_var).pack(side=tk.LEFT, padx=6)

        # ---- Recapture (only active capture_once pointer_capture mods) ----
        # Unlocks the latched pointer so the next hook fire re-grabs it.
        self.recapture_frame = tk.Frame(left, bg=BG)
        self._label(self.recapture_frame, "Capture-once:", fg=MUTED).pack(side=tk.LEFT)
        self.recapture_btn = self._button(
            self.recapture_frame, "Recapture", self.on_recapture)
        self.recapture_btn.pack(side=tk.LEFT, padx=(2, 6))
        self._label(self.recapture_frame,
                    "unlock & grab the pointer on the next hook fire",
                    fg=MUTED, font=(FONT, 8)).pack(side=tk.LEFT)

        # ---- Right: notebook (Selected Mod / Add Mod) ----
        right = tk.Frame(main, bg=BG, width=430)
        right.pack(side=tk.RIGHT, fill=tk.BOTH, padx=(12, 0))
        right.pack_propagate(False)

        self.notebook = ttk.Notebook(right)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        self._build_details_tab()
        self._build_add_tab()

    # ---- Details / Preview tab --------------------------------------
    def _build_details_tab(self):
        tab = tk.Frame(self.notebook, bg=BG)
        self.notebook.add(tab, text="Selected Mod")

        det_header = tk.Frame(tab, bg=BG)
        det_header.pack(fill=tk.X, padx=8, pady=(8, 0))
        self._label(det_header, "Details", fg=ACCENT,
                    font=(FONT, 10, "bold")).pack(side=tk.LEFT)
        # Right-to-left: Delete (danger) | Edit | Edit Notes
        self._button(det_header, "Delete", self.on_delete_mod,
                     bg=RED, activebackground="#e11d48").pack(side=tk.RIGHT, padx=(4, 0))
        self._button(det_header, "Edit", self.on_edit_mod).pack(side=tk.RIGHT, padx=4)
        self._button(det_header, "Edit Notes", self.on_edit_notes).pack(side=tk.RIGHT, padx=4)
        self.details_text = tk.Text(
            tab, height=10, bg=BG3, fg=FG, relief=tk.FLAT, wrap=tk.WORD,
            font=(MONO, 9), state=tk.DISABLED,
        )
        self.details_text.pack(fill=tk.X, padx=8, pady=4)

        self._label(tab, "Preview result", fg=ACCENT, font=(FONT, 10, "bold")).pack(anchor="w", padx=8, pady=(8, 0))
        self.preview_status_var = tk.StringVar(value="(no preview yet)")
        self.preview_status_label = self._label(
            tab, "", fg=MUTED, font=(FONT, 11, "bold"),
            textvariable=self.preview_status_var,
        )
        self.preview_status_label.pack(anchor="w", padx=8)

        # Scan progress bar: packed only while a scan is running, then hidden.
        ttk.Style().configure(
            "GG.Horizontal.TProgressbar",
            troughcolor=BG3, background=ACCENT, bordercolor=BG3,
            lightcolor=ACCENT, darkcolor=ACCENT,
        )
        self.preview_progress_var = tk.DoubleVar(value=0)
        self.preview_progress = ttk.Progressbar(
            tab, orient="horizontal", mode="determinate", maximum=100,
            variable=self.preview_progress_var,
            style="GG.Horizontal.TProgressbar",
        )

        self.preview_text = tk.Text(
            tab, height=12, bg=BG3, fg=FG, relief=tk.FLAT, wrap=tk.WORD,
            font=(MONO, 9), state=tk.DISABLED,
        )
        self.preview_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 8))

    # ---- Add Mod tab -------------------------------------------------
    def _build_add_tab(self):
        tab = tk.Frame(self.notebook, bg=BG)
        self.notebook.add(tab, text="Add Mod")

        self.form = tk.Frame(tab, bg=BG)
        self.form.pack(fill=tk.X, padx=8, pady=8)

        self.form_vars = {}
        self.form_rows = {}   # field name -> row frame (for show/hide)
        self._row_index = 0

        def combo(var, values, on_change=False):
            def _factory(parent):
                cb = ttk.Combobox(
                    parent, textvariable=var, values=values,
                    state="readonly", width=22,
                )
                if on_change:
                    cb.bind("<<ComboboxSelected>>", lambda e: self._refresh_form_fields())
                return cb
            return _factory

        def entry(var, width=24):
            return lambda parent: self._entry(parent, var, width=width)

        # Common fields
        self.form_vars["name"] = tk.StringVar()
        self._add_form_row("name", "Name", entry(self.form_vars["name"]))

        self.form_vars["template"] = tk.StringVar(value=TEMPLATES[0])
        self._add_form_row("template", "Template",
                           combo(self.form_vars["template"], TEMPLATES, on_change=True))

        self.form_vars["aob"] = tk.StringVar()
        self._add_form_row("aob", "AOB", entry(self.form_vars["aob"], width=30))

        # hard_freeze fields
        self.form_vars["offset"] = tk.StringVar()
        self._add_form_row("offset", "Offset", entry(self.form_vars["offset"]))

        self.form_vars["freeze_mode"] = tk.StringVar(value=FREEZE_MODES[0])
        self._add_form_row("freeze_mode", "Freeze mode",
                           combo(self.form_vars["freeze_mode"], FREEZE_MODES, on_change=True))

        self.form_vars["nop_len"] = tk.StringVar()
        self._add_form_row("nop_len", "NOP length", entry(self.form_vars["nop_len"]))

        # pointer_capture: per-hook fields live in a dynamic hooks section
        # (built below); only mod-level fields are flat rows here.
        self.form_vars["capture_at_attach"] = tk.BooleanVar(value=False)
        self._add_form_row(
            "capture_at_attach", "Capture at attach",
            lambda parent: tk.Checkbutton(
                parent, variable=self.form_vars["capture_at_attach"],
                bg=BG, fg=FG, selectcolor=BG3, activebackground=BG,
                activeforeground=FG,
            ),
        )

        # capture_once: latch the pointer on the first non-zero fire, then stop
        # listening (for shared setters that fire for many unrelated objects).
        self.form_vars["capture_once"] = tk.BooleanVar(value=False)
        self._add_form_row(
            "capture_once", "Capture once (lock after first fire)",
            lambda parent: tk.Checkbutton(
                parent, variable=self.form_vars["capture_once"],
                bg=BG, fg=FG, selectcolor=BG3, activebackground=BG,
                activeforeground=FG,
            ),
        )

        self.form_vars["struct_offset"] = tk.StringVar()
        self._add_form_row("struct_offset", "Struct offset (hex)",
                           entry(self.form_vars["struct_offset"]))

        self.form_vars["poll_mode"] = tk.StringVar(value=POLL_MODES[0])
        self._add_form_row("poll_mode", "Poll mode",
                           combo(self.form_vars["poll_mode"], POLL_MODES, on_change=True))

        # value (shared: hard_freeze value-mode OR pointer_capture clamp_min)
        self.form_vars["value"] = tk.StringVar()
        self._add_form_row("value", "Value", entry(self.form_vars["value"]))

        # notes (optional, multi-line; both templates)
        self.notes_text_widget = None

        def notes_factory(parent):
            t = tk.Text(parent, width=34, height=3, bg=BG3, fg=FG,
                        insertbackground=FG, relief=tk.SOLID, bd=1, wrap=tk.WORD,
                        highlightthickness=1, highlightbackground=BORDER,
                        highlightcolor=ACCENT, font=(FONT, 9))
            self.notes_text_widget = t
            return t
        self._add_form_row("notes", "Notes (optional)", notes_factory)

        # ---- Dynamic hooks section (pointer_capture only) ----
        self.hooks_container = tk.Frame(tab, bg=BG)
        self.hooks_container.pack(fill=tk.X, padx=8, pady=(4, 0))
        header = tk.Frame(self.hooks_container, bg=BG)
        header.pack(fill=tk.X)
        self._label(header, "Hooks (each writes the shared pointer):",
                    fg=ACCENT, font=(FONT, 9, "bold")).pack(side=tk.LEFT)
        self._button(header, "+ Add Hook", self._add_hook_row).pack(side=tk.LEFT, padx=8)
        self.hooks_rows = tk.Frame(self.hooks_container, bg=BG)
        self.hooks_rows.pack(fill=tk.X, pady=2)
        self._hook_entries = []   # list of {aob, hook_offset, register, row}

        # Error + save
        self.form_error_var = tk.StringVar(value="")
        self._form_error_label = self._label(
            tab, "", fg=RED, textvariable=self.form_error_var,
            wraplength=400, justify="left")
        self._form_error_label.pack(anchor="w", padx=8)
        self._save_btn = self._button(tab, "Save Mod", self.on_save_mod)
        self._save_btn.pack(anchor="w", padx=8, pady=8)

    # ---- dynamic hook rows -------------------------------------------
    def _add_hook_row(self, prefill=None):
        prefill = prefill or {}
        row = tk.Frame(self.hooks_rows, bg=BG2, bd=1, relief=tk.SOLID)
        row.pack(fill=tk.X, pady=2)
        entry_vars = {
            "aob": tk.StringVar(value=prefill.get("aob", "")),
            "hook_offset": tk.StringVar(value=str(prefill.get("hook_offset", ""))),
            "register": tk.StringVar(value=prefill.get("capture_register", "esi")),
            "module": tk.StringVar(value=prefill.get("module", "") or ""),
        }

        line1 = tk.Frame(row, bg=BG2)
        line1.pack(fill=tk.X, padx=4, pady=(3, 0))
        self._label(line1, "AOB:", fg=MUTED, bg=BG2, width=5, anchor="w").pack(side=tk.LEFT)
        tk.Entry(line1, textvariable=entry_vars["aob"], width=34, bg=BG3, fg=FG,
                 insertbackground=FG, relief=tk.SOLID, bd=1,
                 highlightthickness=1, highlightbackground=BORDER,
                 highlightcolor=ACCENT).pack(side=tk.LEFT)

        line2 = tk.Frame(row, bg=BG2)
        line2.pack(fill=tk.X, padx=4, pady=(0, 3))
        self._label(line2, "steal:", fg=MUTED, bg=BG2, width=5, anchor="w").pack(side=tk.LEFT)
        tk.Entry(line2, textvariable=entry_vars["hook_offset"], width=6, bg=BG3, fg=FG,
                 insertbackground=FG, relief=tk.SOLID, bd=1,
                 highlightthickness=1, highlightbackground=BORDER,
                 highlightcolor=ACCENT).pack(side=tk.LEFT)
        entry = {"row": row, **entry_vars}
        # Auto-calculate the instruction-aligned steal length from the AOB via
        # capstone. Fills the steal field; the user can still override it.
        tk.Button(
            line2, text="Auto", command=lambda: self._auto_steal(entry),
            bg=ACCENT2, fg=WHITE, activebackground=ACCENT, activeforeground=WHITE,
            relief=tk.FLAT, cursor="hand2", font=(FONT, 8, "bold"), padx=6,
        ).pack(side=tk.LEFT, padx=(4, 0))
        self._label(line2, "reg:", fg=MUTED, bg=BG2).pack(side=tk.LEFT, padx=(8, 2))
        ttk.Combobox(line2, textvariable=entry_vars["register"], values=REGISTERS,
                     state="readonly", width=8).pack(side=tk.LEFT)
        # Optional per-hook module target: blank = main .exe; e.g. for Unity/
        # IL2CPP titles whose logic is in GameAssembly.dll.
        self._label(line2, "module:", fg=MUTED, bg=BG2).pack(side=tk.LEFT, padx=(8, 2))
        tk.Entry(line2, textvariable=entry_vars["module"], width=16, bg=BG3, fg=FG,
                 insertbackground=FG, relief=tk.SOLID, bd=1,
                 highlightthickness=1, highlightbackground=BORDER,
                 highlightcolor=ACCENT).pack(side=tk.LEFT)
        self._label(line2, "(blank = main exe)", fg=MUTED, bg=BG2,
                    font=(FONT, 8)).pack(side=tk.LEFT, padx=(4, 0))
        tk.Button(
            line2, text="✕", command=lambda: self._remove_hook_row(entry),
            bg=BG2, fg=RED, activebackground=RED, activeforeground=BG,
            relief=tk.FLAT, cursor="hand2", font=(FONT, 9, "bold"),
        ).pack(side=tk.RIGHT)
        self._hook_entries.append(entry)

    def _remove_hook_row(self, entry):
        entry["row"].destroy()
        if entry in self._hook_entries:
            self._hook_entries.remove(entry)

    def _clear_hook_rows(self):
        for entry in list(self._hook_entries):
            entry["row"].destroy()
        self._hook_entries = []

    def _auto_steal(self, entry):
        """Fill a hook's steal field with the capstone-computed minimum.

        Needs an attached process so the disassembler matches the game's
        bitness (x86/x64). Defaults to a rel32 (5-byte) jmp threshold.
        """
        self.form_error_var.set("")
        if not self.engine.is_attached():
            self.form_error_var.set(
                "Attach to the game first — auto-steal needs the process "
                "bitness (x86 vs x64) to disassemble correctly.")
            return
        aob_text = entry["aob"].get().strip()
        if not aob_text or not self._valid_aob(aob_text):
            self.form_error_var.set(
                "Enter a valid AOB in this hook before auto-calculating steal.")
            return
        pattern, mask = self.engine._parse_aob(aob_text)
        try:
            steal = self.engine.compute_min_steal(pattern, jmp_type="rel32")
        except engine.InsufficientBytesForJmpError as ex:
            self.form_error_var.set("Auto-steal: {}".format(ex))
            return
        except Exception as ex:                     # e.g. capstone missing
            self.form_error_var.set("Auto-steal failed: {}".format(ex))
            return
        entry["hook_offset"].set(str(steal))
        msg = "Auto-steal: {} byte(s) (instruction-aligned, rel32).".format(steal)
        # Wildcards inside the stolen region can make the decode unreliable.
        if not all(mask[:steal]):
            msg += "  WARNING: AOB has wildcards within the steal region — verify."
            self.form_error_var.set(
                "Auto-steal used {}, but this AOB has wildcards in the steal "
                "region — double-check it disassembled correctly.".format(steal))
        self.log(msg)

    def _add_form_row(self, key, label, widget_factory):
        row = tk.Frame(self.form, bg=BG)
        row.grid(row=self._row_index, column=0, sticky="w", pady=2)
        self._label(row, label + ":", fg=MUTED, width=20, anchor="w").pack(side=tk.LEFT)
        widget_factory(row).pack(side=tk.LEFT)
        self.form_rows[key] = row
        self._row_index += 1

    def _refresh_form_fields(self):
        """Show/hide add-form fields based on template + sub-options."""
        template = self.form_vars["template"].get()
        freeze_mode = self.form_vars["freeze_mode"].get()
        poll_mode = self.form_vars["poll_mode"].get()

        hard = template == "hard_freeze"
        ptr = template == "pointer_capture"

        visible = {"name", "template", "notes"}  # notes: optional, both templates
        if hard:
            visible |= {"aob", "offset", "freeze_mode"}  # single flat AOB
            if freeze_mode == "value":
                visible.add("value")
            else:
                visible.add("nop_len")
        if ptr:
            # pointer_capture uses the dynamic hooks section for AOBs, not the
            # flat 'aob' row.
            visible |= {"capture_at_attach", "capture_once", "struct_offset",
                        "poll_mode"}
            if poll_mode in ("clamp_min", "hard_set"):
                visible.add("value")

        for key, row in self.form_rows.items():
            if key in visible:
                row.grid()
            else:
                row.grid_remove()

        # Show the hooks section only for pointer_capture; seed one empty row.
        # Anchor it above the error/save widgets so pack order stays stable.
        if ptr:
            self.hooks_container.pack(fill=tk.X, padx=8, pady=(4, 0),
                                      before=self._form_error_label)
            if not self._hook_entries:
                self._add_hook_row()
        else:
            self.hooks_container.pack_forget()

    # ==================================================================
    # Log panel (docked at bottom)
    # ==================================================================
    def _build_log(self):
        frame = tk.Frame(self.root, bg=BG)
        frame.pack(fill=tk.BOTH, expand=False, padx=12, pady=(0, 12))

        self._label(frame, "Log", fg=ACCENT, font=(FONT, 10, "bold")).pack(anchor="w")

        inner = tk.Frame(frame, bg=BG)
        inner.pack(fill=tk.BOTH, expand=True)
        scrollbar = tk.Scrollbar(inner)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_widget = tk.Text(
            inner, height=8, bg=BG3, fg=FG, insertbackground=FG,
            font=(MONO, 9), wrap=tk.WORD, relief=tk.FLAT,
            yscrollcommand=scrollbar.set, state=tk.DISABLED,
        )
        self.log_widget.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=self.log_widget.yview)

        # ---- Collapsible "Key Input Log" (verbose per-keydown diagnostics) ----
        self._key_log_open = False
        khead = tk.Frame(frame, bg=BG)
        khead.pack(fill=tk.X, pady=(6, 0))
        self._key_log_toggle = tk.Button(
            khead, text="▸ Key Input Log", command=self._toggle_key_log,
            bg=BG, fg=MUTED, activebackground=BG, activeforeground=ACCENT,
            relief=tk.FLAT, cursor="hand2", font=(FONT, 9, "bold"), anchor="w",
            bd=0, padx=0,
        )
        self._key_log_toggle.pack(side=tk.LEFT)
        self._label(khead, "(hotkey scan-code matching)", fg=MUTED,
                    font=(FONT, 8)).pack(side=tk.LEFT, padx=(6, 0))
        self._button_ghost(khead, "Clear", self._clear_key_log).pack(side=tk.RIGHT)

        self._key_log_body = tk.Frame(frame, bg=BG)   # packed only when open
        kbar = tk.Scrollbar(self._key_log_body)
        kbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.key_log_widget = tk.Text(
            self._key_log_body, height=6, bg=BG3, fg=MUTED, insertbackground=FG,
            font=(MONO, 9), wrap=tk.NONE, relief=tk.FLAT,
            yscrollcommand=kbar.set, state=tk.DISABLED,
        )
        self.key_log_widget.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        kbar.config(command=self.key_log_widget.yview)

    def _toggle_key_log(self):
        self._key_log_open = not self._key_log_open
        if self._key_log_open:
            self._key_log_body.pack(fill=tk.BOTH, expand=True)
            self._key_log_toggle.config(text="▾ Key Input Log")
        else:
            self._key_log_body.pack_forget()
            self._key_log_toggle.config(text="▸ Key Input Log")

    def _clear_key_log(self):
        self.key_log_widget.config(state=tk.NORMAL)
        self.key_log_widget.delete("1.0", tk.END)
        self.key_log_widget.config(state=tk.DISABLED)

    def log(self, message):
        """Thread-safe log entry point (engine poll threads call this)."""
        self._log_queue.put(str(message))

    def key_log(self, message):
        """Thread-safe entry point for verbose per-keydown hotkey diagnostics.
        Routed to the collapsible Key Input Log, never the main log."""
        self._key_log_queue.put(str(message))

    def _drain_log(self):
        """Flush queued log messages on the main thread (Tk is not thread-safe)."""
        try:
            while True:
                message = self._log_queue.get_nowait()
                self.log_widget.config(state=tk.NORMAL)
                self.log_widget.insert(tk.END, message + "\n")
                self.log_widget.see(tk.END)
                self.log_widget.config(state=tk.DISABLED)
        except queue.Empty:
            pass
        # Drain the key-input log too, trimming so a flood can't grow unbounded.
        try:
            appended = False
            while True:
                message = self._key_log_queue.get_nowait()
                self.key_log_widget.config(state=tk.NORMAL)
                self.key_log_widget.insert(tk.END, message + "\n")
                appended = True
        except queue.Empty:
            if appended:
                # Keep only the last ~400 lines.
                line_count = int(self.key_log_widget.index("end-1c").split(".")[0])
                if line_count > 400:
                    self.key_log_widget.delete("1.0", "{}.0".format(line_count - 400))
                self.key_log_widget.see(tk.END)
                self.key_log_widget.config(state=tk.DISABLED)
        self.root.after(100, self._drain_log)

    # ==================================================================
    # Game selection / config loading
    # ==================================================================
    def refresh_game_list(self):
        paths = sorted(glob.glob(os.path.join(GAMES_DIR, "*.json")))
        names = [os.path.basename(p) for p in paths]
        self.game_combo["values"] = names
        self.log("Found {} game config(s) in {}/.".format(len(names), GAMES_DIR))

    def on_new_game(self):
        """Dialog to create a fresh empty game config in games/."""
        top = tk.Toplevel(self.root)
        top.title("New Game")
        top.configure(bg=BG)
        top.transient(self.root)
        top.grab_set()

        name_var = tk.StringVar()
        proc_var = tk.StringVar()
        err_var = tk.StringVar()

        form = tk.Frame(top, bg=BG)
        form.pack(padx=14, pady=(14, 6))
        for i, (label, var, hint) in enumerate((
            ("Game name", name_var, "Name for this config file"),
            ("Process name", proc_var, "The game's .exe process name"),
        )):
            self._label(form, label, fg=MUTED, font=(FONT, 9, "bold")).grid(
                row=i * 2, column=0, sticky="w", pady=(4, 0))
            self._entry(form, var, width=30).grid(row=i * 2 + 1, column=0, sticky="w")
            self._label(form, hint, fg=MUTED, font=(FONT, 8)).grid(
                row=i * 2 + 1, column=1, sticky="w", padx=(8, 0))

        self._label(top, "", fg=RED, textvariable=err_var,
                    wraplength=340, justify="left").pack(anchor="w", padx=14)

        def _create():
            game = name_var.get().strip()
            proc = proc_var.get().strip()
            if not game:
                err_var.set("Game name is required.")
                return
            if not proc:
                err_var.set("Process name is required.")
                return
            # Sanitize the filename: keep the entered stem, drop any path parts
            # and a trailing .json the user may have typed.
            stem = os.path.basename(game)
            if stem.lower().endswith(".json"):
                stem = stem[:-5]
            if not stem:
                err_var.set("Invalid game name.")
                return
            path = os.path.join(GAMES_DIR, stem + ".json")
            if os.path.exists(path):
                err_var.set("A config named '{}.json' already exists.".format(stem))
                return
            try:
                os.makedirs(GAMES_DIR, exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump({"process_name": proc, "mods": []}, fh,
                              indent=2, ensure_ascii=False)
            except OSError as exc:
                err_var.set("Could not create file: {}".format(exc))
                return
            self.log("Created new game config: {}".format(path.replace("\\", "/")))
            top.destroy()
            # Refresh the dropdown, select the new config, and load it so the
            # user can start adding mods immediately.
            self.refresh_game_list()
            self.game_var.set(stem + ".json")
            self._load_config(path)

        btns = tk.Frame(top, bg=BG)
        btns.pack(pady=(4, 14))
        self._button(btns, "Create", _create).pack(side=tk.LEFT, padx=4)
        self._button_ghost(btns, "Cancel", top.destroy).pack(side=tk.LEFT, padx=4)

    def on_game_selected(self, _event=None):
        name = self.game_var.get()
        if not name:
            return
        self._load_config(os.path.join(GAMES_DIR, name))

    def on_browse_config(self):
        path = filedialog.askopenfilename(
            title="Load Game Config",
            initialdir=GAMES_DIR if os.path.isdir(GAMES_DIR) else ".",
            filetypes=[("JSON config", "*.json"), ("All files", "*.*")],
        )
        if path:
            self._load_config(path)

    def _load_config(self, path):
        # If already attached, prompt to detach before switching games.
        if self.engine.is_attached():
            if not messagebox.askyesno(
                "Switch game",
                "Currently attached to '{}'. Detach before loading a new "
                "config?".format(self.engine.process_name),
            ):
                self.log("Config load cancelled (still attached).")
                return
            self.on_detach()

        # Keep the raw config so we can re-save on add/hotkey edits.
        try:
            with open(path, "r", encoding="utf-8") as fh:
                self.config_raw = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            messagebox.showerror("Load failed", "Could not read config:\n{}".format(exc))
            return

        self.engine.load_game_config(path)
        self.config_path = path
        # Share the SAME list object so appends/edits stay in sync.
        self.config_raw["mods"] = self.engine.mods
        self.refresh_mod_list()
        # Bind hotkeys for EVERY mod that has one, so a hotkey works cold
        # (before any manual Apply). Clears the previous config's bindings first.
        self._register_config_hotkeys()

    # ==================================================================
    # Attach / detach
    # ==================================================================
    def on_attach(self):
        default = self.config_raw.get("process_name", "") if self.config_raw else ""
        process_name = simpledialog.askstring(
            "Attach", "Process name:", initialvalue=default, parent=self.root
        )
        if not process_name:
            return
        if self.engine.attach(process_name):
            self.status_var.set("● Attached to {}".format(process_name))
            self.status_label.config(fg=GREEN)
        else:
            self.status_var.set("● Not attached")
            self.status_label.config(fg=RED)
        self.refresh_mod_list()

    def on_detach(self):
        if not self.engine.is_attached():
            return
        # Detach tears down all mods in the engine. Hotkey bindings are
        # config-scoped (registered at load), so we KEEP them: a press while
        # detached is safely ignored, and they work again on re-attach. They
        # are cleared only on config reload or app close.
        self.engine.detach()
        self.status_var.set("● Not attached")
        self.status_label.config(fg=RED)
        self.preview_ready_for = None
        self.refresh_mod_list()

    def update_status(self):
        if self.engine.is_attached():
            self.status_var.set("● Attached to {}".format(self.engine.process_name))
            self.status_label.config(fg=GREEN)
        else:
            self.status_var.set("● Not attached")
            self.status_label.config(fg=RED)

    # ==================================================================
    # Mod list
    # ==================================================================
    def refresh_mod_list(self):
        # Remember selection by name.
        selected_name = self.selected_mod.get("name") if self.selected_mod else None

        self.tree.delete(*self.tree.get_children())
        for idx, mod in enumerate(self.engine.mods):
            name = mod.get("name", "?")
            active = self.engine.is_mod_active(name)
            status = "active" if active else "not applied"
            enabled_mark = "☑" if active else "☐"
            tag = "active" if active else "idle"
            note_mark = "!" if (mod.get("notes") or "").strip() else ""
            self.tree.insert(
                "", "end", iid=str(idx), text=GRIP + "  " + name,
                values=(mod.get("template", ""), mod.get("hotkey", "-"),
                        enabled_mark, status, note_mark),
                tags=(tag,),
            )

        # Restore selection if possible.
        if selected_name is not None:
            for idx, mod in enumerate(self.engine.mods):
                if mod.get("name") == selected_name:
                    self.tree.selection_set(str(idx))
                    break
        self.update_action_buttons()

    def _selected_index(self):
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def on_mod_select(self, _event=None):
        idx = self._selected_index()
        if idx is None or idx >= len(self.engine.mods):
            self.selected_mod = None
            self._set_buttons_state(disabled=True)
            return
        self.selected_mod = self.engine.mods[idx]
        # New selection invalidates any prior preview gating.
        self.preview_ready_for = None
        self._reset_preview_display()
        self.show_details(self.selected_mod)
        self.update_action_buttons()

    def on_tree_click(self, event):
        """Click on 'On' toggles apply/disable; click the '!' note cell opens
        the note popup."""
        region = self.tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        col = self.tree.identify_column(event.x)
        row = self.tree.identify_row(event.y)
        if not row:
            return
        idx = int(row)
        mod = self.engine.mods[idx]
        # columns: #0=name, #1=template, #2=hotkey, #3=enabled, #4=status, #5=note
        if col == "#5":
            if (mod.get("notes") or "").strip():
                self._show_note_popup(mod)
            return
        if col != "#3":
            return
        name = mod.get("name")
        self.tree.selection_set(row)
        if self.engine.is_mod_active(name):
            self._do_disable(mod)
        else:
            # Enable path: preview first, apply only if ready.
            self._preview_then_apply(mod)

    # ---- drag-to-reorder ---------------------------------------------
    def _on_drag_start(self, event):
        """Remember which row the user grabbed, so B1-Motion can slide it."""
        self._hide_tip()
        region = self.tree.identify("region", event.x, event.y)
        row = self.tree.identify_row(event.y)
        # Start a drag from any part of a data row. The name column reports its
        # region as "tree" (not "cell"), so accept both; exclude the heading.
        if region in ("cell", "tree") and row:
            self._drag_index = int(row)
        else:
            self._drag_index = None
        self._drag_moved = False

    def _on_drag_motion(self, event):
        """Slide the grabbed mod to whatever row the cursor is now over."""
        if self._drag_index is None:
            return
        self._hide_tip()
        target_row = self.tree.identify_row(event.y)
        if not target_row:
            return
        target = int(target_row)
        src = self._drag_index
        mods = self.engine.mods
        if target == src or not (0 <= target < len(mods)):
            return
        mod = mods.pop(src)
        mods.insert(target, mod)
        self._drag_index = target
        self._drag_moved = True
        # Re-render in the new order and keep the dragged mod selected/highlighted.
        self.selected_mod = mod
        self.refresh_mod_list()
        self.tree.selection_set(str(target))

    def _on_drag_end(self, _event):
        """Persist the new order once, only if the drag actually moved a mod."""
        if self._drag_index is not None and self._drag_moved:
            self.engine.save_mod_config()
            self.log("Reordered mods — saved to config.")
        self._drag_index = None
        self._drag_moved = False

    # ---- notes indicator: hover tooltip + click popup ----------------
    def _on_tree_motion(self, event):
        """Show a tooltip when hovering the '!' note cell of a mod with notes."""
        region = self.tree.identify("region", event.x, event.y)
        col = self.tree.identify_column(event.x)
        row = self.tree.identify_row(event.y)
        note = ""
        if region == "cell" and col == "#5" and row:
            try:
                note = (self.engine.mods[int(row)].get("notes") or "").strip()
            except (ValueError, IndexError):
                note = ""
        if note:
            if self._tip_key != row:          # avoid flicker: only rebuild on change
                self._hide_tip()
                self._show_tip(note, event.x_root, event.y_root)
                self._tip_key = row
        else:
            self._hide_tip()

    def _show_tip(self, text, x, y):
        self._tooltip = tk.Toplevel(self.root)
        self._tooltip.wm_overrideredirect(True)
        self._tooltip.wm_geometry("+{}+{}".format(x + 16, y + 14))
        tk.Label(
            self._tooltip, text=text, bg=BG2, fg=FG, justify="left",
            relief=tk.SOLID, bd=1, font=(FONT, 9), wraplength=360,
            padx=8, pady=6,
        ).pack()

    def _hide_tip(self):
        if self._tooltip is not None:
            self._tooltip.destroy()
            self._tooltip = None
        self._tip_key = None

    def _show_note_popup(self, mod):
        """Read-only popup showing a mod's notes (click on the '!' indicator)."""
        self._hide_tip()
        top = tk.Toplevel(self.root)
        top.title("Notes - {}".format(mod.get("name", "")))
        top.configure(bg=BG)
        top.transient(self.root)
        top.grab_set()
        self._label(top, "Notes for '{}':".format(mod.get("name")),
                    fg=ACCENT, font=(FONT, 10, "bold")).pack(anchor="w", padx=12, pady=(12, 6))
        txt = tk.Text(top, width=52, height=8, bg=BG3, fg=FG, relief=tk.FLAT,
                      wrap=tk.WORD, font=(FONT, 9))
        txt.pack(padx=12, pady=4)
        txt.insert("1.0", mod.get("notes", ""))
        txt.config(state=tk.DISABLED)
        self._button(top, "Close", top.destroy).pack(pady=(4, 12))

    def show_details(self, mod):
        lines = []
        for key, val in mod.items():
            if key == "notes":
                continue  # rendered as its own block at the end
            if key == "hooks" and isinstance(val, list):
                lines.append("{:<20}: {} hook(s)".format("hooks", len(val)))
                for i, hook in enumerate(val):
                    lines.append("  hook[{}] reg={} steal={}".format(
                        i, hook.get("capture_register"),
                        hook.get("steal_len", hook.get("hook_offset"))))
                    lines.append("     aob: {}".format(hook.get("aob")))
                continue
            lines.append("{:<20}: {}".format(key, val))
        notes = mod.get("notes")
        if notes:
            lines.append("")
            lines.append("notes:")
            lines.append(notes)
        self.details_text.config(state=tk.NORMAL)
        self.details_text.delete("1.0", tk.END)
        self.details_text.insert(tk.END, "\n".join(lines))
        self.details_text.config(state=tk.DISABLED)

    def on_edit_notes(self):
        """Open a small dialog to edit the selected mod's notes, then persist."""
        mod = self.selected_mod
        if mod is None:
            self.log("Select a mod first to edit its notes.")
            return

        top = tk.Toplevel(self.root)
        top.title("Edit Notes - {}".format(mod.get("name", "")))
        top.configure(bg=BG)
        top.transient(self.root)
        top.grab_set()
        self._label(top, "Notes for '{}':".format(mod.get("name")),
                    fg=ACCENT, font=(FONT, 10, "bold")).pack(anchor="w", padx=10, pady=(10, 4))
        txt = tk.Text(top, width=54, height=8, bg=BG3, fg=FG, insertbackground=FG,
                      relief=tk.SOLID, bd=1, wrap=tk.WORD, font=(FONT, 9))
        txt.pack(padx=10, pady=4)
        txt.insert("1.0", mod.get("notes", ""))
        txt.focus_set()

        row = tk.Frame(top, bg=BG)
        row.pack(pady=8)

        def _save():
            self._set_mod_notes(mod, txt.get("1.0", "end"))
            if self.engine.save_mod_config(mod.get("name")):
                path = (self.engine.config_path or "").replace("\\", "/")
                self.log("Saved notes for '{}' to {}".format(mod.get("name"), path))
            else:
                self.log("Notes updated in memory (not persisted: no config loaded).")
            top.destroy()
            self.show_details(mod)

        self._button(row, "Save", _save).pack(side=tk.LEFT, padx=4)
        self._button(row, "Cancel", top.destroy).pack(side=tk.LEFT, padx=4)

    # ==================================================================
    # Edit / Delete existing mods
    # ==================================================================
    def on_edit_mod(self):
        """Load the selected mod into the (reused) Add Mod form for editing."""
        mod = self.selected_mod
        if mod is None:
            self.log("Select a mod first to edit it.")
            return
        name = mod.get("name")
        if self.engine.is_mod_active(name):
            self.log("Disable this mod before editing.")
            messagebox.showinfo("Edit mod", "Disable this mod before editing.")
            return
        self._editing_index = self.engine.mods.index(mod)
        self._editing_orig_name = name
        self._prefill_form(mod)
        self._save_btn.config(text="Save Changes")
        self.notebook.select(1)  # Add Mod tab (reused for editing)
        self.log("Editing '{}' — change fields and click Save Changes.".format(name))

    def on_delete_mod(self):
        """Delete the selected mod (disable first if active), persist, refresh."""
        mod = self.selected_mod
        if mod is None:
            self.log("Select a mod first to delete it.")
            return
        name = mod.get("name")
        if not messagebox.askyesno(
            "Delete mod",
            "Delete '{}'?\n\nThis is permanent — there is no undo.".format(name),
        ):
            return
        # If active, tear it down first (stop poll, restore bytes, free cave).
        if self.engine.is_mod_active(name):
            self.engine.toggle_mod(name, False)
        # Drop its hotkey binding, if any.
        self._unregister_mod_hotkey(name)
        # Remove from the config and persist (no name -> writes remaining mods).
        try:
            self.engine.mods.remove(mod)
        except ValueError:
            pass
        self.engine.save_mod_config()
        self.log("Deleted '{}'.".format(name))
        # If we happened to be editing this mod, cancel edit mode.
        if self._editing_index is not None:
            self._editing_index = None
            self._editing_orig_name = None
            self._save_btn.config(text="Save Mod")
        self.selected_mod = None
        self._clear_details()
        self.refresh_mod_list()

    def _prefill_form(self, mod):
        """Fill the Add Mod form widgets from an existing mod's values."""
        # Clear scalar fields first.
        for k in ("name", "aob", "offset", "nop_len", "struct_offset", "value"):
            self.form_vars[k].set("")
        self.form_vars["template"].set(mod.get("template", TEMPLATES[0]))
        self.form_vars["freeze_mode"].set(mod.get("freeze_mode", FREEZE_MODES[0]))
        self.form_vars["poll_mode"].set(mod.get("poll_mode", POLL_MODES[0]))
        self.form_vars["capture_at_attach"].set(bool(mod.get("capture_at_attach", False)))
        self.form_vars["capture_once"].set(bool(mod.get("capture_once", False)))
        self.form_vars["name"].set(mod.get("name", ""))
        self._clear_hook_rows()

        if mod.get("template") == "hard_freeze":
            self.form_vars["aob"].set(mod.get("aob", ""))
            self.form_vars["offset"].set(str(mod.get("offset", "")))
            if mod.get("freeze_mode") == "nop":
                self.form_vars["nop_len"].set(str(mod.get("nop_len", "")))
            else:
                self.form_vars["value"].set(str(mod.get("value", "")))
        else:  # pointer_capture
            self.form_vars["struct_offset"].set(str(mod.get("struct_offset", "")))
            if mod.get("poll_mode") in ("clamp_min", "hard_set"):
                self.form_vars["value"].set(str(mod.get("value", "")))
            for hook in self.engine._mod_hooks(mod):
                steal = hook.get("steal_len", hook.get("hook_offset"))
                self._add_hook_row({
                    "aob": hook.get("aob", ""),
                    "hook_offset": "" if steal is None else steal,
                    "capture_register": hook.get("capture_register", "esi"),
                    "module": hook.get("module", ""),
                })

        if self.notes_text_widget is not None:
            self.notes_text_widget.delete("1.0", tk.END)
            self.notes_text_widget.insert("1.0", mod.get("notes", ""))
        self._refresh_form_fields()

    def _select_mod_by_name(self, name):
        for i, m in enumerate(self.engine.mods):
            if m.get("name") == name:
                self.tree.selection_set(str(i))
                self.tree.see(str(i))
                self.on_mod_select()
                return

    def _clear_details(self):
        self.details_text.config(state=tk.NORMAL)
        self.details_text.delete("1.0", tk.END)
        self.details_text.config(state=tk.DISABLED)
        self._reset_preview_display()
        self.update_action_buttons()

    # ==================================================================
    # Per-mod action buttons (state machine)
    # ==================================================================
    def _set_buttons_state(self, disabled):
        state = tk.DISABLED if disabled else tk.NORMAL
        for b in (self.hotkey_btn, self.preview_btn, self.action_btn):
            b.config(state=state)

    def update_action_buttons(self):
        mod = self.selected_mod
        if mod is None:
            self._set_buttons_state(disabled=True)
            self._update_value_editor(None)
            return

        name = mod.get("name")
        attached = self.engine.is_attached()
        self.hotkey_btn.config(state=tk.NORMAL)  # hotkey capture never needs attach
        self.preview_btn.config(state=tk.NORMAL if attached else tk.DISABLED)

        if self.engine.is_mod_active(name):
            # Active -> offer Disable.
            self.action_btn.config(text="Disable", command=self.on_disable, state=tk.NORMAL)
        else:
            # Not active -> Apply, gated on a successful preview of THIS mod.
            ready = attached and self.preview_ready_for == name
            self.action_btn.config(
                text="Apply", command=self.on_apply,
                state=tk.NORMAL if ready else tk.DISABLED,
            )

        self._update_value_editor(mod)

    def _update_value_editor(self, mod):
        """Show the live-value editor for an ACTIVE hard_set/clamp_min mod, and
        the Force Set control for ANY active pointer_capture mod."""
        is_active_pc = (
            mod is not None
            and mod.get("template") == "pointer_capture"
            and self.engine.is_mod_active(mod.get("name"))
        )
        # Live value editor: hard_set / clamp_min only (unchanged).
        show_value = is_active_pc and mod.get("poll_mode") in ("hard_set", "clamp_min")
        if show_value:
            self.value_err_var.set("")
            self.value_var.set(str(mod.get("value", "")))
            if not self.value_frame.winfo_manager():
                self.value_frame.pack(fill=tk.X, pady=(0, 6))
        else:
            self.value_frame.pack_forget()

        # Force Set: any active pointer_capture mod, regardless of poll_mode.
        if is_active_pc:
            self.force_err_var.set("")
            if not self.force_frame.winfo_manager():
                self.force_frame.pack(fill=tk.X, pady=(0, 6))
        else:
            self.force_frame.pack_forget()

        # Recapture: only active pointer_capture mods with capture_once enabled.
        if is_active_pc and mod.get("capture_once"):
            if not self.recapture_frame.winfo_manager():
                self.recapture_frame.pack(fill=tk.X, pady=(0, 6))
        else:
            self.recapture_frame.pack_forget()

    def on_recapture(self):
        """Unlock a capture_once mod so the next hook fire re-latches the slot."""
        mod = self.selected_mod
        if mod is None:
            return
        self.engine.recapture(mod.get("name"))

    def on_force_set(self):
        """One-time immediate write to [captured_ptr + struct_offset]."""
        mod = self.selected_mod
        if mod is None:
            return
        self.force_err_var.set("")
        raw = self.force_var.get().strip()
        try:
            value = int(raw, 0)   # accepts decimal or 0x hex
        except ValueError:
            self.force_err_var.set("not an integer")
            return
        self.engine.force_set_value(mod.get("name"), value)

    def on_set_value(self):
        mod = self.selected_mod
        if mod is None:
            return
        self.value_err_var.set("")
        raw = self.value_var.get().strip()
        try:
            new_value = int(raw, 0)   # accepts decimal or 0x hex
        except ValueError:
            self.value_err_var.set("not an integer")
            return
        name = mod.get("name")
        if self.engine.set_mod_value(name, new_value):
            self.refresh_mod_list()

    def on_save_to_config(self):
        """Persist the mod's CURRENT in-memory value to the JSON file on disk."""
        mod = self.selected_mod
        if mod is None:
            return
        name = mod.get("name")
        if self.engine.save_mod_config(name):
            path = (self.engine.config_path or "").replace("\\", "/")
            self.log("Saved '{}' value={} to {}".format(name, mod.get("value"), path))

    # ==================================================================
    # Hotkey capture (reuses the proven HotkeyManager.capture_next_key)
    # ==================================================================
    def on_set_hotkey(self):
        mod = self.selected_mod
        if mod is None:
            return
        self.log("Press a key (or hold Ctrl/Shift/Alt for a combo) to bind to "
                 "'{}'...".format(mod.get("name")))
        self.hotkey_btn.config(state=tk.DISABLED)

        def _worker():
            key = self.hotkeys.capture_next_key()
            self.root.after(0, lambda: self._hotkey_captured(mod, key))

        threading.Thread(target=_worker, daemon=True).start()

    def _hotkey_captured(self, mod, key):
        name = mod.get("name")
        # Collision guard: refuse if another mod already holds this key. Leave
        # this mod's existing hotkey untouched and don't save/register.
        conflict = self._find_hotkey_conflict(key, exclude=(name,))
        if conflict is not None:
            self.log(
                "Hotkey conflict: '{}' is already used by '{}'. "
                "Pick a different key.".format(key, conflict)
            )
            self.hotkey_btn.config(state=tk.NORMAL)
            return
        # Remove the stale binding on the OLD key before saving the new one, so
        # _bound never holds duplicate keys pointing at different mods.
        self._unregister_mod_hotkey(name)
        mod["hotkey"] = key
        self._save_config()
        self.log("Bound '{}' -> hotkey '{}'.".format(name, key))
        # Register the new key regardless of active state (bindings are
        # config-scoped, not tied to whether the mod is currently applied).
        self._register_mod_hotkey(mod)
        self.refresh_mod_list()
        self.hotkey_btn.config(state=tk.NORMAL)

    # ==================================================================
    # Global hotkey <-> action wiring
    # ==================================================================
    # Bindings are CONFIG-SCOPED: every mod with a saved hotkey is registered
    # when a config loads (not on Apply), so a hotkey works cold without any
    # prior manual Apply. A single callback handles both states — press an
    # inactive mod to Apply it (through the same preview gate as the button),
    # press an active mod to toggle it off. Bindings are cleared only on config
    # reload, a hotkey rebind (old key), or app close. These helpers touch only
    # self._bound, self.hotkeys, and the thread-safe log queue, so they are
    # safe to call from worker threads.
    def _find_hotkey_conflict(self, key, exclude=()):
        """Return the name of another mod already bound to `key` in self._bound,
        or None if the key is free. Names in `exclude` are ignored (e.g. the
        mod currently being (re)bound or edited)."""
        key = (key or "").strip()
        if not key:
            return None
        for other, other_key in self._bound.items():
            if other in exclude:
                continue
            if other_key == key:
                return other
        return None

    def _register_config_hotkeys(self):
        """Clear any previous config's bindings, then bind every loaded mod that
        has a hotkey — regardless of active state. Duplicate keys are resolved
        deterministically by list order: the FIRST mod to claim a key wins;
        later mods with the same key are skipped (not registered, not added to
        _bound) and left as-is in the JSON for the user to reassign."""
        self.hotkeys.unregister_all()
        self._bound.clear()
        claimed = {}  # key -> name of the mod that first claimed it this pass
        for mod in self.engine.mods:
            key = (mod.get("hotkey") or "").strip()
            if not key:
                continue
            if key in claimed:
                self.log(
                    "Hotkey conflict on load: '{}' claimed by '{}', skipping "
                    "for '{}' — reassign it.".format(key, claimed[key], mod.get("name"))
                )
                continue
            self._register_mod_hotkey(mod)
            claimed[key] = mod.get("name")

    def _register_mod_hotkey(self, mod):
        name = mod.get("name")
        key = (mod.get("hotkey") or "").strip()
        if not key:
            self.log("No hotkey set for '{}'; not registered.".format(name))
            return
        # Conflicts are prevented upstream now (Set Hotkey, Add/Edit save, and
        # config-load each validate before calling here), so a collision at this
        # point signals an upstream bug rather than normal flow. Log defensively
        # but proceed — this path is no longer the primary defense.
        existing = self._find_hotkey_conflict(key, exclude=(name,))
        if existing is not None:
            self.log(
                "BUG: hotkey '{}' already bound to '{}' while registering '{}' "
                "— an upstream collision check was bypassed.".format(key, existing, name)
            )
        # Drop any previous binding for THIS mod before adding the new one.
        self._unregister_mod_hotkey(name, quiet=True)
        # register() supports re-binding the same key_name to a new callback.
        self.hotkeys.register(key, lambda n=name: self._on_hotkey(n))
        self._bound[name] = key
        self.log("Hotkey '{}' -> toggle '{}' registered (global).".format(key, name))

    def _unregister_mod_hotkey(self, name, quiet=False):
        key = self._bound.pop(name, None)
        if key is None:
            return
        self.hotkeys.unregister(key)
        if not quiet:
            self.log("Hotkey '{}' for '{}' unregistered.".format(key, name))

    def _on_hotkey(self, name):
        """Global-hotkey callback (runs on the keyboard library's thread).

        - Active mod  -> toggle off.
        - Inactive mod -> preview first; apply ONLY if status is 'ready'
          (same gate as clicking Apply). If blocked, log and do nothing.

        Heavy work (scan/apply/teardown) is offloaded to a worker so the
        keyboard hook isn't blocked; UI refresh is marshalled to the main thread.
        """
        if not self.engine.is_attached():
            self.log("Hotkey for '{}' ignored: not attached.".format(name))
            return

        if self.engine.is_mod_active(name):
            # Active -> toggle off (binding persists for the next press).
            self.log("Hotkey: disabling '{}'.".format(name))

            def _worker_off():
                self.engine.toggle_mod(name, False)
                self.root.after(0, self.refresh_mod_list)

            threading.Thread(target=_worker_off, daemon=True).start()
            return

        # Inactive -> run the preview gate, then apply only if ready.
        mod = next((m for m in self.engine.mods if m.get("name") == name), None)
        if mod is None:
            return

        def _worker_on():
            result = self.engine.preview_mod(mod)
            status = result.get("status")
            if status == "ready":
                self.log("Hotkey: enabling '{}'.".format(name))
                self.engine.apply_mod(mod)   # re-verifies the gate, then applies
            else:
                self.log(
                    "Hotkey pressed for '{}' but preview blocked: {}".format(name, status)
                )
            self.root.after(0, self.refresh_mod_list)

        threading.Thread(target=_worker_on, daemon=True).start()

    # ==================================================================
    # Preview / Apply / Disable
    # ==================================================================
    def _reset_preview_display(self):
        self.preview_status_var.set("(no preview yet)")
        self.preview_status_label.config(fg=MUTED)
        self.preview_text.config(state=tk.NORMAL)
        self.preview_text.delete("1.0", tk.END)
        self.preview_text.config(state=tk.DISABLED)

    def _show_preview_progress(self, offset, size):
        """Update the scan progress bar (called on the main thread)."""
        pct = (offset / size * 100.0) if size else 0
        self.preview_progress_var.set(pct)

    def on_preview(self):
        mod = self.selected_mod
        if mod is None or not self.engine.is_attached():
            return
        self.preview_btn.config(state=tk.DISABLED)
        self.log("Previewing '{}'...".format(mod.get("name")))

        # Reveal + reset the progress bar for this scan.
        self.preview_progress_var.set(0)
        if not self.preview_progress.winfo_manager():
            self.preview_progress.pack(fill=tk.X, padx=8, pady=(2, 0),
                                       before=self.preview_text)

        def _progress(offset, size):
            # Runs on the worker thread — marshal the UI update to the main one.
            self.root.after(0, lambda: self._show_preview_progress(offset, size))

        def _worker():
            result = self.engine.preview_mod(mod, progress_callback=_progress)
            self.root.after(0, lambda: self._preview_done(mod, result))

        threading.Thread(target=_worker, daemon=True).start()

    def _preview_done(self, mod, result):
        self.preview_btn.config(state=tk.NORMAL)
        # Scan finished — fill then hide the progress bar.
        self.preview_progress_var.set(100)
        self.preview_progress.pack_forget()
        status = result.get("status")
        ready = status == "ready"

        self.preview_status_var.set(status.upper() if status else "?")
        if ready:
            self.preview_status_label.config(fg=GREEN)
        elif status == "blocked_multiple_match":
            self.preview_status_label.config(fg=AMBER)
        else:
            self.preview_status_label.config(fg=RED)

        self.preview_text.config(state=tk.NORMAL)
        self.preview_text.delete("1.0", tk.END)
        self.preview_text.tag_configure("err", foreground=RED)
        self.preview_text.tag_configure("warn", foreground=AMBER)
        self.preview_text.tag_configure("ok", foreground=GREEN)

        def put(text="", tag=None):
            self.preview_text.insert(tk.END, text + "\n", (tag,) if tag else ())

        def put_matches(addrs, count, is_ready, indent=""):
            """Render the match_addresses block for one scan (hook or whole mod)."""
            if is_ready:
                # ready == exactly one match; show it for confirmation.
                if addrs:
                    put("{}matched at   : {}".format(indent, addrs[0]), "ok")
                return
            if count == 0:
                put("{}0 matches found — check AOB, module, or game version."
                    .format(indent), "err")
            else:
                put("{}{} matches found:".format(indent, count), "warn")
                for a in addrs:
                    put("{}   {}".format(indent, a), "warn")
                if count > len(addrs):
                    put("{}   ... (+{} more)".format(indent, count - len(addrs)),
                        "warn")

        put("status        : {}".format(status))
        put("total matches : {}".format(result.get("matches")))

        hooks = result.get("hooks")
        if hooks is not None:
            # pointer_capture: one block per hook with its own match status.
            for hr in hooks:
                ok = hr.get("status") == "ready"
                mark = "OK" if ok else "BLOCKED"
                put("")
                put("hook[{}] {}  matches={}  reg={}  steal={}".format(
                    hr.get("index"), mark, hr.get("matches"),
                    hr.get("capture_register"), hr.get("steal_len")))
                put("   status : {}".format(hr.get("status")))
                # Always show which module was scanned — makes blocked_no_match
                # diagnosable (wrong/missing module vs. wrong AOB).
                put("   module : {}".format(hr.get("module") or "main module"))
                put_matches(hr.get("match_addresses") or [], hr.get("matches"),
                            ok, indent="   ")
                put("   aob    : {}".format(hr.get("aob")))
                put("   orig   : {}".format(hr.get("original_bytes")))
                put("   cave   : {}".format(hr.get("cave_preview")))
                for w in (hr.get("warnings") or []):
                    put("   warn   : {}".format(w), "warn")
        else:
            # hard_freeze: single result.
            put_matches(result.get("match_addresses") or [], result.get("matches"),
                        ready)
            put("original_bytes: {}".format(result.get("original_bytes")))
            put("cave_preview  : {}".format(result.get("cave_preview")))

        for w in (result.get("warnings") or []):
            put("warning       : {}".format(w), "warn")
        self.preview_text.config(state=tk.DISABLED)

        # UI-side Apply gate: enable Apply only when THIS mod previewed ready.
        self.preview_ready_for = mod.get("name") if ready else None
        self.update_action_buttons()

    def on_apply(self):
        mod = self.selected_mod
        if mod is None:
            return
        # Hard UI gate: never apply unless the last preview of this mod was ready.
        if self.preview_ready_for != mod.get("name"):
            self.log("Apply blocked: run a successful Preview first.")
            return
        self._apply(mod)

    def _apply(self, mod):
        self.action_btn.config(state=tk.DISABLED)

        def _worker():
            ok = self.engine.apply_mod(mod)
            self.root.after(0, lambda: self._apply_done(mod, ok))

        threading.Thread(target=_worker, daemon=True).start()

    def _apply_done(self, mod, ok):
        # Hotkeys are registered at config load (config-scoped), so nothing to
        # register here.
        if not ok:
            self.log("Apply failed for '{}'.".format(mod.get("name")))
        self.preview_ready_for = None
        self.refresh_mod_list()

    def on_disable(self):
        mod = self.selected_mod
        if mod is not None:
            self._do_disable(mod)

    def _do_disable(self, mod):
        """UI-driven disable (Disable button / unchecking 'On').

        The hotkey binding is KEPT (config-scoped) so the same key can re-enable
        the mod afterwards — only the runtime patch is torn down here.
        """
        name = mod.get("name")

        def _worker():
            self.engine.toggle_mod(name, False)
            self.root.after(0, self.refresh_mod_list)

        threading.Thread(target=_worker, daemon=True).start()

    def _preview_then_apply(self, mod):
        """Checkbox shortcut: preview, and apply only if ready."""
        if not self.engine.is_attached():
            self.log("Attach before enabling mods.")
            return

        def _worker():
            result = self.engine.preview_mod(mod)
            if result.get("status") == "ready":
                self.engine.apply_mod(mod)   # hotkey already bound at config load
            else:
                self.log("Enable blocked: preview status '{}'.".format(result.get("status")))
            self.root.after(0, self.refresh_mod_list)

        threading.Thread(target=_worker, daemon=True).start()

    # ==================================================================
    # Add Mod form
    # ==================================================================
    @staticmethod
    def _valid_aob(text):
        tokens = text.split()
        if not tokens:
            return False
        return all(_AOB_TOKEN.match(t) for t in tokens)

    def on_save_mod(self):
        self.form_error_var.set("")
        v = {k: var.get() for k, var in self.form_vars.items()}
        template = v["template"]

        editing = self._editing_index is not None
        errors = []
        if not v["name"].strip():
            errors.append("Name is required.")
        # Duplicate-name check excludes the mod currently being edited, so
        # keeping the same name (or another mod's untouched name) is fine.
        if any(m.get("name") == v["name"].strip()
               for i, m in enumerate(self.engine.mods)
               if not (editing and i == self._editing_index)):
            errors.append("A mod named '{}' already exists.".format(v["name"].strip()))

        mod = {"name": v["name"].strip(), "template": template}

        try:
            if template == "hard_freeze":
                if not v["aob"].strip():
                    errors.append("AOB is required.")
                elif not self._valid_aob(v["aob"]):
                    errors.append("AOB must be hex bytes or '??' wildcards.")
                mod["aob"] = v["aob"].strip().upper()
                if not v["offset"].strip():
                    errors.append("Offset is required.")
                else:
                    mod["offset"] = int(v["offset"], 0)
                mod["freeze_mode"] = v["freeze_mode"]
                if v["freeze_mode"] == "nop":
                    if not v["nop_len"].strip():
                        errors.append("NOP length is required in nop mode.")
                    else:
                        mod["nop_len"] = int(v["nop_len"], 0)
                else:
                    if not v["value"].strip():
                        errors.append("Value is required in value mode.")
                    else:
                        mod["value"] = int(v["value"], 0)

            elif template == "pointer_capture":
                # Build the hooks list from the dynamic rows.
                hooks = []
                if not self._hook_entries:
                    errors.append("At least one hook is required.")
                for i, e in enumerate(self._hook_entries):
                    aob = e["aob"].get().strip()
                    ho = e["hook_offset"].get().strip()
                    if not aob:
                        errors.append("Hook {}: AOB is required.".format(i + 1))
                    elif not self._valid_aob(aob):
                        errors.append("Hook {}: AOB must be hex/'??'.".format(i + 1))
                    if not ho:
                        errors.append("Hook {}: hook offset (steal len) required.".format(i + 1))
                    else:
                        steal_val = int(ho, 0)  # raises ValueError -> caught below
                        # Mid-instruction guard: if attached (bitness known) and
                        # the steal region has no wildcards, refuse a value that
                        # splits an instruction — catching it here, not at Apply.
                        if aob and self._valid_aob(aob) and self.engine.is_attached():
                            pattern, mask = self.engine._parse_aob(aob)
                            if all(mask[:steal_val]):  # wildcards -> can't verify
                                try:
                                    self.engine.validate_steal(pattern, steal_val)
                                except engine.MidInstructionStealError as ex:
                                    errors.append("Hook {}: steal {} splits an "
                                                  "instruction — use {} or {}.".format(
                                                      i + 1, steal_val, ex.lo, ex.hi))
                    hook_obj = {
                        "aob": aob.upper(),
                        "hook_offset": int(ho, 0) if ho else None,
                        "capture_register": e["register"].get(),
                    }
                    # Only persist 'module' when set, so configs without a
                    # module target stay byte-for-byte as before (main exe).
                    module = e.get("module").get().strip() if e.get("module") else ""
                    if module:
                        hook_obj["module"] = module
                    hooks.append(hook_obj)
                mod["hooks"] = hooks
                mod["capture_at_attach"] = bool(v["capture_at_attach"])
                # Persist capture_once ONLY when enabled, so existing configs
                # (and diffs) that don't use it stay byte-for-byte unchanged.
                if v["capture_once"]:
                    mod["capture_once"] = True
                if not v["struct_offset"].strip():
                    errors.append("Struct offset is required.")
                else:
                    mod["struct_offset"] = v["struct_offset"].strip()
                mod["poll_mode"] = v["poll_mode"]
                if v["poll_mode"] in ("clamp_min", "hard_set"):
                    if not v["value"].strip():
                        errors.append("Value is required for {}.".format(v["poll_mode"]))
                    else:
                        mod["value"] = int(v["value"], 0)
        except ValueError:
            errors.append("Numeric fields must be integers (decimal or 0x hex).")

        if errors:
            self.form_error_var.set("  •  ".join(errors))
            return

        mod["enabled"] = False
        # Optional notes (only stored when non-empty to keep configs tidy).
        if self.notes_text_widget is not None:
            self._set_mod_notes(mod, self.notes_text_widget.get("1.0", "end"))

        if self.config_path is None:
            messagebox.showerror("No config", "Load or create a game config first.")
            return

        # Hotkey collision guard. The Add form has no hotkey field, so only the
        # edit path can carry a non-empty (preserved) hotkey here; a hand-edited
        # config could also introduce a clash. Block the save if this mod's
        # hotkey is already claimed by another mod.
        if editing:
            preserved_hotkey = (
                self.engine.mods[self._editing_index].get("hotkey") or "").strip()
            conflict = self._find_hotkey_conflict(
                preserved_hotkey,
                exclude=(self._editing_orig_name, mod["name"]),
            )
            if conflict is not None:
                self.form_error_var.set(
                    "Hotkey conflict: '{}' is already used by '{}'. "
                    "Pick a different key.".format(preserved_hotkey, conflict)
                )
                return

        if editing:
            self._save_edited_mod(mod)
        else:
            mod["hotkey"] = ""
            self.engine.mods.append(mod)
            self._save_config()
            self.log("Added mod '{}' to config ({} hook(s)).".format(
                mod["name"], len(mod.get("hooks", [])) if template == "pointer_capture" else 0))
            self.refresh_mod_list()
            self.notebook.select(0)  # jump to Selected Mod tab

        # Reset the form for the next entry.
        self.form_vars["name"].set("")
        self.form_vars["aob"].set("")
        if self.notes_text_widget is not None:
            self.notes_text_widget.delete("1.0", tk.END)
        self._clear_hook_rows()
        self._refresh_form_fields()

    def _save_edited_mod(self, mod):
        """Replace the mod at self._editing_index in place, preserving its
        hotkey/enabled, handle a rename's hotkey binding, then persist."""
        idx = self._editing_index
        old = self.engine.mods[idx]
        old_name = self._editing_orig_name
        new_name = mod["name"]
        # Preserve fields the Add form doesn't manage.
        mod["hotkey"] = old.get("hotkey", "")
        mod["enabled"] = old.get("enabled", False)

        self.engine.mods[idx] = mod   # same list slot -> other mods untouched

        # If renamed, move any hotkey binding to the new name.
        if old_name != new_name:
            self._unregister_mod_hotkey(old_name)
            if (mod.get("hotkey") or "").strip():
                self._register_mod_hotkey(mod)

        self.engine.save_mod_config(new_name)
        self.log("Saved changes to '{}'.".format(new_name))

        # Leave edit mode and return to the details view on the edited mod.
        self._editing_index = None
        self._editing_orig_name = None
        self._save_btn.config(text="Save Mod")
        self.selected_mod = None
        self.refresh_mod_list()
        self.notebook.select(0)
        self._select_mod_by_name(new_name)

    @staticmethod
    def _set_mod_notes(mod, text):
        """Store notes on a mod, stripping; remove the key entirely when empty."""
        text = text.strip()
        if text:
            mod["notes"] = text
        else:
            mod.pop("notes", None)

    # ==================================================================
    # Persistence
    # ==================================================================
    def _save_config(self):
        if self.config_path is None:
            return
        # config_raw['mods'] IS engine.mods (same object), so it's current.
        self.config_raw["mods"] = self.engine.mods
        try:
            with open(self.config_path, "w", encoding="utf-8") as fh:
                json.dump(self.config_raw, fh, indent=2, ensure_ascii=False)
        except OSError as exc:
            self.log("Failed to save config: {}".format(exc))
            messagebox.showerror("Save failed", str(exc))

    # ==================================================================
    # Close / cleanup
    # ==================================================================
    def on_close(self):
        try:
            if self.engine.is_attached():
                self.log("Closing: detaching (stopping threads, restoring bytes)...")
                self.on_detach()          # detaches engine (keeps bindings)
            self.hotkeys.unregister_all()  # final cleanup of all global hotkeys
            self._bound.clear()
        finally:
            self.root.destroy()

    def run(self):
        self.root.mainloop()
