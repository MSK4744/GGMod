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
from engine import DebugSession, MemoryScanner, PointerChainFinder
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, simpledialog, ttk

# ---------------------------------------------------------------------------
# Theme: "Vault" dark — slate surfaces with a muted slate-teal brand accent.
# Softer than a saturated brand-blue: desaturated, calmer, easier on the eyes
# over long sessions, while keeping the same dark slate base and status colors.
# ---------------------------------------------------------------------------
BG = "#0f172a"          # slate-900  — main window / panels
BG2 = "#1e293b"         # slate-800  — raised: headings bar, hover, badges
BG3 = "#020617"         # slate-950  — deepest: log, inputs
FG = "#e2e8f0"          # slate-200  — primary text
MUTED = "#94a3b8"       # slate-400  — muted / secondary text
ACCENT = "#7dabc9"      # muted slate-blue — headings, focus, hover
ACCENT2 = "#3f6f8f"     # desaturated blue — filled primary button base
ACCENT_SOFT = "#22384a" # accent tint on dark — selected/active surface
GREEN = "#22c55e"       # green-500  — success / active / ready
RED = "#f43f5e"         # rose-500   — danger / error / blocked
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
# Small local settings file (scan-hotkey bindings) so they survive a restart.
SETTINGS_PATH = os.path.join(_APP_DIR, "ggmod_settings.json")

# Value-Scanner global hotkey actions. The action key doubles as the Next Scan
# scan_type passed to the scanner, so no extra mapping is needed.
SCAN_HOTKEY_ACTIONS = [
    ("increased", "Next Scan (Increased)"),
    ("decreased", "Next Scan (Decreased)"),
    ("unchanged", "Next Scan (Unchanged)"),
    ("changed",   "Next Scan (Changed)"),
]
SCAN_HOTKEY_LABELS = dict(SCAN_HOTKEY_ACTIONS)

GRIP = "⋮"   # ⋮ three-dot drag handle shown before each mod name

TEMPLATES = ["hard_freeze", "pointer_capture", "pointer_chain"]
FREEZE_MODES = ["value", "nop"]
POLL_MODES = ["never_decrease", "clamp_min", "set_once", "hard_set"]
REGISTERS = [
    "eax", "ebx", "ecx", "edx", "esi", "edi",
    "rax", "rbx", "rcx", "rdx", "rsi", "rdi",
]

# AOB tokens: pairs of hex digits or a "??" / "?" wildcard, space-separated.
_AOB_TOKEN = re.compile(r"^([0-9A-Fa-f]{2}|\?\?|\?)$")

# Sentinel default for _from_address/_paste_line's offset_var param: means
# "use self.form_vars['struct_offset']" (pointer_capture's default), so it's
# distinguishable from an explicit offset_var=None ("don't fill any offset
# field" -- used when wiring these into hard_freeze's AOB row).
_USE_STRUCT_OFFSET = object()


