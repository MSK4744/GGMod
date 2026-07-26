"""Standalone Pointer Chain Finder test harness for GGMod (Stage 1 + Stage 2).

Verifies build_pointer_snapshot + find_pointers_to (Stage 1), then
find_pointer_chains + resolve_chain (Stage 2's recursion and restart-
verification arithmetic) against a REAL live process. Point it at a known
runtime address -- e.g. the resolved pointer_capture "Rewind" pointer from
games/POP_test.json, read live via the mod's shared slot the way
test_pointercapture.py's read_slot() does -- and confirm the tool surfaces
the same static chain CE's pointer scan would.

Run from the GGMod root:  python test_pointerchain.py
"""

import struct
import time

from engine import PointerChainFinder, TrainerEngine


def resolve_chain_traced(engine, module_name, base_offset, offset_chain):
    """Mirrors PointerChainFinder.resolve_chain hop-by-hop (same arithmetic,
    same fresh module lookup -- nothing shared with the production function's
    internals) but also records every intermediate (hop_index, read_address,
    pointer_value) so a failure can be pinned to an exact address AND an
    exact chain position, instead of only the final error string. Used only
    for diagnosing exactly where/why a chain fails to re-resolve -- not a
    replacement for resolve_chain."""
    hops = []
    if engine.pm is None:
        return {"error": "Not attached.", "hops": hops}
    base, size = engine._main_module_range(module_name)
    if not size:
        return {"error": "Module '{}' not found.".format(module_name
                                                          or "main module"),
                "hops": hops}
    if not offset_chain:
        return {"error": "Empty offset chain.", "hops": hops}
    ptr_size = 8 if engine._is64 else 4
    fmt = "<Q" if ptr_size == 8 else "<I"

    addr = base + base_offset
    for i, off in enumerate(offset_chain):
        raw = engine._read(addr, ptr_size)
        if not raw or len(raw) != ptr_size:
            return {"error": "Could not read pointer at {:#x}.".format(addr),
                    "fail_hop": i, "fail_addr": addr, "hops": hops}
        ptr = struct.unpack(fmt, raw)[0]
        hops.append((i, addr, ptr, off))
        addr = ptr + off
    return {"address": addr, "hops": hops}

CONFIG = "games/POP_test.json"
PROCESS = "Prince of Persia.exe"
# Fill this in with a real runtime address before running (hex string).
# Easiest source: attach with test_pointercapture.py, trigger the hook once,
# then read_slot(engine, "Infinite Health") (or similar) to get a live
# pointer value -- or just paste an address straight from Cheat Engine.
TARGET_ADDRESS_HEX = "0x16edafc8"   # e.g. "0x16BBAE38"
MAX_OFFSET = 4096


def log(msg):
    print("[LOG] {}".format(msg))


