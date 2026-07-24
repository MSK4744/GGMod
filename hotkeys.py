"""Global hotkey management for GGMod.

Wraps the `keyboard` library so hotkeys fire even when the game window
(not our tkinter window) has OS focus. We deliberately do NOT use tkinter's
.bind() because that only works when our window is focused.
"""

import keyboard


class HotkeyManager:
    """Manages global hotkeys via the `keyboard` library.

    Keys are identified by the exact name returned from capture_next_key(),
    which distinguishes numpad keys ("num 1") from top-row keys ("1") using
    scan codes rather than characters.
    """

    def __init__(self, log_callback=None, key_log_callback=None):
        # log_callback(message: str) lets the UI receive our messages
        # without this module knowing anything about tkinter.
        self._log = log_callback if log_callback else (lambda msg: None)
        # key_log_callback receives the verbose per-keydown diagnostics so the
        # UI can route them to a separate collapsible panel instead of the main
        # log. Falls back to the main log if not provided.
        self._key_log = key_log_callback if key_log_callback else self._log
        # canonical key_name (e.g. "num 1", "f2", "ctrl+num 1") -> callback.
        # NOTE: we do NOT use keyboard.add_hotkey for matching. add_hotkey maps
        # a name to a SET of scan codes that lumps numpad 1 (sc 79) together
        # with top-row 1 (sc 2), so a "num 1" binding would also fire on the
        # top row (and on OS key-repeat). Instead we run ONE low-level hook and
        # match each event's exact canonical name ourselves (see _on_event).
        self._registered = {}
        self._hook_handle = None       # keyboard.hook handle (installed lazily)
        # Scan codes currently physically held down. Used for down-after-up
        # debounce so OS key-repeat (KEY_DOWN with no intervening KEY_UP) does
        # not fire a hotkey more than once per real press.
        self._pressed = set()
        # Per-keydown diagnostic logging (scan code + canonical name + match
        # result). Matches are always logged; set False to silence non-matches.
        self.debug = True

    def _clean_name(self, event):
        """Build our canonical key name from a keyboard event.

        The `keyboard` library already prefixes numpad keys with "num ",
        e.g. "num 1" for the numpad, "1" for the top row. We rely on the
        scan_code to disambiguate and use event.name as the human name.
        """
        name = event.name
        # Numpad keys report is_keypad=True; ensure the "num " prefix is
        # present so callers always get a distinct string.
        if getattr(event, "is_keypad", False) and not name.startswith("num "):
            name = "num " + name
        return name

    @staticmethod
    def _is_modifier(event):
        """True if the event is a bare modifier key (ctrl/shift/alt/win).

        `keyboard` reports these as 'left ctrl', 'right shift', 'left windows',
        etc. We match on the trailing word so both sides are covered.
        """
        name = (event.name or "").lower()
        return name.split()[-1] in ("ctrl", "shift", "alt", "windows", "menu") \
            or name in ("alt gr",)

    @staticmethod
    def _active_modifiers():
        """Return held modifiers in a stable order, as `keyboard` combo tokens.

        Fixed order (ctrl, alt, shift, windows) so the SAME physical combo
        always produces the SAME string — important because bindings, conflict
        checks and unregister all key off the exact string.
        """
        mods = []
        for token in ("ctrl", "alt", "shift", "windows"):
            try:
                if keyboard.is_pressed(token):
                    mods.append(token)
            except Exception:
                pass
        return mods

    def capture_next_key(self) -> str:
        """Block until the next key press and return its canonical hotkey name.

        A bare key returns its exact name ("num 1", "f2", ...). If modifiers are
        held when the main key is pressed, a combo string is returned instead,
        e.g. "ctrl+num 1" or "ctrl+shift+f2". Uses scan_code matching (via
        keyboard.read_event) so numpad 1 and top-row 1 stay distinct.
        """
        self._log("Waiting for key press (hold Ctrl/Shift/Alt for a combo)...")
        while True:
            event = keyboard.read_event(suppress=False)
            if event.event_type != keyboard.KEY_DOWN:
                continue
            # Ignore lone modifier presses — wait for the actual key so a combo
            # like ctrl+num 1 is captured as a single hotkey, not just "ctrl".
            if self._is_modifier(event):
                continue
            name = self._clean_name(event)
            mods = self._active_modifiers()
            combo = "+".join(mods + [name]) if mods else name
            self._log(
                "Captured hotkey: '{}' (scan_code={}, keypad={})".format(
                    combo, event.scan_code, getattr(event, "is_keypad", False)
                )
            )
            return combo

    def _event_combo(self, event):
        """Canonical hotkey string for a KEY_DOWN event, matching exactly the
        format capture_next_key() saved: numpad keys become 'num 1' (via
        is_keypad), top-row stays '1', and held modifiers prefix in canonical
        order ('ctrl+num 1'). This is what restores the numpad/top-row split at
        MATCH time that keyboard.add_hotkey collapses."""
        name = self._clean_name(event)
        mods = self._active_modifiers()
        return "+".join(mods + [name]) if mods else name

    def _on_event(self, event):
        """Single global hook: exact-name match + repeat debounce."""
        try:
            if event.event_type == keyboard.KEY_UP:
                self._pressed.discard(event.scan_code)
                return
            if event.event_type != keyboard.KEY_DOWN:
                return

            # Debounce: OS key-repeat resends KEY_DOWN with the same scan code
            # and NO KEY_UP in between. Only the first down (after an up) counts.
            is_repeat = event.scan_code in self._pressed
            self._pressed.add(event.scan_code)
            if is_repeat:
                return

            # Bare modifier presses never trigger; they gate combos via
            # _active_modifiers (keyboard.is_pressed) on the main key's event.
            if self._is_modifier(event):
                return
            if not self._registered:
                return

            combo = self._event_combo(event)
            callback = self._registered.get(combo)
            matched = callback is not None
            if matched or self.debug:
                # Verbose per-keydown diagnostics go to the dedicated key log.
                self._key_log(
                    "keydown sc={} keypad={} -> '{}' [{}]".format(
                        event.scan_code, getattr(event, "is_keypad", False),
                        combo, "MATCH -> trigger" if matched else "no match"
                    )
                )
            if matched:
                try:
                    callback()
                except Exception as exc:
                    self._log("hotkey '{}' callback error: {}".format(combo, exc))
        except Exception as exc:            # a hook must never raise
            self._log("hotkey hook error: {}".format(exc))

    def _ensure_hook(self):
        if self._hook_handle is None:
            self._hook_handle = keyboard.hook(self._on_event)

    def register(self, key_name: str, callback):
        """Register a global hotkey for the EXACT canonical key_name.

        Matching is by exact canonical name (numpad-aware) on a single shared
        low-level hook, not keyboard.add_hotkey. Re-registering the same name
        rebinds it to the new callback.
        """
        self._registered[key_name] = callback
        self._ensure_hook()
        self._log("Registered hotkey: '{}'".format(key_name))

    def unregister(self, key_name: str):
        """Remove the hotkey bound to key_name, if any."""
        if self._registered.pop(key_name, None) is None:
            self._log("No hotkey to unregister for: '{}'".format(key_name))
            return
        self._log("Unregistered hotkey: '{}'".format(key_name))

    def unregister_all(self):
        """Remove every hotkey and tear down the shared hook."""
        self._registered.clear()
        self._pressed.clear()
        if self._hook_handle is not None:
            try:
                keyboard.unhook(self._hook_handle)
            except (KeyError, ValueError) as exc:
                self._log("Failed to remove keyboard hook: {}".format(exc))
            self._hook_handle = None
        self._log("All hotkeys unregistered.")
