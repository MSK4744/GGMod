"""GGMod entry point.

Wires together the engine, hotkey manager, and UI, sharing a single log
callback so all actions surface in the UI log window.
"""

from engine import TrainerEngine
from hotkeys import HotkeyManager
from ui import GGModUI


def main():
    # The UI owns the log widget, but engine/hotkeys need to log before the
    # UI's log() method exists as a bound method. We use a small indirection:
    # build the UI first with placeholder objects is awkward, so instead we
    # create the UI, then hand its log method to the engine and hotkeys.
    ui_holder = {}

    def log_callback(message: str):
        ui = ui_holder.get("ui")
        if ui is not None:
            ui.log(message)

    # Verbose per-keydown hotkey diagnostics go to the separate collapsible
    # Key Input Log panel instead of flooding the main log.
    def key_log_callback(message: str):
        ui = ui_holder.get("ui")
        if ui is not None:
            ui.key_log(message)

    engine = TrainerEngine(log_callback=log_callback)
    hotkeys = HotkeyManager(
        log_callback=log_callback, key_log_callback=key_log_callback)

    ui = GGModUI(engine, hotkeys)
    ui_holder["ui"] = ui

    log_callback("GGMod ready.")
    ui.run()


if __name__ == "__main__":
    main()