class GGModUI:
    def __init__(self, engine, hotkeys):
        self.engine = engine
        self.hotkeys = hotkeys
        # In-app value scanner (CE-style find tool); read/find only.
        self.scanner = MemoryScanner(engine)
        # Hardware-breakpoint "what writes to this address" session. Idle until
        # the user explicitly starts a watch -- it makes GGMod the game's
        # debugger, so it must never run in the background. self.log is passed
        # so debugger lifecycle events surface in the main log.
        self.debugger = DebugSession(engine, log_callback=self.log)
        # Static pointer-chain finder (CE-style static pointer scan); read-only,
        # like the value scanner -- discovers candidates, never writes.
        self.pointer_finder = PointerChainFinder(engine)

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
        # Value-Scanner global hotkeys: action -> key string (user-bound), and
        # the set of keys currently registered for them (so we unregister only
        # OUR keys, never a mod's). Loaded from the settings file (root/BoolVar
        # don't exist yet, so the enabled flag is kept as a plain bool for now).
        self._scan_bound = {}
        self._scan_registered = set()
        self._scan_hotkeys_enabled_pref = False
        self._load_settings()
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
        # Width floor is set just above the top bar's natural width (~826px)
        # so the window genuinely shrinks without clipping the Attach/New Game
        # row. Height floor accounts for the Value Scanner tab's fixed chrome
        # (header + Watch List panel + Log panel, ~500px combined) plus a
        # non-degenerate minimum for the results table + controls column
        # (~180px) -- below this the Scanner tab's body would collapse to
        # near-zero height instead of just needing its own scrollbar.
        self.root.minsize(880, 680)

        self._resolve_font()
        self._setup_style()
        self._build_topbar()
        self._build_main()
        self._build_log()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh_game_list()
        self._drain_log()             # start log pump
        self._refresh_form_fields()   # set initial add-form visibility
        # Sync scan hotkeys with restored settings (no-op until attached).
        self._apply_scan_hotkeys()

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
        font = kw.pop("font", (FONT, 9, "bold"))
        padx = kw.pop("padx", 12)
        pady = kw.pop("pady", 5)
        return tk.Button(
            parent, text=text, command=command,
            bg=bg, fg=WHITE, activebackground=active, activeforeground=WHITE,
            relief=tk.FLAT, bd=0, padx=padx, pady=pady,
            font=font, cursor="hand2",
            highlightthickness=0, disabledforeground="#64748b",  # slate-500
            **kw
        )

    def _button_ghost(self, parent, text, command, **kw):
        # Secondary/ghost button: transparent with slate text, subtle hover.
        font = kw.pop("font", (FONT, 9, "bold"))
        padx = kw.pop("padx", 12)
        pady = kw.pop("pady", 5)
        return tk.Button(
            parent, text=text, command=command,
            bg=BG2, fg=FG, activebackground=BORDER, activeforeground=FG,
            relief=tk.FLAT, bd=0, padx=padx, pady=pady,
            font=font, cursor="hand2",
            highlightthickness=0, disabledforeground="#64748b",
            **kw
        )

    def _entry(self, parent, textvariable=None, width=24):
        return tk.Entry(
            parent, textvariable=textvariable, width=width,
            bg=BG3, fg=FG, insertbackground=ACCENT, relief=tk.FLAT, bd=0,
            highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT,
        )

    def _dialog(self, title, minsize=(360, 160), resizable=True, modal=True):
        """Create a Toplevel with the app's consistent dialog conventions:
        dark background, a sensible minimum size so content never clips, and
        genuine resizability (rather than a fixed-size popup that overlaps/
        truncates when content grows).

        `modal=True` (the default, used by short-lived popups like "Build
        hook from address") makes it transient + grabs input focus. Tool
        windows meant to stay open alongside the main window (Value Scanner,
        Find Writes) pass modal=False so the user can interact with both at
        once -- no transient/grab_set, so it behaves as an independent
        top-level window."""
        top = tk.Toplevel(self.root)
        top.title(title)
        top.configure(bg=BG)
        top.minsize(*minsize)
        top.resizable(resizable, resizable)
        if modal:
            top.transient(self.root)
            top.grab_set()
        return top

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

        # Value Scanner / Find Writes are independent, non-modal tool windows
        # (not notebook tabs) so they can stay open side-by-side with the main
        # window -- e.g. to copy an address from one into the other.
        self._button_ghost(bar, "Value Scanner",
                           self.open_scanner_window).pack(side=tk.LEFT, padx=(16, 4))
        self._button_ghost(bar, "Find Writes",
                           self.open_findwrites_window).pack(side=tk.LEFT, padx=4)
        self._button_ghost(bar, "Pointer Chains",
                           self.open_pointerchains_window).pack(side=tk.LEFT, padx=4)

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
        # Value Scanner and Find Writes are separate top-level windows (see
        # open_scanner_window / open_findwrites_window), not tabs here.
        right = tk.Frame(main, bg=BG, width=460)
        right.pack(side=tk.RIGHT, fill=tk.BOTH, padx=(12, 0))
        right.pack_propagate(False)

        self.notebook = ttk.Notebook(right)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        self._build_details_tab()
        self._build_add_tab()

        # Independent tool windows, created lazily on first open (see
        # open_scanner_window / open_findwrites_window / open_pointerchains_
        # window); None while closed.
        self._scan_win = None
        self._fw_win = None
        self._chain_win = None
        # Scanner/Find-Writes session state, initialized unconditionally so
        # detach/close/watchlist helpers work even before either tool window
        # has been opened. Rebuilt fresh each time the owning window opens.
        self._watch_rows = {}
        self._scan_auto_job = None
        self._scan_prev_vals = {}
        self._scan_flash_jobs = {}
        self._fw_rows = {}
        self._fw_poll_job = None
        self._fw_auto_fired = False

    # ---- Value Scanner window (CE-style find tool) -------------------
    _SCAN_VALUE_TYPE_LABELS = [
        ("4 Bytes", "4 bytes"), ("2 Bytes", "2 bytes"), ("1 Byte", "1 byte"),
        ("8 Bytes", "8 bytes"), ("Float", "float"), ("Double", "double"),
        ("Array of Bytes", "aob"),
    ]
    _SCAN_TYPE_LABELS = [
        ("Exact Value", "exact"), ("Unknown Initial Value", "unknown"),
        ("Increased", "increased"), ("Decreased", "decreased"),
        ("Changed", "changed"), ("Unchanged", "unchanged"),
        ("Increased By", "increased_by"), ("Decreased By", "decreased_by"),
    ]
    _SCAN_TYPES_NEED_VALUE = {"exact", "increased_by", "decreased_by"}

    # Fixed-ish width of the right-hand controls column in the scanner tab.
    _SCAN_CTRL_WIDTH = 250
    # Live auto-refresh cadence (CE-style) and how long a changed-value flash
    # stays lit before it resets.
    _SCAN_AUTO_MS = 250
    _SCAN_FLASH_MS = 700

    def open_scanner_window(self):
        """Open the Value Scanner tool window, or focus it if already open."""
        if self._scan_win is not None and self._scan_win.winfo_exists():
            self._scan_win.deiconify()
            self._scan_win.lift()
            self._scan_win.focus_force()
            return
        self._build_scanner_window()

    def _close_scanner_window(self):
        """Tear down the scanner window and reset its session state -- the
        same "cleared on close" convention the Watch List already followed
        while it lived inside the main window."""
        if self._scan_auto_job is not None:
            try:
                self.root.after_cancel(self._scan_auto_job)
            except Exception:
                pass
            self._scan_auto_job = None
        for job in self._scan_flash_jobs.values():
            try:
                self.root.after_cancel(job)
            except Exception:
                pass
        self._scan_flash_jobs = {}
        self._watch_rows = {}
        try:
            self.scanner.new_scan()
        except Exception:
            pass
        win = self._scan_win
        self._scan_win = None
        if win is not None:
            win.destroy()

    def _build_scanner_window(self):
        top = self._dialog("Value Scanner", minsize=(760, 560), modal=False)
        self._scan_win = top
        top.protocol("WM_DELETE_WINDOW", self._close_scanner_window)

        # --- Header row ----------------------------------------------------
        header = tk.Frame(top, bg=BG)
        header.pack(fill=tk.X, padx=8, pady=(8, 2))
        self._label(header, "Memory Value Scanner", fg=ACCENT,
                    font=(FONT, 11, "bold")).pack(side=tk.LEFT)

        # --- Watch List: CE-style saved-address panel, docked to the bottom of
        # the window. Packed with side=BOTTOM BEFORE the expanding body below,
        # so it reliably claims its own space instead of being squeezed to zero.
        self._watch_rows = {}    # wid -> {address, vtype_key/label, desc,
                                 # active, frozen_value} -- session-only, never
                                 # persisted (cleared on detach/New Scan/close).
        self._build_watchlist_panel(top)

        # --- Body: results table (LEFT, expands) + controls (RIGHT, fixed) --
        # Cheat-Engine arrangement in a draggable split: the results list is the
        # left pane, controls the right pane. The sash between them can be
        # dragged so the user scales the table to any width they like; the left
        # pane also stretches when the whole window grows/shrinks.
        body = tk.PanedWindow(
            top, orient=tk.HORIZONTAL, bg=BORDER, sashwidth=7,
            sashrelief=tk.RAISED, bd=0, showhandle=True, handlesize=9,
            handlepad=40, opaqueresize=True)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=(2, 8))
        self._scan_paned = body

        # ---- RIGHT pane: controls column, wrapped in its own scrollable
        # canvas. The Watch List panel below can push the whole tab's
        # available height down to whatever's left, so the controls column
        # must never depend on getting enough natural height to show
        # everything -- scrolling this canvas always reaches every control,
        # including the last Scan Hotkeys row, regardless of window size.
        controls_outer = tk.Frame(body, bg=BG)
        controls_canvas = tk.Canvas(controls_outer, bg=BG, highlightthickness=0, bd=0)
        self._scan_ctrl_canvas = controls_canvas   # exposed for tests / scroll-to
        controls_sb = tk.Scrollbar(controls_outer, orient=tk.VERTICAL,
                                   command=controls_canvas.yview)
        controls_canvas.configure(yscrollcommand=controls_sb.set)
        controls_sb.pack(side=tk.RIGHT, fill=tk.Y)
        controls_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # The actual controls live in this frame; it's just a canvas item, so
        # every widget below is built exactly as before -- only the container
        # around them changed.
        controls = tk.Frame(controls_canvas, bg=BG)
        controls_win = controls_canvas.create_window((0, 0), window=controls,
                                                      anchor="nw")

        def _controls_inner_configure(_e=None):
            controls_canvas.configure(scrollregion=controls_canvas.bbox("all"))
        controls.bind("<Configure>", _controls_inner_configure)

        def _controls_canvas_configure(e):
            # Match the inner frame's width to the visible canvas width so
            # fill=X / sticky="ew" widgets still stretch to the column width.
            controls_canvas.itemconfigure(controls_win, width=e.width)
        controls_canvas.bind("<Configure>", _controls_canvas_configure)

        # Mouse-wheel scrolling, active only while the pointer is actually
        # over this column (bound/unbound on enter/leave) so it never steals
        # wheel events from the results table, log, or other tabs.
        def _controls_wheel(e):
            controls_canvas.yview_scroll(int(-e.delta / 40), "units")
        controls_canvas.bind(
            "<Enter>",
            lambda _e: controls_canvas.bind_all("<MouseWheel>", _controls_wheel))
        controls_canvas.bind(
            "<Leave>", lambda _e: controls_canvas.unbind_all("<MouseWheel>"))

        ctl = tk.Frame(controls, bg=BG)
        ctl.pack(fill=tk.X, pady=(0, 6))
        ctl.columnconfigure(1, weight=1)

        def _ctl_label(r, text):
            self._label(ctl, text, fg=MUTED, width=10, anchor="w").grid(
                row=r, column=0, sticky="w", pady=3)

        _ctl_label(0, "Value type:")
        self.scan_vtype = tk.StringVar(value=self._SCAN_VALUE_TYPE_LABELS[0][0])
        ttk.Combobox(ctl, textvariable=self.scan_vtype, state="readonly",
                     width=10,
                     values=[lbl for lbl, _ in self._SCAN_VALUE_TYPE_LABELS]
                     ).grid(row=0, column=1, sticky="ew")

        _ctl_label(1, "Scan type:")
        self.scan_stype = tk.StringVar(value=self._SCAN_TYPE_LABELS[0][0])
        stype_combo = ttk.Combobox(
            ctl, textvariable=self.scan_stype, state="readonly", width=10,
            values=[lbl for lbl, _ in self._SCAN_TYPE_LABELS])
        stype_combo.grid(row=1, column=1, sticky="ew")
        stype_combo.bind("<<ComboboxSelected>>", lambda _e: self._sync_scan_value_state())

        _ctl_label(2, "Value:")
        self.scan_value = tk.StringVar()
        self.scan_value_entry = self._entry(ctl, self.scan_value, width=12)
        self.scan_value_entry.grid(row=2, column=1, sticky="ew")

        _ctl_label(3, "Region:")
        self.scan_region = tk.StringVar(value="All")
        self.scan_region_combo = ttk.Combobox(
            ctl, textvariable=self.scan_region, width=10,
            values=["All", "main exe"])
        self.scan_region_combo.grid(row=3, column=1, sticky="ew")

        # Scan buttons: 2-column grid so the labels never clip in the narrow
        # column and each cell stretches evenly (sticky="ew" + equal weights).
        btns = tk.Frame(controls, bg=BG)
        btns.pack(fill=tk.X, pady=(2, 2))
        btns.columnconfigure(0, weight=1)
        btns.columnconfigure(1, weight=1)
        self.scan_first_btn = self._button(btns, "First Scan", self.on_first_scan)
        self.scan_first_btn.grid(row=0, column=0, sticky="ew", padx=(0, 3), pady=2)
        self.scan_next_btn = self._button(btns, "Next Scan", self.on_next_scan)
        self.scan_next_btn.grid(row=0, column=1, sticky="ew", padx=(3, 0), pady=2)
        self._button_ghost(btns, "Undo Scan", self.on_undo_scan).grid(
            row=1, column=0, sticky="ew", padx=(0, 3), pady=2)
        self._button_ghost(btns, "New Scan", self.on_new_scan).grid(
            row=1, column=1, sticky="ew", padx=(3, 0), pady=2)
        self._button_ghost(btns, "Refresh values", self.on_refresh_values).grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=2)
        self._button_ghost(btns, "Copy selected address",
                           self._copy_scan_address).grid(
            row=3, column=0, columnspan=2, sticky="ew", pady=2)

        # Live auto-refresh toggle (CE-style continuous value updates). Default
        # ON, but it only actually runs once a scan has results and this
        # window is open + attached (see _update_scan_auto). State lives here
        # so the loop can be armed/paused from window-open/close and
        # attach/detach events.
        self._scan_auto_job = None
        self._scan_prev_vals = {}      # iid -> last shown value (flash compare)
        self._scan_flash_jobs = {}     # iid -> pending flash-reset after() id
        self.scan_auto = tk.BooleanVar(value=True)
        auto = tk.Frame(controls, bg=BG)
        auto.pack(fill=tk.X, pady=(2, 0))
        tk.Checkbutton(
            auto, text="Auto-refresh (live)", variable=self.scan_auto,
            command=self.on_toggle_scan_auto, bg=BG, fg=FG, selectcolor=BG3,
            activebackground=BG, activeforeground=FG,
            font=(FONT, 9)).pack(side=tk.LEFT)

        self.scan_status = tk.StringVar(value="Not scanned yet.")
        self._label(controls, "", fg=MUTED, textvariable=self.scan_status,
                    wraplength=self._SCAN_CTRL_WIDTH - 8, justify="left"
                    ).pack(anchor="w", pady=(4, 2))

        # ---- Scan Hotkeys: user-bindable GLOBAL keys for Next Scan ----------
        hk = tk.Frame(controls, bg=BG)
        hk.pack(fill=tk.X, pady=(6, 0))
        hkhead = tk.Frame(hk, bg=BG)
        hkhead.pack(fill=tk.X)
        self._label(hkhead, "Scan Hotkeys", fg=ACCENT,
                    font=(FONT, 10, "bold")).pack(side=tk.LEFT)
        self.scan_hotkeys_enabled = tk.BooleanVar(
            value=self._scan_hotkeys_enabled_pref)
        tk.Checkbutton(
            hkhead, text="Global", variable=self.scan_hotkeys_enabled,
            command=self.on_toggle_scan_hotkeys, bg=BG, fg=FG, selectcolor=BG3,
            activebackground=BG, activeforeground=FG,
            font=(FONT, 9)).pack(side=tk.LEFT, padx=(8, 0))

        self._scan_key_vars = {}
        rows = tk.Frame(hk, bg=BG)
        rows.pack(fill=tk.X, pady=(2, 0))
        rows.columnconfigure(0, weight=1)
        # Each action gets its own row: label + current key on the top line,
        # Set/Clear buttons below, so nothing spills past the narrow column.
        for r, (action, label) in enumerate(SCAN_HOTKEY_ACTIONS):
            block = tk.Frame(rows, bg=BG)
            block.grid(row=r, column=0, sticky="ew", pady=(2, 0))
            block.columnconfigure(1, weight=1)
            self._label(block, label, fg=MUTED, anchor="w").grid(
                row=0, column=0, sticky="w")
            var = tk.StringVar(value=self._scan_bound.get(action, "") or "(unset)")
            self._scan_key_vars[action] = var
            self._label(block, "", fg=FG, textvariable=var, anchor="e",
                        font=(MONO, 9)).grid(row=0, column=1, sticky="e", padx=6)
            self._button(block, "Set",
                         lambda a=action: self.on_set_scan_hotkey(a)).grid(
                row=0, column=2, sticky="e", padx=(0, 3))
            self._button_ghost(block, "Clear",
                               lambda a=action: self.on_clear_scan_hotkey(a)).grid(
                row=0, column=3, sticky="e")

        # ---- LEFT pane: results table (fills all remaining space) ----------
        results = tk.Frame(body, bg=BG)
        self._scan_intro = self._label(
            results, "Find an address by value (read-only). Double-click "
            "or Copy an address into Add Mod → From address…", fg=MUTED,
            font=(FONT, 8), wraplength=360, justify="left", anchor="w")
        self._scan_intro.pack(fill=tk.X, pady=(0, 4))
        # Keep the intro text wrapping to the (draggable) pane width.
        results.bind("<Configure>", lambda e: self._scan_intro.config(
            wraplength=max(120, e.width - 8)))

        list_frame = tk.Frame(results, bg=BG)
        list_frame.pack(fill=tk.BOTH, expand=True)
        sb = tk.Scrollbar(list_frame)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.scan_tree = ttk.Treeview(
            list_frame, columns=("value", "previous", "module"),
            show="tree headings", height=12, yscrollcommand=sb.set)
        # Headings are left-anchored ("w") to match the left-anchored data
        # cells, so each title sits directly above its own column of values.
        self.scan_tree.heading("#0", text="Address", anchor="w")
        self.scan_tree.column("#0", width=150, minwidth=110, anchor="w")
        self.scan_tree.heading("value", text="Value", anchor="w")
        self.scan_tree.column("value", width=110, minwidth=60, anchor="w")
        self.scan_tree.heading("previous", text="Previous", anchor="w")
        self.scan_tree.column("previous", width=110, minwidth=60, anchor="w")
        self.scan_tree.heading("module", text="Module", anchor="w")
        self.scan_tree.column("module", width=120, minwidth=60, anchor="w")
        self.scan_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.config(command=self.scan_tree.yview)
        # Double-click a result row adds it to the Watch List below (CE-style
        # "add to address list"). Copying still works via the dedicated
        # "Copy selected address" button.
        self.scan_tree.bind("<Double-1>", self._add_to_watchlist)
        # Flash colours for live-changed values (green=up, red=down, amber=other).
        self.scan_tree.tag_configure("flash_up",
                                     background="#14432a", foreground="#4ade80")
        self.scan_tree.tag_configure("flash_down",
                                     background="#4a1414", foreground="#f87171")
        self.scan_tree.tag_configure("flash_chg",
                                     background="#3a3414", foreground="#facc15")

        # Register the panes: results left (stretches with the window), controls
        # right (fixed-ish, minsize keeps it usable). Dragging the sash between
        # them scales the results table to the user's liking.
        body.add(results, stretch="always", minsize=170)
        body.add(controls_outer, stretch="never", minsize=230,
                 width=self._SCAN_CTRL_WIDTH)

        self._sync_scan_value_state()
        self._update_scan_auto()   # sync live-refresh state for the fresh window

    # ==================================================================
    # Find Writes window -- hardware-breakpoint "what writes to this address"
    # ==================================================================
    # Follows the Value Scanner's layout conventions: a PanedWindow with the
    # results list on the left (stretches) and a scrollable controls column on
    # the right. Unlike Selected Mod/Add Mod, this one drives a real Windows
    # DEBUGGER on the target (engine.DebugSession), so the session lifecycle is
    # explicit and always torn down -- see _stop_watch / on_detach / on_close.
    _FW_POLL_MS = 200        # how often the UI drains the hit queue

    def open_findwrites_window(self):
        """Open the Find Writes tool window, or focus it if already open."""
        if self._fw_win is not None and self._fw_win.winfo_exists():
            self._fw_win.deiconify()
            self._fw_win.lift()
            self._fw_win.focus_force()
            return
        self._build_findwrites_window()

    def _close_findwrites_window(self):
        """Tear down the Find Writes window. Stops any running debug session
        first -- a hardware breakpoint left armed with no window to show its
        hits would strand the game, so this mirrors on_detach/on_close."""
        self._stop_watch(reason="Stopped — Find Writes window closed.")
        self._clear_fw_hits()
        win = self._fw_win
        self._fw_win = None
        if win is not None:
            win.destroy()

    def _build_findwrites_window(self):
        top = self._dialog("Find Writes", minsize=(760, 520), modal=False)
        self._fw_win = top
        top.protocol("WM_DELETE_WINDOW", self._close_findwrites_window)

        # Session state. _fw_rows maps instruction address -> hit payload so
        # repeat writes from the same instruction update one row (CE-style)
        # instead of flooding the list. Reset fresh on every open, consistent
        # with "state resets when the window is closed and reopened".
        self._fw_rows = {}
        self._fw_poll_job = None
        self._fw_auto_fired = False

        header = tk.Frame(top, bg=BG)
        header.pack(fill=tk.X, padx=8, pady=(8, 2))
        self._label(header, "Find What Writes to an Address", fg=ACCENT,
                    font=(FONT, 11, "bold")).pack(side=tk.LEFT)

        body = tk.PanedWindow(
            top, orient=tk.HORIZONTAL, bg=BORDER, sashwidth=7,
            sashrelief=tk.RAISED, bd=0, showhandle=True, handlesize=9,
            handlepad=40, opaqueresize=True)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=(2, 8))

        # ---- RIGHT pane: scrollable controls column (same pattern as the
        # scanner's, so the lower controls stay reachable at any window size).
        ctl_outer = tk.Frame(body, bg=BG)
        ctl_canvas = tk.Canvas(ctl_outer, bg=BG, highlightthickness=0, bd=0)
        self._fw_ctrl_canvas = ctl_canvas
        ctl_sb = tk.Scrollbar(ctl_outer, orient=tk.VERTICAL,
                              command=ctl_canvas.yview)
        ctl_canvas.configure(yscrollcommand=ctl_sb.set)
        ctl_sb.pack(side=tk.RIGHT, fill=tk.Y)
        ctl_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        controls = tk.Frame(ctl_canvas, bg=BG)
        ctl_win = ctl_canvas.create_window((0, 0), window=controls, anchor="nw")
        controls.bind("<Configure>", lambda _e: ctl_canvas.configure(
            scrollregion=ctl_canvas.bbox("all")))
        ctl_canvas.bind("<Configure>",
                        lambda e: ctl_canvas.itemconfigure(ctl_win, width=e.width))

        def _wheel(e):
            ctl_canvas.yview_scroll(int(-e.delta / 40), "units")
        ctl_canvas.bind("<Enter>",
                        lambda _e: ctl_canvas.bind_all("<MouseWheel>", _wheel))
        ctl_canvas.bind("<Leave>",
                        lambda _e: ctl_canvas.unbind_all("<MouseWheel>"))

        grid = tk.Frame(controls, bg=BG)
        grid.pack(fill=tk.X, pady=(0, 6))
        grid.columnconfigure(1, weight=1)
        self._label(grid, "Address:", fg=MUTED, width=8, anchor="w").grid(
            row=0, column=0, sticky="w", pady=3)
        self.fw_address = tk.StringVar()
        self._entry(grid, self.fw_address, width=14).grid(
            row=0, column=1, sticky="ew")
        self._label(grid, "Size:", fg=MUTED, width=8, anchor="w").grid(
            row=1, column=0, sticky="w", pady=3)
        self.fw_size = tk.StringVar(value="4 Bytes")
        ttk.Combobox(grid, textvariable=self.fw_size, state="readonly", width=10,
                     values=["1 Byte", "2 Bytes", "4 Bytes", "8 Bytes"]).grid(
            row=1, column=1, sticky="ew")

        btns = tk.Frame(controls, bg=BG)
        btns.pack(fill=tk.X, pady=(2, 2))
        btns.columnconfigure(0, weight=1)
        self.fw_start_btn = self._button(btns, "Start Watching",
                                         self.on_start_watch)
        self.fw_start_btn.grid(row=0, column=0, sticky="ew", pady=2)
        self.fw_stop_btn = self._button_ghost(btns, "Stop Watching",
                                              self.on_stop_watch)
        self.fw_stop_btn.grid(row=1, column=0, sticky="ew", pady=2)
        self.fw_stop_btn.config(state=tk.DISABLED)
        self._button_ghost(btns, "Clear hits", self._clear_fw_hits).grid(
            row=2, column=0, sticky="ew", pady=2)

        self.fw_auto_open = tk.BooleanVar(value=True)
        tk.Checkbutton(
            controls, text="Auto-open Add Mod on first hit",
            variable=self.fw_auto_open, bg=BG, fg=FG, selectcolor=BG3,
            activebackground=BG, activeforeground=FG, anchor="w",
            wraplength=self._SCAN_CTRL_WIDTH - 20, justify="left",
            font=(FONT, 9)).pack(fill=tk.X, pady=(4, 0))

        self.fw_status = tk.StringVar(value="Not watching.")
        self._label(controls, "", fg=MUTED, textvariable=self.fw_status,
                    wraplength=self._SCAN_CTRL_WIDTH - 8, justify="left",
                    anchor="w").pack(fill=tk.X, pady=(6, 2))

        self._label(
            controls,
            "Uses a hardware breakpoint, which makes GGMod the game's "
            "debugger. Only one debugger can attach at a time, so close "
            "Cheat Engine's debug mode on this process first. The session "
            "stops automatically on Detach or exit.",
            fg=MUTED, font=(FONT, 8),
            wraplength=self._SCAN_CTRL_WIDTH - 8, justify="left",
            anchor="w").pack(fill=tk.X, pady=(6, 0))

        # ---- LEFT pane: captured hits ---------------------------------
        results = tk.Frame(body, bg=BG)
        self._label(
            results, "Each row is one instruction that wrote to the address. "
            "Double-click a row (or Send) to build a hook from it.",
            fg=MUTED, font=(FONT, 8), justify="left", anchor="w"
        ).pack(fill=tk.X, pady=(0, 4))

        list_frame = tk.Frame(results, bg=BG)
        list_frame.pack(fill=tk.BOTH, expand=True)
        fsb = tk.Scrollbar(list_frame)
        fsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.fw_tree = ttk.Treeview(
            list_frame,
            columns=("hits", "time", "address", "module", "insn", "send",
                     "chains"),
            show="headings", height=12, yscrollcommand=fsb.set)
        for key, text, width, minw in (
            ("hits", "Hits", 50, 40), ("time", "Last", 70, 60),
            ("address", "Instruction", 130, 100), ("module", "Module", 120, 70),
            ("insn", "Decoded", 240, 120), ("send", "", 110, 110),
            ("chains", "", 110, 110),
        ):
            self.fw_tree.heading(key, text=text, anchor="w")
            self.fw_tree.column(key, width=width, minwidth=minw, anchor="w",
                                stretch=(key == "insn"))
        self.fw_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        fsb.config(command=self.fw_tree.yview)
        self.fw_tree.bind("<Button-1>", self._on_fw_click)
        self.fw_tree.bind("<Double-1>", self._on_fw_double_click)

        body.add(results, stretch="always", minsize=200)
        body.add(ctl_outer, stretch="never", minsize=230,
                 width=self._SCAN_CTRL_WIDTH)

    # ---- Find Writes: session control ---------------------------------
    def on_start_watch(self):
        """Begin a debug session watching the entered address for writes."""
        if not self.engine.is_attached():
            self.fw_status.set("Attach to a game first.")
            return
        if self.debugger.is_running():
            self.fw_status.set("Already watching — stop the current session first.")
            return
        raw = self.fw_address.get().strip()
        try:
            address = int(raw, 16)        # hex by convention, like CE
        except (TypeError, ValueError):
            self.fw_status.set("Enter a valid hex address (e.g. 16BBAE38).")
            return
        size = {"1 Byte": 1, "2 Bytes": 2, "4 Bytes": 4,
                "8 Bytes": 8}.get(self.fw_size.get(), 4)

        self._clear_fw_hits()
        self._fw_auto_fired = False
        self.fw_status.set("Attaching debugger…")
        self.root.update_idletasks()

        res = self.debugger.start(address, size)
        if "error" in res:
            self.fw_status.set(res["error"])
            self.log("Find Writes: {}".format(res["error"]))
            return
        used = res.get("size", size)
        note = "" if used == size else \
            " (size reduced to {} for address alignment)".format(used)
        self.fw_status.set(
            "Watching 0x{:X} on {} thread(s){} — play the game so the value "
            "changes.".format(address, res.get("threads", 0), note))
        self.log("Find Writes: watching 0x{:X} ({} byte(s)).".format(address, used))
        self.fw_start_btn.config(state=tk.DISABLED)
        self.fw_stop_btn.config(state=tk.NORMAL)
        self._schedule_fw_poll()

    def on_stop_watch(self):
        self._stop_watch(reason="Stopped watching.")

    def _fw_window_open(self):
        """True while the Find Writes tool window is open (it's a separate
        Toplevel now, not a notebook tab). Checked instead of hasattr(), since
        the widget attributes linger (pointing at destroyed widgets) after the
        window has been closed once."""
        return self._fw_win is not None and bool(self._fw_win.winfo_exists())

    def _stop_watch(self, reason="Stopped watching."):
        """Tear the debug session down. Safe to call when nothing is running,
        so detach/close paths can call it unconditionally."""
        if self._fw_poll_job is not None:
            try:
                self.root.after_cancel(self._fw_poll_job)
            except Exception:
                pass
            self._fw_poll_job = None
        running = self.debugger.is_running()
        res = self.debugger.stop()
        if running:
            self.log("Find Writes: session stopped; debugger detached.")
        if self._fw_window_open():
            self.fw_start_btn.config(state=tk.NORMAL)
            self.fw_stop_btn.config(state=tk.DISABLED)
            if "error" in res:
                self.fw_status.set(res["error"])
            elif running:
                self.fw_status.set(reason)
        return res

    def _clear_fw_hits(self):
        self._fw_rows = {}
        if self._fw_window_open():
            self.fw_tree.delete(*self.fw_tree.get_children())

    def _schedule_fw_poll(self):
        self._fw_poll_job = self.root.after(self._FW_POLL_MS, self._poll_fw_hits)

    def _poll_fw_hits(self):
        """Drain the debug thread's hit queue on the UI thread.

        The debug loop never touches tkinter; it only puts dicts on a
        queue.Queue, and this is the single place they become widgets.
        """
        self._fw_poll_job = None
        drained = 0
        while True:
            try:
                hit = self.debugger.hits.get_nowait()
            except Exception:
                break
            self._apply_fw_hit(hit)
            drained += 1
            if drained >= 200:            # keep the UI responsive under spam
                break

        if self.debugger.is_running():
            if drained:
                total = sum(r["count"] for r in self._fw_rows.values())
                self.fw_status.set(
                    "Watching — {} write(s) from {} instruction(s).".format(
                        total, len(self._fw_rows)))
            self._schedule_fw_poll()
        else:
            # The session ended on its own (e.g. the game exited).
            self.fw_start_btn.config(state=tk.NORMAL)
            self.fw_stop_btn.config(state=tk.DISABLED)

    def _apply_fw_hit(self, hit):
        """Upsert one hit, keyed by the writing instruction address.

        Repeat writes from the same instruction bump its count rather than
        adding rows — the interesting signal is WHICH instructions write, and
        how often, not thousands of individual traps.
        """
        key = hit["address"]
        known = self._fw_rows.get(key)
        if known is None:
            self._fw_rows[key] = hit
            self.fw_tree.insert(
                "", "end", iid=key,
                values=(hit["count"], hit["time"], hit["address"],
                        hit["module"], hit["text"], "→ Send", "→ Chains"))
        else:
            known["count"] = hit["count"]
            known["time"] = hit["time"]
            if self.fw_tree.exists(key):
                self.fw_tree.set(key, "hits", hit["count"])
                self.fw_tree.set(key, "time", hit["time"])

        # Full automation: the first captured write goes straight into the
        # Add Mod flow. The session is stopped first -- the dialog is modal, so
        # leaving a debugger attached behind it would strand the game with no
        # reachable Stop button.
        if not self._fw_auto_fired and self.fw_auto_open.get():
            self._fw_auto_fired = True
            self._stop_watch(reason="Stopped — first hit sent to Add Mod.")
            self._send_address_to_add_mod(hit["address"], auto_read=True)

    def _fw_selected_address(self, event=None):
        if event is not None:
            if self.fw_tree.identify("region", event.x, event.y) != "cell":
                return None
            row = self.fw_tree.identify_row(event.y)
            return row or None
        sel = self.fw_tree.selection()
        return sel[0] if sel else None

    def _on_fw_click(self, event):
        """Clicking the Send cell pushes that specific instruction into Add
        Mod, or the Chains cell into Pointer Chains -- important when several
        different code paths write the same address and the user needs a
        particular one, not just the first."""
        if self.fw_tree.identify("region", event.x, event.y) != "cell":
            return
        cols = self.fw_tree["columns"]
        col_id = self.fw_tree.identify_column(event.x)
        idx = int(col_id[1:]) - 1 if col_id.startswith("#") else -1
        if idx < 0 or idx >= len(cols) or cols[idx] not in ("send", "chains"):
            return
        wid = self.fw_tree.identify_row(event.y)
        if not wid:
            return
        if cols[idx] == "send":
            self._send_fw_address(wid)
        else:
            self._send_fw_address_to_pointerchains(wid)

    def _on_fw_double_click(self, event):
        wid = self._fw_selected_address(event)
        if wid:
            self._send_fw_address(wid)

    def _send_fw_address(self, key):
        """Send one captured instruction address into the Add Mod flow.

        Stops the watch session first for the same modal-dialog reason as the
        auto-open path above.
        """
        hit = self._fw_rows.get(key)
        if hit is None:
            return
        if self.debugger.is_running():
            self._stop_watch(reason="Stopped — address sent to Add Mod.")
        self._send_address_to_add_mod(hit["address"], auto_read=True)

    def _send_fw_address_to_pointerchains(self, key):
        """Send one captured instruction's write address into Pointer
        Chains as the target to search for a static chain to."""
        hit = self._fw_rows.get(key)
        if hit is None:
            return
        self._send_address_to_pointerchains(hit["address"])

    # ---- Watch List: CE-style saved-address panel ------------------------
    def _build_watchlist_panel(self, tab):
        frame = tk.Frame(tab, bg=BG)
        frame.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(4, 8))

        whead = tk.Frame(frame, bg=BG)
        whead.pack(fill=tk.X)
        self._label(whead, "Watch List", fg=ACCENT,
                    font=(FONT, 10, "bold")).pack(side=tk.LEFT)
        self._label(
            whead, "double-click a result above to add — Active = Freeze "
                   "(repeated write, for address verification only)",
            fg=MUTED, font=(FONT, 8)).pack(side=tk.LEFT, padx=(8, 0))
        self._button_ghost(whead, "Clear Watch List",
                           self._clear_watchlist).pack(side=tk.RIGHT)

        list_frame = tk.Frame(frame, bg=BG)
        list_frame.pack(fill=tk.X, pady=(4, 0))
        wsb = tk.Scrollbar(list_frame)
        wsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.watch_tree = ttk.Treeview(
            list_frame,
            columns=("active", "desc", "address", "type", "value",
                     "addmod", "chains", "remove"),
            show="headings", height=5, yscrollcommand=wsb.set)
        self.watch_tree.heading("active", text="Active", anchor="w")
        self.watch_tree.column("active", width=55, minwidth=50, anchor="w",
                               stretch=False)
        self.watch_tree.heading("desc", text="Description", anchor="w")
        self.watch_tree.column("desc", width=150, minwidth=80, anchor="w")
        self.watch_tree.heading("address", text="Address", anchor="w")
        self.watch_tree.column("address", width=110, minwidth=90, anchor="w")
        self.watch_tree.heading("type", text="Type", anchor="w")
        self.watch_tree.column("type", width=90, minwidth=70, anchor="w")
        self.watch_tree.heading("value", text="Value", anchor="w")
        self.watch_tree.column("value", width=90, minwidth=60, anchor="w")
        self.watch_tree.heading("addmod", text="", anchor="center")
        self.watch_tree.column("addmod", width=90, minwidth=90,
                               anchor="center", stretch=False)
        self.watch_tree.heading("chains", text="", anchor="center")
        self.watch_tree.column("chains", width=100, minwidth=100,
                               anchor="center", stretch=False)
        self.watch_tree.heading("remove", text="", anchor="center")
        self.watch_tree.column("remove", width=28, minwidth=28,
                               anchor="center", stretch=False)
        self.watch_tree.pack(side=tk.LEFT, fill=tk.X, expand=True)
        wsb.config(command=self.watch_tree.yview)
        self.watch_tree.bind("<Button-1>", self._on_watch_click)
        self.watch_tree.bind("<Double-1>", self._on_watch_double_click)
        # Tint a row while its Freeze is active, so it's visually distinct
        # from a plain watched (non-frozen) row.
        self.watch_tree.tag_configure(
            "frozen", background="#4a1414", foreground="#f87171")

    def _watch_column_at(self, event):
        """Return (wid, column_name) for a <Button-1> on self.watch_tree, or
        (None, None) if the click wasn't on a data cell."""
        if self.watch_tree.identify("region", event.x, event.y) != "cell":
            return None, None
        wid = self.watch_tree.identify_row(event.y)
        col_id = self.watch_tree.identify_column(event.x)
        if not wid or not col_id.startswith("#"):
            return None, None
        cols = self.watch_tree["columns"]
        idx = int(col_id[1:]) - 1
        if idx < 0 or idx >= len(cols):
            return None, None
        return wid, cols[idx]

    def _on_watch_click(self, event):
        wid, col = self._watch_column_at(event)
        if wid is None:
            return
        if col == "active":
            self._toggle_watch_active(wid)
        elif col == "desc":
            self._edit_watch_desc(wid)
        elif col == "addmod":
            self._use_watch_in_add_mod(wid)
        elif col == "chains":
            self._use_watch_in_pointerchains(wid)
        elif col == "remove":
            self._remove_watch_row(wid)

    def _on_watch_double_click(self, event):
        """Double-clicking the Value cell of a watched row edits it directly
        (a one-time write via write_value -- not a mod, just like Force Set)."""
        wid, col = self._watch_column_at(event)
        if wid is None or col != "value":
            return
        self._edit_watch_value(wid)

    def _add_to_watchlist(self, _event=None):
        """Double-click handler on the main results table: copy the selected
        address (+ its current value type) into the Watch List below, without
        removing it from the results table. No duplicate rows per address."""
        sel = self.scan_tree.selection()
        if not sel:
            return
        addr_str = self.scan_tree.item(sel[0], "text")
        try:
            addr = int(addr_str, 16)
        except (TypeError, ValueError):
            return
        if any(r["address"] == addr for r in self._watch_rows.values()):
            self.scan_status.set(
                "{} is already in the Watch List.".format(addr_str))
            return
        vtype_label = self.scan_vtype.get()
        vtype_key = dict(self._SCAN_VALUE_TYPE_LABELS).get(
            vtype_label, "4 bytes")
        value_str = self.scan_tree.set(sel[0], "value")
        wid = "w{:x}".format(addr)
        self._watch_rows[wid] = {
            "address": addr, "address_str": addr_str,
            "vtype_key": vtype_key, "vtype_label": vtype_label,
            "desc": "", "active": False, "frozen_value": None,
        }
        self.watch_tree.insert(
            "", "end", iid=wid,
            values=("☐", "", addr_str, vtype_label, value_str,
                    "→ Add Mod", "→ Chains", "✕"))
        self.scan_status.set("Added {} to Watch List.".format(addr_str))
        self._update_scan_auto()   # the watch tick may now need to start

    def _toggle_watch_active(self, wid):
        """Active checkbox = Freeze. Turning it on captures the CURRENT live
        value as the frozen target (re-written every tick); turning it off
        simply stops writing and lets the game's own value resume."""
        row = self._watch_rows.get(wid)
        if row is None:
            return
        row["active"] = not row["active"]
        if row["active"]:
            cur = self.scanner.read_value(row["address"], row["vtype_key"])
            row["frozen_value"] = cur
            if cur is None:
                self.scan_status.set(
                    "Watch: couldn't read {} to freeze (not attached or "
                    "unreadable address).".format(row["address_str"]))
        else:
            row["frozen_value"] = None
        self.watch_tree.set(wid, "active", "☑" if row["active"] else "☐")
        self.watch_tree.item(wid, tags=("frozen",) if row["active"] else ())
        self._update_scan_auto()   # a newly-active row needs the tick running

    def _edit_watch_desc(self, wid):
        row = self._watch_rows.get(wid)
        if row is None:
            return
        new_desc = simpledialog.askstring(
            "Watch List", "Description for {}:".format(row["address_str"]),
            initialvalue=row["desc"], parent=self.root)
        if new_desc is None:
            return
        row["desc"] = new_desc.strip()
        self.watch_tree.set(wid, "desc", row["desc"])

    def _edit_watch_value(self, wid):
        """Prompt for a new value and write it once to the watched address
        (reuses scanner.write_value -- the same primitive Freeze uses, not a
        mod). If the row is currently frozen, the new value becomes the value
        held from then on, so the edit doesn't get overwritten on the next
        tick by the OLD frozen value."""
        row = self._watch_rows.get(wid)
        if row is None:
            return
        if not self.engine.is_attached():
            self.scan_status.set("Attach to a game first to edit a watched value.")
            return
        current = self.watch_tree.set(wid, "value")
        new_str = simpledialog.askstring(
            "Watch List", "New value for {} ({}):".format(
                row["address_str"], row["vtype_label"]),
            initialvalue=current, parent=self.root)
        if new_str is None:
            return
        try:
            parsed = self.scanner._parse_scalar(new_str, row["vtype_key"])
        except (TypeError, ValueError):
            self.scan_status.set(
                "'{}' is not a valid {} value.".format(new_str, row["vtype_label"]))
            return
        if not self.scanner.write_value(row["address"], parsed, row["vtype_key"]):
            self.scan_status.set(
                "Failed to write to {} (Array of Bytes rows can't be edited "
                "here).".format(row["address_str"]))
            return
        if row["active"]:
            row["frozen_value"] = parsed
        display = self.scanner._fmt_value(parsed)
        self.watch_tree.set(wid, "value", display)
        self.scan_status.set("Set {} = {}.".format(row["address_str"], display))

    def _send_address_to_add_mod(self, address_str, auto_read=False):
        """Shared 'take this address into Add Mod' navigation.

        Used by BOTH the Watch List's "-> Add Mod" button and Find Writes'
        "Send this address", so the two never drift apart. Value Scanner and
        Find Writes are separate windows now, so this also brings the main
        window to the front before switching tabs. Then opens the same "From
        address..." dialog a hook row already offers, pre-filled. Pure
        navigation + pre-fill -- it does not touch build_candidate_from_address
        or any of its matching/auto-fill logic.
        """
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.notebook.select(1)   # Add Mod tab (0=Selected Mod, 1=Add Mod)
        # Hook rows -- and therefore "From address..." -- only exist for the
        # pointer_capture template. Switching template only toggles which form
        # rows are shown; it does not clear anything the user already typed.
        if self.form_vars["template"].get() != "pointer_capture":
            self.form_vars["template"].set("pointer_capture")
            self._refresh_form_fields()   # seeds one empty hook row
        if not self._hook_entries:
            self._add_hook_row()
        self._from_address(self._hook_entries[0], prefill_address=address_str,
                           auto_read=auto_read)

    def _send_address_to_pointerchains(self, address_str):
        """Shared 'take this address into Pointer Chains' navigation, mirroring
        _send_address_to_add_mod's pattern for the third tool window: opens
        (or focuses) the Pointer Chains window and pre-fills its target
        address field. Pure navigation + pre-fill -- does not start a scan."""
        self.open_pointerchains_window()
        self._chain_win.lift()
        self._chain_win.focus_force()
        self.chain_target_var.set(address_str)

    def _use_watch_in_add_mod(self, wid):
        """'Use in Add Mod' on a Watch List row."""
        row = self._watch_rows.get(wid)
        if row is None:
            return
        self._send_address_to_add_mod(row["address_str"])

    def _use_watch_in_pointerchains(self, wid):
        """'Use in Pointer Chains' on a Watch List row."""
        row = self._watch_rows.get(wid)
        if row is None:
            return
        self._send_address_to_pointerchains(row["address_str"])

    def _remove_watch_row(self, wid):
        self._watch_rows.pop(wid, None)
        if self.watch_tree.exists(wid):
            self.watch_tree.delete(wid)
        self._update_scan_auto()   # loop may no longer be needed

    def _clear_watchlist(self):
        """Drop every watched address (no writes are pending after this — any
        row that was frozen simply stops being rewritten). Called on manual
        Clear, New Scan, detach, and app close; the Watch List is session-only
        and is never persisted to disk."""
        self._watch_rows.clear()
        # Guard on the window (not just hasattr) -- watch_tree is destroyed
        # along with the Value Scanner window on close, but the attribute
        # itself lingers, so a stale reference would raise TclError here.
        if self._scan_window_open():
            self.watch_tree.delete(*self.watch_tree.get_children())
        self._update_scan_auto()

    def _tick_watchlist(self):
        """One tick's worth of Watch List work: write each active (frozen)
        row's held value first, then read the live value for display -- so a
        successful freeze shows the value holding steady, exactly like CE."""
        for wid, row in list(self._watch_rows.items()):
            if not self.watch_tree.exists(wid):
                self._watch_rows.pop(wid, None)
                continue
            if row["active"] and row["frozen_value"] is not None:
                self.scanner.write_value(
                    row["address"], row["frozen_value"], row["vtype_key"])
            cur = self.scanner.read_value(row["address"], row["vtype_key"])
            display = self.scanner._fmt_value(cur) if cur is not None else "?"
            self.watch_tree.set(wid, "value", display)

    # ---- Scan-hotkey settings persistence --------------------------------
    def _load_settings(self):
        """Load scan-hotkey bindings + enabled flag from the settings file into
        self._scan_bound / self._scan_hotkeys_enabled_pref (best-effort)."""
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        raw = data.get("scan_hotkeys", {})
        if isinstance(raw, dict):
            # Keep only known actions with a non-empty string key.
            self._scan_bound = {a: str(raw[a]) for a, _ in SCAN_HOTKEY_ACTIONS
                                if isinstance(raw.get(a), str) and raw.get(a)}
        self._scan_hotkeys_enabled_pref = bool(data.get("scan_hotkeys_enabled"))

    def _save_settings(self):
        try:
            enabled = bool(self.scan_hotkeys_enabled.get()) \
                if hasattr(self, "scan_hotkeys_enabled") \
                else self._scan_hotkeys_enabled_pref
            with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
                json.dump({"scan_hotkeys": self._scan_bound,
                           "scan_hotkeys_enabled": enabled}, fh, indent=2)
        except OSError as exc:
            self.log("Could not save settings: {}".format(exc))

    def _sync_scan_value_state(self):
        """Enable the Value field only for scan types that need a target."""
        key = dict(self._SCAN_TYPE_LABELS).get(self.scan_stype.get(), "exact")
        need = key in self._SCAN_TYPES_NEED_VALUE
        self.scan_value_entry.config(state=tk.NORMAL if need else tk.DISABLED)

    def _scan_selected_keys(self):
        vtype = dict(self._SCAN_VALUE_TYPE_LABELS).get(self.scan_vtype.get(), "4 bytes")
        stype = dict(self._SCAN_TYPE_LABELS).get(self.scan_stype.get(), "exact")
        return vtype, stype

    def _fill_scan_tree(self, rows):
        """Rebuild the results tree from scratch, keyed by address (iid) so the
        live auto-refresh can update rows in place. Resets flash tracking."""
        # Cancel any pending flash resets from the previous result set.
        for job in self._scan_flash_jobs.values():
            try:
                self.root.after_cancel(job)
            except Exception:
                pass
        self._scan_flash_jobs = {}
        self._scan_prev_vals = {}
        self.scan_tree.delete(*self.scan_tree.get_children())
        for row in rows:
            iid = row["address"]
            self.scan_tree.insert(
                "", "end", iid=iid, text=row["address"],
                values=(row["value"], row.get("previous", ""),
                        row.get("module", "")))
            self._scan_prev_vals[iid] = row["value"]

    def _render_scan(self, res):
        """Render a scanner result dict into the tree + status (main thread)."""
        if "error" in res:
            self.scan_status.set(res["error"])
            self._set_scan_buttons(True)
            return
        self._fill_scan_tree(res.get("results", []))
        count = res.get("count", 0)
        msg = "{} result(s).".format(count)
        if res.get("truncated"):
            msg += " Showing first {} — narrow your value/region.".format(
                self.scanner.PREVIEW_LIMIT)
        self.scan_status.set(msg)
        self._set_scan_buttons(True)
        # A fresh result set may enable (or, if empty, disable) live refresh.
        self._update_scan_auto()

    def _set_scan_buttons(self, enabled):
        state = tk.NORMAL if enabled else tk.DISABLED
        self.scan_first_btn.config(state=state)
        self.scan_next_btn.config(state=state)

    def _run_scan_async(self, fn):
        """Run a (possibly slow) scan on a worker thread, render on the main one."""
        if not self.engine.is_attached():
            self.scan_status.set("Attach to a game first (Value Scanner reads "
                                 "live memory).")
            return
        self._set_scan_buttons(False)
        self.scan_status.set("Scanning…")

        def _worker():
            try:
                res = fn()
            except Exception as exc:                 # never leave UI stuck
                res = {"error": "Scan failed: {}".format(exc)}
            self.root.after(0, lambda: self._render_scan(res))

        threading.Thread(target=_worker, daemon=True).start()

    def on_first_scan(self):
        vtype, stype = self._scan_selected_keys()
        if stype not in ("exact", "unknown"):
            self.scan_status.set("First Scan supports only Exact Value or "
                                 "Unknown Initial Value.")
            return
        value = self.scan_value.get()
        region = self.scan_region.get().strip() or "All"
        self._run_scan_async(
            lambda: self.scanner.first_scan(vtype, stype, value, region))

    def on_next_scan(self):
        _vtype, stype = self._scan_selected_keys()
        if stype == "unknown":
            self.scan_status.set("Pick a Next Scan type (Increased, Decreased, "
                                 "Exact Value, …).")
            return
        value = self.scan_value.get()
        self._run_scan_async(lambda: self.scanner.next_scan(stype, value))

    def on_undo_scan(self):
        self._render_scan(self.scanner.undo())

    def on_new_scan(self):
        self._render_scan(self.scanner.new_scan())
        self._clear_watchlist()      # session-only; a fresh scan starts clean
        self.scan_status.set("New scan — ready for First Scan.")

    def on_refresh_values(self):
        if not self.engine.is_attached():
            self.scan_status.set("Attach to a game first.")
            return
        res = self.scanner.refresh_values()
        if "error" in res:
            self.scan_status.set(res["error"])
            return
        self._fill_scan_tree(res.get("results", []))
        self.scan_status.set("{} result(s) — values refreshed.".format(
            res.get("count", 0)))

    # ---- Live auto-refresh (CE-style continuous value updates) -----------
    def on_toggle_scan_auto(self):
        """Checkbox handler: arm or pause the live-refresh loop."""
        self._update_scan_auto()

    def _scan_window_open(self):
        """True while the Value Scanner tool window is open (it's a separate
        Toplevel now, not a notebook tab)."""
        return self._scan_win is not None and bool(self._scan_win.winfo_exists())

    def _scan_main_wanted(self):
        """True when the MAIN results table should keep live-refreshing:
        toggle on, attached, window open, and a scan has results to show.

        Window-open is checked FIRST: self.scan_auto only exists once the
        Value Scanner window has been built at least once, so this must
        short-circuit before touching it.
        """
        if not self._scan_window_open():
            return False
        if not self.scan_auto.get():
            return False
        if not self.engine.is_attached():
            return False
        return bool(self.scanner.results)

    def _watch_wanted(self):
        """True when the Watch List needs ticking: attached, window open, and
        at least one watched address exists. Independent of the main table's
        Auto-refresh toggle/results -- the Watch List always stays live."""
        if not self.engine.is_attached():
            return False
        if not self._scan_window_open():
            return False
        return bool(self._watch_rows)

    def _scan_auto_wanted(self):
        try:
            return self._scan_main_wanted() or self._watch_wanted()
        except Exception:
            return False

    def _update_scan_auto(self):
        """Start the single shared tick loop if either the main table or the
        Watch List needs it and it isn't running, or stop it if neither does.
        Called from tab-change, attach/detach, toggles, add/remove-watch, and
        after every render so the timer never leaks or spins needlessly."""
        want = self._scan_auto_wanted()
        if want and self._scan_auto_job is None:
            self._scan_auto_job = self.root.after(
                self._SCAN_AUTO_MS, self._scan_auto_tick)
        elif not want and self._scan_auto_job is not None:
            try:
                self.root.after_cancel(self._scan_auto_job)
            except Exception:
                pass
            self._scan_auto_job = None

    def _scan_auto_tick(self):
        """One tick of the SHARED live-refresh loop: re-reads the main table's
        displayed candidates (no scan-type filtering, no Previous change, no
        scan-state advance) AND services the Watch List (freeze-writes +
        live reads), whichever of the two currently applies. Single timer,
        two jobs -- never two competing loops."""
        self._scan_auto_job = None
        if not self._scan_auto_wanted():
            return
        if self._scan_main_wanted():
            try:
                res = self.scanner.refresh_values()   # displayed subset only
            except Exception:
                res = {"error": "read failed"}
            if "error" not in res:
                self._apply_live_values(res.get("results", []))
        if self._watch_wanted():
            self._tick_watchlist()
        # Re-arm for the next tick (conditions re-checked at the top).
        self._update_scan_auto()

    def _apply_live_values(self, rows):
        """Update the Value cell of each already-rendered row in place, flashing
        rows whose value moved since the last tick. Leaves Previous untouched."""
        for row in rows:
            iid = row["address"]
            if not self.scan_tree.exists(iid):
                continue
            new_val = row["value"]
            old_val = self._scan_prev_vals.get(iid)
            if new_val != old_val:
                self.scan_tree.set(iid, "value", new_val)
                if old_val is not None:
                    self._flash_row(iid, self._value_direction(old_val, new_val))
                self._scan_prev_vals[iid] = new_val

    @staticmethod
    def _value_direction(old, new):
        """'up'/'down' for numeric moves, 'changed' for anything else."""
        try:
            o, n = float(old), float(new)
        except (TypeError, ValueError):
            return "changed"
        if n > o:
            return "up"
        if n < o:
            return "down"
        return None

    def _flash_row(self, iid, direction):
        tag = {"up": "flash_up", "down": "flash_down",
               "changed": "flash_chg"}.get(direction)
        if not tag:
            return
        self.scan_tree.item(iid, tags=(tag,))
        # Cancel a still-pending reset so a steadily-changing row stays lit.
        prev = self._scan_flash_jobs.get(iid)
        if prev is not None:
            try:
                self.root.after_cancel(prev)
            except Exception:
                pass
        self._scan_flash_jobs[iid] = self.root.after(
            self._SCAN_FLASH_MS, lambda i=iid: self._clear_flash(i))

    def _clear_flash(self, iid):
        self._scan_flash_jobs.pop(iid, None)
        try:
            if self.scan_tree.exists(iid):
                self.scan_tree.item(iid, tags=())
        except Exception:
            pass

    def _copy_scan_address(self, _event=None):
        sel = self.scan_tree.selection()
        if not sel:
            return
        address = self.scan_tree.item(sel[0], "text")
        self.root.clipboard_clear()
        self.root.clipboard_append(address)
        self.scan_status.set("Copied {} to clipboard.".format(address))

    # ---- Scan hotkeys: global, user-bindable, reuse HotkeyManager --------
    def _scan_key_conflict(self, key, exclude_action=None):
        """Return a description of what holds `key` across mod AND scan
        bindings, or None. Keeps mod and scan hotkeys from stealing each
        other's key (both live in the same HotkeyManager)."""
        key = (key or "").strip()
        if not key:
            return None
        for name, k in self._bound.items():
            if k == key:
                return "mod '{}'".format(name)
        for action, k in self._scan_bound.items():
            if action != exclude_action and k == key:
                return "scan hotkey '{}'".format(SCAN_HOTKEY_LABELS[action])
        return None

    def on_set_scan_hotkey(self, action):
        label = SCAN_HOTKEY_LABELS[action]
        self.scan_status.set(
            "Press a key (or Ctrl/Shift/Alt combo) for {}…".format(label))

        def _worker():
            key = self.hotkeys.capture_next_key()
            self.root.after(0, lambda: self._scan_hotkey_captured(action, key))

        threading.Thread(target=_worker, daemon=True).start()

    def _scan_hotkey_captured(self, action, key):
        label = SCAN_HOTKEY_LABELS[action]
        conflict = self._scan_key_conflict(key, exclude_action=action)
        if conflict is not None:
            self.scan_status.set(
                "'{}' is already used by {}. Pick a different key.".format(
                    key, conflict))
            return
        self._scan_bound[action] = key
        self._scan_key_vars[action].set(key)
        self._save_settings()
        self._apply_scan_hotkeys()            # (re)register if enabled+attached
        self.log("Scan hotkey set: {} -> '{}'.".format(label, key))
        self.scan_status.set("{} bound to '{}'.".format(label, key))

    def on_clear_scan_hotkey(self, action):
        if self._scan_bound.pop(action, None) is None:
            return
        self._scan_key_vars[action].set("(unset)")
        self._save_settings()
        self._apply_scan_hotkeys()
        self.log("Scan hotkey cleared: {}.".format(SCAN_HOTKEY_LABELS[action]))

    def on_toggle_scan_hotkeys(self):
        self._save_settings()
        self._apply_scan_hotkeys()
        if self.scan_hotkeys_enabled.get():
            if not self.engine.is_attached():
                self.scan_status.set("Scan hotkeys enabled — they activate once "
                                     "you attach to a game.")
            else:
                self.scan_status.set("Scan hotkeys enabled (global).")
        else:
            self.scan_status.set("Scan hotkeys disabled.")

    def _apply_scan_hotkeys(self):
        """Reconcile scan-hotkey registrations with the current enabled/attach
        state. Only OUR keys are touched — mod hotkeys are never disturbed."""
        # Remove whatever WE previously registered (never a mod's key).
        for key in list(self._scan_registered):
            if key in self.hotkeys._registered:
                self.hotkeys.unregister(key)
        self._scan_registered = set()

        enabled = (hasattr(self, "scan_hotkeys_enabled")
                   and self.scan_hotkeys_enabled.get())
        if not enabled or not self.engine.is_attached():
            return
        for action, key in self._scan_bound.items():
            if not key:
                continue
            # Never steal a mod's key (collisions are blocked at set-time; this
            # is a defensive guard for keys loaded from settings/config).
            holder = next((n for n, k in self._bound.items() if k == key), None)
            if holder is not None:
                self.log("Scan hotkey '{}' key '{}' also used by mod '{}'; "
                         "skipping.".format(SCAN_HOTKEY_LABELS[action], key, holder))
                continue
            self.hotkeys.register(key, lambda a=action: self._on_scan_hotkey(a))
            self._scan_registered.add(key)

    def _on_scan_hotkey(self, action):
        """Global-hotkey callback (runs on the keyboard hook thread). Fires the
        same Next Scan the button would, with this action's fixed scan type."""
        if not self._scan_window_open():
            self.log("Scan hotkey: {} ignored — open the Value Scanner window "
                     "first.".format(SCAN_HOTKEY_LABELS[action]))
            return
        self.log("Scan hotkey: {}.".format(SCAN_HOTKEY_LABELS[action]))
        # Marshal to the main thread; the async helper does the worker + render.
        self.root.after(
            0, lambda: self._run_scan_async(lambda: self.scanner.next_scan(action)))

    # ==================================================================
    # Pointer Chains window -- automates CE's static pointer scan (Stage 2:
    # recursion via engine.find_pointer_chains + restart-verification UI).
    # Read-only/discovery only, same philosophy as the Value Scanner -- never
    # writes to the target. A surviving chain gets copied into a mod by the
    # user; nothing here auto-applies.
    # ==================================================================
    def open_pointerchains_window(self):
        """Open the Pointer Chains tool window, or focus it if already open."""
        if self._chain_win is not None and self._chain_win.winfo_exists():
            self._chain_win.deiconify()
            self._chain_win.lift()
            self._chain_win.focus_force()
            return
        self._build_pointerchains_window()

    def _close_pointerchains_window(self):
        """Tear down the window and drop its session state -- same
        "cleared on close" convention as Value Scanner/Find Writes. The
        snapshot is a large in-memory numpy structure; no reason to keep it
        once the window is gone."""
        self._chain_snapshot = None
        self._chain_rows = {}
        win = self._chain_win
        self._chain_win = None
        if win is not None:
            win.destroy()

    def _build_pointerchains_window(self):
        top = self._dialog("Pointer Chains", minsize=(760, 520), modal=False)
        self._chain_win = top
        top.protocol("WM_DELETE_WINDOW", self._close_pointerchains_window)

        self._chain_snapshot = None
        self._chain_rows = {}   # iid -> chain dict (module/base_offset/
                                # offset_chain/level_count) + "marked" bool

        header = tk.Frame(top, bg=BG)
        header.pack(fill=tk.X, padx=8, pady=(8, 2))
        self._label(header, "Pointer Chain Finder", fg=ACCENT,
                    font=(FONT, 11, "bold")).pack(side=tk.LEFT)

        intro = self._label(
            top, "Automates Cheat Engine's static pointer scan: finds a "
            "module_base + fixed-offset chain that resolves to a target "
            "address without needing a live pointer_capture hook to fire "
            "first. Read-only -- copy a surviving chain into a mod by hand.",
            fg=MUTED, font=(FONT, 8), wraplength=700, justify="left")
        intro.pack(fill=tk.X, padx=8, pady=(0, 4))
        top.bind("<Configure>", lambda e: intro.config(
            wraplength=max(200, e.width - 16)))

        # ---- Target address + Scan ----
        scan_row = tk.Frame(top, bg=BG)
        scan_row.pack(fill=tk.X, padx=8, pady=(0, 4))
        scan_row.columnconfigure(1, weight=1)
        self._label(scan_row, "Target address:", fg=MUTED, width=14,
                    anchor="w").grid(row=0, column=0, sticky="w")
        self.chain_target_var = tk.StringVar()
        self._entry(scan_row, self.chain_target_var, width=18).grid(
            row=0, column=1, sticky="ew", padx=(0, 6))
        self.chain_scan_btn = self._button(scan_row, "Scan", self.on_chain_scan)
        self.chain_scan_btn.grid(row=0, column=2, sticky="e")

        opts = tk.Frame(top, bg=BG)
        opts.pack(fill=tk.X, padx=8, pady=(0, 4))
        self._label(opts, "Max offset:", fg=MUTED).pack(side=tk.LEFT)
        self.chain_max_offset = tk.StringVar(value="4096")
        self._entry(opts, self.chain_max_offset, width=8).pack(
            side=tk.LEFT, padx=(4, 12))
        self._label(opts, "Max level:", fg=MUTED).pack(side=tk.LEFT)
        self.chain_max_level = tk.StringVar(value="5")
        self._entry(opts, self.chain_max_level, width=4).pack(
            side=tk.LEFT, padx=(4, 0))
        # Level 4-5 chains route through several dynamic (heap/stack) hops
        # before ever reaching a static base -- each hop is a fresh chance
        # for the value to move across a restart, so they're far less likely
        # to survive one than a 1-2 level chain. Default to hiding them so
        # the table isn't dominated by results that are unlikely to be
        # useful; the checkbox reveals them on demand without re-scanning.
        self.chain_show_deep_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            opts, text="Show level 3-5 chains", variable=self.chain_show_deep_var,
            command=self._refresh_chain_tree, bg=BG, fg=MUTED, selectcolor=BG3,
            activebackground=BG, activeforeground=MUTED, font=(FONT, 8)
        ).pack(side=tk.LEFT, padx=(16, 0))

        # ---- Progress (packed only while a scan is running) ----
        self.chain_progress_var = tk.DoubleVar(value=0)
        self.chain_progress = ttk.Progressbar(
            top, orient="horizontal", mode="determinate", maximum=100,
            variable=self.chain_progress_var, style="GG.Horizontal.TProgressbar")

        self.chain_status = tk.StringVar(value="Not scanned yet.")
        self._chain_status_label = self._label(
            top, "", fg=MUTED, textvariable=self.chain_status,
            wraplength=700, justify="left")
        self._chain_status_label.pack(anchor="w", padx=8, pady=(2, 4))
        top.bind("<Configure>", lambda e: self._chain_status_label.config(
            wraplength=max(200, e.width - 16)), add="+")

        # ---- Results table ----
        list_frame = tk.Frame(top, bg=BG)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 4))
        sb = tk.Scrollbar(list_frame)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.chain_tree = ttk.Treeview(
            list_frame,
            columns=("mark", "module", "base_offset", "offset_chain",
                     "levels", "status"),
            show="headings", height=12, yscrollcommand=sb.set)
        for key, text, width, anchor, stretch in (
            ("mark", "Mark", 50, "center", False),
            ("module", "Module", 140, "w", False),
            ("base_offset", "Base Offset", 100, "w", False),
            ("offset_chain", "Offset Chain", 220, "w", True),
            ("levels", "Levels", 55, "center", False),
            ("status", "Status", 200, "w", True),
        ):
            self.chain_tree.heading(key, text=text, anchor=anchor)
            self.chain_tree.column(key, width=width, anchor=anchor,
                                   stretch=stretch)
        self.chain_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.config(command=self.chain_tree.yview)
        self.chain_tree.bind("<Button-1>", self._on_chain_tree_click)
        self.chain_tree.bind("<Double-1>", self._on_chain_tree_double_click)
        self.chain_tree.tag_configure("survived", foreground=GREEN)
        self.chain_tree.tag_configure("failed", foreground=RED)
        self.chain_tree.tag_configure("unknown", foreground=AMBER)

        # ---- Use a chain as a new mod ----
        use_row = tk.Frame(top, bg=BG)
        use_row.pack(fill=tk.X, padx=8, pady=(0, 4))
        self._button(use_row, "Use for New Mod",
                     self.on_chain_use_for_new_mod).pack(side=tk.LEFT)
        self._label(
            use_row, "Select a row above, then click here (or double-click "
            "the row) to pre-fill a pointer_chain mod in Add Mod.",
            fg=MUTED, font=(FONT, 8)).pack(side=tk.LEFT, padx=(8, 0))

        # ---- Re-resolve after restart ----
        verify_row = tk.Frame(top, bg=BG)
        verify_row.pack(fill=tk.X, padx=8, pady=(0, 8))
        verify_row.columnconfigure(1, weight=1)
        self._label(verify_row, "Expected address (optional):", fg=MUTED,
                    anchor="w").grid(row=0, column=0, sticky="w")
        self.chain_expected_var = tk.StringVar()
        self._entry(verify_row, self.chain_expected_var, width=18).grid(
            row=0, column=1, sticky="w", padx=(6, 6))
        self._button(verify_row, "Re-resolve after restart",
                     self.on_chain_reresolve).grid(row=0, column=2, sticky="e")
        self._label(
            verify_row, "Mark chains (checkbox) to verify, then re-resolve "
            "after restarting the game -- compare against a known-good "
            "address, or leave it blank to just eyeball the resolved "
            "address.", fg=MUTED, font=(FONT, 8), wraplength=700,
            justify="left").grid(row=1, column=0, columnspan=3, sticky="w",
                                 pady=(4, 0))

    # ---- Pointer Chains: scan --------------------------------------------
    def on_chain_scan(self):
        if not self.engine.is_attached():
            self.chain_status.set("Attach to a game first.")
            return
        raw = self.chain_target_var.get().strip()
        try:
            target_address = int(raw, 16)
        except ValueError:
            self.chain_status.set(
                "Enter a valid hex target address (e.g. 16EDAFC8).")
            return
        try:
            max_offset = int(self.chain_max_offset.get().strip() or "4096", 0)
        except ValueError:
            max_offset = 4096
        try:
            max_level = int(self.chain_max_level.get().strip() or "5", 0)
        except ValueError:
            max_level = 5

        self.chain_tree.delete(*self.chain_tree.get_children())
        self._chain_rows = {}
        self.chain_scan_btn.config(state=tk.DISABLED)
        self.chain_progress_var.set(0)
        if not self.chain_progress.winfo_manager():
            self.chain_progress.pack(fill=tk.X, padx=8, pady=(0, 2),
                                     before=self._chain_status_label)
        self.chain_status.set(
            "Building pointer snapshot (walks ALL committed memory -- can "
            "take a minute or more on a large process)...")

        def _progress(done, total):
            pct = (done / total * 100.0) if total else 0
            self.root.after(0, lambda: self._chain_progress_update(pct, done, total))

        def _chain_progress(level, calls_made, chains_found):
            if calls_made % 25 != 0:
                return   # throttle -- this can fire thousands of times
            self.root.after(0, lambda: self.chain_status.set(
                "Searching for chains... level {}, {} branch(es) explored, "
                "{} chain(s) found so far.".format(level, calls_made,
                                                   chains_found)))

        def _worker():
            snap = self.pointer_finder.build_pointer_snapshot(
                progress_callback=_progress)
            if "error" in snap:
                self.root.after(0, lambda: self._chain_scan_failed(snap["error"]))
                return
            res = self.pointer_finder.find_pointer_chains(
                snap, target_address, max_offset=max_offset,
                max_level=max_level, progress_callback=_chain_progress)
            self.root.after(0, lambda: self._chain_scan_done(snap, res, max_level))

        threading.Thread(target=_worker, daemon=True).start()

    def _chain_progress_update(self, pct, done, total):
        self.chain_progress_var.set(pct)
        self.chain_status.set(
            "Building pointer snapshot... {:.0f}% ({}/{} bytes)".format(
                pct, done, total))

    def _chain_scan_failed(self, message):
        self.chain_progress.pack_forget()
        self.chain_scan_btn.config(state=tk.NORMAL)
        self.chain_status.set(message)

    def _chain_scan_done(self, snapshot, res, max_level):
        self._chain_snapshot = snapshot
        self.chain_progress_var.set(100)
        self.chain_progress.pack_forget()
        self.chain_scan_btn.config(state=tk.NORMAL)
        if "error" in res:
            self.chain_status.set(res["error"])
            return

        chains = res["chains"]
        self._chain_order = []
        for i, chain in enumerate(chains):
            iid = "c{}".format(i)
            self._chain_order.append(iid)
            self._chain_rows[iid] = dict(chain, marked=False, status_text="",
                                         status_tag=None)
        self._refresh_chain_tree()

        # Plain-language status: distinguish "nothing within max_level" from
        # "capped -- value too common" (the two failure modes read very
        # differently to the user: one says "give up on a short chain", the
        # other says "try a smaller max offset").
        if chains:
            msg = "{} chain(s) found ({} snapshot slot(s)).".format(
                len(chains), snapshot.get("count", 0))
            if res.get("any_capped"):
                msg += (" Note: at least one branch was capped (a common "
                       "value) -- there may be more chains a smaller max "
                       "offset would reveal.")
            if res.get("capped_total_calls") or res.get("capped_time"):
                msg += (" Search stopped early ({}) -- there may be more "
                       "chains a smaller max offset or lower max level "
                       "would reveal faster.".format(
                           "time budget reached" if res.get("capped_time")
                           else "max branches explored"))
        elif res.get("capped_total_calls") or res.get("capped_time"):
            msg = ("Search capped ({}) without finding a static chain -- "
                  "the search space is too wide (likely a common value "
                  "with lots of incidental hits). Try a smaller max offset "
                  "or a lower max level.".format(
                      "time budget reached" if res.get("capped_time")
                      else "max branches explored"))
        elif res.get("any_capped"):
            msg = ("Scan capped -- a value along the search path is too "
                  "common (e.g. 0 or a small int) to search exhaustively. "
                  "Try a smaller max offset.")
        else:
            msg = ("No static chain found within {} level(s) -- this "
                  "address may only be reachable through a live "
                  "pointer_capture hook, not a short static chain.").format(
                max_level)
        self.chain_status.set(msg)

    def _refresh_chain_tree(self):
        """Repopulate the tree from self._chain_rows, applying the "Show
        level 3-5 chains" filter. Marked/status state lives on the row dict
        (not just the tree widget) so toggling the filter never loses a
        mark or a re-resolve result on a chain that's temporarily hidden --
        it just re-renders. A 4-5 level chain crosses several dynamic
        (heap/stack) hops before reaching a static base, and each hop is
        another chance for the value to move across a restart, so they're
        hidden by default to keep the table focused on the chains most
        likely to actually survive one."""
        if not hasattr(self, "_chain_order"):
            return
        self.chain_tree.delete(*self.chain_tree.get_children())
        show_deep = self.chain_show_deep_var.get()
        for iid in self._chain_order:
            entry = self._chain_rows.get(iid)
            if entry is None:
                continue
            if not show_deep and entry["level_count"] >= 3:
                continue
            mod_label = entry["module"] or "main module"
            offs = ", ".join("0x{:X}".format(o) for o in entry["offset_chain"])
            tags = (entry["status_tag"],) if entry["status_tag"] else ()
            self.chain_tree.insert(
                "", "end", iid=iid, tags=tags,
                values=("☑" if entry["marked"] else "☐", mod_label,
                        "0x{:X}".format(entry["base_offset"]), offs,
                        entry["level_count"], entry["status_text"]))

    # ---- Pointer Chains: mark for verification -----------------------------
    def _on_chain_tree_click(self, event):
        if self.chain_tree.identify("region", event.x, event.y) != "cell":
            return
        col_id = self.chain_tree.identify_column(event.x)
        row = self.chain_tree.identify_row(event.y)
        if not row or col_id != "#1":   # "mark" is the first column
            return
        entry = self._chain_rows.get(row)
        if entry is None:
            return
        entry["marked"] = not entry["marked"]
        self.chain_tree.set(row, "mark", "☑" if entry["marked"] else "☐")

    # ---- Pointer Chains: send a chain to Add Mod --------------------------
    def _on_chain_tree_double_click(self, event):
        """Double-clicking a chain row is a shortcut for "Use for New Mod" --
        skips the mark-column toggle since double-click always lands on
        whichever cell the pointer is over, not necessarily the mark column."""
        row = self.chain_tree.identify_row(event.y)
        if row:
            self.chain_tree.selection_set(row)
            self.on_chain_use_for_new_mod()

    def on_chain_use_for_new_mod(self):
        """Send the selected chain's (module, base_offset, offset_chain) into
        a fresh pointer_chain mod in the Add Mod tab -- mirrors
        _send_address_to_add_mod's navigation pattern for the other tools.
        Only fills the chain-identifying fields; name and write-behavior
        (poll_mode/value) are left for the user to fill in, same as
        pointer_capture's "From address..." leaves the register editable."""
        sel = self.chain_tree.selection()
        if not sel:
            self.chain_status.set(
                "Select a chain row first (click it), then Use for New Mod.")
            return
        entry = self._chain_rows.get(sel[0])
        if entry is None:
            return
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.notebook.select(1)   # Add Mod tab
        self.form_vars["template"].set("pointer_chain")
        self._refresh_form_fields()
        self.form_vars["module"].set(entry["module"] or "")
        self.form_vars["base_offset"].set("{:X}".format(entry["base_offset"]))
        self.form_vars["offset_chain"].set(
            ", ".join("{:X}".format(o) for o in entry["offset_chain"]))
        self.log(
            "Pointer Chains: sent chain to Add Mod (module={}, "
            "base_offset=0x{:X}, offset_chain=[{}]) -- set a name and "
            "write-behavior fields, then Save Mod.".format(
                entry["module"] or "main module", entry["base_offset"],
                ", ".join("0x{:X}".format(o) for o in entry["offset_chain"])))

    # ---- Pointer Chains: restart verification ------------------------------
    def on_chain_reresolve(self):
        """Re-resolve every marked chain from scratch against whatever
        process is CURRENTLY attached (meant to be run after restarting the
        game) and compare against an optional expected address. Read-only:
        resolve_chain only reads memory, never writes."""
        if not self.engine.is_attached():
            self.chain_status.set(
                "Attach to the (possibly restarted) game first.")
            return
        marked = [(iid, entry) for iid, entry in self._chain_rows.items()
                 if entry.get("marked")]
        if not marked:
            self.chain_status.set(
                "Mark at least one chain (checkbox in the Mark column) to "
                "re-resolve.")
            return

        expected_raw = self.chain_expected_var.get().strip()
        expected = None
        if expected_raw:
            try:
                expected = int(expected_raw, 16)
            except ValueError:
                self.chain_status.set(
                    "Expected address must be valid hex, or leave it blank.")
                return

        survived = failed = 0
        for iid, entry in marked:
            res = self.pointer_finder.resolve_chain(
                entry["module"], entry["base_offset"], entry["offset_chain"])
            if "error" in res:
                entry["status_text"] = res["error"]
                entry["status_tag"] = "failed"
                failed += 1
            else:
                addr = res["address"]
                if expected is None:
                    entry["status_text"] = "0x{:X} (no expected value given)".format(addr)
                    entry["status_tag"] = "unknown"
                elif addr == expected:
                    entry["status_text"] = "Survived: 0x{:X}".format(addr)
                    entry["status_tag"] = "survived"
                    survived += 1
                else:
                    entry["status_text"] = "Failed: 0x{:X} (expected 0x{:X})".format(
                        addr, expected)
                    entry["status_tag"] = "failed"
                    failed += 1
            if self.chain_tree.exists(iid):
                self.chain_tree.set(iid, "status", entry["status_text"])
                self.chain_tree.item(iid, tags=(entry["status_tag"],))

        if expected is None:
            self.chain_status.set(
                "Re-resolved {} marked chain(s) -- compare the resolved "
                "address(es) manually (no expected value given).".format(
                    len(marked)))
        else:
            self.chain_status.set(
                "Re-resolved {} marked chain(s): {} survived, {} failed."
                .format(len(marked), survived, failed))

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

        # hard_freeze's AOB gets its own AOB-building tools. "From address..."
        # now calls build_hard_freeze_candidate_from_address (the real,
        # verified fix -- see its docstring in engine.py): it fills AOB,
        # Offset, AND NOP length together by locating the target instruction
        # and walking backward for uniqueness. "Auto" is intentionally
        # blocked here: compute_min_steal (pointer_capture's steal-length
        # calculator) answers "how many bytes for a jmp hook," which has no
        # relationship to hard_freeze's Offset field and was confirmed to
        # produce wrong values against a real hand-built mod (POP_test.json's
        # "Infinite Rewind": correct offset=11, but compute_min_steal gave 7
        # or 17). hard_freeze has no register/module/struct_offset concept,
        # so "Paste line..." still can't usefully fill anything for it beyond
        # the AOB itself (offset_var=None, same no-op as before).
        self.form_vars["offset"] = tk.StringVar()
        self._hf_aob_entry = {
            "aob": self.form_vars["aob"],
            # Throwaway: _paste_line always sets entry["register"] (it's used
            # by pointer_capture's hook rows), but hard_freeze has no register
            # concept and never reads this var back.
            "register": tk.StringVar(value=""),
        }
        aob_tools = tk.Frame(self.form, bg=BG)
        aob_tools.grid(row=self._row_index, column=0, sticky="w", pady=(0, 2))
        self._label(aob_tools, "", width=20, anchor="w").pack(side=tk.LEFT)
        self._button(
            aob_tools, "From address…",
            lambda: self._from_address_hard_freeze(self._hf_aob_entry),
            font=(FONT, 8, "bold")).pack(side=tk.LEFT, padx=(0, 4))
        self._button(
            aob_tools, "Paste line…",
            lambda: self._paste_line(self._hf_aob_entry, offset_var=None),
            font=(FONT, 8, "bold")).pack(side=tk.LEFT, padx=(0, 4))
        self._button(
            aob_tools, "Auto", self._hf_auto_blocked,
            font=(FONT, 8, "bold")).pack(side=tk.LEFT)
        self.form_rows["aob_tools"] = aob_tools
        self._row_index += 1

        # hard_freeze fields
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

        # pointer_chain: module_base + base_offset, then walk offset_chain via
        # live reads. No hooks/cave at all -- resolves straight to the target
        # address every poll tick. Same write-behavior fields (poll_mode,
        # value) as pointer_capture, shared below.
        self.form_vars["module"] = tk.StringVar()
        self._add_form_row("module", "Module (blank = main exe)",
                           entry(self.form_vars["module"], width=24))

        self.form_vars["base_offset"] = tk.StringVar()
        self._add_form_row("base_offset", "Base offset (hex)",
                           entry(self.form_vars["base_offset"]))

        self.form_vars["offset_chain"] = tk.StringVar()
        self._add_form_row(
            "offset_chain", "Offset chain (hex, comma-separated)",
            entry(self.form_vars["offset_chain"], width=30))
        chain_tools = tk.Frame(self.form, bg=BG)
        chain_tools.grid(row=self._row_index, column=0, sticky="w", pady=(0, 2))
        self._label(chain_tools, "", width=20, anchor="w").pack(side=tk.LEFT)
        self._button(
            chain_tools, "Open Pointer Chains…",
            self.open_pointerchains_window,
            font=(FONT, 8, "bold")).pack(side=tk.LEFT)
        self._label(
            chain_tools, "find a chain there, then \"Use for New Mod\".",
            fg=MUTED, font=(FONT, 8)).pack(side=tk.LEFT, padx=(6, 0))
        self.form_rows["chain_tools"] = chain_tools
        self._row_index += 1

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
            # jmp type used for the Auto steal computation (rel32 -> 5-byte min,
            # absolute -> 14-byte min). Advisory only; not stored in JSON.
            "jmp_type": tk.StringVar(value="rel32"),
            "suggest": tk.StringVar(value=""),   # inline suggestion note
        }

        # Row 1: AOB label + entry, stretching to the full row width.
        line1 = tk.Frame(row, bg=BG2)
        line1.pack(fill=tk.X, padx=4, pady=(3, 0))
        self._label(line1, "AOB:", fg=MUTED, bg=BG2, width=5, anchor="w").pack(side=tk.LEFT)
        tk.Entry(line1, textvariable=entry_vars["aob"], bg=BG3, fg=FG,
                 insertbackground=FG, relief=tk.SOLID, bd=1,
                 highlightthickness=1, highlightbackground=BORDER,
                 highlightcolor=ACCENT).pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Row 1b: the two fill-from actions on their own line, in a 2-column
        # grid with equal weight, so they always share the available width
        # evenly and never clip or overlap regardless of the panel's width.
        line1b = tk.Frame(row, bg=BG2)
        line1b.pack(fill=tk.X, padx=4, pady=(3, 0))
        line1b.columnconfigure(0, weight=1)
        line1b.columnconfigure(1, weight=1)
        # Auto-fill AOB/register/offset from a live memory address (Cheat Engine).
        # `entry` is assigned below; the closure resolves it at click time.
        self._button(
            line1b, "From address…", lambda: self._from_address(entry),
            font=(FONT, 8, "bold"),
        ).grid(row=0, column=0, sticky="ew", padx=(0, 3))
        # Offline: paste a disassembly line to fill register/offset (no attach).
        self._button(
            line1b, "Paste line…", lambda: self._paste_line(entry),
            font=(FONT, 8, "bold"),
        ).grid(row=0, column=1, sticky="ew", padx=(3, 0))

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
        self._button(
            line2, "Auto", lambda: self._auto_steal(entry),
            font=(FONT, 8, "bold"), padx=6,
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

        # ---- line3: jmp type (for Auto steal) + advisory suggestion note ----
        line3 = tk.Frame(row, bg=BG2)
        line3.pack(fill=tk.X, padx=4, pady=(0, 3))
        self._label(line3, "jmp:", fg=MUTED, bg=BG2, width=5, anchor="w").pack(side=tk.LEFT)
        ttk.Combobox(line3, textvariable=entry_vars["jmp_type"],
                     values=["rel32", "absolute"], state="readonly",
                     width=9).pack(side=tk.LEFT)
        self._label(line3, "", fg=ACCENT, bg=BG2, font=(FONT, 8),
                    textvariable=entry_vars["suggest"], wraplength=430,
                    justify="left").pack(side=tk.LEFT, padx=(8, 0))

        self._hook_entries.append(entry)

    def _remove_hook_row(self, entry):
        entry["row"].destroy()
        if entry in self._hook_entries:
            self._hook_entries.remove(entry)

    def _clear_hook_rows(self):
        for entry in list(self._hook_entries):
            entry["row"].destroy()
        self._hook_entries = []

    def _show_jmp_suggestion(self, entry, hook_address=None):
        """Compute + display the advisory rel32/absolute suggestion for a hook.

        Advisory only — the real decision is _alloc_cave's automatic fallback at
        apply time. Needs attach to know the module size/bitness."""
        if not self.engine.is_attached():
            entry["suggest"].set("")
            return None
        module = entry["module"].get().strip() or None
        sug = self.engine.suggest_jmp_type(hook_address=hook_address,
                                           module_name=module)
        entry["suggest"].set("Suggested: {} — {}".format(
            sug["jmp_type"], sug["reason"]))
        return sug

    def _auto_steal(self, entry):
        """Fill a hook's steal field with the capstone-computed minimum for the
        selected jmp type (rel32 -> 5-byte, absolute -> 14-byte threshold).

        Needs an attached process so the disassembler matches the game's
        bitness (x86/x64).
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
        # Advisory jmp-type suggestion (does not override the user's choice).
        self._show_jmp_suggestion(entry)
        jmp_type = entry["jmp_type"].get() or "rel32"
        pattern, mask = self.engine._parse_aob(aob_text)
        try:
            steal = self.engine.compute_min_steal(pattern, jmp_type=jmp_type)
        except engine.InsufficientBytesForJmpError as ex:
            self.form_error_var.set("Auto-steal ({}): {}".format(jmp_type, ex))
            return
        except Exception as ex:                     # e.g. capstone missing
            self.form_error_var.set("Auto-steal failed: {}".format(ex))
            return
        entry["hook_offset"].set(str(steal))
        msg = "Auto-steal: {} byte(s) (instruction-aligned, {}).".format(
            steal, jmp_type)
        # Wildcards inside the stolen region can make the decode unreliable.
        if not all(mask[:steal]):
            msg += "  WARNING: AOB has wildcards within the steal region — verify."
            self.form_error_var.set(
                "Auto-steal used {}, but this AOB has wildcards in the steal "
                "region — double-check it disassembled correctly.".format(steal))
        self.log(msg)

    def _from_address(self, entry, prefill_address=None, auto_read=False,
                      offset_var=_USE_STRUCT_OFFSET):
        """Dialog: read a live address, disassemble, and auto-fill the hook.

        Reuses engine.build_candidate_from_address (capstone + scan_aob). The
        user reviews the decoded instructions + match count, then fills the
        AOB / register / struct_offset / module fields (all stay editable).

        `prefill_address` (e.g. from the Watch List's "Use in Add Mod") only
        fills the Address field -- it's a navigation convenience and does not
        change any matching/auto-fill behaviour below. `auto_read` additionally
        presses Read for you (used by Find Writes, where the address came from
        a captured hit so there is nothing for the user to type). Neither flag
        auto-fills the form: "Fill fields" stays an explicit click.

        `offset_var`: which StringVar receives the decoded struct_offset (the
        memory operand's displacement, e.g. the "18" in "mov [ebx+18],edi").
        Defaults to self.form_vars["struct_offset"] (pointer_capture's hook
        rows). hard_freeze has no struct_offset concept -- its "Offset" field
        is a different quantity (a byte position within the AOB match,
        computed by the Auto-steal button, not this dialog) -- so the wiring
        for hard_freeze's AOB row passes offset_var=None to skip filling any
        offset field at all, leaving only the AOB copied over.
        """
        resolved_offset_var = (self.form_vars["struct_offset"]
                               if offset_var is _USE_STRUCT_OFFSET else offset_var)
        self.form_error_var.set("")
        if not self.engine.is_attached():
            self.form_error_var.set(
                "Attach to the game first — 'From address' reads live memory.")
            return

        top = self._dialog("Build hook from address", minsize=(520, 380))

        head = tk.Frame(top, bg=BG)
        head.pack(fill=tk.X, padx=12, pady=(12, 4))
        head.columnconfigure(1, weight=1)
        self._label(head, "Address (hex, from Cheat Engine):", fg=MUTED).grid(
            row=0, column=0, sticky="w")
        addr_var = tk.StringVar(value=prefill_address or "")
        addr_entry = self._entry(head, addr_var, width=18)
        addr_entry.grid(row=0, column=1, sticky="ew", padx=6)
        addr_entry.focus_set()
        if prefill_address:
            addr_entry.select_range(0, tk.END)   # pre-filled + selected, ready to Read
        read_btn = self._button(head, "Read", lambda: _read())
        read_btn.grid(row=0, column=2, sticky="e")

        result_txt = tk.Text(top, width=64, height=12, bg=BG3, fg=FG, relief=tk.FLAT,
                             wrap=tk.NONE, font=(MONO, 9), state=tk.DISABLED)
        result_txt.pack(fill=tk.BOTH, expand=True, padx=12, pady=4)
        result_txt.tag_configure("ok", foreground=GREEN)
        result_txt.tag_configure("warn", foreground=AMBER)
        result_txt.tag_configure("err", foreground=RED)

        # Holds the last successful candidate so "Fill fields" can apply it.
        state = {"candidate": None}

        def _put(text="", tag=None):
            result_txt.config(state=tk.NORMAL)
            result_txt.insert(tk.END, text + "\n", (tag,) if tag else ())
            result_txt.config(state=tk.DISABLED)

        def _read():
            result_txt.config(state=tk.NORMAL); result_txt.delete("1.0", tk.END)
            result_txt.config(state=tk.DISABLED)
            state["candidate"] = None
            fill_btn.config(state=tk.DISABLED)
            raw = addr_var.get().strip()
            try:
                address = int(raw, 16)   # CE addresses are hex (0x optional)
            except ValueError:
                _put("Enter a valid hex address (e.g. 16BBAE38).", "err")
                return
            res = self.engine.build_candidate_from_address(address)
            if "error" in res:
                _put(res["error"], "err")
                return
            m = res.get("matches")
            mtag = "ok" if m == 1 else "warn"
            _put("AOB     : {}".format(res["aob"]))
            _put("matches : {}".format(m), mtag)
            _put("module  : {}".format(res.get("module") or "main module"))
            reg, off = res.get("capture_register"), res.get("struct_offset")
            if reg and off is not None:
                _put("register: {}    struct_offset: {} (hex)".format(reg, off), "ok")
            else:
                _put("register/offset: not auto-filled — {}".format(
                    res.get("reason", "")), "warn")
            # Advisory jmp-type suggestion for this address/module.
            sug = self.engine.suggest_jmp_type(
                hook_address=address, module_name=res.get("module"))
            state["suggest"] = sug
            _put("jmp type: suggested {} — {}".format(
                sug["jmp_type"], sug["reason"]),
                "ok" if sug["jmp_type"] == "rel32" else "warn")
            _put("")
            _put("decoded instructions:")
            for ins in res.get("instructions", []):
                _put("  {}  {:<22} {}".format(
                    ins["address"], ins["bytes"], ins["text"]))
            if m != 1:
                _put("")
                _put("Not unique yet — you can extend/edit the AOB after filling.",
                     "warn")
            state["candidate"] = res
            fill_btn.config(state=tk.NORMAL)

        def _fill():
            res = state["candidate"]
            if not res:
                return
            entry["aob"].set(res["aob"])
            if res.get("capture_register"):
                entry["register"].set(res["capture_register"])
            if res.get("module"):
                entry["module"].set(res["module"])
            filled_offset = False
            if resolved_offset_var is not None and res.get("struct_offset") is not None:
                resolved_offset_var.set(res["struct_offset"])
                filled_offset = True
            # Pre-select + surface the advisory jmp type (user can still change).
            sug = state.get("suggest")
            if sug:
                entry["jmp_type"].set(sug["jmp_type"])
                entry["suggest"].set("Suggested: {} — {}".format(
                    sug["jmp_type"], sug["reason"]))
            self.log(
                "From address: filled AOB ({} match(es)){}{}.".format(
                    res.get("matches"),
                    ", reg=" + res["capture_register"] if res.get("capture_register") else "",
                    ", offset=" + res["struct_offset"] if filled_offset else "",
                ))
            top.destroy()

        btns = tk.Frame(top, bg=BG)
        btns.pack(fill=tk.X, padx=12, pady=(0, 12))
        addr_entry.bind("<Return>", lambda _e: _read())
        fill_btn = self._button(btns, "Fill fields", _fill)
        fill_btn.pack(side=tk.LEFT)
        fill_btn.config(state=tk.DISABLED)
        self._button_ghost(btns, "Cancel", top.destroy).pack(side=tk.LEFT, padx=(6, 0))

        # Deferred so _read() runs against a fully built dialog (it enables
        # fill_btn, which only exists once the lines above have executed).
        if auto_read and prefill_address:
            top.after(0, _read)

    def _from_address_hard_freeze(self, entry, prefill_address=None, auto_read=False):
        """Dialog: read a live address and auto-fill hard_freeze's AOB/Offset/
        NOP length -- the real automated fix for hard_freeze (see
        engine.build_hard_freeze_candidate_from_address's docstring for why
        this needed its own function rather than reusing compute_min_steal,
        which answers a different question -- jmp-hook steal size, not "where
        is the instruction I want to freeze/NOP").

        Deliberately separate from _from_address (pointer_capture's dialog,
        left untouched) rather than branching inside it, since the two
        engine calls return differently-shaped results (offset/nop_len here
        vs capture_register/struct_offset there) and this is purely additive.
        Same interaction pattern as _from_address: Read shows the decoded
        AOB/offset/instructions, Fill fields applies them to the form.
        """
        self.form_error_var.set("")
        if not self.engine.is_attached():
            self.form_error_var.set(
                "Attach to the game first — 'From address' reads live memory.")
            return

        top = self._dialog("Build hard_freeze target from address",
                           minsize=(520, 380))

        head = tk.Frame(top, bg=BG)
        head.pack(fill=tk.X, padx=12, pady=(12, 4))
        head.columnconfigure(1, weight=1)
        self._label(head, "Address (hex, from Cheat Engine):", fg=MUTED).grid(
            row=0, column=0, sticky="w")
        addr_var = tk.StringVar(value=prefill_address or "")
        addr_entry = self._entry(head, addr_var, width=18)
        addr_entry.grid(row=0, column=1, sticky="ew", padx=6)
        addr_entry.focus_set()
        if prefill_address:
            addr_entry.select_range(0, tk.END)   # pre-filled + selected, ready to Read
        read_btn = self._button(head, "Read", lambda: _read())
        read_btn.grid(row=0, column=2, sticky="e")

        result_txt = tk.Text(top, width=64, height=12, bg=BG3, fg=FG, relief=tk.FLAT,
                             wrap=tk.NONE, font=(MONO, 9), state=tk.DISABLED)
        result_txt.pack(fill=tk.BOTH, expand=True, padx=12, pady=4)
        result_txt.tag_configure("ok", foreground=GREEN)
        result_txt.tag_configure("warn", foreground=AMBER)
        result_txt.tag_configure("err", foreground=RED)

        state = {"candidate": None}

        def _put(text="", tag=None):
            result_txt.config(state=tk.NORMAL)
            result_txt.insert(tk.END, text + "\n", (tag,) if tag else ())
            result_txt.config(state=tk.DISABLED)

        def _read():
            result_txt.config(state=tk.NORMAL); result_txt.delete("1.0", tk.END)
            result_txt.config(state=tk.DISABLED)
            state["candidate"] = None
            fill_btn.config(state=tk.DISABLED)
            raw = addr_var.get().strip()
            try:
                address = int(raw, 16)   # CE addresses are hex (0x optional)
            except ValueError:
                _put("Enter a valid hex address (e.g. 16BBAE38).", "err")
                return
            try:
                res = self.engine.build_hard_freeze_candidate_from_address(address)
            except engine.HardFreezeUniquenessError as ex:
                _put(str(ex), "err")
                return
            if "error" in res:
                _put(res["error"], "err")
                return
            _put("AOB     : {}".format(res["aob"]))
            _put("matches : {}".format(res["matches"]), "ok")  # always 1 or it raised
            _put("module  : {}".format(res.get("module") or "main module"))
            _put("offset  : {}   nop_len: {}".format(
                res["offset"], res["nop_len"]), "ok")
            _put("")
            _put("decoded instructions (context, ending with the target):")
            for ins in res.get("instructions", []):
                _put("  {}  {:<22} {}".format(
                    ins["address"], ins["bytes"], ins["text"]))
            state["candidate"] = res
            fill_btn.config(state=tk.NORMAL)

        def _fill():
            res = state["candidate"]
            if not res:
                return
            entry["aob"].set(res["aob"])
            self.form_vars["offset"].set(str(res["offset"]))
            self.form_vars["nop_len"].set(str(res["nop_len"]))
            self.log(
                "From address (hard_freeze): filled AOB (1 match), offset={}, "
                "nop_len={}.".format(res["offset"], res["nop_len"]))
            top.destroy()

        btns = tk.Frame(top, bg=BG)
        btns.pack(fill=tk.X, padx=12, pady=(0, 12))
        addr_entry.bind("<Return>", lambda _e: _read())
        fill_btn = self._button(btns, "Fill fields", _fill)
        fill_btn.pack(side=tk.LEFT)
        fill_btn.config(state=tk.DISABLED)
        self._button_ghost(btns, "Cancel", top.destroy).pack(side=tk.LEFT, padx=(6, 0))

        if auto_read and prefill_address:
            top.after(0, _read)

    def _hf_auto_blocked(self):
        """hard_freeze's "Auto" button: compute_min_steal answers a jmp-hook
        question (pointer_capture's steal length), not "where is the
        instruction I want to freeze/NOP" -- there is no relationship between
        the two, so it must never write into hard_freeze's Offset field (see
        engine.build_hard_freeze_candidate_from_address's docstring, and the
        POP_test.json "Infinite Rewind" mismatch that surfaced this: correct
        offset=11/nop_len=6, but compute_min_steal gave 7 (rel32) or 17
        (absolute) -- neither matches). Use "From address..." instead, which
        now calls the real fix."""
        self.form_error_var.set(
            "Auto-steal doesn't apply to hard_freeze — use From address... "
            "to auto-locate the target instruction instead.")

    def _paste_line(self, entry, offset_var=_USE_STRUCT_OFFSET):
        """Offline dialog: paste a disassembly line, fill register + offset.

        Pure text parse (engine.parse_disasm_line) — needs no attached process.
        Does NOT touch the AOB (supply that via CE / From address / by hand).

        `offset_var`: see _from_address -- defaults to
        self.form_vars["struct_offset"]; pass None (as hard_freeze's AOB row
        does) to skip filling any offset field, since hard_freeze's "Offset"
        is a different quantity than a struct-member displacement."""
        resolved_offset_var = (self.form_vars["struct_offset"]
                               if offset_var is _USE_STRUCT_OFFSET else offset_var)
        top = self._dialog("Paste disassembly line", minsize=(460, 280))

        self._label(top, "Paste a line from Cheat Engine (e.g. "
                    "\"0053FBBA - 89 7B 18 - mov [ebx+18],edi\"):",
                    fg=MUTED, wraplength=440, justify="left").pack(
                        anchor="w", padx=12, pady=(12, 4))
        line_var = tk.StringVar()
        line_entry = self._entry(top, line_var, width=60)
        line_entry.pack(fill=tk.X, padx=12)
        line_entry.focus_set()

        result_txt = tk.Text(top, width=60, height=6, bg=BG3, fg=FG, relief=tk.FLAT,
                             wrap=tk.WORD, font=(MONO, 9), state=tk.DISABLED)
        result_txt.pack(fill=tk.BOTH, expand=True, padx=12, pady=6)
        result_txt.tag_configure("ok", foreground=GREEN)
        result_txt.tag_configure("warn", foreground=AMBER)

        state = {"parsed": None}

        def _put(text="", tag=None):
            result_txt.config(state=tk.NORMAL)
            result_txt.insert(tk.END, text + "\n", (tag,) if tag else ())
            result_txt.config(state=tk.DISABLED)

        def _parse():
            result_txt.config(state=tk.NORMAL); result_txt.delete("1.0", tk.END)
            result_txt.config(state=tk.DISABLED)
            state["parsed"] = None
            apply_btn.config(state=tk.DISABLED)
            res = engine.parse_disasm_line(line_var.get())
            if res.get("mnemonic"):
                _put("mnemonic       : {}".format(res["mnemonic"]))
            if res.get("memory_operand"):
                _put("memory operand : {}".format(res["memory_operand"]))
            reg, off = res.get("capture_register"), res.get("struct_offset")
            if reg and off is not None:
                _put("register       : {}".format(reg), "ok")
                _put("struct_offset  : {} (hex)".format(off), "ok")
                state["parsed"] = res
                apply_btn.config(state=tk.NORMAL)
            else:
                _put("could not fill  : {}".format(res.get("reason", "")), "warn")

        def _apply():
            res = state["parsed"]
            if not res:
                return
            entry["register"].set(res["capture_register"])
            filled_offset = False
            if resolved_offset_var is not None:
                resolved_offset_var.set(res["struct_offset"])
                filled_offset = True
            self.log("Paste line: register={}{} (AOB unchanged).".format(
                res["capture_register"],
                ", struct_offset={}".format(res["struct_offset"])
                if filled_offset else ""))
            top.destroy()

        btns = tk.Frame(top, bg=BG)
        btns.pack(fill=tk.X, padx=12, pady=(0, 12))
        self._button(btns, "Parse", _parse).pack(side=tk.LEFT)
        line_entry.bind("<Return>", lambda _e: _parse())
        apply_btn = self._button(btns, "Apply", _apply)
        apply_btn.pack(side=tk.LEFT, padx=(6, 0))
        apply_btn.config(state=tk.DISABLED)
        self._button_ghost(btns, "Cancel", top.destroy).pack(side=tk.LEFT, padx=(6, 0))

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
        chain = template == "pointer_chain"

        visible = {"name", "template", "notes"}  # notes: optional, all templates
        if hard:
            visible |= {"aob", "aob_tools", "offset", "freeze_mode"}  # single flat AOB
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
        if chain:
            # No hooks/cave -- module_base + base_offset, walked via
            # offset_chain (every entry a real dereferenced hop). struct_offset
            # is shared with pointer_capture's row: an OPTIONAL flat field
            # displacement added on top of the chain's result with no further
            # dereference -- for when offset_chain reaches an object's base
            # and the actual field to poll sits at a fixed byte offset inside
            # it (do NOT fold that into offset_chain as an extra hop, or the
            # resolver will try to dereference the object's own data).
            visible |= {"module", "base_offset", "offset_chain", "chain_tools",
                        "struct_offset", "poll_mode"}
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

        # ---- Collapsible main Log -----------------------------------------
        self._log_open = True
        lhead = tk.Frame(frame, bg=BG)
        lhead.pack(fill=tk.X)
        self._log_toggle = tk.Button(
            lhead, text="▾ Log", command=self._toggle_log,
            bg=BG, fg=ACCENT, activebackground=BG, activeforeground=ACCENT,
            relief=tk.FLAT, cursor="hand2", font=(FONT, 10, "bold"), anchor="w",
            bd=0, padx=0,
        )
        self._log_toggle.pack(side=tk.LEFT)
        self._button_ghost(lhead, "Clear", self._clear_log).pack(side=tk.RIGHT)

        self._log_body = tk.Frame(frame, bg=BG)   # packed only when open
        self._log_body.pack(fill=tk.BOTH, expand=True)
        scrollbar = tk.Scrollbar(self._log_body)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_widget = tk.Text(
            self._log_body, height=8, bg=BG3, fg=FG, insertbackground=FG,
            font=(MONO, 9), wrap=tk.WORD, relief=tk.FLAT,
            yscrollcommand=scrollbar.set, state=tk.DISABLED,
        )
        self.log_widget.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=self.log_widget.yview)

        # ---- Collapsible "Key Input Log" (verbose per-keydown diagnostics) ----
        self._key_log_open = False
        khead = tk.Frame(frame, bg=BG)
        khead.pack(fill=tk.X, pady=(6, 0))
        # Reopening the main Log must re-pack it ABOVE this head, not at the end.
        self._klog_head = khead
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

    def _toggle_log(self):
        self._log_open = not self._log_open
        if self._log_open:
            # Re-pack above the Key Input Log head so ordering is preserved.
            self._log_body.pack(fill=tk.BOTH, expand=True,
                                before=self._klog_head)
            self._log_toggle.config(text="▾ Log")
        else:
            self._log_body.pack_forget()
            self._log_toggle.config(text="▸ Log")

    def _clear_log(self):
        self.log_widget.config(state=tk.NORMAL)
        self.log_widget.delete("1.0", tk.END)
        self.log_widget.config(state=tk.DISABLED)

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
        top = self._dialog("New Game", minsize=(360, 220))

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
        self._apply_scan_hotkeys()   # activate scan hotkeys now attached (if on)
        self._update_scan_auto()     # resume live refresh if toggle still on

    def on_detach(self):
        if not self.engine.is_attached():
            return
        # Kill any debug session FIRST: it holds a hardware breakpoint in the
        # game's threads, and those must be cleared while the process handle is
        # still valid. A debugger left attached past detach would freeze or
        # crash the game.
        self._stop_watch(reason="Stopped — detached from the game.")
        # Detach tears down all mods in the engine. Mod hotkey bindings are
        # config-scoped (registered at load), so we KEEP them: a press while
        # detached is safely ignored, and they work again on re-attach. Scan
        # hotkeys, however, need a live process, so we unregister them here and
        # re-register on re-attach (see _apply_scan_hotkeys).
        self.engine.detach()
        self.status_var.set("● Not attached")
        self.status_label.config(fg=RED)
        self.preview_ready_for = None
        self.refresh_mod_list()
        self._apply_scan_hotkeys()   # attached now False -> unregisters scan keys
        self._clear_watchlist()      # frozen writes must not survive detach
        self._update_scan_auto()     # not attached -> pauses live refresh

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
        top = self._dialog("Notes - {}".format(mod.get("name", "")),
                           minsize=(360, 220))
        self._label(top, "Notes for '{}':".format(mod.get("name")),
                    fg=ACCENT, font=(FONT, 10, "bold")).pack(anchor="w", padx=12, pady=(12, 6))
        txt = tk.Text(top, width=52, height=8, bg=BG3, fg=FG, relief=tk.FLAT,
                      wrap=tk.WORD, font=(FONT, 9))
        txt.pack(fill=tk.BOTH, expand=True, padx=12, pady=4)
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

        top = self._dialog("Edit Notes - {}".format(mod.get("name", "")),
                           minsize=(360, 220))
        self._label(top, "Notes for '{}':".format(mod.get("name")),
                    fg=ACCENT, font=(FONT, 10, "bold")).pack(anchor="w", padx=10, pady=(10, 4))
        txt = tk.Text(top, width=54, height=8, bg=BG3, fg=FG, insertbackground=FG,
                      relief=tk.SOLID, bd=1, wrap=tk.WORD, font=(FONT, 9))
        txt.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)
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
        self._button_ghost(row, "Cancel", top.destroy).pack(side=tk.LEFT, padx=4)

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
        for k in ("name", "aob", "offset", "nop_len", "struct_offset", "value",
                 "module", "base_offset", "offset_chain"):
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
        elif mod.get("template") == "pointer_chain":
            self.form_vars["module"].set(mod.get("module", "") or "")
            self.form_vars["base_offset"].set(str(mod.get("base_offset", "")))
            self.form_vars["offset_chain"].set(
                ", ".join(str(o) for o in mod.get("offset_chain") or []))
            if mod.get("struct_offset"):
                self.form_vars["struct_offset"].set(str(mod.get("struct_offset")))
            if mod.get("poll_mode") in ("clamp_min", "hard_set"):
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
            and mod.get("template") in ("pointer_capture", "pointer_chain")
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
        # Also refuse if a Value-Scanner hotkey holds this key (both systems
        # share one HotkeyManager; don't let them steal each other's key).
        scan_owner = next(
            (SCAN_HOTKEY_LABELS[a] for a, k in self._scan_bound.items() if k == key),
            None)
        if scan_owner is not None:
            self.log(
                "Hotkey conflict: '{}' is already used by scan hotkey '{}'. "
                "Pick a different key.".format(key, scan_owner)
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
        # unregister_all() tears down the shared hook and EVERY registration,
        # including our scan hotkeys, so forget which scan keys were live and
        # re-apply them after the mod bindings below.
        self.hotkeys.unregister_all()
        self._bound.clear()
        self._scan_registered = set()
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
        # Re-register scan hotkeys on top (mods take priority for a shared key).
        self._apply_scan_hotkeys()

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
        elif result.get("resolved_address") is not None or mod.get("template") == "pointer_chain":
            # pointer_chain: no AOB, no cave -- just the resolved address.
            put("resolves to  : {}".format(result.get("resolved_address")),
                "ok" if ready else "err")
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

            elif template == "pointer_chain":
                if not v["base_offset"].strip():
                    errors.append("Base offset is required.")
                else:
                    int(v["base_offset"], 16)  # validate; stored as the hex string
                    mod["base_offset"] = v["base_offset"].strip()
                chain_tokens = [t.strip() for t in
                               re.split(r"[,\s]+", v["offset_chain"].strip()) if t.strip()]
                if not chain_tokens:
                    errors.append("Offset chain is required (e.g. \"58\" or \"58, 10\").")
                else:
                    for t in chain_tokens:
                        int(t, 16)  # validate each hop; stored as hex strings
                    mod["offset_chain"] = chain_tokens
                # Only persist 'module' when set, so configs without a module
                # target stay byte-for-byte the same (main exe), matching
                # pointer_capture's per-hook module convention.
                module = v["module"].strip()
                if module:
                    mod["module"] = module
                # struct_offset is OPTIONAL here (unlike pointer_capture, where
                # it's required) -- blank means offset_chain alone already
                # reaches the target. When given, it's a FLAT displacement
                # added on top of offset_chain's resolved pointer, with no
                # further dereference (see apply_pointer_chain's docstring) --
                # do not fold a struct-field offset into offset_chain itself.
                struct_offset = v["struct_offset"].strip()
                if struct_offset:
                    int(struct_offset, 16)  # validate
                    mod["struct_offset"] = struct_offset
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
        self.form_vars["module"].set("")
        self.form_vars["base_offset"].set("")
        self.form_vars["offset_chain"].set("")
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
            # Before anything else: end any debug session. Closing GGMod with a
            # hardware breakpoint still armed in the game's threads would leave
            # it trapping into a debugger that no longer exists.
            try:
                self._stop_watch(reason="Stopped — GGMod closing.")
            except Exception as exc:
                self.log("Error stopping watch session on close: {}".format(exc))
            # Stop the live auto-refresh loop + any pending flash resets so no
            # timer fires against a destroyed window or detached process.
            if self._scan_auto_job is not None:
                try:
                    self.root.after_cancel(self._scan_auto_job)
                except Exception:
                    pass
                self._scan_auto_job = None
            for job in self._scan_flash_jobs.values():
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
            self._scan_flash_jobs.clear()
            self._watch_rows.clear()     # Watch List is session-only
            if self.engine.is_attached():
                self.log("Closing: detaching (stopping threads, restoring bytes)...")
                self.on_detach()          # detaches engine (keeps bindings)
            self.hotkeys.unregister_all()  # final cleanup of all global hotkeys
            self._bound.clear()
        finally:
            self.root.destroy()

    def run(self):
        self.root.mainloop()
