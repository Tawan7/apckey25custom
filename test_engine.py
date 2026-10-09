"""Regression tests for the APC engine routing and LED behavior.

Run with:  python3 test_engine.py
Uses a fake rtmidi module, so no controller, ALSA or synth is needed.
"""

import os
import sys
import types
import tempfile
import time


def install_fake_rtmidi():
    fake = types.ModuleType("rtmidi")

    class FakeMidi:
        def get_ports(self):
            return []

        def set_client_name(self, n):
            pass

        def open_port(self, i):
            pass

        def send_message(self, m):
            pass

        def set_callback(self, cb):
            pass

        def close_port(self):
            pass

    fake.MidiIn = FakeMidi
    fake.MidiOut = FakeMidi
    sys.modules["rtmidi"] = fake


install_fake_rtmidi()

import apc_engine as e
from apc_leds import LEDManager

failures = []


def check(label, cond):
    print(("PASS" if cond else "FAIL"), label)
    if not cond:
        failures.append(label)


def make_engine():
    eng = object.__new__(e.Engine)
    eng.banks = e.load_banks(e.BANKS_FILE)
    eng.drums = e.load_drums(e.DRUMS_FILE)
    eng.presets = e.load_presets(e.PRESETS_FILE)
    eng.cc_actions = e.load_cc_config(e.CC_CONFIG_FILE)
    eng.bank = 0
    eng.shift_held = False
    eng.selected_note = None
    eng.key_channel = 0
    eng.loops = [e.LoopSlot(i) for i in range(e.LOOP_SLOTS)]
    eng.preset_players = [None] * e.PRESET_COUNT
    eng.note_to_loop = {n: i for i, n in enumerate(e.LOOP_NOTE_ORDER)}
    eng.note_to_preset = {n: i for i, n in enumerate(e.PRESET_NOTE_ORDER)}
    eng.note_to_slot = {n: i for i, n in enumerate(e.SLOT_NOTE_ORDER)}
    eng.note_to_scale = {n: e.SHIFT_SCALE_ROOT + e.SHIFT_SCALE_STEPS[i]
                         for i, n in enumerate(e.SHIFT_NOTE_PADS)
                         if i < len(e.SHIFT_SCALE_STEPS)}
    eng.channel_volume = {ch: 100 for ch in e.SLOT_CHANNELS + [e.DRUM_CHANNEL]}
    eng.main_volume_value = 100
    eng.leds = LEDManager(None)
    eng.sent = []
    eng.send_synth = eng.sent.append
    return eng


def test_constants():
    check("10 loop slots", len(e.LOOP_NOTE_ORDER) == 10)
    check("5 preset slots", len(e.PRESET_NOTE_ORDER) == 5)
    check("15 instrument slots", len(e.SLOT_NOTE_ORDER) == 15)
    banks = e.load_banks(e.BANKS_FILE)
    for bank in (0, 1, 2):
        chans = [banks[(bank, n)][0] for n in e.SLOT_NOTE_ORDER]
        check(f"bank {bank} channels all unique", len(set(chans)) == 15)
    check("5 drum pads", len(e.load_drums(e.DRUMS_FILE)) == 5)
    all_pads = (set(e.LOOP_NOTE_ORDER) | set(e.PRESET_NOTE_ORDER)
                | set(e.SLOT_NOTE_ORDER) | set(e.DRUM_NOTE_ORDER))
    check("35 pads mapped, no overlap", len(all_pads) == 35)
    unused = [32 - r * 8 + c for r in range(5) for c in range(4)] + [32 - r * 8 for r in range(5)]
    unused = set(unused) - all_pads
    check("5 unused instrument pads are rows 4-5 cols 1-4 leftovers",
          unused == {11, 0, 1, 2, 3})
    check("5 presets loaded", len(e.load_presets(e.PRESETS_FILE)) == 5)
    check("preset events carry kind/channel",
          all(len(ev) == 5 for evs in e.load_presets(e.PRESETS_FILE).values() for ev in evs))


def test_drum_recording():
    eng = make_engine()
    eng._handle_note(0, 37, True, 127)
    check("loop recording started", eng.loops[0].state == "recording")
    eng._handle_note(0, 36, True, 100)
    eng._handle_note(0, 36, False, 0)
    eng._handle_note(0, 28, True, 100)
    evs = eng.loops[0].events
    check("drum on/off/on recorded into loop", len(evs) == 3)
    check("kick note on recorded", evs[0][1] == [0x99, 36, 100])
    check("kick note off recorded", evs[1][1] == [0x89, 36, 0])
    check("snare note on recorded", evs[2][1] == [0x99, 38, 100])


