# GGMod

A generic, config-driven trainer engine for game modding. Instead of writing a new Python trainer from scratch for every game, you describe a cheat (an AOB pattern, a memory template, a hotkey) through the UI, and GGMod handles the memory scanning, hooking, and code cave injection for you.

Currently supports Windows x86 and x64 processes.

---

## ⚠️ Read this before using GGMod

- **Single-player / offline use only.** GGMod reads and writes another process's memory and injects code caves. Many anti-cheat systems (BattlEye, EAC, etc.) will detect this and can get you banned. Never use GGMod on a game while connected to an online mode, ranked mode, or any service with active anti-cheat.
- **No warranty, use at your own risk.** You are responsible for anything that happens to your game, save files, or system as a result of using this tool. Always back up save files before testing a new mod.
- **Your antivirus may flag GGMod.exe.** This is expected, not a sign of malware. Any tool that reads/writes another process's memory and writes executable code into it matches the same behavioral pattern antivirus software uses to detect real malware. The source code is public in this repo — you're welcome to read exactly what it does before trusting it, or build it yourself from source.
- **This is a tool for people learning reverse engineering**, not a one-click "no experience needed" cheat generator. You'll need a working knowledge of Cheat Engine (AOB scanning, "find what writes") to find the values GGMod hooks into. See [TUTORIAL.md](./TUTORIAL.md) for a full walkthrough if you're new to this.

---

## What GGMod actually does

You give it:
- An **AOB (array-of-bytes) pattern** — a byte signature that uniquely identifies a specific instruction in the game's memory
- A **template type** describing how to treat the value at that instruction (freeze it, clamp it, capture a pointer to poll it, etc.)
- A **hotkey** to toggle it in-game

GGMod scans the running process for that pattern, verifies it's unique, and (depending on the template) either NOPs out the instruction or installs a small code cave hook that lets it read/write the value on an ongoing basis. Everything is config-driven — cheats live in a per-game JSON file, and the Add Mod / Preview / Apply UI writes and reads that file for you, so no hand-written JSON or Python is required to build a working trainer.

### Templates

- **`hard_freeze`** — NOPs a specific write instruction, so the value it would have written is skipped entirely. Good for values with one write path you want to permanently prevent (like a decrement).
- **`pointer_capture`** — installs a code cave hook at one or more write sites that all funnel into a shared object pointer. Once captured, GGMod can poll and enforce a value (never decrease, clamp minimum, hard-set every tick) independent of how many different code paths write to it.

### Safety features

- **Preview is always required before Apply.** Preview performs a live, read-only scan against the running process, shows you the exact bytes it found and the computed jmp math, and reports the match count. Apply is only enabled when Preview reports exactly one match (or, for multi-hook mods, when every hook individually reports exactly one match).
- **Match-count gating.** If your AOB pattern matches zero times (wrong bytes, wrong game version) or more than once (pattern isn't unique enough), GGMod blocks Apply and shows you the conflicting addresses so you can refine the pattern.
- **Hotkey conflict prevention.** GGMod won't let two mods silently share the same hotkey — conflicts are caught and reported instead of one binding silently overriding the other.
- **Safe jmp-distance handling (x64).** GGMod tries to allocate its code caves within reach of a short (5-byte) relative jump, and falls back to a longer absolute jump if it can't. If neither is possible for a given mod's configuration, it aborts cleanly with a log message rather than crashing or corrupting memory.

---

## Getting started

1. Download the latest `GGMod.exe` from the [Releases](../../releases) page (or build from source, see below).
2. Launch your game, then launch GGMod.
3. Use **New Game** to create a config for your game's process name, or select an existing one from the dropdown.
4. Click **Attach** once the game is running.
5. Use **Add Mod** to define a cheat (see [TUTORIAL.md](./TUTORIAL.md) for how to find the AOB pattern in Cheat Engine).
6. Click **Preview**. If it comes back green/ready, click **Apply**.
7. Set a hotkey to toggle the mod on/off in-game.

## Building from source

Requires Python 3.x and `pymem`.

```
pip install pymem keyboard
python main.py
```

## Reporting bugs / requesting features

Please use the [Issues](../../issues) tab. Include:
- The game and whether it's x86 or x64
- The template type you were using
- The exact log output from the Log panel (copy/paste, not a screenshot if possible)
- Steps to reproduce

## License

GPLv3 — see [LICENSE](./LICENSE). You're free to use, modify, and redistribute GGMod, but any distributed modified version must also be released under GPLv3 with source available.
