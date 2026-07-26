"""Core trainer engine for GGMod.

Real memory logic: AOB scanning, hard-freeze (continuous overwrite) and
pointer-capture (code-cave) mod templates.

This module must NOT import tkinter. All user-facing messages go through
the log_callback passed into __init__ so the UI stays decoupled from logic.

NOTE ON TESTING: the memory routines below cannot be exercised against a
real game from this dev box; they are written to be correct-by-construction
and are meant to be validated manually against POP.exe / PRAGMATA. Places
where a live game is required to confirm behaviour are called out in
comments and surfaced via the log callback.
"""

import ctypes
import json
import os
import queue
import re
import struct
import threading
import time
from ctypes import wintypes

import pymem

try:
    import capstone
except Exception:                       # capstone is optional at import time
    capstone = None                     # auto-steal helpers raise if it's absent

try:
    import numpy as np
except Exception:                       # numpy is optional at import time
    np = None                           # PointerChainFinder errors if it's absent

# ---------------------------------------------------------------------------
# Win32 bindings we need beyond what pymem exposes conveniently.
# ---------------------------------------------------------------------------
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_kernel32.VirtualAllocEx.restype = wintypes.LPVOID
_kernel32.VirtualAllocEx.argtypes = [
    wintypes.HANDLE, wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD
]
_kernel32.VirtualProtectEx.restype = wintypes.BOOL
_kernel32.VirtualProtectEx.argtypes = [
    wintypes.HANDLE, wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    """Layout matches the Win32 struct; ctypes inserts natural alignment
    padding (so RegionSize lands at offset 24 on 64-bit)."""
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


_kernel32.VirtualQueryEx.restype = ctypes.c_size_t
_kernel32.VirtualQueryEx.argtypes = [
    wintypes.HANDLE, wintypes.LPCVOID,
    ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t,
]

_kernel32.IsWow64Process.restype = wintypes.BOOL
_kernel32.IsWow64Process.argtypes = [wintypes.HANDLE,
                                     ctypes.POINTER(wintypes.BOOL)]


def process_is_wow64(handle):
    """True if `handle`'s process is 32-bit running under WoW64.

    Asks Windows directly instead of trusting pymem's is_WoW64 attribute,
    which is only populated on some of pymem's construction paths (it stays
    False after open_process_from_id). Bitness drives BOTH the capstone
    disassembly mode and the debugger's thread-context API choice, so getting
    it wrong corrupts decoding and thread contexts alike. Returns None if the
    query fails, letting the caller fall back.
    """
    flag = wintypes.BOOL()
    if not _kernel32.IsWow64Process(handle, ctypes.byref(flag)):
        return None
    return bool(flag.value)

MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
PAGE_EXECUTE_READWRITE = 0x40
# Page protections that make a committed region unreadable for scanning.
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100

# Cave layout: first bytes are the "shared slot" the cave writes the captured
# pointer into; code starts further in (aligned) so we never overlap the slot.
_SLOT_OFF = 0
_CODE_OFF = 16
_CAVE_SIZE = 256

# ModRM reg-field codes. The same low-3-bit code is used for the e** and r**
# names; the r8-r15 names set REX.R. Width is decided by process bitness, not
# by whether the caller wrote "esi" vs "rsi".
_REG_CODES = {
    "eax": 0, "ecx": 1, "edx": 2, "ebx": 3, "esp": 4, "ebp": 5, "esi": 6, "edi": 7,
    "rax": 0, "rcx": 1, "rdx": 2, "rbx": 3, "rsp": 4, "rbp": 5, "rsi": 6, "rdi": 7,
    "r8": 8, "r9": 9, "r10": 10, "r11": 11, "r12": 12, "r13": 13, "r14": 14, "r15": 15,
}


class JmpOutOfRangeError(Exception):
    """Raised when a rel32 jmp offset does not fit in signed 32 bits.

    Signals that a cave was allocated further from its hook than a 5-byte
    E9 rel32 can reach — an abort condition, not a crash. Carries the jmp
    target address and the computed (out-of-range) relative offset.
    """

    def __init__(self, dst_addr, rel):
        self.dst_addr = dst_addr
        self.rel = rel
        super().__init__(
            "jmp target {:#x} out of rel32 range (rel={})".format(dst_addr, rel)
        )


class InsufficientBytesForJmpError(Exception):
    """Raised when the AOB is too short to cover whole instructions up to the
    jmp size threshold, so a correct steal length cannot be computed. Means the
    user must supply a longer AOB (more trailing bytes past the hook)."""

    def __init__(self, required, got, aob_len):
        self.required = required      # bytes the jmp needs (5 or 14)
        self.got = got                # bytes of fully-decoded instructions
        self.aob_len = aob_len
        super().__init__(
            "AOB decodes to only {} instruction byte(s); need >= {} to fit the "
            "jmp. Provide a longer AOB (a few more bytes past the hook).".format(
                got, required)
        )


class MidInstructionStealError(Exception):
    """Raised when a manually-entered steal value does not fall on an
    instruction boundary — it would cut an instruction in half and crash the
    game. Carries the surrounding valid boundaries for a helpful message."""

    def __init__(self, steal, lo, hi, boundaries):
        self.steal = steal
        self.lo = lo                  # nearest boundary below steal
        self.hi = hi                  # nearest boundary above steal
        self.boundaries = boundaries
        super().__init__(
            "steal {} lands mid-instruction (splits the instruction between "
            "byte {} and byte {}). Use {} or {} instead.".format(
                steal, lo, hi, lo, hi)
        )


class HardFreezeUniquenessError(Exception):
    """Raised by build_hard_freeze_candidate_from_address when walking the AOB
    backward by whole instructions still hasn't produced a unique match within
    the allowed context window. Means the bytes around the target instruction
    recur elsewhere in the module (or the walk hit unresolvable bytes); the
    user should try a different address or build the AOB manually."""

    def __init__(self, context_bytes, max_context_bytes, matches):
        self.context_bytes = context_bytes
        self.max_context_bytes = max_context_bytes
        self.matches = matches         # -1 if a read/decode failure stopped us
        detail = ("still {} match(es)".format(matches) if matches >= 0
                  else "a read/decode failure")
        super().__init__(
            "Could not reach a unique AOB within {} byte(s) of backward "
            "context (limit {}) -- {}. Try a different address, or build "
            "the AOB manually.".format(context_bytes, max_context_bytes, detail)
        )


def _parse_int(value, default=0):
    """Parse an int that may be given as an int or a hex/dec string."""
    if value is None:
        return default
    if isinstance(value, int):
        return value
    return int(str(value), 0)  # base 0 handles "0x10" and "16"


def _parse_offset(value, default=0):
    """Parse a struct offset, which is HEX by convention.

    Memory offsets come from Cheat Engine, which shows them in hex, so a bare
    "18" means 0x18 (24), and "0x18" also means 0x18. This is deliberately NOT
    _parse_int (base 0), which would misread bare "18" as decimal 18 (0x12) and
    reject a bare "19C" outright. An int is treated as already resolved and
    passed through unchanged.
    """
    if value is None:
        return default
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if not s:
        return default
    return int(s, 16)  # accepts an optional "0x" prefix too


# A single Intel-style memory operand: [base +/- disp], e.g. "[ebx+18]",
# "[rsi+0x1F4]", "[rax-10]". Bytes/addresses in a pasted line have no brackets,
# so scanning the whole line for brackets is safe.
_MEM_OPERAND_RE = re.compile(r"\[([^\]]+)\]")
_BASE_DISP_RE = re.compile(
    r"^(?P<base>[a-z][a-z0-9]*)(?P<disp>[+-](?:0x)?[0-9a-f]+)?$", re.IGNORECASE)


def parse_disasm_line(line):
    """Parse a pasted disassembly line into capture_register + struct_offset.

    Pure text — no live process needed. Tolerates Cheat-Engine 'find what
    writes' formats with or without a leading address/bytes, e.g.:
        "0053FBBA - 89 7B 18 - mov [ebx+18],edi"
        "mov [ebx+18],edi"
        "dec [rax+20]"
        "mov [rsi+0x1F4],rbx"
    Extracts a SINGLE memory operand's base register and hex displacement,
    returning struct_offset as a hex string (no 0x prefix, negatives as "-10")
    so it round-trips through _parse_offset exactly like a hand-typed value.

    Returns a dict: mnemonic, capture_register, struct_offset, memory_operand,
    reason. On anything ambiguous, capture_register/struct_offset stay None and
    reason explains why (rather than guessing).
    """
    result = {"mnemonic": None, "capture_register": None, "struct_offset": None,
              "memory_operand": None, "reason": "ok"}
    if not line or not line.strip():
        result["reason"] = "empty line"
        return result
    text = line.strip()

    # Mnemonic (informational): first alphabetic token of the segment that has
    # the operand (CE uses 'addr - bytes - mnemonic operands').
    segments = [s.strip() for s in text.split(" - ")]
    instr = next((s for s in segments if "[" in s), segments[-1])
    mtok = re.match(r"\s*([a-zA-Z]+)", instr)
    if mtok:
        result["mnemonic"] = mtok.group(1).lower()

    mem_ops = _MEM_OPERAND_RE.findall(text)
    if not mem_ops:
        result["reason"] = "no memory operand — nothing to fill"
        return result
    uniq = list(dict.fromkeys(m.strip() for m in mem_ops))
    if len(uniq) > 1:
        result["reason"] = "multiple memory operands — set register/offset manually"
        return result

    inner = uniq[0]
    result["memory_operand"] = "[{}]".format(inner)
    norm = inner.replace(" ", "").lower()
    if "*" in norm:
        result["reason"] = "scaled-index addressing not supported — set manually"
        return result
    m = _BASE_DISP_RE.match(norm)
    if not m:
        result["reason"] = "complex operand (index/absolute) — set manually"
        return result
    base = m.group("base")
    if base not in _REG_CODES:
        result["reason"] = "unsupported base register '{}'".format(base)
        return result
    disp = m.group("disp")
    if not disp:
        result["reason"] = "memory operand has no displacement (register-only)"
        return result

    sign = -1 if disp[0] == "-" else 1
    hexpart = disp[1:]
    if hexpart.lower().startswith("0x"):
        hexpart = hexpart[2:]
    try:
        val = int(hexpart, 16) * sign
    except ValueError:
        result["reason"] = "could not parse displacement '{}'".format(disp)
        return result

    result["capture_register"] = base
    result["struct_offset"] = "-{:X}".format(-val) if val < 0 else "{:X}".format(val)
    return result


class TrainerEngine:
    def __init__(self, log_callback=None):
        self._log = log_callback if log_callback else (lambda msg: None)
        self.pm = None                # pymem.Pymem handle when attached
        self.process_name = None      # name of the attached process
        self.mods = []                # loaded mod definitions
        self.config_path = None       # path the current config was loaded from
        self.config_data = None       # full parsed config dict (for re-saving)
        self._is64 = False            # target process is 64-bit

        # Registry of currently-active mods, keyed by mod name. Each entry:
        #   {
        #     "stop": threading.Event,
        #     "thread": threading.Thread,
        #     "patches": [(address, original_bytes), ...],  # to restore
        #     "cave": <address or None>,                     # to free
        #     "kind": "hard_freeze" | "pointer_capture",
        #   }
        self._active = {}

    # ==================================================================
    # Process attach / detach
    # ==================================================================
    def attach(self, process_name: str) -> bool:
        """Attach to a running process by name."""
        try:
            self.pm = pymem.Pymem(process_name)
        except pymem.exception.ProcessNotFound:
            self._log("Could not find process: '{}'".format(process_name))
            self.pm = None
            self.process_name = None
            return False
        except Exception as exc:
            self._log("Failed to attach to '{}': {}".format(process_name, exc))
            self.pm = None
            self.process_name = None
            return False

        self.process_name = process_name
        # WoW64 => a 32-bit process on 64-bit Windows; anything else is 64-bit.
        # (On a 32-bit OS this would misreport, but GGMod targets modern 64-bit
        # Windows.) Ask Windows directly and only fall back to pymem's
        # attribute, which is not populated on every pymem construction path.
        wow64 = process_is_wow64(self.pm.process_handle)
        if wow64 is None:
            try:
                wow64 = bool(self.pm.is_WoW64)
            except Exception:
                wow64 = False
        self._is64 = not wow64
        self._log(
            "Attached to '{}' (pid {}, {}).".format(
                process_name, self.pm.process_id, "x64" if self._is64 else "x86"
            )
        )
        return True

    def detach(self):
        """Stop all mods, restore patched bytes, free caves, then close."""
        if self.pm is None:
            self._log("Not attached; nothing to detach.")
            return

        for name in list(self._active.keys()):
            self._teardown_mod(name)

        try:
            self.pm.close_process()
            self._log("Detached from '{}'.".format(self.process_name))
        except Exception as exc:
            self._log("Error while detaching: {}".format(exc))
        finally:
            self.pm = None
            self.process_name = None
            self._active = {}

    # ==================================================================
    # Config loading
    # ==================================================================
    def load_game_config(self, json_path: str):
        try:
            with open(json_path, "r", encoding="utf-8") as fh:
                config = json.load(fh)
        except FileNotFoundError:
            self._log("Config file not found: {}".format(json_path))
            return
        except json.JSONDecodeError as exc:
            self._log("Invalid JSON in config: {}".format(exc))
            return

        self.mods = config.get("mods", [])
        # Remember the file + full parsed dict so save_mod_config can write it
        # back later, preserving process_name / notes / other mods untouched.
        self.config_path = json_path
        self.config_data = config
        self.config_data["mods"] = self.mods   # share the same list object
        cfg_process = config.get("process_name", "<unknown>")
        self._log(
            "Loaded config for '{}': {} mod(s).".format(cfg_process, len(self.mods))
        )

    def save_mod_config(self, name=None):
        """Write the current in-memory mods list back to the JSON file it was
        loaded from, preserving every other field. `name` is optional and used
        only to validate the mod exists (the whole list is written regardless).
        Returns True on success. Never called automatically — only on explicit
        'Save to Config'.
        """
        if not self.config_path or self.config_data is None:
            self._log("save_mod_config: no config loaded.")
            return False
        if name is not None and not any(m.get("name") == name for m in self.mods):
            self._log("save_mod_config: no mod named '{}'.".format(name))
            return False
        self.config_data["mods"] = self.mods   # ensure current list is written
        try:
            with open(self.config_path, "w", encoding="utf-8") as fh:
                # ensure_ascii=False keeps note fields' UTF-8 (em-dashes etc.)
                # literal, so we don't rewrite them as \uXXXX escapes.
                json.dump(self.config_data, fh, indent=2, ensure_ascii=False)
        except OSError as exc:
            self._log("save_mod_config: write failed: {}".format(exc))
            return False
        return True

    # ==================================================================
    # Low-level memory helpers
    # ==================================================================
    def _main_module_range(self, module_name=None):
        """Return (base_address, size) for a loaded module.

        module_name=None (default, unchanged behavior) -> the main executable
        module. This keeps every existing config (POP1/POP2/GTA V/PRAGMATA),
        which has no per-hook 'module' field, scanning the main .exe exactly as
        before.

        module_name given (e.g. 'GameAssembly.dll') -> that module's range,
        matched case-insensitively by base name; the extension may be omitted.
        Returns (0, 0) if the named module is not loaded, so callers abort with
        a clear diagnostic instead of scanning the wrong range.
        """
        try:
            if module_name:
                target = module_name.lower()
                mods = list(self.pm.list_modules())
                for module in mods:
                    if module.name.lower() == target:
                        return int(module.lpBaseOfDll), int(module.SizeOfImage)
                # Forgiving match: allow the extension to be dropped
                # ('GameAssembly' matching 'GameAssembly.dll').
                for module in mods:
                    if module.name.lower().rsplit(".", 1)[0] == target:
                        return int(module.lpBaseOfDll), int(module.SizeOfImage)
                self._log("Module '{}' not found among {} loaded modules.".format(
                    module_name, len(mods)))
                return 0, 0

            base = self.pm.base_address
            for module in self.pm.list_modules():
                if int(module.lpBaseOfDll) == int(base):
                    return int(base), int(module.SizeOfImage)
            # Fall back to the first module (the main exe is normally first).
            first = next(iter(self.pm.list_modules()))
            return int(first.lpBaseOfDll), int(first.SizeOfImage)
        except Exception as exc:
            self._log("Could not determine module range: {}".format(exc))
            return (0, 0) if module_name else (int(self.pm.base_address), 0)

    def _read(self, address, size):
        try:
            return self.pm.read_bytes(address, size)
        except Exception:
            return None

    def _write(self, address, data):
        self.pm.write_bytes(address, bytes(data), len(data))

    def _make_rwx(self, address, size):
        """Force a region to RWX so we can patch code. Returns old protect."""
        old = wintypes.DWORD(0)
        ok = _kernel32.VirtualProtectEx(
            self.pm.process_handle, ctypes.c_void_p(address), size,
            PAGE_EXECUTE_READWRITE, ctypes.byref(old),
        )
        if not ok:
            self._log("VirtualProtectEx failed at {:#x}".format(address))
        return old.value

    # ------------------------------------------------------------------
    # AOB scanning
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_aob(aob_pattern):
        """Parse 'AA BB ?? DD' into (pattern_bytes, mask) where mask[i] is
        True for a fixed byte and False for a wildcard."""
        pattern = []
        mask = []
        for token in aob_pattern.split():
            if token in ("??", "?"):
                pattern.append(0)
                mask.append(False)
            else:
                pattern.append(int(token, 16))
                mask.append(True)
        return bytes(pattern), mask

    @staticmethod
    def _ts():
        """Wall-clock HH:MM:SS.mmm timestamp for diagnostic log lines."""
        now = time.time()
        return time.strftime("%H:%M:%S", time.localtime(now)) + \
            ".{:03d}".format(int((now % 1) * 1000))

    def scan_aob(self, aob_pattern, progress_callback=None, module_name=None):
        """Scan a module and return ALL addresses matching the pattern.

        module_name=None scans the main executable module (default). A module
        name (e.g. 'GameAssembly.dll') scans that module instead — needed for
        Unity/IL2CPP titles whose game logic lives in a separate module.

        Reads in overlapping chunks so matches that straddle a chunk boundary
        are still found, and so we never pull the whole module into memory at
        once.

        progress_callback(offset, size), if given, is invoked during the scan
        (throttled to ~1 MB of progress) so the UI can show a progress bar. It
        runs on the calling (worker) thread, so the callback must marshal any
        UI work to the main thread itself.
        """
        if self.pm is None:
            self._log("scan_aob: not attached.")
            return []

        pattern, mask = self._parse_aob(aob_pattern)
        plen = len(pattern)
        if plen == 0:
            return []

        # First fixed (non-wildcard) byte lets us fast-skip with bytes.find.
        first_fixed = next((i for i, m in enumerate(mask) if m), 0)
        first_byte = pattern[first_fixed]

        # Resolving the module range walks the process module list; log around
        # it so a stall *here* (before any read) is distinguishable in the log
        # from a stall inside the read loop.
        mod_label = module_name if module_name else "main module"
        self._log("[{}] scan_aob: resolving range for {}...".format(
            self._ts(), mod_label))
        base, size = self._main_module_range(module_name)
        self._log("[{}] scan_aob: {} range resolved base={:#x} size={} "
                  "({:.1f} MB).".format(self._ts(), mod_label, base, size,
                                        size / (1024 * 1024)))
        if size == 0:
            self._log("scan_aob: {} unavailable (size 0), aborting scan.".format(
                mod_label))
            return []

        CHUNK = 0x10000                 # 64 KB
        overlap = plen - 1              # so boundary-straddling matches survive
        PROGRESS_STEP = 0x100000        # report at most every ~1 MB
        results = []
        offset = 0
        skipped = 0                     # chunks _read() could not read (None)
        next_progress = 0
        while offset < size:
            read_len = min(CHUNK + overlap, size - offset)
            chunk = self._read(base + offset, read_len)
            if chunk:
                pos = 0
                while True:
                    idx = chunk.find(first_byte, pos)
                    if idx < 0 or idx + plen > len(chunk):
                        break
                    start = idx - first_fixed
                    if start >= 0 and self._match_at(chunk, start, pattern, mask):
                        addr = base + offset + start
                        if addr not in results:
                            results.append(addr)
                    pos = idx + 1
            else:
                skipped += 1            # unreadable region — count, don't fail
            # Throttled progress: only after crossing each ~1 MB boundary.
            if progress_callback is not None and offset >= next_progress:
                try:
                    progress_callback(offset, size)
                except Exception:
                    pass                # a broken UI callback must not kill scan
                next_progress = offset + PROGRESS_STEP
            offset += CHUNK             # advance by CHUNK, not read_len (overlap)

        # Final 100% tick so the bar completes even if the last step was <1 MB.
        if progress_callback is not None:
            try:
                progress_callback(size, size)
            except Exception:
                pass

        if skipped:
            self._log("[{}] scan_aob: {} of {} chunk(s) unreadable and skipped "
                      "(~{} bytes not scanned).".format(
                          self._ts(), skipped, (size + CHUNK - 1) // CHUNK,
                          skipped * CHUNK))
        self._log("[{}] scan_aob: complete, {} match(es).".format(
            self._ts(), len(results)))
        return results

    @staticmethod
    def _match_at(buf, start, pattern, mask):
        if start + len(pattern) > len(buf):
            return False
        for i, fixed in enumerate(mask):
            if fixed and buf[start + i] != pattern[i]:
                return False
        return True

    # ------------------------------------------------------------------
    # Instruction encoders
    # ------------------------------------------------------------------
    def _enc_mov_slot_reg(self, reg_name, slot_addr, instr_addr):
        """Encode 'mov [slot], reg'.

        x86: absolute  ->  89 /r  mod=00 rm=101 disp32=slot           (6 bytes)
        x64: rip-rel   ->  REX.W  89 /r  mod=00 rm=101 disp32=slot-rip (7 bytes)
             (x64 has no direct mov [abs64], reg for general regs, so we make
              the slot rip-relative; the slot lives in our own cave, well
              within +/-2GB of the mov, so disp32 always fits.)
        """
        code = _REG_CODES.get(reg_name.lower())
        if code is None:
            raise ValueError("unsupported capture register: {}".format(reg_name))

        if not self._is64:
            if code > 7:
                raise ValueError("r8-r15 not available in x86")
            modrm = (code << 3) | 0x05
            return bytes([0x89, modrm]) + struct.pack("<I", slot_addr & 0xFFFFFFFF)

        rex = 0x48
        if code > 7:
            rex |= 0x04                 # REX.R for r8-r15
        modrm = ((code & 7) << 3) | 0x05
        instr_len = 7
        disp = slot_addr - (instr_addr + instr_len)
        return bytes([rex, 0x89, modrm]) + struct.pack("<i", disp)

    @staticmethod
    def _enc_jmp(src_addr, dst_addr, use_rel):
        """Encode a jmp from src to dst.

        use_rel True  -> E9 rel32                                   (5 bytes)
        use_rel False -> FF 25 00000000 <abs8>  (jmp [rip+0])       (14 bytes)
                         Register-safe absolute jump that reaches anywhere in
                         the 64-bit address space (needed when the cave is
                         allocated further than rel32 can reach).
        """
        if use_rel:
            rel = dst_addr - (src_addr + 5)
            # Explicit bounds check: a rel that overflows signed int32 must not
            # reach struct.pack (which would raise an opaque struct.error). Fail
            # with a specific, catchable exception carrying the diagnostics.
            if not (-2147483648 <= rel <= 2147483647):
                raise JmpOutOfRangeError(dst_addr, rel)
            return bytes([0xE9]) + struct.pack("<i", rel)
        return bytes([0xFF, 0x25, 0x00, 0x00, 0x00, 0x00]) + struct.pack("<Q", dst_addr)

    # ------------------------------------------------------------------
    # Cave allocation (with x64 near-allocation so rel32 can reach)
    # ------------------------------------------------------------------
    def _alloc_cave(self, near_addr):
        """Allocate an RWX cave. Returns (address, use_rel).

        x86: rel32 spans the whole 4GB space, so allocate anywhere.
        x64: try to allocate within +/-2GB of `near_addr` so a 5-byte rel32
             jmp reaches; if that fails, fall back to a far allocation and
             signal use_rel=False so callers emit the 14-byte absolute jmp.
        """
        if not self._is64:
            addr = self.pm.allocate(_CAVE_SIZE)
            return int(addr), True

        handle = self.pm.process_handle
        # Search downward from just below the module (code caves are usually
        # free there) in 64KB (allocation-granularity) steps, within 2GB.
        step = 0x10000
        limit = near_addr - 0x7F000000
        hint = (near_addr - step) & ~(step - 1)
        tries = 0
        while hint > limit and hint > 0 and tries < 20000:
            p = _kernel32.VirtualAllocEx(
                handle, ctypes.c_void_p(hint), _CAVE_SIZE,
                MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE,
            )
            if p:
                # Defensive: VirtualAllocEx with a hint can, in principle, be
                # satisfied at a different address. Confirm the result is really
                # within rel32 reach before trusting use_rel=True; if not, free
                # it and fall through to the absolute-jmp fallback rather than
                # emitting a rel32 that cannot reach.
                if abs(int(p) - near_addr) < 0x7FFFFFFF:
                    self._log("x64 near-cave allocated at {:#x} (rel32 reachable).".format(p))
                    return int(p), True
                self._log(
                    "x64 near-cave at {:#x} is {:#x} from hook {:#x} — outside "
                    "rel32 range; freeing and falling back to absolute jmp.".format(
                        int(p), abs(int(p) - near_addr), near_addr)
                )
                self._safe_free(int(p))
                break
            hint -= step
            tries += 1

        # Fallback: let the OS place it anywhere; use absolute jumps.
        addr = self.pm.allocate(_CAVE_SIZE)
        self._log(
            "x64 near allocation failed; cave at {:#x} uses 14-byte absolute "
            "jmp (requires steal_len >= 14).".format(addr)
        )
        return int(addr), False

    # ------------------------------------------------------------------
    # steal-length helper
    # ------------------------------------------------------------------
    def _steal_len(self, mod):
        """The number of original bytes to relocate into the cave.

        We treat mod['steal_len'] (preferred) or mod['hook_offset'] as an
        already instruction-aligned length. GGMod does NOT bundle a length-
        disassembler, so it cannot auto-extend a too-short hook_offset to the
        next instruction boundary; callers must supply an aligned value.
        """
        return _parse_int(mod.get("steal_len", mod.get("hook_offset")))

    def _min_jmp_len(self, use_rel=True):
        return 5 if (not self._is64 or use_rel) else 14

    # ------------------------------------------------------------------
    # capstone-backed steal computation (edit-time aid; not a schema change)
    # ------------------------------------------------------------------
    def _make_disassembler(self, is64=None):
        """Build a capstone x86 disassembler matching the target's bitness.

        is64=None uses the attached process's detected bitness (self._is64),
        so decoding matches exactly how the engine encodes jmps for it.
        """
        if capstone is None:
            raise RuntimeError(
                "capstone is not installed; cannot auto-calculate steal length.")
        if is64 is None:
            is64 = self._is64
        mode = capstone.CS_MODE_64 if is64 else capstone.CS_MODE_32
        return capstone.Cs(capstone.CS_ARCH_X86, mode)

    def steal_boundaries(self, aob_bytes, is64=None):
        """Return the cumulative instruction-end offsets within aob_bytes.

        e.g. [3, 9, 15] means instructions end at bytes 3, 9 and 15 — those are
        the ONLY steal values that don't split an instruction. Decoding stops at
        the first byte capstone can't decode, so the list only spans the region
        that disassembled cleanly.
        """
        md = self._make_disassembler(is64)
        bounds = []
        total = 0
        for insn in md.disasm(bytes(aob_bytes), 0):
            total += insn.size
            bounds.append(total)
        return bounds

    def compute_min_steal(self, aob_bytes, jmp_type="rel32", is64=None):
        """Smallest instruction-aligned steal length >= the jmp size.

        Walks instructions from the hook, summing sizes, and returns the first
        cumulative total that reaches the jmp threshold (5 for rel32, 14 for a
        14-byte absolute jmp). Raises InsufficientBytesForJmpError if the AOB
        runs out before the threshold is reached.
        """
        min_len = 14 if jmp_type in ("abs", "absolute") else 5
        total = 0
        for boundary in self.steal_boundaries(aob_bytes, is64):
            total = boundary
            if total >= min_len:
                return total
        raise InsufficientBytesForJmpError(min_len, total, len(aob_bytes))

    def validate_steal(self, aob_bytes, steal, is64=None):
        """Guard a manually-entered steal value against mid-instruction cuts.

        Raises MidInstructionStealError if `steal` falls strictly inside a
        decoded instruction. Returns the boundary list on success. If the bytes
        can't be disassembled (empty list) or `steal` is beyond the decoded
        region (can't be verified from this AOB), it does NOT block — best
        effort, so we never reject a value we cannot actually prove is wrong.
        """
        bounds = self.steal_boundaries(aob_bytes, is64)
        if not bounds or steal in bounds:
            return bounds
        if steal < bounds[-1]:
            lo = max((b for b in bounds if b < steal), default=0)
            hi = min(b for b in bounds if b > steal)
            raise MidInstructionStealError(steal, lo, hi, bounds)
        return bounds  # steal >= last boundary: beyond decoded AOB, unverifiable

    def _locate_instruction_boundary(self, target_addr,
                                     lookback_sizes=(24, 16, 12, 8, 6, 4, 3, 2)):
        """Find the instruction that ends exactly at `target_addr`.

        x86 can't be disassembled backwards, so this anchors a few bytes
        earlier and disassembles forward; whichever anchor produces an
        instruction stream landing exactly on target_addr identifies the real
        instruction boundary. Prefers the farthest-back anchor that lands
        cleanly -- the longer the validated stream, the less likely it decoded
        mid-instruction garbage.

        Shared by DebugSession._resolve_writer (resolving what wrote to a Find
        Writes address) and build_hard_freeze_candidate_from_address (walking
        an AOB backward by whole instructions) -- the same problem in both
        cases: given an address that is the END of some instruction, find
        where that instruction starts.

        Returns the capstone instruction ending at target_addr, or None if it
        can't be resolved from the available bytes.
        """
        blob, window = None, 0
        for size in lookback_sizes:
            data = self._read(target_addr - size, size)
            if data:
                blob, window = data, size
                break
        if not blob:
            return None
        start = target_addr - window
        md = self._make_disassembler()
        for back in range(window, 1, -1):
            anchor = target_addr - back
            offset = anchor - start
            stream = list(md.disasm(bytes(blob[offset:]), anchor))
            if not stream:
                continue
            last = None
            for insn in stream:
                if insn.address >= target_addr:
                    break
                last = insn
            if last is not None and last.address + last.size == target_addr:
                return last
        return None

    def _module_for_address(self, address):
        """Return (module_name_or_None, base, size) for the module containing
        `address`. module_name is None when it's the main executable module
        (so callers pass None to scan_aob for the default main-module scan)."""
        try:
            main_base = int(self.pm.base_address)
            for m in self.pm.list_modules():
                base = int(m.lpBaseOfDll)
                size = int(m.SizeOfImage)
                if base <= address < base + size:
                    return (None if base == main_base else m.name), base, size
        except Exception:
            pass
        return None, 0, 0

    def _iter_committed(self):
        """Walk the whole address space via VirtualQueryEx, yielding (base,
        size) of every committed, readable (non-guard/non-noaccess) region --
        heap/stack included, not just module images. Shared by MemoryScanner
        (region='All' value scans) and PointerChainFinder (the static
        pointer-scan snapshot, which specifically needs off-module memory
        since that's where object instances live) rather than duplicated."""
        handle = self.pm.process_handle
        max_addr = 0x7FFFFFFFFFFF if self._is64 else 0x7FFFFFFF
        addr = 0
        mbi = MEMORY_BASIC_INFORMATION()
        while addr < max_addr:
            if not _kernel32.VirtualQueryEx(
                    handle, ctypes.c_void_p(addr),
                    ctypes.byref(mbi), ctypes.sizeof(mbi)):
                break
            base = mbi.BaseAddress or 0
            size = mbi.RegionSize or 0
            if size == 0:
                break
            prot = mbi.Protect
            if (mbi.State == MEM_COMMIT and prot not in (0, PAGE_NOACCESS)
                    and not (prot & PAGE_GUARD)):
                yield base, size
            addr = base + size

    def build_candidate_from_address(self, address, byte_span=32):
        """Read live bytes at `address`, disassemble, and build a hook candidate
        for the Add Mod form: a unique AOB plus best-effort register/offset.

        Reuses the existing pieces (capstone via _make_disassembler, uniqueness
        via scan_aob, hex offsets via the _parse_offset convention). Does NOT
        compute steal — that stays the 'Auto' button's job (compute_min_steal).

        Returns a dict with aob/matches/module/capture_register/struct_offset/
        reason/instructions, or {"error": "..."} on failure.
        """
        if self.pm is None:
            return {"error": "Not attached — attach to the game first."}
        try:
            address = int(address)
        except (TypeError, ValueError):
            return {"error": "Invalid address."}
        raw = self._read(address, byte_span)
        if not raw:
            return {"error": "Could not read {} bytes at {:#x}.".format(
                byte_span, address)}
        try:
            md = self._make_disassembler()
        except RuntimeError as exc:
            return {"error": str(exc)}
        md.detail = True
        insns = list(md.disasm(bytes(raw), address))
        if not insns:
            return {"error": "Could not disassemble bytes at {:#x}.".format(address)}

        # Scan the module that actually contains this address (so a DLL hook
        # like GameAssembly.dll checks uniqueness in the right module, and the
        # hook targets it at apply time).
        module_name, _, _ = self._module_for_address(address)

        # Progressive AOB: start at the first whole instruction, extend by whole
        # instructions until the pattern is unique (1 match) or a cap is hit.
        # Adding bytes only narrows matches, so this converges monotonically.
        CAP_BYTES, CAP_INSNS = 20, 6
        cum = 0
        used = 0
        aob_hex = ""
        matches = None
        for i, insn in enumerate(insns):
            if i >= CAP_INSNS or cum + insn.size > CAP_BYTES:
                break
            cum += insn.size
            used = i + 1
            aob_hex = " ".join("{:02X}".format(b) for b in raw[:cum])
            matches = len(self.scan_aob(aob_hex, module_name=module_name))
            if matches == 1:
                break

        # Best-effort register/offset from the FIRST instruction's memory operand.
        first = insns[0]
        reg = None
        offset_str = None
        reason = "ok"
        mem_ops = [op for op in first.operands if op.type == capstone.CS_OP_MEM]
        if not mem_ops:
            reason = "no memory operand — set register/offset manually"
        elif len(mem_ops) > 1:
            reason = "multiple memory operands — set register/offset manually"
        elif mem_ops[0].mem.base == 0:
            reason = "memory operand has no base register — set manually"
        else:
            mem = mem_ops[0].mem
            reg = first.reg_name(mem.base)
            disp = mem.disp
            # Hex string, no 0x prefix, so it round-trips through _parse_offset
            # (hex-by-convention) exactly like a hand-typed offset.
            offset_str = "-{:X}".format(-disp) if disp < 0 else "{:X}".format(disp)

        instructions = [
            {
                "address": "0x{:X}".format(insn.address),
                "text": "{} {}".format(insn.mnemonic, insn.op_str).strip(),
                "bytes": " ".join("{:02X}".format(b) for b in insn.bytes),
                "size": insn.size,
            }
            for insn in (insns[:used] or insns[:1])
        ]

        return {
            "aob": aob_hex,
            "matches": matches,
            "module": module_name,
            "capture_register": reg,
            "struct_offset": offset_str,
            "reason": reason,
            "instructions": instructions,
        }

    def build_hard_freeze_candidate_from_address(self, address,
                                                 max_context_before=128):
        """Build a hard_freeze AOB/offset/nop_len candidate from a live address.

        hard_freeze has NO jmp/code-cave concept -- apply_hard_freeze() just
        writes bytes in place (target = match_address + offset, then either a
        4-byte value or nop_len 0x90 bytes at that target). So unlike
        build_candidate_from_address (used for pointer_capture hooks, whose
        struct_offset/register describe a memory operand and whose companion
        Auto-steal button computes a jmp-sized steal length), this function
        answers a different question entirely: "starting from a unique AOB
        anchored somewhere before `address`, how far in is the instruction I
        actually want to freeze/NOP, and how long is it?" There is no
        jmp-size threshold involved anywhere in this computation.

        `address` is expected to already be an instruction START address (as
        copied from Cheat Engine, or from a Find Writes hit, whose reported
        address is itself resolved to an instruction boundary). Steps:

          1. Disassemble forward from `address` to get the target
             instruction's own length -> nop_len.
          2. Walk backward from `address` ONE WHOLE INSTRUCTION at a time
             (never a raw byte count), extending the AOB and re-checking
             scan_aob uniqueness in the owning module after each step -- the
             backward mirror of build_candidate_from_address's forward
             extension-for-uniqueness loop. x86 can't be disassembled
             backwards, so each backward step reuses
             _locate_instruction_boundary -- the same anchor-and-disassemble-
             forward technique _resolve_writer uses for Find Writes hits.
          3. offset = the distance from the AOB's start to `address` (the
             length of that prepended context).

        Raises HardFreezeUniquenessError if uniqueness can't be reached within
        max_context_before bytes of backward context (mirrors compute_min_
        steal raising InsufficientBytesForJmpError when it runs out of AOB) --
        this fails clearly rather than silently returning a non-unique match.

        Returns a dict shaped like build_candidate_from_address's: aob,
        matches, module, offset, nop_len, instructions -- or {"error": "..."}
        for setup failures (not attached, bad address, can't read/disassemble).
        """
        if self.pm is None:
            return {"error": "Not attached — attach to the game first."}
        try:
            address = int(address)
        except (TypeError, ValueError):
            return {"error": "Invalid address."}

        # 1. Decode the target instruction itself; its length becomes nop_len.
        # 16 bytes covers the longest possible x86 instruction (15 bytes) with
        # a byte to spare.
        raw = self._read(address, 16)
        if not raw:
            return {"error": "Could not read bytes at {:#x}.".format(address)}
        try:
            md = self._make_disassembler()
        except RuntimeError as exc:
            return {"error": str(exc)}
        target_insns = list(md.disasm(bytes(raw), address))
        if not target_insns or target_insns[0].address != address:
            return {"error": "Could not disassemble a clean instruction at "
                            "{:#x} -- is this really an instruction start?"
                            .format(address)}
        nop_len = target_insns[0].size

        # Scan the module that actually contains this address (so uniqueness
        # is checked, and the mod later scans, against the right module).
        module_name, _base, _size = self._module_for_address(address)

        # 2. Walk backward one whole instruction at a time, checking
        # uniqueness after each step (including the zero-context case: just
        # the target instruction alone might already be unique).
        aob_start = address
        end_addr = address + nop_len
        matches = None
        aob_hex = ""
        while True:
            span = end_addr - aob_start
            raw_span = self._read(aob_start, span)
            if raw_span and len(raw_span) == span:
                aob_hex = " ".join("{:02X}".format(b) for b in raw_span)
                matches = len(self.scan_aob(aob_hex, module_name=module_name))
                if matches == 1:
                    break
            context_used = address - aob_start
            if context_used >= max_context_before:
                raise HardFreezeUniquenessError(
                    context_used, max_context_before,
                    matches if matches is not None else -1)
            prev_insn = self._locate_instruction_boundary(aob_start)
            if prev_insn is None:
                raise HardFreezeUniquenessError(
                    context_used, max_context_before,
                    matches if matches is not None else -1)
            aob_start = prev_insn.address

        offset = address - aob_start

        # Re-disassemble the final (unique) AOB span for the preview listing
        # -- context instructions in order, ending with the target.
        final_insns = list(md.disasm(bytes(raw_span), aob_start))
        instructions = [
            {
                "address": "0x{:X}".format(insn.address),
                "text": "{} {}".format(insn.mnemonic, insn.op_str).strip(),
                "bytes": " ".join("{:02X}".format(b) for b in insn.bytes),
                "size": insn.size,
            }
            for insn in final_insns
        ]

        return {
            "aob": aob_hex,
            "matches": matches,
            "module": module_name,
            "offset": offset,
            "nop_len": nop_len,
            "instructions": instructions,
        }

    def suggest_jmp_type(self, hook_address=None, module_name=None):
        """Advisory heuristic: is a rel32 near-cave likely reachable, or should
        the user pre-commit to a 14-byte absolute-jmp steal?

        ADVICE ONLY. The real decision happens at apply time in _alloc_cave,
        which searches for a near rel32 cave and automatically falls back to an
        absolute jmp when none is found — that remains the source of truth. This
        just helps the user pick a steal length up front that won't need redoing
        (rel32 -> 5-byte min, absolute -> 14-byte min).

        Resolves the target module by containing address (if given) or by name.
        Returns {jmp_type, reason, module, module_size, is64}.
        """
        MB = 1024 * 1024
        module_label = module_name or "main module"

        # 32-bit: an E9 rel32 reaches the whole 4 GB space — always rel32.
        if not self._is64:
            return {"jmp_type": "rel32", "module": module_label,
                    "module_size": 0, "is64": False,
                    "reason": "32-bit process — rel32 reaches the entire "
                              "address space."}

        if self.pm is None:
            return {"jmp_type": "rel32", "module": module_label,
                    "module_size": 0, "is64": True,
                    "reason": "Not attached — assuming rel32 (apply-time "
                              "absolute fallback still applies)."}

        # Resolve the module range: by containing module for a live address,
        # else by module name (None = main executable module).
        size = 0
        if hook_address is not None:
            try:
                name, _base, size = self._module_for_address(int(hook_address))
                module_label = name or "main module"
            except (TypeError, ValueError):
                size = 0
        else:
            _base, size = self._main_module_range(module_name)

        if not size:
            return {"jmp_type": "rel32", "module": module_label,
                    "module_size": 0, "is64": True,
                    "reason": "Module size unknown — assuming rel32 (apply-time "
                              "absolute fallback still applies)."}

        mb = size / MB
        # rel32 reaches +/-2GB; _alloc_cave searches downward from the hook for a
        # free near page. Small/medium modules almost always yield one. Only for
        # unusually large modules is a near allocation notably less certain, so
        # we advise the safe 14-byte absolute steal there.
        if size >= 256 * MB:
            return {"jmp_type": "absolute", "module": module_label,
                    "module_size": size, "is64": True,
                    "reason": "Module is {:.0f} MB (unusually large): a near "
                              "rel32 cave may not be found. Absolute (14-byte "
                              "steal) avoids an apply-time abort; automatic "
                              "fallback still applies.".format(mb)}
        if size >= 64 * MB:
            return {"jmp_type": "rel32", "module": module_label,
                    "module_size": size, "is64": True,
                    "reason": "Module is {:.0f} MB: a near rel32 cave should "
                              "still be reachable (automatic absolute fallback "
                              "applies if not).".format(mb)}
        return {"jmp_type": "rel32", "module": module_label,
                "module_size": size, "is64": True,
                "reason": "Module is {:.1f} MB: near rel32 cave easily "
                          "reachable.".format(mb)}

    @staticmethod
    def _mod_hooks(mod):
        """Return a mod's hook list.

        New schema: mod['hooks'] is a list of {aob, hook_offset/steal_len,
        capture_register}. For backward compatibility, a legacy flat mod
        (top-level aob/hook_offset/capture_register) is wrapped as a 1-item
        list so old configs still load.
        """
        if mod.get("hooks"):
            return mod["hooks"]
        if mod.get("aob"):
            return [{
                "aob": mod.get("aob"),
                "hook_offset": mod.get("hook_offset"),
                "steal_len": mod.get("steal_len"),
                "capture_register": mod.get("capture_register"),
            }]
        return []

    # ==================================================================
    # PREVIEW  (gates Apply)
    # ==================================================================
    def preview_mod(self, mod, progress_callback=None):
        """Return match count + status + byte previews without writing.

        hard_freeze: single AOB -> single result.
        pointer_capture: one entry per hook under result['hooks']; overall
        status is 'ready' only if EVERY hook is ready.
        """
        result = {
            "matches": 0,
            "status": "blocked_no_match",
            "original_bytes": None,
            "cave_preview": None,
            "hooks": None,
            "warnings": [],
        }
        if self.pm is None:
            result["status"] = "blocked_not_attached"
            self._log("preview: not attached.")
            return result

        template = mod.get("template")

        if template == "hard_freeze":
            matches = self.scan_aob(mod.get("aob", ""),
                                    progress_callback=progress_callback)
            result["matches"] = len(matches)
            result["match_addresses"] = ["0x{:X}".format(a) for a in matches[:20]]
            if len(matches) == 0:
                result["status"] = "blocked_no_match"
            elif len(matches) > 1:
                result["status"] = "blocked_multiple_match"
            else:
                length = self._freeze_len(mod)
                target = matches[0] + _parse_int(mod.get("offset"))
                result["original_bytes"] = (self._read(target, length) or b"").hex(" ")
                result["status"] = "ready"

        elif template == "pointer_capture":
            self._preview_pointer_capture(mod, result,
                                          progress_callback=progress_callback)

        elif template == "pointer_chain":
            self._preview_pointer_chain(mod, result)

        else:
            result["status"] = "blocked_unknown_template"
            result["warnings"].append("unknown template: {}".format(template))

        self._log(
            "preview '{}': matches={} status={} {}".format(
                mod.get("name"), result["matches"], result["status"],
                ("warn: " + "; ".join(result["warnings"])) if result["warnings"] else "",
            )
        )
        return result

    def _preview_pointer_capture(self, mod, result, progress_callback=None):
        """Fill result with per-hook match/byte info for a pointer_capture mod."""
        hooks = self._mod_hooks(mod)
        if not hooks:
            result["status"] = "blocked_no_match"
            result["warnings"].append("no hooks defined")
            result["hooks"] = []
            return

        hook_results = []
        overall_ready = True
        total_matches = 0
        for i, hook in enumerate(hooks):
            aob = hook.get("aob", "")
            steal_len = _parse_int(hook.get("steal_len", hook.get("hook_offset")))
            reg = hook.get("capture_register", "eax")
            module = hook.get("module")            # None -> main module
            matches = self.scan_aob(aob, progress_callback=progress_callback,
                                    module_name=module)
            total_matches += len(matches)
            hr = {
                "index": i, "aob": aob, "capture_register": reg,
                "steal_len": steal_len, "matches": len(matches),
                "module": module or "main module",
                "match_addresses": ["0x{:X}".format(a) for a in matches[:20]],
                "status": "ready", "original_bytes": None,
                "cave_preview": None, "warnings": [],
            }
            if len(matches) == 0:
                hr["status"] = "blocked_no_match"
                overall_ready = False
            elif len(matches) > 1:
                hr["status"] = "blocked_multiple_match"
                overall_ready = False
            else:
                addr = matches[0]
                steal_bytes = self._read(addr, steal_len) or b""
                hr["original_bytes"] = steal_bytes.hex(" ")
                min_len = self._min_jmp_len(use_rel=True)  # optimistic (near-alloc)
                if steal_len < min_len:
                    hr["status"] = "blocked_steal_len"
                    hr["warnings"].append(
                        "steal_len {} < {} (min for jmp).".format(steal_len, min_len)
                    )
                    overall_ready = False
                if self._is64 and steal_len < 14:
                    hr["warnings"].append(
                        "x64: if near-allocation fails a 14-byte absolute jmp is "
                        "needed but steal_len is {}.".format(steal_len)
                    )
                # Placeholder (zero) addresses; stolen bytes are real. With
                # code_start=0 and a real hook_addr, the return jmp's rel32 is
                # deliberately unrealistic (the true cave address isn't known
                # until apply), so it can exceed rel32 range — that's expected
                # for the preview and must not abort the scan. Show a note.
                try:
                    hr["cave_preview"] = self._build_cave_code(
                        steal_bytes, reg, slot_addr=0, code_start=0,
                        hook_addr=addr, steal_len=steal_len, use_rel=True,
                    ).hex(" ")
                except JmpOutOfRangeError:
                    hr["cave_preview"] = (
                        "(preview n/a: placeholder jmp exceeds rel32 range; "
                        "actual cave is allocated near the hook at apply time)"
                    )
            hook_results.append(hr)

        result["hooks"] = hook_results
        result["matches"] = total_matches
        result["status"] = "ready" if overall_ready else "blocked_hooks"

    def _resolve_pointer_chain(self, base, offset_chain):
        """Walk offset_chain from `base` (already module_base + base_offset),
        one pointer read per hop -- the same arithmetic as
        PointerChainFinder.resolve_chain, but taking an already-resolved
        module base so callers (preview + the poll loop) don't re-enumerate
        process modules on every call. The LAST offset is added but not
        dereferenced again -- it IS the resolved pointer, matching
        resolve_chain's convention (every offset_chain entry is a real hop;
        that's what makes a chain from find_pointer_chains resolve correctly
        at every level). This function does NOT know about struct_offset --
        callers that need a flat field displacement on top of this result
        (apply_pointer_chain, _preview_pointer_chain, force_set_value via
        _effective_ptr) add it themselves, exactly like pointer_capture adds
        struct_offset to its captured pointer. Returns the resolved pointer,
        or None if any hop's read fails (unreadable/decommitted -- e.g. the
        object doesn't exist yet, or the process is mid-load)."""
        ptr_size = 8 if self._is64 else 4
        fmt = "<Q" if ptr_size == 8 else "<I"
        addr = base
        for off in offset_chain:
            raw = self._read(addr, ptr_size)
            if not raw or len(raw) != ptr_size:
                return None
            ptr = struct.unpack(fmt, raw)[0]
            addr = ptr + off
        return addr

    def _preview_pointer_chain(self, mod, result):
        """Fill result with the chain's currently-resolved address.

        Unlike hard_freeze/pointer_capture, there is no AOB to scan and no
        "multiple matches" possibility -- a pointer chain either resolves
        (every hop reads a valid pointer) or it doesn't. status is 'ready'
        only when every hop read succeeds.

        offset_chain resolves to a POINTER (every entry in it is a real hop
        that gets dereferenced, except the very last add -- see
        _resolve_pointer_chain). struct_offset, if given, is then added FLAT
        on top with NO further dereference -- exactly pointer_capture's
        struct_offset semantics, for reaching a data field inside whatever
        object offset_chain resolves to (see apply_pointer_chain's docstring
        for why this is a separate field rather than one more offset_chain
        entry).
        """
        module = mod.get("module")
        offset_chain = [_parse_offset(o) for o in mod.get("offset_chain") or []]
        if not offset_chain:
            result["status"] = "blocked_no_match"
            result["warnings"].append("offset_chain is empty")
            return
        base, size = self._main_module_range(module)
        if not size:
            result["status"] = "blocked_no_match"
            result["warnings"].append(
                "module '{}' not found in the current process.".format(
                    module or "main module"))
            return
        base_offset = _parse_offset(mod.get("base_offset"))
        addr = self._resolve_pointer_chain(base + base_offset, offset_chain)
        if addr is None:
            result["status"] = "blocked_no_match"
            result["warnings"].append(
                "could not read a pointer somewhere along the chain -- "
                "the chain may not have survived a restart/update.")
            return
        struct_offset = _parse_offset(mod.get("struct_offset", 0))
        addr += struct_offset
        result["matches"] = 1
        result["match_addresses"] = ["0x{:X}".format(addr)]
        result["resolved_address"] = "0x{:X}".format(addr)
        result["status"] = "ready"

    # ==================================================================
    # APPLY  (dispatch + gate)
    # ==================================================================
    def apply_mod(self, mod):
        """Apply a mod, refusing unless preview status is 'ready'."""
        preview = self.preview_mod(mod)
        if preview["status"] != "ready":
            self._log(
                "Refusing to apply '{}': status is '{}'.".format(
                    mod.get("name"), preview["status"]
                )
            )
            return False

        template = mod.get("template")
        if template == "hard_freeze":
            return self.apply_hard_freeze(mod)
        if template == "pointer_capture":
            return self.apply_pointer_capture(mod)
        if template == "pointer_chain":
            return self.apply_pointer_chain(mod)
        self._log("Unknown template for '{}'.".format(mod.get("name")))
        return False

    # ==================================================================
    # TEMPLATE: hard_freeze
    # ==================================================================
    def _freeze_len(self, mod):
        """Bytes written per tick: nop_len for 'nop' mode, else 4 (int32)."""
        if mod.get("freeze_mode") == "nop":
            return _parse_int(mod.get("nop_len", 0))
        return 4

    def apply_hard_freeze(self, mod):
        """Continuously overwrite a target address (50ms poll).

        Two modes:
          - default ("value"): write mod['value'] as int32 to match+offset.
            Use when the target is a DATA address the game keeps rewriting.
          - "nop": write nop_len 0x90 bytes over match+offset. Use when the
            target is a CODE instruction that must be neutralised (e.g. a
            'dec [reg+x]' counter decrement). Original bytes are restored on
            stop/detach.
        """
        name = mod.get("name")
        matches = self.scan_aob(mod.get("aob", ""))
        if len(matches) != 1:
            self._log(
                "hard_freeze '{}': need exactly 1 match, got {}. Aborting.".format(
                    name, len(matches)
                )
            )
            return False

        target = matches[0] + _parse_int(mod.get("offset"))
        nop_mode = mod.get("freeze_mode") == "nop"
        length = self._freeze_len(mod)

        original = self._read(target, length) or b""
        patches = []
        if nop_mode:
            if length <= 0:
                self._log("hard_freeze '{}': nop mode needs nop_len > 0.".format(name))
                return False
            # Code write: make the page writable and remember bytes to restore.
            self._make_rwx(target, length)
            patches.append((target, original))
            payload = b"\x90" * length
        else:
            payload = struct.pack("<i", _parse_int(mod.get("value")))

        stop = threading.Event()

        def _loop():
            while not stop.is_set():
                try:
                    self._write(target, payload)
                except Exception:
                    pass  # transient failures (paused/loading) are non-fatal
                time.sleep(0.05)

        thread = threading.Thread(target=_loop, daemon=True)
        self._active[name] = {
            "stop": stop, "thread": thread, "patches": patches,
            "cave": None, "kind": "hard_freeze",
        }
        thread.start()
        self._log(
            "hard_freeze '{}' active at {:#x} ({} mode).".format(
                name, target, "nop" if nop_mode else "value"
            )
        )
        return True

    # ==================================================================
    # TEMPLATE: pointer_capture
    # ==================================================================
    def _build_cave_code(self, steal_bytes, reg_name, slot_addr, code_start,
                         hook_addr, steal_len, use_rel):
        """Assemble the cave body: stolen bytes, capture store, jmp back."""
        code = bytearray()
        code += steal_bytes                                   # relocated originals
        mov_addr = code_start + len(code)
        code += self._enc_mov_slot_reg(reg_name, slot_addr, mov_addr)  # capture
        jmp_addr = code_start + len(code)
        code += self._enc_jmp(jmp_addr, hook_addr + steal_len, use_rel)  # return
        return bytes(code)

    def _install_hook(self, name, hook, slot_addr):
        """Build one hook's cave (writing to the SHARED slot_addr) and patch
        its site. Returns (cave_addr, (hook_addr, original_bytes)) on success,
        or None on failure. Reuses the proven single-hook cave-building path
        (_alloc_cave / _build_cave_code / _enc_jmp) unchanged.
        """
        # caller pre-validated exactly one match in this hook's module.
        hook_addr = self.scan_aob(
            hook.get("aob", ""), module_name=hook.get("module"))[0]
        reg_name = hook.get("capture_register", "eax")
        steal_len = _parse_int(hook.get("steal_len", hook.get("hook_offset")))

        # _alloc_cave places the cave near hook_addr — which is the hook's REAL
        # runtime address in whatever module it lives in (main .exe or a DLL
        # like GameAssembly.dll), so rel32 reach is computed correctly wherever
        # the hook actually is; no assumption that it's near the main exe.
        cave, use_rel = self._alloc_cave(hook_addr)
        min_len = self._min_jmp_len(use_rel)
        if steal_len < min_len:
            self._log(
                "pointer_capture '{}': hook @{:#x} steal_len {} < required {} "
                "for this jmp mode. Aborting.".format(name, hook_addr, steal_len, min_len)
            )
            self._safe_free(cave)
            return None

        code_start = cave + _CODE_OFF
        steal_bytes = self._read(hook_addr, steal_len)
        if not steal_bytes:
            self._log("pointer_capture '{}': could not read hook @{:#x}.".format(name, hook_addr))
            self._safe_free(cave)
            return None

        # Encode both jmps under a guard: the return jmp (inside
        # _build_cave_code) and the outbound jmp below both use rel32 on the
        # near-alloc path, so a miscalculated cave distance surfaces here as
        # JmpOutOfRangeError. Treat it exactly like the steal_len abort: free
        # the cave and return None (the caller rolls back cleanly). Both encodes
        # happen BEFORE any write to the hook site, so no partial patch leaks.
        try:
            # Every hook's cave writes the captured pointer to the SAME slot_addr.
            cave_code = self._build_cave_code(
                steal_bytes, reg_name, slot_addr, code_start, hook_addr,
                steal_len, use_rel,
            )
            jmp_bytes = self._enc_jmp(hook_addr, code_start, use_rel)
        except JmpOutOfRangeError as exc:
            self._log(
                "Hook '{}': jmp target out of range (rel={}), aborting — cave "
                "allocation may have miscalculated proximity.".format(name, exc.rel)
            )
            self._safe_free(cave)
            return None

        self._write(code_start, cave_code)

        # Patch the hook site: jmp to cave + NOP-fill the remainder.
        self._make_rwx(hook_addr, steal_len)
        patch = bytearray(jmp_bytes) + b"\x90" * (steal_len - len(jmp_bytes))
        self._write(hook_addr, patch)

        self._log(
            "  hook @{:#x} -> cave {:#x} (reg {}, steal {}, {}).".format(
                hook_addr, cave, reg_name, steal_len,
                "rel32" if use_rel else "abs-jmp",
            )
        )
        return cave, (hook_addr, bytes(steal_bytes))

    def apply_pointer_capture(self, mod):
        """Hook one OR MORE instructions, all capturing a base pointer into a
        single shared slot, and poll-write [pointer + struct_offset].

        Multiple hooks (e.g. a heal path and a damage path) feed the SAME
        slot: whichever path the game executes refreshes the one pointer the
        poll thread reads.
        """
        name = mod.get("name")
        hooks = self._mod_hooks(mod)
        if not hooks:
            self._log("pointer_capture '{}': no hooks defined. Aborting.".format(name))
            return False

        # --- Pre-validate ALL hooks before writing anything. A mod with only
        #     some hooks working behaves inconsistently, so it's all-or-nothing.
        for i, hook in enumerate(hooks):
            n = len(self.scan_aob(hook.get("aob", ""),
                                  module_name=hook.get("module")))
            if n != 1:
                self._log(
                    "pointer_capture '{}': hook #{} AOB has {} matches in {} "
                    "(need exactly 1). Aborting whole mod.".format(
                        name, i, n, hook.get("module") or "main module")
                )
                return False

        ptr_size = 8 if self._is64 else 4
        struct_offset = _parse_offset(mod.get("struct_offset"))
        poll_mode = mod.get("poll_mode", "never_decrease")
        capture_once = bool(mod.get("capture_once"))

        # --- ONE shared slot for the whole mod. Its own allocation (near hook
        #     #0 on x64 so rip-relative movs from every hook's cave can reach).
        #     VirtualAllocEx is page-aligned, so slot_addr is pointer-aligned
        #     and pointer-sized stores from concurrent hooks are atomic (no
        #     torn pointer); only the first ptr_size bytes are used.
        slot_region, _ = self._alloc_cave(self.scan_aob(
            hooks[0].get("aob", ""), module_name=hooks[0].get("module"))[0])
        slot_addr = slot_region + _SLOT_OFF
        self._write(slot_addr, b"\x00" * ptr_size)

        caves = [slot_region]     # free all of these on teardown
        patches = []              # (addr, original_bytes) per hook, to restore
        for hook in hooks:
            installed = self._install_hook(name, hook, slot_addr)
            if installed is None:
                # Roll back everything already written/allocated: restore any
                # patched bytes and free every cave (including the slot region).
                for addr, original in patches:
                    try:
                        self._make_rwx(addr, len(original))
                        self._write(addr, original)
                    except Exception:
                        pass
                for c in caves:
                    self._safe_free(c)
                return False
            cave, patch_entry = installed
            caves.append(cave)
            patches.append(patch_entry)

        stop = threading.Event()
        # "max": never_decrease tracking. "last_slot": last-seen slot value, so
        # we can log only when the captured pointer CHANGES (not every tick).
        # Init to 0 since the slot was just zeroed at apply.
        # "value": the live value the poll thread writes (hard_set) or clamps to
        # (clamp_min). set_mod_value() reassigns this key at runtime; a single
        # dict-key assignment is atomic under the GIL, so no lock is needed.
        # capture_once fields:
        #   "capture_once": lock the pointer after the first non-zero capture.
        #   "locked_ptr":   the latched pointer (0 = not yet locked).
        #   "recapture":    UI-set flag asking the loop to unlock + relatch.
        state = {
            "max": None, "last_slot": 0, "value": _parse_int(mod.get("value")),
            "capture_once": capture_once, "locked_ptr": 0, "recapture": False,
        }

        # (e) Best-effort attach-time capture — see single-hook note: the slot
        # only holds a real pointer AFTER a hooked instruction runs in-game.
        if mod.get("capture_at_attach"):
            raw = self._read(slot_addr, ptr_size) or b""
            initial = int.from_bytes(raw, "little") if raw else 0
            self._log(
                "pointer_capture '{}': attach-time capture (best-effort) = "
                "{:#x}. If 0, no hooked path has executed yet.".format(name, initial)
            )
            # capture_once + capture_at_attach: if the attach read already got a
            # non-zero pointer, lock to it now (it counts as the "first fire").
            # If it was 0x0, stay unlocked so the first real hook fire locks it.
            if capture_once and initial:
                state["locked_ptr"] = initial
                state["last_slot"] = initial
                self._log(
                    "slot locked (capture_once): 0x{:x} at attach — writes target "
                    "0x{:x} (locked_ptr+offset); further hook fires ignored "
                    "(mod '{}').".format(initial, initial + struct_offset, name)
                )

        def _loop():
            while not stop.is_set():
                try:
                    # Recapture: unlock and clear the slot so the NEXT non-zero
                    # hook fire is captured fresh (not the stale slot value).
                    if state["capture_once"] and state["recapture"]:
                        state["recapture"] = False
                        state["locked_ptr"] = 0
                        state["last_slot"] = 0
                        self._write(slot_addr, b"\x00" * ptr_size)
                        self._log(
                            "recapture (capture_once): '{}' unlocked; slot "
                            "cleared, next hook fire recaptures.".format(name)
                        )

                    raw = self._read(slot_addr, ptr_size)
                    raw_ptr = int.from_bytes(raw, "little") if raw else 0

                    # Resolve the effective pointer. capture_once latches the
                    # first non-zero value and then ignores the (garbage-prone)
                    # slot; normal mods track the live slot every tick.
                    if state["capture_once"]:
                        if state["locked_ptr"]:
                            ptr = state["locked_ptr"]
                        elif raw_ptr:
                            state["locked_ptr"] = raw_ptr
                            ptr = raw_ptr
                            self._log(
                                "slot locked (capture_once): 0x{:x} — writes "
                                "target 0x{:x} (locked_ptr+offset); further hook "
                                "fires ignored (mod '{}').".format(
                                    raw_ptr, raw_ptr + struct_offset, name)
                            )
                        else:
                            ptr = 0
                    else:
                        ptr = raw_ptr

                    if ptr != state["last_slot"]:
                        # capture_once already logs its lock/recapture events;
                        # skip the per-change line so a hot slot doesn't flood.
                        if not state["capture_once"]:
                            self._log(
                                "slot updated: 0x{:x} -> 0x{:x} (mod '{}'); "
                                "writes target 0x{:x}".format(
                                    state["last_slot"], ptr, name,
                                    (ptr + struct_offset) if ptr else 0)
                            )
                        state["last_slot"] = ptr
                    if ptr:
                        target = ptr + struct_offset
                        if poll_mode == "hard_set":
                            # Unconditional overwrite every tick — no read/compare.
                            # Matches the original trainer's continuous-write
                            # freeze: agnostic to how many code paths drain the
                            # value, we just keep slamming it back.
                            self._write(target, struct.pack("<i", state["value"]))
                        else:
                            cur = struct.unpack("<i", self._read(target, 4))[0]
                            if poll_mode == "never_decrease":
                                if state["max"] is None or cur > state["max"]:
                                    state["max"] = cur
                                elif cur < state["max"]:
                                    self._write(target, struct.pack("<i", state["max"]))
                            elif poll_mode == "clamp_min":
                                floor = state["value"]  # snapshot once per tick
                                if cur < floor:
                                    self._write(target, struct.pack("<i", floor))
                            # "set_once" handled by set_once_trigger(), not here.
                except Exception:
                    pass
                time.sleep(0.05)

        thread = threading.Thread(target=_loop, daemon=True)
        self._active[name] = {
            "stop": stop, "thread": thread,
            "patches": patches,            # ALL hooks' original bytes
            "cave": None, "caves": caves,  # slot region + every hook cave
            "kind": "pointer_capture",
            "slot": slot_addr, "struct_offset": struct_offset,
            "poll_mode": poll_mode, "ptr_size": ptr_size,
            "state": state,                # live poll state (settable value)
        }
        if poll_mode != "set_once":
            thread.start()
        self._log(
            "pointer_capture '{}' active: {} hook(s), shared slot {:#x}, "
            "mode {}.".format(name, len(hooks), slot_addr, poll_mode)
        )
        return True

    # ==================================================================
    # TEMPLATE: pointer_chain
    # ==================================================================
    def apply_pointer_chain(self, mod):
        """Poll [module_base + base_offset -> ...offset_chain] (+struct_offset)
        directly -- no hook, no code cave, no capture event.

        offset_chain is a real multi-hop pointer chain: every entry except the
        last is dereferenced (its sum is itself a pointer slot to read), and
        the last entry's sum is returned as-is -- see _resolve_pointer_chain
        and PointerChainFinder.find_pointer_chains' docstring for why (a
        chain discovered via the Pointer Chains window has this shape at
        every level; changing that would break genuine multi-level chains).

        struct_offset is a SEPARATE, optional flat displacement (default 0)
        applied AFTER offset_chain resolves, with NO further dereference --
        exactly pointer_capture's struct_offset semantics. This is what you
        want when offset_chain lands on an object's base address and the
        actual field to poll is a fixed byte offset inside that object (a
        real field holding data, not another pointer to hop through): put
        the object-reaching hops in offset_chain and the field's displacement
        in struct_offset, rather than appending it to offset_chain as a fake
        extra hop -- that would make _resolve_pointer_chain dereference the
        object's own bytes as if they were a pointer, reading garbage.

        The module base is resolved ONCE here (not re-enumerated every poll
        tick -- list_modules() is comparatively expensive to call 20x/sec).
        The offset_chain itself IS re-walked every tick (cheap: one pointer
        read per hop) rather than caching the resolved address -- unlike a
        captured pointer, a chain has no "trigger" telling us when to
        refresh, and re-walking is exactly what makes it self-healing if the
        target object is destroyed and recreated at a new address but the
        chain leading to it is still structurally valid. A hop read failing
        (object not currently alive) just skips that tick's write.
        """
        name = mod.get("name")
        module = mod.get("module")
        offset_chain = [_parse_offset(o) for o in mod.get("offset_chain") or []]
        if not offset_chain:
            self._log("pointer_chain '{}': offset_chain is empty. Aborting.".format(name))
            return False
        base, size = self._main_module_range(module)
        if not size:
            self._log("pointer_chain '{}': module '{}' not found. Aborting.".format(
                name, module or "main module"))
            return False
        base_offset = _parse_offset(mod.get("base_offset"))
        chain_base = base + base_offset
        struct_offset = _parse_offset(mod.get("struct_offset", 0))

        def _resolve():
            return self._resolve_pointer_chain(chain_base, offset_chain)

        if _resolve() is None:
            self._log(
                "pointer_chain '{}': could not resolve the chain (a hop read "
                "failed). Aborting.".format(name))
            return False

        poll_mode = mod.get("poll_mode", "never_decrease")
        state = {"max": None, "value": _parse_int(mod.get("value"))}
        stop = threading.Event()

        def _loop():
            while not stop.is_set():
                try:
                    addr = _resolve()
                    if addr is not None:
                        target = addr + struct_offset
                        if poll_mode == "hard_set":
                            self._write(target, struct.pack("<i", state["value"]))
                        else:
                            raw = self._read(target, 4)
                            if raw:
                                cur = struct.unpack("<i", raw)[0]
                                if poll_mode == "never_decrease":
                                    if state["max"] is None or cur > state["max"]:
                                        state["max"] = cur
                                    elif cur < state["max"]:
                                        self._write(target, struct.pack("<i", state["max"]))
                                elif poll_mode == "clamp_min":
                                    floor = state["value"]
                                    if cur < floor:
                                        self._write(target, struct.pack("<i", floor))
                                # "set_once" handled by set_once_trigger(), not here.
                except Exception:
                    pass
                time.sleep(0.05)

        thread = threading.Thread(target=_loop, daemon=True)
        self._active[name] = {
            "stop": stop, "thread": thread, "patches": [], "cave": None,
            "kind": "pointer_chain",
            # resolve_fn returns the raw offset_chain hop result (the
            # "pointer"), NOT +struct_offset -- _effective_ptr returns it
            # unchanged, and force_set_value/set_once_trigger already add
            # entry["struct_offset"] themselves, exactly like pointer_capture.
            "struct_offset": struct_offset, "ptr_size": 8 if self._is64 else 4,
            "poll_mode": poll_mode, "state": state, "resolve_fn": _resolve,
        }
        if poll_mode != "set_once":
            thread.start()
        self._log(
            "pointer_chain '{}' active: base {:#x}, offset_chain {}, "
            "struct_offset {:#x}, mode {}.".format(
                name, chain_base,
                ["0x{:X}".format(o) for o in offset_chain], struct_offset,
                poll_mode))
        return True

    def _effective_ptr(self, entry):
        """The base pointer a write should target for an active mod.

        Once capture_once has locked, EVERY write path must use the latched
        pointer (state['locked_ptr']) — the raw shared slot keeps receiving
        garbage from the shared hook instruction, so reading it after lock
        would compute the wrong address. Non-capture_once (or not-yet-locked)
        mods fall back to the live slot value, exactly as before.

        Returns (ptr, source) where source is 'locked_ptr' or 'slot' for logs.
        pointer_chain mods have no slot at all -- their "pointer" IS the
        freshly-resolved chain address (struct_offset is always 0 for them),
        so this re-walks the chain via the stored resolve_fn instead.
        """
        if entry.get("kind") == "pointer_chain":
            addr = entry["resolve_fn"]()
            return (addr or 0), "chain"
        state = entry.get("state") or {}
        if state.get("capture_once") and state.get("locked_ptr"):
            return state["locked_ptr"], "locked_ptr"
        raw = self._read(entry["slot"], entry["ptr_size"])
        return (int.from_bytes(raw, "little") if raw else 0), "slot"

    def set_once_trigger(self, mod_name):
        """For poll_mode 'set_once': write mod['value'] once, now."""
        entry = self._active.get(mod_name)
        if not entry or entry.get("poll_mode") != "set_once":
            self._log("set_once_trigger: '{}' not a set_once mod.".format(mod_name))
            return
        mod = next((m for m in self.mods if m.get("name") == mod_name), None)
        if mod is None:
            return
        ptr, src = self._effective_ptr(entry)
        if not ptr:
            self._log("set_once '{}': no pointer captured yet.".format(mod_name))
            return
        target = ptr + entry["struct_offset"]
        self._write(target, struct.pack("<i", _parse_int(mod.get("value"))))
        self._log("set_once '{}': wrote value at {:#x} ({}+offset).".format(
            mod_name, target, src))

    def recapture(self, mod_name):
        """Unlock a capture_once mod so the next hook fire re-latches the slot.

        Lets the user grab a fresh pointer (e.g. after switching to the right
        object) without disabling/re-enabling the mod. The poll thread performs
        the actual unlock + slot-clear on its next tick (flag set atomically).
        """
        entry = self._active.get(mod_name)
        if entry is None or entry.get("kind") != "pointer_capture":
            self._log("recapture: '{}' is not an active pointer_capture mod.".format(
                mod_name))
            return False
        state = entry.get("state")
        if not state or not state.get("capture_once"):
            self._log("recapture: '{}' is not a capture_once mod.".format(mod_name))
            return False
        state["recapture"] = True            # atomic; picked up next poll tick
        self._log("recapture requested for '{}'.".format(mod_name))
        return True

    def set_mod_value(self, name, new_value):
        """Live-update the value an active hard_set / clamp_min mod writes.

        Updates the poll thread's in-memory value (atomic dict assignment, no
        disable/reapply needed) and also mod['value'] in the loaded config so
        it persists across a disable/reapply within this session. Does NOT
        write back to the JSON file.
        """
        entry = self._active.get(name)
        if entry is None:
            self._log("set_mod_value: '{}' is not active.".format(name))
            return False
        if entry.get("poll_mode") not in ("hard_set", "clamp_min"):
            self._log(
                "set_mod_value: '{}' poll_mode '{}' has no settable value.".format(
                    name, entry.get("poll_mode")
                )
            )
            return False
        try:
            new_value = int(new_value)
        except (TypeError, ValueError):
            self._log("set_mod_value: '{}' is not a valid integer.".format(new_value))
            return False

        state = entry.get("state")
        old = state.get("value") if state else None
        if state is not None:
            state["value"] = new_value       # atomic; poll thread reads next tick
        mod = next((m for m in self.mods if m.get("name") == name), None)
        if mod is not None:
            mod["value"] = new_value          # session persistence, not JSON
        self._log("Value updated: '{}' {} -> {}".format(name, old, new_value))
        return True

    def force_set_value(self, mod_name, value):
        """One-time write of `value` to [captured_pointer + struct_offset] for
        ANY active pointer_capture mod, regardless of poll_mode.

        This does NOT alter ongoing poll behaviour (a never_decrease mod stays
        never_decrease; it simply receives one immediate write on top). Rejects
        cleanly if the mod isn't active or no pointer has been captured yet.
        """
        entry = self._active.get(mod_name)
        if entry is None:
            self._log("force_set: '{}' is not active.".format(mod_name))
            return False
        if entry.get("kind") not in ("pointer_capture", "pointer_chain"):
            self._log("force_set: '{}' is not a pointer_capture/pointer_chain "
                      "mod.".format(mod_name))
            return False
        try:
            value = int(value)
        except (TypeError, ValueError):
            self._log("force_set: '{}' is not a valid integer.".format(value))
            return False

        # Prefer the locked pointer once capture_once has latched — the raw slot
        # keeps drifting to garbage from the shared hook, so reading it here
        # would compute the wrong target (this was the capture_once force-set bug).
        ptr, src = self._effective_ptr(entry)
        if not ptr:
            self._log(
                "force_set: '{}' has no pointer captured yet (0x0); "
                "trigger the hooked code path in-game first.".format(mod_name)
            )
            return False

        target = ptr + entry["struct_offset"]
        try:
            self._write(target, struct.pack("<i", value))
        except Exception as exc:
            self._log("force_set: '{}' write failed: {}".format(mod_name, exc))
            return False
        self._log("Force-set '{}' -> {} at {:#x} ({}+offset).".format(
            mod_name, value, target, src))
        return True

    # ==================================================================
    # TOGGLE / TEARDOWN
    # ==================================================================
    def toggle_mod(self, mod_name, enabled):
        """Enable or disable a loaded mod by name."""
        mod = next((m for m in self.mods if m.get("name") == mod_name), None)
        if mod is None:
            self._log("toggle_mod: no mod named '{}'.".format(mod_name))
            return

        if enabled:
            if mod_name in self._active:
                self._log("toggle_mod: '{}' already active.".format(mod_name))
                return
            self.apply_mod(mod)
        else:
            self._teardown_mod(mod_name)

    def _teardown_mod(self, name):
        """Stop a mod's thread and restore any code it patched."""
        entry = self._active.pop(name, None)
        if entry is None:
            return
        entry["stop"].set()
        thread = entry.get("thread")
        if thread and thread.is_alive():
            thread.join(timeout=0.5)
        for address, original in entry.get("patches", []):
            try:
                self._make_rwx(address, len(original))
                self._write(address, original)
            except Exception as exc:
                self._log("Failed to restore bytes at {:#x}: {}".format(address, exc))
        # Free every cave for this mod (single legacy 'cave' + the 'caves' list
        # used by multi-hook pointer_capture — slot region and per-hook caves).
        self._safe_free(entry.get("cave"))
        for cave in entry.get("caves", []):
            self._safe_free(cave)
        self._log("Disabled '{}' (bytes restored, {} cave(s) freed).".format(
            name, len(entry.get("caves", [])) + (1 if entry.get("cave") else 0)))

    def _safe_free(self, cave):
        if not cave:
            return
        try:
            self.pm.free(cave)
        except Exception as exc:
            self._log("Failed to free cave {:#x}: {}".format(cave, exc))

    # ==================================================================
    # Read-only status helpers (thin wrappers for the UI; no side effects)
    # ==================================================================
    def is_attached(self):
        """True if currently attached to a process."""
        return self.pm is not None

    def is_mod_active(self, mod_name):
        """True if the named mod currently has a live patch/poll thread."""
        return mod_name in self._active


# ===========================================================================
# In-app memory value scanner (Part A) -- a Cheat-Engine-style find tool.
# Separate from the AOB/hook scanning above; reuses TrainerEngine's memory
# helpers (_read, pm, _is64, module resolution) rather than duplicating them.
# ===========================================================================

# Value Type table mirrors CE's dropdown. Ints are signed (CE's default).
_SCAN_VALUE_TYPES = {
    "1 byte":  ("<b", 1),
    "2 bytes": ("<h", 2),
    "4 bytes": ("<i", 4),
    "8 bytes": ("<q", 8),
    "float":   ("<f", 4),
    "double":  ("<d", 8),
    "aob":     (None, None),   # array of bytes (hex pattern, wildcards allowed)
}

# Scan types (Next Scan). First Scan accepts only "exact" or "unknown".
_SCAN_TYPES_NUMERIC = (
    "exact", "increased", "decreased", "changed", "unchanged",
    "increased_by", "decreased_by",
)
_SCAN_TYPES_AOB = ("exact", "changed", "unchanged")


class MemoryScanner:
    """Cheat-Engine-style value scanner over a live process.

    Holds candidate addresses + their last-seen values and supports First Scan,
    Next Scan (with CE's scan-type semantics), one-level Undo, and a live value
    refresh. Read/find only -- it never writes to the target.
    """

    MAX_COLLECT = 100000     # cap first-scan collection (bounds memory + time)
    PREVIEW_LIMIT = 200      # rows returned for display
    _CHUNK = 0x100000        # 1 MB read window

    def __init__(self, engine):
        self.engine = engine
        self.value_type = "4 bytes"
        self.region = "All"
        self.results = []        # list of (address, value)
        self._history = []       # stack of prior result lists (undo)
        self._module_map = []    # [(base, end, name)] for the Module column

    # ---- value (de)coding -------------------------------------------------
    def _fmt(self, value_type=None):
        return _SCAN_VALUE_TYPES[value_type or self.value_type]

    def _decode(self, raw, value_type=None):
        fmt, size = self._fmt(value_type)
        if fmt is None:
            return bytes(raw) if raw else None
        if raw is None or len(raw) < size:
            return None
        try:
            return struct.unpack(fmt, raw[:size])[0]
        except struct.error:
            return None

    def _parse_scalar(self, value, value_type=None):
        """Parse a target scalar for a numeric type (hex or decimal)."""
        fmt, _size = self._fmt(value_type)
        if fmt in ("<f", "<d"):
            return float(value)
        if isinstance(value, int):
            return value
        return int(str(value), 0)   # accepts 0x.. and decimal

    # ---- region enumeration ----------------------------------------------
    def _regions(self, region):
        """Yield (base, size) ranges to scan. region: 'All' -> every committed
        readable page; 'main' -> main module; otherwise a named module."""
        eng = self.engine
        if region in (None, "All", "all"):
            yield from self._iter_committed()
        elif region in ("main", "main exe", "Main"):
            base, size = eng._main_module_range(None)
            if size:
                yield base, size
        else:
            base, size = eng._main_module_range(region)
            if size:
                yield base, size

    def _iter_committed(self):
        """Walk the address space via VirtualQueryEx, yielding committed,
        readable (non-guard/non-noaccess) regions.

        Delegates to TrainerEngine._iter_committed (shared with
        PointerChainFinder) rather than duplicating the VirtualQueryEx loop.
        """
        yield from self.engine._iter_committed()

    def _read_chunks(self, base, size, overlap):
        """Yield (chunk_bytes, chunk_offset) covering [base, base+size), reading
        in 1 MB windows with `overlap` extra bytes so a value straddling a
        window boundary is still found."""
        off = 0
        while off < size:
            rlen = min(self._CHUNK + overlap, size - off)
            data = self.engine._read(base + off, rlen)
            if data:
                yield data, off
            off += self._CHUNK

    # ---- module column ----------------------------------------------------
    def _build_module_map(self):
        self._module_map = []
        try:
            for m in self.engine.pm.list_modules():
                b = int(m.lpBaseOfDll)
                self._module_map.append((b, b + int(m.SizeOfImage), m.name))
        except Exception:
            pass
        self._module_map.sort()

    def _module_at(self, addr):
        for b, e, name in self._module_map:
            if b <= addr < e:
                return name
        return ""

    # ---- result formatting ------------------------------------------------
    def _fmt_value(self, value):
        if value is None:
            return "?"
        if isinstance(value, (bytes, bytearray)):
            return value.hex(" ").upper()
        if isinstance(value, float):
            return repr(value)
        return str(value)

    def _row(self, addr, value, prev):
        return {"address": "0x{:X}".format(addr), "address_int": addr,
                "value": self._fmt_value(value),
                "previous": self._fmt_value(prev),
                "module": self._module_at(addr)}

    def _summary(self, truncated=False):
        return {
            "count": len(self.results),
            "results": [self._row(a, v, p)
                        for a, v, p in self.results[:self.PREVIEW_LIMIT]],
            "truncated": bool(truncated) or len(self.results) > self.PREVIEW_LIMIT,
        }

    # ---- scans ------------------------------------------------------------
    def new_scan(self):
        self.results = []
        self._history = []
        return self._summary()

    def first_scan(self, value_type, scan_type, value=None, region="All"):
        if not self.engine.is_attached():
            return {"error": "Not attached -- attach to the game first."}
        if value_type not in _SCAN_VALUE_TYPES:
            return {"error": "Unknown value type: {}".format(value_type)}
        self.value_type = value_type
        self.region = region
        self._history = []
        self._build_module_map()

        if scan_type == "unknown":
            return self._first_unknown(region)
        if scan_type != "exact":
            return {"error": "First scan supports only Exact Value or Unknown "
                             "Initial Value."}
        try:
            return self._first_exact(value_type, value, region)
        except (ValueError, struct.error) as ex:
            return {"error": "Invalid value: {}".format(ex)}

    def _first_exact(self, value_type, value, region):
        fmt, size = self._fmt(value_type)
        results = []
        truncated = False
        if fmt is None:                         # AOB pattern scan
            pattern, mask = TrainerEngine._parse_aob(value or "")
            plen = len(pattern)
            if plen == 0:
                return {"error": "Empty AOB pattern."}
            first_fixed = next((i for i, m in enumerate(mask) if m), 0)
            first_byte = pattern[first_fixed]
            for base, rsize in self._regions(region):
                for chunk, coff in self._read_chunks(base, rsize, plen - 1):
                    pos = 0
                    while True:
                        idx = chunk.find(first_byte, pos)
                        if idx < 0 or idx + plen > len(chunk):
                            break
                        start = idx - first_fixed
                        if start >= 0 and TrainerEngine._match_at(
                                chunk, start, pattern, mask):
                            found = bytes(chunk[start:start + plen])
                            # (address, value, previous) — previous starts equal
                            # to the found value, like CE's first scan.
                            results.append((base + coff + start, found, found))
                            if len(results) >= self.MAX_COLLECT:
                                truncated = True
                                break
                        pos = idx + 1
                    if truncated:
                        break
                if truncated:
                    break
        else:                                   # numeric exact scan
            v = self._parse_scalar(value, value_type)
            needle = struct.pack(fmt, v)
            for base, rsize in self._regions(region):
                for chunk, coff in self._read_chunks(base, rsize, size - 1):
                    pos = 0
                    while True:
                        idx = chunk.find(needle, pos)
                        if idx < 0:
                            break
                        results.append((base + coff + idx, v, v))
                        if len(results) >= self.MAX_COLLECT:
                            truncated = True
                            break
                        pos = idx + 1
                    if truncated:
                        break
                if truncated:
                    break
        self.results = results
        return self._summary(truncated)

    def _first_unknown(self, region):
        fmt, size = self._fmt()
        if fmt is None:
            return {"error": "Unknown Initial Value is not supported for AOB."}
        results = []
        truncated = False
        for base, rsize in self._regions(region):
            for chunk, coff in self._read_chunks(base, rsize, 0):
                n = len(chunk) - (len(chunk) % size)
                for o in range(0, n, size):
                    val = struct.unpack(fmt, chunk[o:o + size])[0]
                    results.append((base + coff + o, val, val))
                    if len(results) >= self.MAX_COLLECT:
                        truncated = True
                        break
                if truncated:
                    break
            if truncated:
                break
        self.results = results
        return self._summary(truncated)

    def next_scan(self, scan_type, value=None):
        if not self.engine.is_attached():
            return {"error": "Not attached -- attach to the game first."}
        fmt, size = self._fmt()
        valid = _SCAN_TYPES_AOB if fmt is None else _SCAN_TYPES_NUMERIC
        if scan_type not in valid:
            return {"error": "Scan type '{}' not valid for {}.".format(
                scan_type, self.value_type)}
        target = None
        try:
            if scan_type == "exact":
                if fmt is None:
                    pattern, _mask = TrainerEngine._parse_aob(value or "")
                    target = bytes(pattern)
                else:
                    target = self._parse_scalar(value)
            elif scan_type in ("increased_by", "decreased_by"):
                target = self._parse_scalar(value)
        except (ValueError, struct.error) as ex:
            return {"error": "Invalid value: {}".format(ex)}

        self._history.append(list(self.results))   # snapshot for undo
        new = []
        for addr, value, _oldprev in self.results:
            cur = self._decode(self.engine._read(addr, size))
            if cur is None:
                continue                            # unreadable now -> drop
            # Compare against the last-known value; on survival that value
            # becomes the "previous" column so the user sees what it was.
            if self._compare(scan_type, cur, value, target):
                new.append((addr, cur, value))
        self.results = new
        return self._summary()

    @staticmethod
    def _compare(scan_type, cur, prev, target):
        if scan_type == "exact":
            return cur == target
        if scan_type == "changed":
            return cur != prev
        if scan_type == "unchanged":
            return cur == prev
        if scan_type == "increased":
            return cur > prev
        if scan_type == "decreased":
            return cur < prev
        if scan_type == "increased_by":
            return cur == prev + target
        if scan_type == "decreased_by":
            return cur == prev - target
        return False

    def undo(self):
        if not self._history:
            return {"error": "Nothing to undo."}
        self.results = self._history.pop()
        return self._summary()

    def refresh_values(self, limit=None):
        """Re-read the CURRENT value of each displayed candidate WITHOUT
        filtering (live view). Does not alter the candidate set."""
        if not self.engine.is_attached():
            return {"error": "Not attached -- attach to the game first."}
        limit = limit or self.PREVIEW_LIMIT
        fmt, size = self._fmt()
        rows = []
        for addr, _value, prev in self.results[:limit]:
            cur = self._decode(self.engine._read(addr, size))
            rows.append(self._row(addr, cur, prev))
        return {"count": len(self.results), "results": rows,
                "truncated": len(self.results) > limit}

    # ---- Watch List support (arbitrary, manually-picked addresses) --------
    # These read/write a single address directly, independent of self.results
    # -- used by the UI's Watch List panel to keep a handful of user-picked
    # addresses live and, optionally, frozen for address verification.
    def read_value(self, address, value_type=None):
        """Read one scalar at `address` for the given value-type key. Does not
        touch or require self.results."""
        if not self.engine.is_attached():
            return None
        fmt, size = self._fmt(value_type)
        if fmt is None:          # aob has no fixed scalar shape to read here
            return None
        return self._decode(self.engine._read(address, size), value_type)

    def write_value(self, address, value, value_type=None):
        """Write one scalar to `address` for the given value-type key.

        Reuses the engine's raw write primitive (engine._write -- the same
        one force_set_value uses) rather than duplicating write logic. This
        is a direct memory write only: no AOB scan, no code cave, no JMP --
        it is NOT a mod. It exists so the Watch List's Freeze checkbox can
        hold a manually-found address steady for verification, mirroring
        Cheat Engine's Freeze, and must stay distinct from pointer_capture /
        hard_freeze mods."""
        if not self.engine.is_attached():
            return False
        fmt, _size = self._fmt(value_type)
        if fmt is None:
            return False
        try:
            self.engine._write(address, struct.pack(fmt, value))
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Part D: Pointer Chain Finder -- automates CE's static pointer-scan.
# ---------------------------------------------------------------------------
# pointer_capture only captures a pointer while its hook instruction is
# actively executing, so a mod is blind until the player triggers that code
# path in-game. A static pointer chain -- module_base + fixed offsets, walked
# fresh every time -- can be read the instant GGMod attaches instead. Finding
# one is CE's classic two-phase algorithm:
#   1. Snapshot every pointer-aligned value in committed memory ONCE (this is
#      the expensive pass -- heap/stack included, not just module images,
#      since that's where object instances actually live).
#   2. Repeatedly binary-search that same snapshot (no further memory scans)
#      for "what points near address X", walking backward from a known
#      runtime address toward something in a module's static range.
# Stage 1 (this section) is snapshot + a single binary-search level. Read-only
# throughout, like MemoryScanner -- it never writes to the target, and
# resolved chains are meant to be copied into a mod by hand, not auto-applied.
class PointerChainFinder:
    """Automates CE's static pointer-scan algorithm over a live process."""

    def __init__(self, engine):
        self.engine = engine

    def build_pointer_snapshot(self, progress_callback=None):
        """One-time pass: read every committed, readable region and collect
        every pointer-aligned slot whose value falls within the committed
        address range observed during this same walk (a cheap garbage filter
        -- most slots aren't pointers at all).

        On a real process this is 10s-100s of MILLIONS of slots (valid heap/
        stack pointers span nearly the whole address space, so the min/max
        filter barely excludes anything) -- both the per-chunk filtering and
        the final sort are vectorized with numpy rather than a Python loop +
        list.sort(), which is what made this hang indefinitely (~275M slots)
        on a real 1.1 GB 32-bit process scan. See find_pointers_to for the
        matching numpy.searchsorted lookup side.

        `progress_callback(done_bytes, total_bytes)`, if given, is invoked
        periodically (throttled) so the UI can show progress -- this pass
        walks the ENTIRE address space and can take a few seconds.

        Returns a snapshot dict: {"values": numpy array (uint32/uint64) sorted
        ascending, "addresses": parallel numpy array (slot address for
        values[i]), "ptr_size": 4 or 8, "min_addr", "max_addr", "count"}, or
        {"error": "..."} if not attached. The dict shape is unchanged from the
        pre-numpy version -- only the values/addresses representation moved
        from Python lists to numpy arrays -- so find_pointer_chains (Stage 2)
        does not need to change.
        """
        if self.engine.pm is None:
            return {"error": "Not attached — attach to the game first."}
        if np is None:
            return {"error": "numpy is not installed; Pointer Chain Finder "
                             "needs it (pip install numpy) to handle the "
                             "tens-to-hundreds of millions of pointer-aligned "
                             "slots a real snapshot collects."}
        ptr_size = 8 if self.engine._is64 else 4
        dtype = np.uint64 if ptr_size == 8 else np.uint32

        regions = list(self.engine._iter_committed())
        if not regions:
            return {"error": "No committed, readable regions found."}
        min_addr = min(base for base, _size in regions)
        max_addr = max(base + size for base, size in regions)

        CHUNK = 0x100000   # 1 MB; divisible by both 4 and 8, so as long as
                           # each chunk starts pointer-aligned (pages are
                           # 4 KB-aligned, so this always holds in practice),
                           # no pointer-aligned slot straddles a chunk edge --
                           # no read overlap needed, unlike AOB byte scanning.
        value_chunks = []
        addr_chunks = []
        total = sum(size for _base, size in regions)
        done = 0
        next_report = 0
        PROGRESS_STEP = 0x400000   # report at most every ~4 MB

        for base, size in regions:
            # First absolute address in this region that's pointer-aligned
            # (region bases are effectively always page-, hence ptr-, aligned
            # already, but this is correct even if that ever weren't true).
            first_off = (-base) % ptr_size
            off = first_off
            while off < size:
                rlen = min(CHUNK, size - off)
                rlen -= rlen % ptr_size    # keep every read slot-aligned
                if rlen == 0:
                    break
                data = self.engine._read(base + off, rlen)
                if data:
                    count = len(data) // ptr_size
                    if count:
                        arr = np.frombuffer(data, dtype=dtype, count=count)
                        mask = (arr >= min_addr) & (arr < max_addr)
                        if mask.any():
                            addr_all = (np.uint64(base + off)
                                       + np.arange(count, dtype=np.uint64)
                                       * np.uint64(ptr_size))
                            value_chunks.append(arr[mask])
                            addr_chunks.append(addr_all[mask].astype(dtype))
                off += rlen
                done += rlen
                if progress_callback and done >= next_report:
                    progress_callback(done, total)
                    next_report = done + PROGRESS_STEP

        if value_chunks:
            values = np.concatenate(value_chunks)
            addresses = np.concatenate(addr_chunks)
        else:
            values = np.empty(0, dtype=dtype)
            addresses = np.empty(0, dtype=dtype)

        order = np.argsort(values, kind="quicksort")
        values = values[order]
        addresses = addresses[order]

        return {
            "values": values,
            "addresses": addresses,
            "ptr_size": ptr_size,
            "min_addr": min_addr,
            "max_addr": max_addr,
            "count": int(values.size),
        }

    def find_pointers_to(self, snapshot, target_address, max_offset=4096,
                         max_results=10000):
        """Binary-search `snapshot` for slots that could plausibly point at
        `target_address` -- i.e. whose value falls in
        [target_address - max_offset, target_address] (a container object's
        base address, plus a small positive field offset, landing exactly on
        the target). Reuses the ONE snapshot -- no memory is touched here.
        Uses numpy.searchsorted (the vectorized equivalent of bisect) against
        the snapshot's numpy arrays, matching build_pointer_snapshot's scale.

        Capped at `max_results` hits; if the cap is hit, `capped` is True so
        the caller can warn that this value is too "common" (e.g. 0 or a
        small int) to be a useful pointer target, and suggest a smaller
        max_offset.

        Returns {"hits": [{"address": L, "offset": target_address - value at
        L}, ...], "capped": bool, "count": total matches before capping} or
        {"error": "..."} if the snapshot looks invalid.
        """
        values = snapshot.get("values")
        addresses = snapshot.get("addresses")
        if values is None or addresses is None:
            return {"error": "Invalid snapshot."}
        # Clamp instead of letting a negative value hit an unsigned numpy
        # array (target_address < max_offset is only possible right near
        # address 0, never a real pointer, but must not raise/wrap).
        lo_value = max(0, target_address - max_offset)
        lo_idx = int(np.searchsorted(values, lo_value, side="left"))
        hi_idx = int(np.searchsorted(values, target_address, side="right"))
        count = hi_idx - lo_idx
        capped = count > max_results
        end = min(hi_idx, lo_idx + max_results)
        # .tolist() first: numpy unsigned subtraction wraps around on
        # underflow instead of going negative, so the offset must be computed
        # in plain Python ints.
        hits = [
            {"address": a, "offset": target_address - v}
            for a, v in zip(addresses[lo_idx:end].tolist(),
                            values[lo_idx:end].tolist())
        ]
        return {"hits": hits, "capped": capped, "count": count}

    def find_pointer_chains(self, snapshot, target_address, max_offset=4096,
                            max_level=5, max_candidates_per_level=25,
                            max_total_calls=5000, max_seconds=120,
                            progress_callback=None):
        """Recursively resolve a static pointer chain to target_address by
        repeatedly calling find_pointers_to (Stage 1, UNCHANGED) against the
        SAME snapshot -- CE's classic algorithm: walk backward from a known
        runtime address, one dereference at a time, until something lands in
        a loaded module's static range. No memory is rescanned between
        levels; only the (cheap) binary search runs again each time.

        At each level, a candidate slot address L either:
          - falls inside a loaded module's static range (_module_for_address):
            the chain TERMINATES here. (module_name, L - module_base) is the
            static anchor.
          - otherwise: L is itself a dynamic (heap/stack) address, so it
            becomes the next level's target and gets its own find_pointers_to
            call, recursing until max_level is hit (that branch is then
            discarded -- no chain within the allowed depth).

        offset_chain is built in RESOLUTION order (module-adjacent offset
        first, target-adjacent offset last) so resolve_chain can consume it
        directly: the first offset is applied to the pointer read from
        module_base + base_offset, the last offset is added but not
        dereferenced again -- it IS target_address.

        Candidates are capped at max_candidates_per_level per level
        (independent of find_pointers_to's own max_results cap). Real heap
        memory generates far more incidental near-hits per level than small
        synthetic tests ever did -- with a per-level cap alone, the number of
        find_pointers_to CALLS still grows multiplicatively across levels
        (branches-per-level ^ max_level), which can run for a very long time
        even with a modest per-level cap. So max_total_calls is a hard
        ceiling on the TOTAL number of find_pointers_to calls made across the
        whole search (all levels, all branches combined); once hit, the
        search stops expanding further branches and returns whatever chains
        were already found, with `capped_total_calls` set on the result so
        the caller can report "search capped after exploring N branches"
        rather than the caller mistaking a bounded-but-incomplete search for
        an exhaustive one. progress_callback(level, calls_made, chains_found),
        if given, is invoked after every find_pointers_to call so a long
        search is observable instead of silent.

        If a level's own find_pointers_to call reports `capped` (too many
        hits -- a "common" value like 0 or a small int), `any_capped` is set
        on the return so the caller can tell "no chain found" apart from
        "search was too common to explore exhaustively, try a smaller
        max_offset" -- candidates ARE still expanded (up to
        max_candidates_per_level) rather than the branch being silently
        dropped, since a genuine chain could still be hiding among a common
        value's hits.

        A slot can coincidentally hold a value within max_offset of its OWN
        address (self-referential/cyclic data), which would otherwise make
        the recursion re-discover the same candidates at ever-increasing
        depth forever (well, until max_level) without ever converging on
        anything new -- pure noise below the real, shorter chain in the
        results. `visited` tracks addresses already used as a target ALONG
        THE CURRENT PATH (not globally -- different branches may legitimately
        revisit an address a sibling branch also used) and skips recursing
        into any candidate that would revisit one. This check runs BEFORE
        the total-call cap is consulted, so cycle detection is never starved
        out by the cap -- a cyclic candidate never spends a call slot at all.

        max_total_calls alone does not bound wall-clock time: each
        find_pointers_to call's cost scales with snapshot size (measured at
        ~2.6s per call on a real ~275M-slot full-process snapshot, vs.
        microseconds on a small synthetic one), so a fixed call count that is
        safe on a small process could still take hours on a large one.
        max_seconds is therefore a wall-clock budget (checked between calls,
        default 120s) that stops the search independently of how many calls
        that budget bought -- whichever cap (calls or time) is hit first wins.

        Returns {"chains": [...], "any_capped": bool,
        "capped_total_calls": bool, "capped_time": bool} where each chain is
        {"module": name_or_None, "base_offset": int, "offset_chain": [...],
        "level_count": int}, deduplicated and sorted ascending by
        level_count (CE convention: fewer levels = more likely stable/
        intentional). Or {"error": "..."} if the snapshot looks invalid.
        """
        if snapshot.get("values") is None:
            return {"error": "Invalid snapshot."}

        chains = []
        seen = set()
        any_capped = [False]
        calls_made = [0]
        capped_total_calls = [False]
        capped_time = [False]
        start_time = time.time()

        def _stop():
            return capped_total_calls[0] or capped_time[0]

        def _recurse(addr, offsets_so_far, level, visited):
            if _stop():
                return
            if calls_made[0] >= max_total_calls:
                capped_total_calls[0] = True
                return
            if time.time() - start_time >= max_seconds:
                capped_time[0] = True
                return
            # max_results is capped to max_candidates_per_level here (rather
            # than find_pointers_to's own default of 10000) since only the
            # first max_candidates_per_level hits are ever used below -- with
            # a real high-branching-factor value, building thousands of
            # unused hit dicts per call was the dominant cost across
            # max_total_calls calls.
            res = self.find_pointers_to(snapshot, addr, max_offset=max_offset,
                                        max_results=max_candidates_per_level)
            calls_made[0] += 1
            if progress_callback:
                progress_callback(level, calls_made[0], len(chains))
            if "error" in res:
                return
            if res["capped"]:
                any_capped[0] = True
            for hit in res["hits"][:max_candidates_per_level]:
                if _stop():
                    break
                slot_addr = hit["address"]
                if slot_addr in visited:
                    continue   # cyclic/self-referential -- not a new chain
                offset = hit["offset"]
                mod_name, mod_base, mod_size = \
                    self.engine._module_for_address(slot_addr)
                offset_chain = [offset] + offsets_so_far
                if mod_size:
                    key = (mod_name, slot_addr - mod_base, tuple(offset_chain))
                    if key not in seen:
                        seen.add(key)
                        chains.append({
                            "module": mod_name,
                            "base_offset": slot_addr - mod_base,
                            "offset_chain": offset_chain,
                            "level_count": level + 1,
                        })
                elif level + 1 < max_level:
                    _recurse(slot_addr, offset_chain, level + 1,
                            visited | {slot_addr})
                # level + 1 == max_level and not in a module: branch
                # discarded -- exceeded max_level without a static base.

        _recurse(target_address, [], 0, {target_address})
        chains.sort(key=lambda c: c["level_count"])
        return {"chains": chains, "any_capped": any_capped[0],
                "capped_total_calls": capped_total_calls[0],
                "capped_time": capped_time[0]}

    def resolve_chain(self, module_name, base_offset, offset_chain):
        """Re-resolve a stored chain description from scratch against
        whatever process is CURRENTLY attached -- this is what makes restart
        verification possible: the exact same (module, base_offset,
        offset_chain) triple, walked fresh, either survives (still lands on
        something sensible) or doesn't (the game's memory layout changed).

        Mirrors find_pointer_chains' offset_chain convention: start at
        module_base + base_offset, then for each offset in order, read a
        pointer at the current address and add the offset to get the next
        address -- the LAST addition is not followed by another read, since
        it IS the final resolved (target) address, not another pointer slot.

        `module_name=None` means the main executable module (matching
        _module_for_address's convention, so a chain found via
        find_pointer_chains can be passed straight through unchanged).

        Returns {"address": resolved_address} or {"error": "..."}.
        """
        if self.engine.pm is None:
            return {"error": "Not attached — attach to the game first."}
        base, size = self.engine._main_module_range(module_name)
        if not size:
            return {"error": "Module '{}' not found in the current process."
                            .format(module_name or "main module")}
        if not offset_chain:
            return {"error": "Empty offset chain."}
        ptr_size = 8 if self.engine._is64 else 4
        fmt = "<Q" if ptr_size == 8 else "<I"

        addr = base + base_offset
        for off in offset_chain:
            raw = self.engine._read(addr, ptr_size)
            if not raw or len(raw) != ptr_size:
                return {"error": "Could not read pointer at {:#x}.".format(addr)}
            ptr = struct.unpack(fmt, raw)[0]
            addr = ptr + off
        return {"address": addr}


# ---------------------------------------------------------------------------
# Part B: "Find what writes to this address" -- hardware-breakpoint debugger.
# ---------------------------------------------------------------------------
# Everything else in GGMod talks to the game with plain ReadProcessMemory /
# WriteProcessMemory through pymem. Catching a hardware (data) breakpoint is
# fundamentally different: it requires GGMod to become the target's actual
# Windows DEBUGGER via DebugActiveProcess plus a WaitForDebugEvent /
# ContinueDebugEvent loop.
#
# Consequences that shape the design below:
#   * Only ONE debugger can be attached to a process at a time, so this is an
#     explicit, user-started session -- never an always-on background mode. It
#     WILL conflict with Cheat Engine's own debug mode on the same process.
#   * Win32 binds debugging to the THREAD that called DebugActiveProcess: only
#     that same thread may call WaitForDebugEvent / ContinueDebugEvent /
#     DebugActiveProcessStop. So every debug API call happens on DebugSession's
#     single worker thread; start()/stop() merely hand it a request and read
#     results back through Event/Queue objects.
#   * A hardware breakpoint left set in a thread's debug registers after the
#     debugger goes away will fault with nobody to handle it and take the game
#     down. Clearing DR0/DR7 on every thread BEFORE DebugActiveProcessStop is
#     therefore mandatory, not best-effort -- see _clear_breakpoints/_cleanup.

TH32CS_SNAPTHREAD = 0x00000004

THREAD_GET_CONTEXT = 0x0008
THREAD_SET_CONTEXT = 0x0010
THREAD_SUSPEND_RESUME = 0x0002
THREAD_QUERY_INFORMATION = 0x0040
_THREAD_BP_ACCESS = (THREAD_GET_CONTEXT | THREAD_SET_CONTEXT
                     | THREAD_SUSPEND_RESUME | THREAD_QUERY_INFORMATION)

# Debug event codes
EXCEPTION_DEBUG_EVENT = 1
CREATE_THREAD_DEBUG_EVENT = 2
CREATE_PROCESS_DEBUG_EVENT = 3
EXIT_THREAD_DEBUG_EVENT = 4
EXIT_PROCESS_DEBUG_EVENT = 5
LOAD_DLL_DEBUG_EVENT = 6
UNLOAD_DLL_DEBUG_EVENT = 7
OUTPUT_DEBUG_STRING_EVENT = 8
RIP_EVENT = 9

# Exception codes we care about.
EXCEPTION_SINGLE_STEP = 0x80000004      # what a data breakpoint raises
EXCEPTION_BREAKPOINT = 0x80000003       # the int3 injected when we attach
# WoW64 equivalents. When a 64-bit debugger debugs a 32-bit process, traps
# raised by the 32-bit code arrive under these WX86 codes instead of the
# native ones above. Failing to recognise them is fatal, not cosmetic: the
# trap falls through to DBG_EXCEPTION_NOT_HANDLED and our own breakpoint gets
# delivered to the game as an unhandled exception, killing it instantly.
STATUS_WX86_SINGLE_STEP = 0x4000001E
STATUS_WX86_BREAKPOINT = 0x4000001F

# Every code that means "this trap is ours to swallow".
_OUR_SINGLE_STEP = (EXCEPTION_SINGLE_STEP, STATUS_WX86_SINGLE_STEP)
_OUR_BREAKPOINT = (EXCEPTION_BREAKPOINT, STATUS_WX86_BREAKPOINT)
_OUR_TRAPS = _OUR_SINGLE_STEP + _OUR_BREAKPOINT

DBG_CONTINUE = 0x00010002
DBG_EXCEPTION_NOT_HANDLED = 0x80010001

# CONTEXT flag sets (architecture-specific prefixes: AMD64=0x00100000,
# x86=0x00010000; |0x10 = DEBUG_REGISTERS, |0x01 = CONTROL for Rip/Eip).
# Architecture prefixes: AMD64=0x00100000, x86=0x00010000.
# 0x01 = CONTEXT_CONTROL (Rip/Eip, Rsp/Esp, segment regs, EFlags)
# 0x10 = CONTEXT_DEBUG_REGISTERS (Dr0-Dr7)
#
# These MUST stay separate. SetThreadContext writes back every group named in
# ContextFlags, so including CONTEXT_CONTROL while arming a breakpoint would
# rewrite the thread's instruction and stack pointers from the snapshot we
# read. For a suspended WoW64 thread that snapshot can be stale (the thread may
# be mid-transition inside wow64cpu.dll), and restoring a stale Eip/Esp crashes
# the target the moment it resumes. Arming therefore uses *_DEBUG_ONLY, and
# CONTROL is only ever used for READS, where writing nothing back is safe.
CONTEXT_AMD64_DEBUG_ONLY = 0x00100000 | 0x10
CONTEXT_X86_DEBUG_ONLY = 0x00010000 | 0x10
CONTEXT_AMD64_CONTROL = 0x00100000 | 0x00000001
CONTEXT_X86_CONTROL = 0x00010000 | 0x00000001

# DR7 length encoding. Note 8 bytes is x64-only, and the watched address must
# be aligned to the length or the CPU simply won't trap correctly.
_DR7_LEN_BITS = {1: 0b00, 2: 0b01, 4: 0b11, 8: 0b10}
_DR7_RW_WRITE = 0b01                    # break on data WRITE
_DR7_LE = 1 << 8                        # local exact breakpoint (ignored on
                                        # modern CPUs, harmless to set)

ERROR_SEM_TIMEOUT = 121
ERROR_ACCESS_DENIED = 5
ERROR_NOT_SUPPORTED = 50
ERROR_INVALID_PARAMETER = 87
_STILL_ACTIVE = 259

_kernel32.GetExitCodeProcess.restype = wintypes.BOOL
_kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE,
                                         ctypes.POINTER(wintypes.DWORD)]


class THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", ctypes.c_long),
        ("tpDeltaPri", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
    ]


# ---------------------------------------------------------------------------
# Process picker support: enumerate running processes that look like "a
# program the user is running" (visible top-level window), for the Attach
# process picker in the UI. Kept here alongside the other Toolhelp32 code.
# ---------------------------------------------------------------------------
TH32CS_SNAPPROCESS = 0x00000002
_MAX_PATH = 260


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * _MAX_PATH),
    ]


_kernel32.Process32FirstW.restype = wintypes.BOOL
_kernel32.Process32FirstW.argtypes = [wintypes.HANDLE,
                                      ctypes.POINTER(PROCESSENTRY32W)]
_kernel32.Process32NextW.restype = wintypes.BOOL
_kernel32.Process32NextW.argtypes = [wintypes.HANDLE,
                                     ctypes.POINTER(PROCESSENTRY32W)]

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.IsWindowVisible.restype = wintypes.BOOL
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.GetWindow.restype = wintypes.HWND
_user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetWindowTextLengthW.restype = ctypes.c_int
_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.GetWindowTextW.restype = ctypes.c_int
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                             ctypes.POINTER(wintypes.DWORD)]
_EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_user32.EnumWindows.restype = wintypes.BOOL
_user32.EnumWindows.argtypes = [_EnumWindowsProc, wintypes.LPARAM]

