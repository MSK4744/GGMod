# Making your first mod with GGMod + Cheat Engine

This is a full walkthrough of finding a value in memory with Cheat Engine and turning it into a working mod in GGMod. It assumes no prior Cheat Engine experience, but does assume basic comfort with the idea of "a game stores values like health/ammo/currency somewhere in memory, and we can find and change them."

**Before you start:** do this on a single-player game, offline, not connected to any online mode. See the disclaimer in [README.md](./README.md).

---

## Part 1 — Finding a value with Cheat Engine

We'll use "player health" as the running example, but the same process works for ammo, currency, timers, or any other numeric value.

### Step 1: Attach Cheat Engine to the game

1. Launch the game and get into a state where you can see the value you want to change (e.g. in a level, health bar visible).
2. Open Cheat Engine.
3. Click the computer icon (top-left, "Open process") and select the game's `.exe` from the list.

### Step 2: Find the address

**If the game shows you the exact number (e.g. "100 HP"):**
1. Set **Scan Type** to "Exact Value" and **Value Type** to whatever fits (usually "4 Bytes" for whole numbers).
2. Type the current value (e.g. `100`) and click **First Scan**.
3. Take damage in-game so the value changes to something else (e.g. `80`).
4. Type the new value (`80`) and click **Next Scan**.
5. Repeat this a couple of times (take more damage, scan again) until the result list narrows down to one or a small handful of addresses.

