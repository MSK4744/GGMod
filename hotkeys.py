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

    def __init__(self, log_callback=None):
        # log_callback(message: str) lets the UI receive our messages
        # without this module knowing anything about tkinter.
        self._log = log_callback if log_callback else (lambda msg: None)
        # key_name -> keyboard hook handle, so we can unregister.
        self._registered = {}

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

    def register(self, key_name: str, callback):
        """Register a global hotkey for the exact key_name.

        Re-registering the same key_name rebinds it to the new callback.
        """
        # Rebind support: drop any existing hook for this key first.
        if key_name in self._registered:
            self.unregister(key_name)

        handle = keyboard.add_hotkey(key_name, callback, suppress=False)
        self._registered[key_name] = handle
        self._log("Registered hotkey: '{}'".format(key_name))

    def unregister(self, key_name: str):
        """Remove the global hotkey bound to key_name, if any."""
        handle = self._registered.pop(key_name, None)
        if handle is None:
            self._log("No hotkey to unregister for: '{}'".format(key_name))
            return
        try:
            keyboard.remove_hotkey(handle)
            self._log("Unregistered hotkey: '{}'".format(key_name))
        except (KeyError, ValueError) as exc:
            self._log("Failed to unregister '{}': {}".format(key_name, exc))

    def unregister_all(self):
        """Remove every hotkey this manager registered."""
        for key_name in list(self._registered.keys()):
            self.unregister(key_name)
        self._log("All hotkeys unregistered.")
