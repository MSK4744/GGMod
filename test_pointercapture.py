"""Standalone pointer_capture test harness for GGMod.

Isolates the pointer_capture template (POP "Infinite Health", now a MULTI-HOOK
mod: an esi heal-path hook and an edi damage-path hook, both feeding one
shared slot) end-to-end against a live POP.exe. Manual gates let you read the
preview before applying and exercise both hooks (take damage, then heal)
before bytes are restored. A background poller prints the shared slot every
~2s so you can watch whether/when a real pointer gets captured.

Run from the GGMod root:  python test_pointercapture.py
"""

import threading
import time

from engine import TrainerEngine

CONFIG = "games/POP_test.json"
PROCESS = "POP.exe"
TARGET_MOD = "Infinite Health"  # pointer_capture, multi-hook


def log(msg):
    print("[LOG] {}".format(msg))


def print_preview(name, preview):
    print("\n--- Preview: {} ---".format(name))
    print("  status        : {}".format(preview.get("status")))
    print("  total matches : {}".format(preview.get("matches")))
    hooks = preview.get("hooks")
    if hooks is not None:
        for hr in hooks:
            ok = hr.get("status") == "ready"
            print("  hook[{}] {}  matches={}  reg={}  steal={}".format(
                hr.get("index"), "OK" if ok else "BLOCKED",
                hr.get("matches"), hr.get("capture_register"), hr.get("steal_len")))
            print("     status : {}".format(hr.get("status")))
            print("     aob    : {}".format(hr.get("aob")))
            print("     orig   : {}".format(hr.get("original_bytes")))
            for w in (hr.get("warnings") or []):
                print("     warn   : {}".format(w))
    else:
        print("  original_bytes: {}".format(preview.get("original_bytes")))
    for w in (preview.get("warnings") or []):
        print("  warning       : {}".format(w))


def read_slot(engine, name):
    """Return the current captured pointer from the mod's shared slot, or None."""
    entry = engine._active.get(name)
    if not entry or "slot" not in entry:
        return None
    raw = engine._read(entry["slot"], entry["ptr_size"])
    if not raw:
        return None
    return int.from_bytes(raw, "little")


def show_slot(engine, name, label):
    val = read_slot(engine, name)
    if val is None:
        shown = "unavailable"
    elif val == 0:
        shown = "0x0 (no hooked path has executed yet)"
    else:
        shown = hex(val)
    print(">> Shared slot {}: {}".format(label, shown))


def main():
    engine = TrainerEngine(log_callback=log)

    if not engine.attach(PROCESS):
        print("\nCould not attach to {}. Is the game running? Aborting.".format(PROCESS))
        return

    engine.load_game_config(CONFIG)

    mod = next((m for m in engine.mods if m.get("name") == TARGET_MOD), None)
    if mod is None:
        print("\nMod '{}' not found in config. Aborting.".format(TARGET_MOD))
        engine.detach()
        return
    if mod.get("template") != "pointer_capture":
        print("\n'{}' is not a pointer_capture mod. Aborting.".format(TARGET_MOD))
        engine.detach()
        return

    # Preview (per-hook)
    preview = engine.preview_mod(mod)
    print_preview(TARGET_MOD, preview)
    if preview.get("status") != "ready":
        print("\nNot applying — preview status is '{}'. Aborting.".format(
            preview.get("status")))
        engine.detach()
        return

    input("\nReview the preview above. Press Enter to APPLY '{}'... ".format(TARGET_MOD))

    # 1. Apply
    if not engine.apply_mod(mod):
        print("Apply FAILED for '{}'. Aborting.".format(TARGET_MOD))
        engine.detach()
        return

    # Snapshot ALL hooks' patch entries (hook_addr, original_bytes).
    hook_patches = list(engine._active[TARGET_MOD]["patches"])
    print("\nApplied {} hook(s):".format(len(hook_patches)))
    for i, (addr, original) in enumerate(hook_patches):
        print("  hook[{}] @ {:#x}  original: {}".format(
            i, addr, original.hex(" ") if original else None))

    # 2. Slot value immediately after apply (best-effort attach-time capture).
    show_slot(engine, TARGET_MOD, "BEFORE test (just applied)")

    print("\n" + "#" * 64)
    print("# APPLIED - exercise BOTH hooks: take damage, then heal.")
    print("# Health should stay maxed (never_decrease) once a hook fires.")
    print("#" * 64)

    # Background poller: print the slot every ~2s throughout the test.
    stop_poll = threading.Event()

    def _poll():
        while not stop_poll.is_set():
            val = read_slot(engine, TARGET_MOD)
            if val is None:
                shown = "unavailable"
            elif val == 0:
                shown = "0x0 (no hook fired yet)"
            else:
                shown = hex(val)
            print("   [slot poll] captured pointer = {}".format(shown))
            stop_poll.wait(2.0)

    poller = threading.Thread(target=_poll, daemon=True)
    poller.start()

    # 3. Exercise the damage-path (edi) hook.
    input("\nTake DAMAGE now, then press Enter... ")
    show_slot(engine, TARGET_MOD, "after taking damage (edi damage-path hook)")

    # 4. Exercise the heal-path (esi) hook.
    input("\nNow HEAL, then press Enter... ")
    show_slot(engine, TARGET_MOD, "after healing (esi heal-path hook)")

    # Capture the 'while-on' patched bytes for every hook BEFORE detaching,
    # so the restoration report can show original / while-on / after-off.
    while_on = []
    for addr, original in hook_patches:
        patched = engine._read(addr, len(original))
        while_on.append(patched)

    # 5. Detach (stops poll thread, restores ALL hooks' bytes, frees all caves).
    stop_poll.set()
    poller.join(timeout=1.0)
    input("\nPress Enter to DETACH and restore bytes... ")
    engine.detach()

    # Per-hook restoration check: re-attach and read each hook site back.
    print("\n--- Restoration check ({} hook(s)) ---".format(len(hook_patches)))
    verify = TrainerEngine(log_callback=log)
    if not verify.attach(PROCESS):
        print("  (Could not re-attach to verify; game may have closed.)")
        print("\nDone.")
        return

    all_ok = True
    for i, (addr, original) in enumerate(hook_patches):
        now = verify._read(addr, len(original))
        ok = now == original
        all_ok = all_ok and ok
        print("  hook[{}] @ {:#x}".format(i, addr))
        print("     original  : {}".format(original.hex(" ") if original else None))
        print("     while on  : {}".format(while_on[i].hex(" ") if while_on[i] else None))
        print("     after off : {}".format(now.hex(" ") if now else None))
        print("     RESTORED  : {}".format("YES" if ok else "NO  <-- MISMATCH"))
    verify.detach()

    print("\nAll hooks restored: {}".format("YES" if all_ok else "NO"))
    print("Done.")


if __name__ == "__main__":
    main()