**If there's no visible number (e.g. just a health bar with no digits):**
1. Set **Scan Type** to "Unknown initial value" and click **First Scan**. This grabs a snapshot of everything.
2. Take damage (deplete the bar somewhat), then set Scan Type to "Decreased value" and click **Next Scan**.
3. If nothing changed for a while (e.g. you didn't take damage), you can also do "Unchanged value" scans to filter out noise.
4. Repeat decreased/increased scans (taking damage / healing) until you narrow down to one address.

**Important gotcha:** if you're scanning for something in the game's *code* (not a stored value, e.g. a specific instruction), make sure the memory region checkbox in Cheat Engine's scan settings is set to **Executable**, not **Writable**. It's easy to mix these up and get zero results for reasons that aren't obvious.

### Step 3: Confirm it's the right address

Double-click the address to add it to your cheat table at the bottom. Right-click it → **"Change record"** or just watch its value column live update as you take damage/heal in-game, to confirm it's tracking the value you expect (not some unrelated counter that happens to match).

### Step 4: Find what writes to it

This is the critical step — GGMod doesn't hook the *value's address* directly, it hooks the *instruction that writes to it*, because the value's address can change between game sessions (memory gets reallocated), but the instruction that writes to it lives in the game's code and stays put.

1. Right-click the address in your cheat table → **"Find out what writes to this address"**.
2. A window opens and starts listening. Now trigger the write in-game (take damage, or heal, depending on what you're tracking).
3. You'll see one or more entries appear, each showing an instruction like:
   ```
   7FF656D983C3 - 89 4F 20 - mov [rdi+20],ecx
   ```
   This tells you:
   - The **address** of the instruction itself (not the value's address)
   - The **opcode bytes** (`89 4F 20`)
   - The **disassembly** (`mov [rdi+20], ecx`) — this means "write the value in register ecx into memory at [rdi + 0x20]"

4. **If you see more than one instruction firing**, that usually means there are multiple separate code paths that write to this value — for example, one instruction for taking damage and a different one for healing. This is common and not a problem; GGMod's `pointer_capture` template supports multiple hooks feeding into one shared value, specifically for this case.

Write down, for each write instruction you found:
- The full instruction address
- The register used (e.g. `rdi`, `esi`, `ecx` — whichever register holds the *base pointer*, not the source value register)
- The struct offset (the `+0x20` or `+18` part)

### Step 5: Get enough bytes for a unique AOB pattern

Right-click the instruction in the "opcodes that write" list → **"Show disassembler"**. This opens a memory viewer centered on that instruction. Note down the bytes of that instruction plus several instructions around it (10-20 bytes total is a good starting point) — you'll paste these into GGMod and it will tell you if they're unique enough.

**A short pattern (4-6 bytes) will often match dozens of places in memory** by coincidence — common byte sequences like `mov` instructions with small offsets show up constantly in real code. The fix is always the same: include more surrounding bytes until the pattern only matches once. GGMod's Preview button will show you the exact match count and, if there's a collision, the addresses involved — use that to tell whether you need more bytes.

---

## Part 2 — Building the mod in GGMod

### Step 1: Attach

Open GGMod, select or create a config for your game, and click **Attach** while the game is running.

### Step 2: Add Mod

Click the **Add Mod** tab and fill in:

- **Name** — anything descriptive, e.g. "Infinite Health"
- **Template** — choose based on what you found:
  - **`hard_freeze`** if there's a single write instruction you want to just skip entirely (e.g. a value that only ever decreases, and you never need the game to legitimately spend it)
  - **`pointer_capture`** if you want ongoing control over the value (never let it drop below X, always hard-set it to X, or you found multiple write paths that need to share one hook)
  - **`pointer_chain`** if the **Pointer Chains** tool found a static `module_base + offset` path to your target — see "Choosing `pointer_chain` vs `pointer_capture`" below
- **AOB** — the bytes you gathered in Part 1, Step 5 (`hard_freeze`/`pointer_capture` only — `pointer_chain` has no AOB at all)

### Choosing `pointer_chain` vs `pointer_capture`

Both templates end up doing the same thing — reading/writing a live game object's memory every poll tick — but they get there differently, and one is strictly less hassle when it's available:

- **`pointer_chain`** resolves `module_base + base_offset`, walks a fixed list of pointer offsets, and reads/writes whatever address that lands on — directly, every tick. There's no hook, no code cave, and nothing to trigger in-game: the moment you Apply, the mod is live. **Live-verified**: Preview reported `status: ready` immediately after Attach, before performing the in-game action the value is tied to — there is genuinely no trigger step. The catch is you first need the **Pointer Chains** window to actually find a chain that survives a game restart (not every object has one within a reasonable search depth).
- **`pointer_capture`** installs a hook on an instruction that writes the value, and only starts working once that instruction actually executes in-game (e.g. you have to take damage once before "never decrease" has anything to enforce). Preview stays `blocked` until then. It has no dependency on finding a static chain — it works as long as you can find *any* instruction that touches the value — but it needs that one trigger, and it patches code (a hook + a cave) rather than just reading/writing data.

Rule of thumb: **try Pointer Chains first** (Tools → Pointer Chains, or the "Open Pointer Chains…" shortcut on the Add Mod tab) if you want a mod that's immediately active with no setup dance in-game. Fall back to `pointer_capture` when no chain survives a restart within a reasonable search depth, or when you've already found a clean write instruction and don't need the extra step.

For `pointer_chain` specifically:
- **Module** — leave blank for the main game .exe, or name a specific module (e.g. a Unity/IL2CPP `GameAssembly.dll`)
- **Base offset** — the fixed offset from the module's base address, in hex (e.g. `DA5358` for `0xDA5358`)
- **Offset chain** — the list of pointer offsets to walk, in hex, comma-separated (e.g. `58` for a single-hop chain, or `58, 10` for two hops). **Every entry here is dereferenced**: at each hop, GGMod reads whatever pointer value lives at `current_address + offset` and uses *that* as the next `current_address`, all the way down to the last entry — which is also dereferenced and used as the next hop's address, unless it's the *only* remaining thing to resolve, in which case its sum is returned as the resolved pointer. In short: this is a real multi-level pointer chain, exactly the kind CE's pointer scan (and GGMod's **Pointer Chains** window) discovers, where every intermediate address genuinely holds another pointer. The **Pointer Chains** window fills this in for you automatically via "Use for New Mod" — you shouldn't normally need to type it by hand.
- **Struct offset** *(optional, hex)* — a **flat** displacement added on top of `offset_chain`'s resolved pointer, with **no further dereference**. This is the same field `pointer_capture` uses, and it means the same thing here: use it when `offset_chain` lands you on a container **object's base address**, and the value you actually want to poll is a data field sitting at a fixed byte offset *inside* that object — not another pointer to hop through. Leave it blank (or `0`) if `offset_chain`'s last hop already reaches the target field directly.
  - **Do not** fold a struct-field offset into `offset_chain` as an extra entry — GGMod has no way to tell "this number is one more pointer hop" apart from "this number is a flat field offset," so it will dereference it like every other entry, which means reading the target object's own data as if it were a pointer. See the worked example and failure signature below.
- **Poll mode** — the same options as `pointer_capture` (`never_decrease` / `clamp_min` / `hard_set` / `set_once`); the chain is re-walked fresh every tick, so if the target object is destroyed and recreated at a new address, the next tick just follows the pointer to wherever it lives now.

#### Worked example: the Forgotten Sands "Rewind" charge counter

`games/POP The Forgoten Sands.json` has two mods that both end up controlling the same in-game value, built two different ways — useful as a side-by-side reference:

- **`pointer_capture` "Rewind"** hooks an instruction that puts the Rewind object's pointer in `ebx`, then polls `[ebx + 0x18]` (`struct_offset: "18"`) — `0x18` is the counter field's displacement inside that object.
- **`pointer_chain` "Rewind (chain)"** reaches the *same* object without any hook at all: `base_offset: "DA5358"` + `offset_chain: ["58"]` resolves to the Rewind object's address directly (`0x16E3AF48` in the tested session — the container, confirmed live by Preview). `struct_offset: "18"` is then added flat on top, landing on `0x16E3AF60` — the exact same counter field `pointer_capture`'s `ebx + 0x18` reaches, just via a static chain instead of a runtime-captured register.

This was live-verified end to end: **Force Set** wrote the counter correctly at the resolved address, and with `poll_mode: never_decrease`, the charge count did not drop after actually consuming a rewind charge in-game.

**Failure signature to recognize:** if you mistakenly write this as `offset_chain: ["58", "18"]` instead of `offset_chain: ["58"]` + `struct_offset: "18"`, GGMod will treat `0x18` as *another hop* and try to dereference `0x16E3AF48` (the object's own address) as if it held a pointer — reading whatever raw field happens to live at its first 4 (or 8, on x64) bytes and using that garbage value as an address. The symptom is a **Force Set write failure at a nonsensical address**, e.g.:
```
force_set: 'Rewind (chain)' write failed: Could not write memory at: 16332056 (0xF93518), length: ... - GetLastError: 998
```
`GetLastError: 998` is Windows' "invalid access to memory location" — a strong hint the resolved address isn't a real object address at all, but a stray field value being misread as a pointer. If you see this, check whether the last `offset_chain` entry should actually be a `struct_offset` instead.

For `pointer_capture` specifically:
- **Struct offset** — the `+0x20` style offset you noted
- **Register** — the base register (e.g. `rdi`)
- **Steal** — how many bytes the hook needs to "steal" from the original instruction stream to make room for the jump. The target instruction itself might be short (e.g. 3 bytes), so this typically needs to include the next instruction or two as well, ending on a clean instruction boundary. Note: for `pointer_capture`, the AOB must start exactly at the target write instruction — there's no separate byte-offset field like `hard_freeze` has.
- **Poll mode** — how GGMod should enforce the value once captured:
  - `never_decrease` — lets the value go up, but blocks any decrease (good for "can still take damage animations but health never actually drops")
  - `clamp_min` — keeps the value at or above a minimum you set
  - `hard_set` — unconditionally overwrites the value every tick with a fixed number (best when multiple different code paths drain the same counter and you don't care which)

For `hard_freeze` specifically:
- **Offset** — where in your AOB pattern the target instruction actually starts (since this template's AOB can include leading context bytes before the instruction)
- **Freeze mode: nop** — replaces the instruction with no-ops
- **NOP length** — how many bytes to nop out (matches the instruction's length)

### Step 3: Preview

Click **Preview**. This runs a live, read-only check of the game's memory and reports one of three outcomes (for `pointer_chain`, there's no AOB to scan — Preview just walks the chain and shows what it currently resolves to, so only "ready" / "blocked (no match)" apply):

- 🟢 **Ready** — exactly one match found. Shows the matched address and original bytes. You're clear to Apply.
- 🔴 **Blocked (no match)** — zero matches. Your AOB is wrong, stale (game got updated), or you copied bytes from the wrong module. Double check your bytes against Cheat Engine.
- 🟡 **Blocked (multiple matches)** — the pattern isn't unique. GGMod shows you the colliding addresses. Go back to Cheat Engine, grab more surrounding bytes, and try again.

**Never skip this step, and never try to force Apply through a blocked state.** It exists specifically so you never write a hook to the wrong address.

### Step 4: Apply

Once Preview is green, click **Apply**. GGMod installs the hook (or NOPs the instruction, for `hard_freeze`). You should see log lines confirming the hook is active.

### Step 5: Set a hotkey

Click **Set Hotkey**, then press the key you want to toggle this mod with. GGMod will refuse to let you bind a key already used by another mod — pick a free one if you get a conflict message.

### Step 6: Test it

Go back into the game and verify the mod actually works as intended (health doesn't drop, ammo doesn't decrease, etc.) — not just that the number changed once in the tool.

---

## Troubleshooting

- **Preview takes a long time / seems stuck.** Larger, more modern games have much bigger memory footprints than older titles, so scans can take longer than you'd expect — check the progress bar and log timestamps before assuming it's hung.
- **Apply succeeds but the effect doesn't actually show up in-game.** You may have hooked a *display/sync* value rather than the value the game's logic actually checks. Some games maintain a separate "real" internal value and a UI-facing copy. Go back to Cheat Engine and trace further to confirm which one you're editing.
- **My AOB worked yesterday, now it's zero matches.** The game likely updated and the instruction moved or changed. You'll need to re-find it in Cheat Engine.

## Getting help

Open an [Issue](../../issues) with your log output and a description of what you tried. Please don't ask for help finding AOBs for specific cheats in already-released, protected multiplayer games — this tool and tutorial are for single-player modding and learning reverse engineering.
