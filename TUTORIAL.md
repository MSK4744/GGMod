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
- **AOB** — the bytes you gathered in Part 1, Step 5

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

Click **Preview**. This runs a live, read-only scan of the game's memory and reports one of three outcomes:

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