GW_OWNER = 4


def _snapshot_process_names():
    """pid -> exe filename (e.g. 'GTA5_Enhanced.exe'), via a full process
    snapshot. Unlike an OpenProcess-based name query, this works even for
    processes GGMod doesn't have access rights to open."""
    names = {}
    snap = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == _INVALID_HANDLE_VALUE:
        return names
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = _kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            names[entry.th32ProcessID] = entry.szExeFile
            ok = _kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snap)
    return names


def list_visible_processes():
    """List running processes that own at least one visible, unowned
    top-level window with a title -- i.e. things a user would recognise as
    "a running program", cutting out background services/helpers. GGMod's
    own process is always excluded.

    Returns a list of dicts: {"pid": int, "exe": str, "title": str}, one
    entry per process (first matching window title wins), sorted by title.
    """
    self_pid = os.getpid()
    titles = {}  # pid -> first visible top-level window title

    def _callback(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        if _user32.GetWindow(hwnd, GW_OWNER):
            return True  # an owned window (e.g. a dialog), not a top-level app
        length = _user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if not title:
            return True
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value and pid.value != self_pid and pid.value not in titles:
            titles[pid.value] = title
        return True

    _user32.EnumWindows(_EnumWindowsProc(_callback), 0)

    exe_names = _snapshot_process_names()
    results = [
        {"pid": pid, "exe": exe_names[pid], "title": title}
        for pid, title in titles.items()
        if pid in exe_names
    ]
    results.sort(key=lambda r: r["title"].lower())
    return results


class EXCEPTION_RECORD(ctypes.Structure):
    pass


EXCEPTION_RECORD._fields_ = [
    ("ExceptionCode", wintypes.DWORD),
    ("ExceptionFlags", wintypes.DWORD),
    ("ExceptionRecord", ctypes.POINTER(EXCEPTION_RECORD)),
    ("ExceptionAddress", ctypes.c_void_p),
    ("NumberParameters", wintypes.DWORD),
    ("ExceptionInformation", ctypes.c_size_t * 15),
]


class EXCEPTION_DEBUG_INFO(ctypes.Structure):
    _fields_ = [
        ("ExceptionRecord", EXCEPTION_RECORD),
        ("dwFirstChance", wintypes.DWORD),
    ]


class _DEBUG_EVENT_UNION(ctypes.Union):
    # Only the exception arm is read; _raw just guarantees the union is large
    # enough for every other event kind the OS may write into it.
    _fields_ = [
        ("Exception", EXCEPTION_DEBUG_INFO),
        ("_raw", ctypes.c_byte * 176),
    ]


class DEBUG_EVENT(ctypes.Structure):
    _fields_ = [
        ("dwDebugEventCode", wintypes.DWORD),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
        ("u", _DEBUG_EVENT_UNION),
    ]


class CONTEXT64(ctypes.Structure):
    """x64 CONTEXT, declared exactly through Rip (offset 248) with the rest as
    opaque tail padding so the total size matches the real 1232-byte struct.
    Only ContextFlags / Dr0-Dr7 / Rip are ever touched, and all of those sit in
    the declared prefix. The struct needs 16-byte alignment, which ctypes will
    not guarantee -- see _alloc_context64()."""
    _fields_ = [
        ("P1Home", ctypes.c_ulonglong), ("P2Home", ctypes.c_ulonglong),
        ("P3Home", ctypes.c_ulonglong), ("P4Home", ctypes.c_ulonglong),
        ("P5Home", ctypes.c_ulonglong), ("P6Home", ctypes.c_ulonglong),
        ("ContextFlags", wintypes.DWORD), ("MxCsr", wintypes.DWORD),
        ("SegCs", wintypes.WORD), ("SegDs", wintypes.WORD),
        ("SegEs", wintypes.WORD), ("SegFs", wintypes.WORD),
        ("SegGs", wintypes.WORD), ("SegSs", wintypes.WORD),
        ("EFlags", wintypes.DWORD),
        ("Dr0", ctypes.c_ulonglong), ("Dr1", ctypes.c_ulonglong),
        ("Dr2", ctypes.c_ulonglong), ("Dr3", ctypes.c_ulonglong),
        ("Dr6", ctypes.c_ulonglong), ("Dr7", ctypes.c_ulonglong),
        ("Rax", ctypes.c_ulonglong), ("Rcx", ctypes.c_ulonglong),
        ("Rdx", ctypes.c_ulonglong), ("Rbx", ctypes.c_ulonglong),
        ("Rsp", ctypes.c_ulonglong), ("Rbp", ctypes.c_ulonglong),
        ("Rsi", ctypes.c_ulonglong), ("Rdi", ctypes.c_ulonglong),
        ("R8", ctypes.c_ulonglong), ("R9", ctypes.c_ulonglong),
        ("R10", ctypes.c_ulonglong), ("R11", ctypes.c_ulonglong),
        ("R12", ctypes.c_ulonglong), ("R13", ctypes.c_ulonglong),
        ("R14", ctypes.c_ulonglong), ("R15", ctypes.c_ulonglong),
        ("Rip", ctypes.c_ulonglong),
        ("_tail", ctypes.c_byte * (1232 - 256)),
    ]


class FLOATING_SAVE_AREA(ctypes.Structure):
    _fields_ = [
        ("ControlWord", wintypes.DWORD), ("StatusWord", wintypes.DWORD),
        ("TagWord", wintypes.DWORD), ("ErrorOffset", wintypes.DWORD),
        ("ErrorSelector", wintypes.DWORD), ("DataOffset", wintypes.DWORD),
        ("DataSelector", wintypes.DWORD), ("RegisterArea", ctypes.c_byte * 80),
        ("Cr0NpxState", wintypes.DWORD),
    ]


class WOW64_CONTEXT(ctypes.Structure):
    """32-bit CONTEXT, used for WoW64 targets (a 32-bit game on 64-bit
    Windows). The debug registers are the same physical registers, but the
    instruction pointer must be read as the 32-bit Eip -- the 64-bit Rip of a
    WoW64 thread points into wow64cpu.dll, not into game code."""
    _fields_ = [
        ("ContextFlags", wintypes.DWORD),
        ("Dr0", wintypes.DWORD), ("Dr1", wintypes.DWORD),
        ("Dr2", wintypes.DWORD), ("Dr3", wintypes.DWORD),
        ("Dr6", wintypes.DWORD), ("Dr7", wintypes.DWORD),
        ("FloatSave", FLOATING_SAVE_AREA),
        ("SegGs", wintypes.DWORD), ("SegFs", wintypes.DWORD),
        ("SegEs", wintypes.DWORD), ("SegDs", wintypes.DWORD),
        ("Edi", wintypes.DWORD), ("Esi", wintypes.DWORD),
        ("Ebx", wintypes.DWORD), ("Edx", wintypes.DWORD),
        ("Ecx", wintypes.DWORD), ("Eax", wintypes.DWORD),
        ("Ebp", wintypes.DWORD), ("Eip", wintypes.DWORD),
        ("SegCs", wintypes.DWORD), ("EFlags", wintypes.DWORD),
        ("Esp", wintypes.DWORD), ("SegSs", wintypes.DWORD),
        ("ExtendedRegisters", ctypes.c_byte * 512),
    ]


_kernel32.DebugActiveProcess.restype = wintypes.BOOL
_kernel32.DebugActiveProcess.argtypes = [wintypes.DWORD]
_kernel32.DebugActiveProcessStop.restype = wintypes.BOOL
_kernel32.DebugActiveProcessStop.argtypes = [wintypes.DWORD]
_kernel32.DebugSetProcessKillOnExit.restype = wintypes.BOOL
_kernel32.DebugSetProcessKillOnExit.argtypes = [wintypes.BOOL]
_kernel32.WaitForDebugEvent.restype = wintypes.BOOL
_kernel32.WaitForDebugEvent.argtypes = [ctypes.POINTER(DEBUG_EVENT),
                                        wintypes.DWORD]
_kernel32.ContinueDebugEvent.restype = wintypes.BOOL
_kernel32.ContinueDebugEvent.argtypes = [wintypes.DWORD, wintypes.DWORD,
                                         wintypes.DWORD]
_kernel32.OpenThread.restype = wintypes.HANDLE
_kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.SuspendThread.restype = wintypes.DWORD
_kernel32.SuspendThread.argtypes = [wintypes.HANDLE]
_kernel32.ResumeThread.restype = wintypes.DWORD
_kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_kernel32.Thread32First.restype = wintypes.BOOL
_kernel32.Thread32First.argtypes = [wintypes.HANDLE,
                                    ctypes.POINTER(THREADENTRY32)]
_kernel32.Thread32Next.restype = wintypes.BOOL
_kernel32.Thread32Next.argtypes = [wintypes.HANDLE,
                                   ctypes.POINTER(THREADENTRY32)]
# GetThreadContext/SetThreadContext take a manually 16-aligned buffer for the
# x64 CONTEXT, so they are typed as void* rather than POINTER(CONTEXT64).
_kernel32.GetThreadContext.restype = wintypes.BOOL
_kernel32.GetThreadContext.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
_kernel32.SetThreadContext.restype = wintypes.BOOL
_kernel32.SetThreadContext.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
try:
    _kernel32.Wow64GetThreadContext.restype = wintypes.BOOL
    _kernel32.Wow64GetThreadContext.argtypes = [wintypes.HANDLE,
                                                ctypes.c_void_p]
    _kernel32.Wow64SetThreadContext.restype = wintypes.BOOL
    _kernel32.Wow64SetThreadContext.argtypes = [wintypes.HANDLE,
                                                ctypes.c_void_p]
    _HAVE_WOW64_CTX = True
except AttributeError:                  # 32-bit Windows has no WoW64 layer
    _HAVE_WOW64_CTX = False

_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

# Is GGMod ITSELF a 64-bit process? This matters as much as the target's
# bitness: the Wow64*ThreadContext APIs exist to let a 64-bit caller reach a
# 32-bit thread's context, and they are the wrong call in any other pairing.
_SELF_IS64 = ctypes.sizeof(ctypes.c_void_p) == 8

# Which context API pairing to use for a given (debugger, target) bitness.
CTX_NATIVE64 = "native64"    # 64-bit GGMod  -> 64-bit target
CTX_NATIVE32 = "native32"    # 32-bit GGMod  -> 32-bit target (no WoW64 layer)
CTX_WOW64 = "wow64"          # 64-bit GGMod  -> 32-bit target (WoW64)
CTX_UNSUPPORTED = "unsupported"   # 32-bit GGMod -> 64-bit target


def context_mode(target_is64, self_is64=None, have_wow64=None):
    """Pick the thread-context API path for a debugger/target bitness pair.

    Windows requires Wow64GetThreadContext/Wow64SetThreadContext specifically
    when a 64-bit process manipulates a 32-bit (WoW64) thread's context --
    plain Get/SetThreadContext there returns the 64-bit wow64cpu.dll context,
    not the game's real 32-bit one. Conversely a 32-bit debugger talking to a
    32-bit target has no WoW64 layer at all and must use the plain APIs, whose
    native CONTEXT is the 32-bit layout. A 32-bit process cannot debug a
    64-bit one at all.
    """
    if self_is64 is None:
        self_is64 = _SELF_IS64
    if have_wow64 is None:
        have_wow64 = _HAVE_WOW64_CTX
    if target_is64:
        return CTX_NATIVE64 if self_is64 else CTX_UNSUPPORTED
    if not self_is64:
        return CTX_NATIVE32
    return CTX_WOW64 if have_wow64 else CTX_UNSUPPORTED


def _alloc_context64():
    """Return (backing_buffer, CONTEXT64 view) 16-byte aligned.

    The x64 CONTEXT is declared DECLSPEC_ALIGN(16) and GetThreadContext fails
    with ERROR_NOACCESS/invalid-parameter on a misaligned buffer, which ctypes
    (8-byte alignment) does not guarantee. Over-allocate and align by hand. The
    caller MUST keep the returned buffer alive for as long as the view is used.
    """
    raw = ctypes.create_string_buffer(ctypes.sizeof(CONTEXT64) + 16)
    aligned = (ctypes.addressof(raw) + 15) & ~15
    return raw, ctypes.cast(aligned, ctypes.POINTER(CONTEXT64)).contents


def _dr7_for(slot, size):
    """Build a DR7 value enabling `slot` (0-3) as a break-on-write watchpoint
    of `size` bytes. Returns 0 for an unsupported size."""
    if size not in _DR7_LEN_BITS:
        return 0
    enable = 1 << (slot * 2)                      # L0/L1/L2/L3 local enable
    field = (_DR7_RW_WRITE | (_DR7_LEN_BITS[size] << 2)) << (16 + slot * 4)
    return enable | field | _DR7_LE


class DebugAttachError(Exception):
    """DebugActiveProcess failed. Carries the raw Win32 error so the UI can
    explain the common causes (already debugged / privileges)."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class DebugSession:
    """Watch an address for writes using an x86/x64 hardware breakpoint.

    Lifecycle is strictly user-driven: start() attaches as a debugger and arms
    DR0 on every thread; stop() disarms and detaches. Hits arrive on the
    thread-safe `hits` queue -- the debug loop NEVER touches the UI directly.
    """

    # Slot 0 of DR0-DR3. One watched address at a time keeps the debug-register
    # bookkeeping (and the DR6 hit attribution) trivial; the other three slots
    # stay free and untouched.
    _SLOT = 0
    _WAIT_MS = 100            # WaitForDebugEvent timeout -> stop-flag polling
    _MAX_HITS = 5000          # bound the queue if a hot instruction spams us

    def __init__(self, engine, log_callback=None):
        self.engine = engine
        self._log = log_callback or (lambda msg: None)
        self.hits = queue.Queue()
        self._thread = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._result = None
        self._pid = None
        self._address = None
        self._size = 4
        self._is64 = True
        self._attached = False
        self._hit_no = 0
        self._counts = {}         # instruction address -> times seen
        self._armed = set()       # thread ids we successfully armed

    # ---- public API (called from the UI thread) -------------------------
    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, address, size=4, timeout=10.0):
        """Attach as debugger and arm a write-watchpoint on `address`.

        Blocks only until the worker reports the attach outcome, then returns
        {"ok": True, ...} or {"error": "..."}; the event loop keeps running on
        the worker thread afterwards.
        """
        if self.is_running():
            return {"error": "A watch session is already running -- stop it first."}
        if self.engine.pm is None:
            return {"error": "Not attached -- attach to the game first."}
        if capstone is None:
            return {"error": "capstone is not installed; cannot decode the "
                             "writing instruction."}
        try:
            address = int(address)
        except (TypeError, ValueError):
            return {"error": "Invalid address."}

        self._pid = int(self.engine.pm.process_id)
        self._is64 = bool(getattr(self.engine, "_is64", True))
        mode = context_mode(self._is64)
        if mode == CTX_UNSUPPORTED:
            return {"error": (
                "Cannot debug a 64-bit game from a 32-bit GGMod build. Use "
                "the 64-bit build to watch this process."
                if self._is64 else
                "This Windows build has no WoW64 thread-context support, so a "
                "32-bit game cannot be watched from 64-bit GGMod.")}
        self._address = address
        self._size = self._usable_size(address, size)
        self._hit_no = 0
        self._counts = {}
        self._armed = set()
        self._result = None
        self._stop.clear()
        self._ready.clear()
        # Drain any hits left over from a previous session.
        while not self.hits.empty():
            try:
                self.hits.get_nowait()
            except queue.Empty:
                break

        self._thread = threading.Thread(
            target=self._run, name="GGModDebugSession", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            self._stop.set()
            return {"error": "Timed out waiting for the debugger to attach."}
        return self._result or {"error": "Debugger failed to start."}

    def stop(self, timeout=10.0):
        """Signal the worker to disarm + detach, and wait for it to finish."""
        if not self.is_running():
            self._stop.set()
            return {"ok": True, "note": "No watch session was running."}
        self._stop.set()
        self._thread.join(timeout)
        if self._thread.is_alive():
            self._log("Debug session did not stop within {}s -- the debugger "
                      "may still be attached.".format(timeout))
            return {"error": "Watch session did not stop cleanly."}
        self._thread = None
        return {"ok": True}

    def _usable_size(self, address, size):
        """Clamp the watch length to something the CPU can actually arm: a
        supported width, 8 bytes only on x64, and aligned to the address (an
        unaligned data breakpoint does not trap reliably, so degrade rather
        than silently miss writes)."""
        if size not in _DR7_LEN_BITS:
            size = 4
        if size == 8 and not self._is64:
            size = 4
        while size > 1 and (address % size) != 0:
            size //= 2
        return size

    # ---- worker thread: EVERY debug API call happens below here ---------
    def _run(self):
        try:
            self._attach()
            armed = self._set_breakpoints()
            if not armed:
                raise DebugAttachError(
                    0, "Attached, but no thread could be armed with a "
                       "hardware breakpoint.")
            self._result = {
                "ok": True, "pid": self._pid, "address": self._address,
                "size": self._size, "threads": armed,
            }
        except DebugAttachError as exc:
            self._result = {"error": str(exc), "code": exc.code}
            self._safe_cleanup()
            self._ready.set()
            return
        except Exception as exc:
            self._result = {"error": "Debugger error: {}".format(exc)}
            self._safe_cleanup()
            self._ready.set()
            return
        finally:
            self._ready.set()

        try:
            self._loop()
        except Exception as exc:
            self._log("Debug loop error: {}".format(exc))
        finally:
            self._safe_cleanup()

    def _attach(self):
        if not _kernel32.DebugActiveProcess(self._pid):
            err = ctypes.get_last_error()
            raise DebugAttachError(err, self._attach_error_text(err))
        self._attached = True
        # Without this, the game is KILLED when GGMod's debugger goes away --
        # including if GGMod crashes. Must be set right after attaching.
        if not _kernel32.DebugSetProcessKillOnExit(False):
            self._log("Warning: DebugSetProcessKillOnExit failed ({}); the "
                      "game could be killed if GGMod exits abruptly.".format(
                          ctypes.get_last_error()))
        self._log("Debugger attached to pid {}.".format(self._pid))

    def _process_alive(self):
        """True if the target is still running. Used to disambiguate the
        attach error below -- Windows reports the same code either way."""
        try:
            handle = self.engine.pm.process_handle
            code = wintypes.DWORD(0)
            if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True             # can't tell; assume alive
            return code.value == _STILL_ACTIVE
        except Exception:
            return True

    _ALREADY_DEBUGGED = (
        "Could not attach as debugger — another debugger is already attached "
        "to this process. Is Cheat Engine's debug mode active on it? Close it "
        "and try again. (Windows allows only one debugger per process.)")

    def _attach_error_text(self, err):
        if err == ERROR_ACCESS_DENIED:
            return (self._ALREADY_DEBUGGED + " If no other debugger is "
                    "running, try starting GGMod as administrator.")
        if err == ERROR_NOT_SUPPORTED:
            return ("Could not attach as debugger: not supported for this "
                    "process (32/64-bit mismatch or a protected process).")
        if err == ERROR_INVALID_PARAMETER:
            # Windows returns ERROR_INVALID_PARAMETER both when the pid does
            # not exist AND when the process is already being debugged, so the
            # code alone cannot tell them apart -- ask the process itself.
            if self._process_alive():
                return self._ALREADY_DEBUGGED
            return ("Could not attach as debugger: the process is no longer "
                    "running.")
        return "Could not attach as debugger (Win32 error {}).".format(err)

    def _iter_thread_ids(self):
        snap = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
        if not snap or snap == _INVALID_HANDLE_VALUE:
            return []
        ids = []
        try:
            entry = THREADENTRY32()
            entry.dwSize = ctypes.sizeof(THREADENTRY32)
            ok = _kernel32.Thread32First(snap, ctypes.byref(entry))
            while ok:
                if entry.th32OwnerProcessID == self._pid:
                    ids.append(entry.th32ThreadID)
                ok = _kernel32.Thread32Next(snap, ctypes.byref(entry))
        finally:
            _kernel32.CloseHandle(snap)
        return ids

    def _apply_dr(self, tid, address, dr7):
        """Set (or clear, with address=0/dr7=0) the slot-0 debug register on
        one thread. Returns True on success. Suspends the thread around the
        context swap, which is required for a reliable SetThreadContext.

        Only CONTEXT_DEBUG_REGISTERS is requested, so SetThreadContext writes
        back the debug registers and NOTHING else. Including CONTEXT_CONTROL
        here would also restore Eip/Esp from the snapshot -- unreliable for a
        suspended WoW64 thread and a crash-on-resume for the target.
        """
        mode = context_mode(self._is64)
        if mode == CTX_UNSUPPORTED:
            return False
        handle = _kernel32.OpenThread(_THREAD_BP_ACCESS, False, tid)
        if not handle:
            return False
        try:
            if _kernel32.SuspendThread(handle) == 0xFFFFFFFF:
                return False
            try:
                if mode == CTX_NATIVE64:
                    _raw, ctx = _alloc_context64()
                    ctx.ContextFlags = CONTEXT_AMD64_DEBUG_ONLY
                    if not _kernel32.GetThreadContext(handle,
                                                      ctypes.byref(ctx)):
                        return False
                    ctx.Dr0 = address
                    ctx.Dr6 = 0
                    ctx.Dr7 = dr7
                    ctx.ContextFlags = CONTEXT_AMD64_DEBUG_ONLY
                    return bool(_kernel32.SetThreadContext(handle,
                                                           ctypes.byref(ctx)))
                # Both 32-bit target paths share the 32-bit CONTEXT layout and
                # differ only in which API reaches it: Wow64* from a 64-bit
                # GGMod, the plain ones when GGMod is itself 32-bit.
                get = (_kernel32.Wow64GetThreadContext if mode == CTX_WOW64
                       else _kernel32.GetThreadContext)
                put = (_kernel32.Wow64SetThreadContext if mode == CTX_WOW64
                       else _kernel32.SetThreadContext)
                ctx = WOW64_CONTEXT()
                ctx.ContextFlags = CONTEXT_X86_DEBUG_ONLY
                if not get(handle, ctypes.byref(ctx)):
                    return False
                ctx.Dr0 = address & 0xFFFFFFFF
                ctx.Dr6 = 0
                ctx.Dr7 = dr7 & 0xFFFFFFFF
                ctx.ContextFlags = CONTEXT_X86_DEBUG_ONLY
                return bool(put(handle, ctypes.byref(ctx)))
            finally:
                _kernel32.ResumeThread(handle)
        finally:
            _kernel32.CloseHandle(handle)

    def _set_breakpoints(self):
        """Arm the write-watchpoint on every current thread. A write can come
        from any thread, so all of them get it; newly created threads are armed
        as their CREATE_THREAD_DEBUG_EVENT arrives."""
        dr7 = _dr7_for(self._SLOT, self._size)
        if not dr7:
            raise DebugAttachError(0, "Unsupported watch size {}.".format(
                self._size))
        armed = 0
        for tid in self._iter_thread_ids():
            if self._apply_dr(tid, self._address, dr7):
                self._armed.add(tid)
                armed += 1
        self._log("Armed write-watchpoint at {:#x} ({} byte(s)) on {} "
                  "thread(s).".format(self._address, self._size, armed))
        return armed

    def _clear_breakpoints(self):
        """Disarm slot 0 everywhere. Runs before DebugActiveProcessStop --
        a breakpoint left armed once the debugger detaches raises an exception
        with nobody to handle it and crashes the game."""
        cleared = 0
        # Re-enumerate rather than trusting self._armed: threads may have been
        # created (and armed) after the initial pass.
        for tid in set(self._iter_thread_ids()) | set(self._armed):
            if self._apply_dr(tid, 0, 0):
                cleared += 1
        self._armed.clear()
        if cleared:
            self._log("Cleared write-watchpoint on {} thread(s).".format(cleared))
        return cleared

    def _drain_pending(self, budget=3.0, wait_ms=60):
        """Continue every debug event still queued, until none is pending.

        This is a correctness requirement, not a tidy-up: a trap that fired
        while we were not pumping stays STOPPED in the kernel, and
        DebugActiveProcessStop delivers any un-continued exception to the app
        as unhandled -- a STATUS_SINGLE_STEP with no handler terminates the
        game. Draining with DBG_CONTINUE first is what makes detach safe.
        Only safe to call once the breakpoints are disarmed, otherwise new
        traps keep arriving and this never finishes.
        """
        evt = DEBUG_EVENT()
        deadline = time.time() + budget
        drained = 0
        while time.time() < deadline:
            if not _kernel32.WaitForDebugEvent(ctypes.byref(evt), wait_ms):
                break                      # timed out => queue is empty
            status = DBG_CONTINUE
            if evt.dwDebugEventCode == EXCEPTION_DEBUG_EVENT:
                code = (evt.u.Exception.ExceptionRecord.ExceptionCode
                        & 0xFFFFFFFF)
                # Swallow our own traps (native and WoW64 WX86 codes alike);
                # hand anything else back to the game.
                if code not in _OUR_TRAPS:
                    status = DBG_EXCEPTION_NOT_HANDLED
            exiting = evt.dwDebugEventCode == EXIT_PROCESS_DEBUG_EVENT
            _kernel32.ContinueDebugEvent(evt.dwProcessId, evt.dwThreadId,
                                         status)
            drained += 1
            if exiting:
                self._attached = False     # nothing left to detach from
                break
        return drained

    def _safe_cleanup(self):
        """Idempotent teardown: disarm, flush pending traps, then detach.

        Order matters. Disarm first so no new traps can be generated, then
        drain what already fired, and only then stop debugging -- detaching
        with a trap still pending crashes the game.
        """
        try:
            if self._attached:
                self._clear_breakpoints()
                self._drain_pending()
        except Exception as exc:
            self._log("Error clearing breakpoints: {}".format(exc))
        try:
            if self._attached:
                if _kernel32.DebugActiveProcessStop(self._pid):
                    self._log("Debugger detached from pid {}.".format(self._pid))
                else:
                    self._log("DebugActiveProcessStop failed ({}).".format(
                        ctypes.get_last_error()))
        except Exception as exc:
            self._log("Error detaching debugger: {}".format(exc))
        finally:
            self._attached = False

    def _loop(self):
        """WaitForDebugEvent / ContinueDebugEvent pump.

        Uses a short timeout so the stop flag is honoured promptly. Every event
        MUST be continued, otherwise the game stays frozen -- so the continue
        status is decided per event and always sent.
        """
        evt = DEBUG_EVENT()
        while not self._stop.is_set():
            if not _kernel32.WaitForDebugEvent(ctypes.byref(evt), self._WAIT_MS):
                err = ctypes.get_last_error()
                if err == ERROR_SEM_TIMEOUT:
                    continue                      # nothing happened; poll stop
                self._log("WaitForDebugEvent failed ({}).".format(err))
                break

            status = DBG_CONTINUE
            code = evt.dwDebugEventCode

            if code == EXCEPTION_DEBUG_EVENT:
                status = self._on_exception(evt)
            elif code == CREATE_THREAD_DEBUG_EVENT:
                # A brand-new thread starts with empty debug registers, so it
                # would silently miss writes unless we arm it too.
                dr7 = _dr7_for(self._SLOT, self._size)
                if self._apply_dr(evt.dwThreadId, self._address, dr7):
                    self._armed.add(evt.dwThreadId)
            elif code == EXIT_THREAD_DEBUG_EVENT:
                self._armed.discard(evt.dwThreadId)
            elif code == EXIT_PROCESS_DEBUG_EVENT:
                self._log("Target process exited; stopping watch session.")
                _kernel32.ContinueDebugEvent(evt.dwProcessId, evt.dwThreadId,
                                             DBG_CONTINUE)
                self._attached = False   # nothing left to detach from
                break

            if not _kernel32.ContinueDebugEvent(evt.dwProcessId,
                                                evt.dwThreadId, status):
                self._log("ContinueDebugEvent failed ({}).".format(
                    ctypes.get_last_error()))
                break

    def _on_exception(self, evt):
        """Classify a first-chance exception. Returns the continue status.

        Only OUR data breakpoint is swallowed (DBG_CONTINUE). Everything else
        is handed back to the game (DBG_EXCEPTION_NOT_HANDLED) so its own
        exception handling keeps working exactly as it would unattached.
        """
        rec = evt.u.Exception.ExceptionRecord
        code = rec.ExceptionCode & 0xFFFFFFFF

        if code in _OUR_SINGLE_STEP:
            # Native STATUS_SINGLE_STEP, or STATUS_WX86_SINGLE_STEP when the
            # trap came from 32-bit code in a WoW64 target.
            try:
                self._record_hit(evt.dwThreadId)
            except Exception as exc:
                self._log("Error recording hit: {}".format(exc))
            return DBG_CONTINUE
        if code in _OUR_BREAKPOINT:
            # The int3 Windows injects into the target when a debugger
            # attaches. Ours to swallow; passing it on would crash the game.
            return DBG_CONTINUE
        return DBG_EXCEPTION_NOT_HANDLED

    def _read_ip(self, tid):
        """Return the trapping thread's instruction pointer, or None.

        Read-only, so CONTEXT_CONTROL is safe to request here -- nothing is
        written back. For a WoW64 target the 32-bit Eip is the game's real
        instruction pointer; the 64-bit Rip of such a thread points into
        wow64cpu.dll and would be useless for locating game code.
        """
        mode = context_mode(self._is64)
        if mode == CTX_UNSUPPORTED:
            return None
        handle = _kernel32.OpenThread(_THREAD_BP_ACCESS, False, tid)
        if not handle:
            return None
        try:
            if mode == CTX_NATIVE64:
                _raw, ctx = _alloc_context64()
                ctx.ContextFlags = CONTEXT_AMD64_CONTROL
                if not _kernel32.GetThreadContext(handle, ctypes.byref(ctx)):
                    return None
                return int(ctx.Rip)
            get = (_kernel32.Wow64GetThreadContext if mode == CTX_WOW64
                   else _kernel32.GetThreadContext)
            ctx = WOW64_CONTEXT()
            ctx.ContextFlags = CONTEXT_X86_CONTROL
            if not get(handle, ctypes.byref(ctx)):
                return None
            return int(ctx.Eip)
        finally:
            _kernel32.CloseHandle(handle)

    def _resolve_writer(self, next_ip):
        """Find the instruction that performed the write.

        A data breakpoint traps AFTER the storing instruction retires, so the
        reported IP is the address of the NEXT instruction. Delegates the
        actual backward resolution to TrainerEngine._locate_instruction_
        boundary -- the anchor-and-disassemble-forward technique, shared with
        build_hard_freeze_candidate_from_address rather than duplicated here.
        (self._is64 is kept in sync with self.engine._is64 at start(), so
        using the engine's own disassembler here is equivalent to before.)

        Returns (address, text, bytes_hex) for the writer, or None.
        """
        best = self.engine._locate_instruction_boundary(next_ip)
        if best is None:
            return None
        return (best.address,
                "{} {}".format(best.mnemonic, best.op_str).strip(),
                bytes(best.bytes).hex(" ").upper())

    def _record_hit(self, tid):
        """Turn a trap into a report and push it onto the thread-safe queue.

        Never touches the UI -- the UI drains `hits` on its own timer.
        """
        next_ip = self._read_ip(tid)
        if next_ip is None:
            return
        resolved = self._resolve_writer(next_ip)
        if resolved:
            ip, text, raw = resolved
        else:
            # Fall back to reporting the trap site itself rather than dropping
            # the hit; the user still gets a usable address to inspect.
            ip, text, raw = next_ip, "(could not decode writing instruction)", ""
        module, _base, _size = self.engine._module_for_address(ip)
        self._hit_no += 1
        self._counts[ip] = self._counts.get(ip, 0) + 1
        if self.hits.qsize() < self._MAX_HITS:
            self.hits.put({
                "n": self._hit_no,
                "time": time.strftime("%H:%M:%S"),
                "address": "0x{:X}".format(ip),
                "address_int": ip,
                "next_ip": "0x{:X}".format(next_ip),
                "module": module or "main exe",
                "text": text,
                "bytes": raw,
                "count": self._counts[ip],
                "thread": tid,
            })