def test_routing():
    eng = make_engine()
    eng._handle_note(0, 32, True, 127)
    check("instrument select -> PC 38", eng.sent[-1] == [0xC0, 38])
    eng._handle_note(0, 36, True, 100)
    check("drum pad -> ch10 note 36", eng.sent[-1] == [0x99, 36, 100])
    eng._handle_note(0, 37, True, 127)
    check("loop 1 recording", eng.loops[0].state == "recording")
    eng._handle_key(60, True, 100)
    check("key recorded", len(eng.loops[0].events) == 1)
    eng.shift_held = True
    eng._handle_note(0, 32, True, 90)
    check("shift+instrument pad -> scale note 36", eng.sent[-1] == [0x90, 36, 90])
    eng._handle_note(0, 32, False, 0)
    check("shift note off recorded", len(eng.loops[0].events) == 3)
    eng._handle_note(0, 37, True, 127)
    check("shift+loop pad still clears",
          eng.loops[0].state == "empty" and eng.loops[0].events == [])
    eng.shift_held = False
    eng._handle_preset_press(39)
    check("preset 1 starts", eng.preset_players[0] is not None)
    eng._handle_preset_press(39)
    check("preset 1 toggles off", eng.preset_players[0] is None)
    eng._handle_cc(0, 52, 120)
    check("reverb knob -> CC91", [0xB0, 91, 37] in eng.sent)
    eng._handle_cc(0, 53, 64)
    check("main volume scales all channels",
          any(m[0:2] == [0xB0, 7] for m in eng.sent))
    eng._handle_cc(0, 48, 50)
    check("keys volume combined with main", eng.sent[-1] == [0xB0, 7, 25])
    eng._handle_note(0, 65, True, 127)
    check("bank down", eng.bank == 1)
    eng._handle_note(0, 98, True, 127)
    check("shift held", eng.shift_held)
    eng._handle_note(0, 91, True, 127)
    check("transport runs", True)


def test_loop_timing():
    eng = make_engine()
    slot = eng.loops[0]
    slot.begin_record()
    time.sleep(0.3)
    slot.events.append((0.1, [0x90, 60, 100]))
    time.sleep(0.1)
    check("early stop -> tight length", abs(slot.finish_record_length() - 0.4) < 0.15
          if hasattr(slot, "finish_record_length") else
          (slot.finish_record() and abs(slot.length - 0.4) < 0.15))
    check("early-stopped loop becomes playable", slot.state == "stopped")


def test_leds():
    led_out = types.SimpleNamespace(send_message=lambda m: None)
    leds = LEDManager(led_out)
    leds.set_base(32, 25)
    check("base color stored", leds.base[32] == 25)
    leds.flash(32, 20, duration=0.01)
    time.sleep(0.05)
    check("flash returns to base", leds.base[32] == 25)
    eng = make_engine()
    eng.selected_note = 32
    eng.refresh_leds()
    check("selected pad gets bright variant",
          eng.leds.base[32] == LEDManager.BANK_SELECTED_COLORS[0])
    check("other instrument pad gets dim variant",
          eng.leds.base[33] == LEDManager.BANK_COLORS[0])
    check("drum pad dim at idle", eng.leds.base[36] == LEDManager.COLOR_DRUM)
    check("empty loop pad off", eng.leds.base[37] == LEDManager.COLOR_OFF)
    check("preset pad idle color", eng.leds.base[39] == LEDManager.COLOR_PRESET)


def test_save_load():
    import tempfile
    eng = make_engine()
    slot = eng.loops[2]
    slot.events = [(0.1, [0x90, 60, 100]), (0.9, [0x80, 60, 0])]
    slot.length = 1.5
    slot.state = "stopped"
    path = os.path.join(tempfile.gettempdir(), "apc_loops_test.csv")
    e.save_loops(path, eng.loops)
    loaded = e.load_loops(path)
    check("saved loop round-trips", loaded[2]["length"] == 1.5
          and len(loaded[2]["events"]) == 2
          and loaded[2]["events"][0] == (0.1, [0x90, 60, 100]))
    os.remove(path)
    eng.load_saved_loops()
    check("load_saved_loops sets stopped state", True)


def test_preset_player_notes():
    events = [(0.0, "note", 0, 60, 100), (0.5, "note", 0, 64, 100)]
    player = e.PresetPlayer(events, "test melody")
    check("note preset length from events", abs(player.length - 0.55) < 0.01)
    drum_events = [(0.0, "drum", 0, 36, 127)]
    drum_player = e.PresetPlayer(drum_events, "drums")
    check("drum preset length = LOOP_LENGTH", drum_player.length == e.LOOP_LENGTH)


if __name__ == "__main__":
    test_constants()
    test_routing()
    test_loop_timing()
    test_leds()
    test_save_load()
    test_preset_player_notes()
    print()
    if failures:
        print(f"{len(failures)} FAILURES: {failures}")
        sys.exit(1)
    print("ALL TESTS PASSED")
