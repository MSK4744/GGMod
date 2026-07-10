"""Standalone hard_freeze test harness for GGMod.

Isolates the hard_freeze template (POP "Infinite Rewind" + "Time Slow
Charges") end-to-end against a live POP.exe, with manual gates so you can
read the preview before applying and test in-game before bytes are restored.

Run from the GGMod root:  python test_hardfreeze.py
"""

from engine import TrainerEngine

CONFIG = "games/POP_test.json"
PROCESS = "POP.exe"
TARGET_MODS = ["Infinite Rewind", "Time Slow Charges"]  # hard_freeze only


def log(msg):
    print("[LOG] {}".format(msg))


def print_preview(name, preview):
    print("\n--- Preview: {} ---".format(name))
    print("  status        : {}".format(preview.get("status")))
    print("  matches       : {}".format(preview.get("matches")))
    print("  original_bytes: {}".format(preview.get("original_bytes")))
    cave = preview.get("cave_preview")
    if cave is not None:
        print("  cave_preview  : {}".format(cave))
    warnings = preview.get("warnings") or []
    for w in warnings:
        print("  warning       : {}".format(w))


def main():
    engine = TrainerEngine(log_callback=log)

    # 3. Attach
    if not engine.attach(PROCESS):
        print("\nCould not attach to {}. Is the game running? Aborting.".format(PROCESS))
        return

    # 4. Load config
    engine.load_game_config(CONFIG)

    # Pull just the two hard_freeze mods, in order.
    mods = {m.get("name"): m for m in engine.mods}
    selected = [(n, mods[n]) for n in TARGET_MODS if n in mods]
    if len(selected) != len(TARGET_MODS):
        missing = [n for n in TARGET_MODS if n not in mods]
        print("\nMissing expected mod(s) in config: {}. Aborting.".format(missing))
        engine.detach()
        return

    # 5. Preview both
    previews = {}
    for name, mod in selected:
        previews[name] = engine.preview_mod(mod)
        print_preview(name, previews[name])

    all_ready = all(p.get("status") == "ready" for p in previews.values())
    print("\n" + "=" * 60)
    print("Both previews ready: {}".format(all_ready))
    print("=" * 60)

    if not all_ready:
        print("\nNot applying — at least one preview is not 'ready'. Aborting.")
        engine.detach()
        return

    # 6. Gate before applying
    input("\nReview the preview above. Press Enter to APPLY both mods... ")

    # 7. Apply both
    applied = []
    for name, mod in selected:
        if engine.apply_mod(mod):
            applied.append(name)
        else:
            print("Apply FAILED for '{}'.".format(name))

    # 8. Applied message
    print("\n" + "#" * 60)
    print("# APPLIED ({} of {}) - go test in game now".format(len(applied), len(selected)))
    print("#   {}".format(", ".join(applied) if applied else "(none)"))
    print("#" * 60)

    # 9. Gate before detach so bytes aren't restored underneath you.
    input("\nTest in-game now. Press Enter to DETACH and restore bytes... ")

    # 10. Detach + confirm restoration.
    #     Grab the current (patched) bytes first so we can prove they changed
    #     back after detach restores the originals.
    checks = []
    for name, mod in selected:
        entry = engine._active.get(name)
        if entry and entry.get("patches"):
            addr, original = entry["patches"][0]
            before = engine._read(addr, len(original))
            checks.append((name, addr, original, before))

    engine.detach()

    print("\n--- Restoration check ---")
    # detach() closed the handle, so re-attach briefly to read the bytes back
    # and confirm they are the originals, not the NOP patch.
    verify = TrainerEngine(log_callback=log)
    if verify.attach(PROCESS):
        for name, addr, original, patched_before in checks:
            now = verify._read(addr, len(original))
            ok = now == original
            print("  {:<20} @ {:#x}".format(name, addr))
            print("     original : {}".format(original.hex(" ") if original else None))
            print("     while on  : {}".format(patched_before.hex(" ") if patched_before else None))
            print("     after off : {}".format(now.hex(" ") if now else None))
            print("     RESTORED  : {}".format("YES" if ok else "NO  <-- MISMATCH"))
        verify.detach()
    else:
        print("  (Could not re-attach to verify; game may have closed.)")

    print("\nDone.")


if __name__ == "__main__":
    main()