def main():
    if not TARGET_ADDRESS_HEX:
        print("Set TARGET_ADDRESS_HEX to a real runtime address before running.")
        return

    engine = TrainerEngine(log_callback=log)
    print("Attaching to {}...".format(PROCESS))
    if not engine.attach(PROCESS):
        print("Attach failed -- is the game running?")
        return

    target_address = int(TARGET_ADDRESS_HEX, 16)
    finder = PointerChainFinder(engine)

    print("Building pointer snapshot (walks ALL committed memory -- heap, "
          "stack, modules -- can take a few seconds)...")
    t0 = time.time()

    def progress(done, total):
        pct = (done / total * 100.0) if total else 0
        print("  ...{:.0f}% ({}/{} bytes)".format(pct, done, total))

    snap = finder.build_pointer_snapshot(progress_callback=progress)
    if "error" in snap:
        print("Snapshot failed:", snap["error"])
        return
    print("Snapshot done in {:.1f}s: {} pointer-aligned slot(s), ptr_size={}, "
          "committed range [{:#x}, {:#x}).".format(
              time.time() - t0, snap["count"], snap["ptr_size"],
              snap["min_addr"], snap["max_addr"]))

    res = finder.find_pointers_to(snap, target_address, max_offset=MAX_OFFSET)
    if "error" in res:
        print("find_pointers_to failed:", res["error"])
        return
    print("\nfind_pointers_to(0x{:X}, max_offset={}): {} hit(s){}.".format(
        target_address, MAX_OFFSET, res["count"],
        " (CAPPED -- try a smaller max_offset, this value is too common)"
        if res["capped"] else ""))
    for h in res["hits"]:
        mod, base, _size = engine._module_for_address(h["address"])
        loc = ("{}+{:#x}".format(mod or "main module", h["address"] - base)
               if base else "not in a loaded module (heap/stack)")
        print("  address=0x{:X}  offset=0x{:X}  ({})".format(
            h["address"], h["offset"], loc))

    if not res["hits"]:
        print("\nNo hits within max_offset={} -- try a larger value, or "
              "confirm target_address is actually a live runtime address "
              "right now (it can move between game sessions).".format(
                  MAX_OFFSET))

    # --- Stage 2: full recursive chain search, reusing the SAME snapshot ---
    print("\nRunning find_pointer_chains (reuses the same snapshot, no "
          "re-scan)...")
    t1 = time.time()

    def chain_progress(level, calls_made, chains_found):
        if calls_made % 25 == 0:
            print("  ...level {}, {} branch(es) explored, {} chain(s) found "
                  "so far ({:.1f}s elapsed)".format(
                      level, calls_made, chains_found, time.time() - t1))

    chains_res = finder.find_pointer_chains(snap, target_address,
                                            max_offset=MAX_OFFSET,
                                            progress_callback=chain_progress)
    print("find_pointer_chains done in {:.2f}s.".format(time.time() - t1))
    if "error" in chains_res:
        print("find_pointer_chains failed:", chains_res["error"])
        return
    chains = chains_res["chains"]
    note = ""
    if chains_res["any_capped"]:
        note += " (at least one branch capped -- try a smaller max_offset for more)"
    if chains_res.get("capped_total_calls"):
        note += " (TOTAL CALL CAP HIT -- search stopped early, incomplete)"
    print("{} chain(s) found{}.".format(len(chains), note))
    for c in chains:
        offs = ", ".join("0x{:X}".format(o) for o in c["offset_chain"])
        print("  module={}  base_offset=0x{:X}  offset_chain=[{}]  levels={}"
             .format(c["module"] or "main module", c["base_offset"], offs,
                     c["level_count"]))

    # --- Sanity check: does the top-ranked chain reproduce target_address? ---
    if chains:
        best = chains[0]
        rr = finder.resolve_chain(best["module"], best["base_offset"],
                                  best["offset_chain"])
        if "error" in rr:
            print("\nresolve_chain on the top chain failed:", rr["error"])
        else:
            match = rr["address"] == target_address
            print("\nresolve_chain on the top-ranked chain -> 0x{:X} "
                 "({} target_address 0x{:X}).".format(
                     rr["address"], "==" if match else "!=", target_address))

    # --- Full-list verification: resolve EVERY returned chain, not just the
    # top-ranked one, and confirm each one actually reproduces
    # target_address. This is the ground-truth check for whether the chain
    # list itself is trustworthy (e.g. duplicate offset_chain arrays across
    # different base_offsets could either be a real bug or a genuine data
    # collision -- this tells them apart empirically instead of guessing). ---
    if chains:
        print("\nVerifying ALL {} returned chain(s) against target_address "
              "0x{:X}...".format(len(chains), target_address))

        # Sanity check first: how many of the "715" are actually distinct
        # offset_chain arrays, vs. duplicates that legitimately share one
        # (module, base_offset, offset_chain) key from find_pointer_chains'
        # own dedup set? If many chains share an identical offset_chain,
        # any shared upstream hop that has since gone stale (freed/moved
        # since the snapshot was taken) will make ALL of them fail at the
        # exact same address -- that's a data/timing story, not a code bug.
        distinct_full = set(tuple(c["offset_chain"]) for c in chains)
        print("  {} distinct offset_chain array(s) among the {} chains "
              "(each array can be shared by multiple module/base_offset "
              "entries -- see Stage 2 corrupted-chain-list investigation)."
              .format(len(distinct_full), len(chains)))

        n_ok, n_bad = 0, 0
        bad_examples = []
        fail_addr_groups = {}   # fail_addr -> list of (module, base_offset, fail_hop)
        for c in chains:
            rr = resolve_chain_traced(engine, c["module"], c["base_offset"],
                                      c["offset_chain"])
            if "error" in rr or rr.get("address") != target_address:
                n_bad += 1
                if len(bad_examples) < 10:
                    bad_examples.append((c, rr))
                if "fail_addr" in rr:
                    fail_addr_groups.setdefault(rr["fail_addr"], []).append(
                        (c["module"] or "main module", c["base_offset"],
                         rr["fail_hop"], tuple(c["offset_chain"])))
            else:
                n_ok += 1
        print("  {}/{} chains verified correctly, {} did NOT reproduce "
              "target_address.".format(n_ok, len(chains), n_bad))
        if bad_examples:
            print("  First mismatches (with the exact hop that failed):")
            for c, rr in bad_examples:
                offs = ", ".join("0x{:X}".format(o) for o in c["offset_chain"])
                if "error" in rr:
                    detail = "{} (failed at hop {} of {}, reading 0x{:X})".format(
                        rr["error"], rr["fail_hop"] + 1, len(c["offset_chain"]),
                        rr["fail_addr"])
                    for hop_i, hop_addr, hop_ptr, hop_off in rr["hops"]:
                        detail += ("\n        hop {}: read 0x{:X} -> ptr=0x{:X}, "
                                  "+off 0x{:X} -> 0x{:X}").format(
                            hop_i + 1, hop_addr, hop_ptr, hop_off, hop_ptr + hop_off)
                else:
                    detail = "0x{:X}".format(rr["address"])
                print("    module={}  base_offset=0x{:X}  offset_chain=[{}]  "
                     "-> {}".format(c["module"] or "main module",
                                   c["base_offset"], offs, detail))

        # The concrete proof-or-disproof: group failures by the exact
        # address the read failed at. If N unrelated (module, base_offset)
        # entries all fail at the SAME address, check whether they also
        # share the SAME offset_chain SUFFIX from that hop onward -- if so,
        # they were always going to converge on reading that one shared
        # address, and its failure is a single real fact about current
        # memory state, not 463 independent bugs or a loop/state leak.
        if fail_addr_groups:
            print("\n  Grouped by failure address (proves/disproves a "
                  "loop-state bug vs. a shared, now-stale, upstream hop):")
            for fail_addr, entries in sorted(fail_addr_groups.items(),
                                             key=lambda kv: -len(kv[1])):
                suffixes = set()
                for _mod, _base, fail_hop, full_chain in entries:
                    suffixes.add(full_chain[fail_hop:])
                print("    0x{:X}: {} chain(s) failed here, {} distinct "
                     "offset_chain suffix(es) from the failing hop onward."
                     .format(fail_addr, len(entries), len(suffixes)))
                if len(suffixes) == 1:
                    print("      -> ALL of them share the identical "
                         "remaining offset_chain suffix {} -- they "
                         "structurally converge on this one address; it "
                         "is one real, shared hop that is unreadable right "
                         "now (freed/decommitted since the snapshot was "
                         "taken), not independent failures.".format(
                             ["0x{:X}".format(o) for o in next(iter(suffixes))]))
                else:
                    print("      -> these do NOT share a common suffix -- "
                         "this would be suspicious and warrants further "
                         "investigation (possible genuine bug).")

    # --- Explicit check against the manually-found (main module, 0xda5358,
    # [0x58]) chain from the live Stage 1 run, regardless of what the
    # recursive search above ranks first -- confirms resolve_chain's
    # arithmetic against this exact real description. ---
    print("\nExplicit check: resolve_chain(main module, 0xda5358, [0x58])...")
    manual = finder.resolve_chain(None, 0xda5358, [0x58])
    if "error" in manual:
        print("  failed:", manual["error"])
    else:
        print("  -> 0x{:X}  (locked_ptr should currently be this address)"
             .format(manual["address"]))


if __name__ == "__main__":
    main()
