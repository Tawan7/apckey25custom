import csv
import os
import subprocess
import sys
import threading
import time

import rtmidi

from apc_leds import LEDManager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BANKS_FILE = os.path.join(BASE_DIR, "apc_banks.csv")
DRUMS_FILE = os.path.join(BASE_DIR, "apc_drums.csv")
PRESETS_FILE = os.path.join(BASE_DIR, "apc_presets.csv")
CC_CONFIG_FILE = os.path.join(BASE_DIR, "apc_config.csv")
LOOPS_FILE = os.path.join(BASE_DIR, "apc_loops.csv")

KEYS_INPUT_CHANNEL = 1
DRUM_CHANNEL = 9
LOOP_LENGTH = 4.0
LOOP_SLOTS = 10
PRESET_COUNT = 5

SLOT_NOTE_ORDER = [32 - r * 8 + c for r in range(5) for c in range(4)]
DRUM_NOTE_ORDER = [36 - r * 8 for r in range(5)]
LOOP_NOTE_ORDER = [37 - r * 8 for r in range(5)] + [38 - r * 8 for r in range(5)]
PRESET_NOTE_ORDER = [39 - r * 8 for r in range(5)]
ALL_PAD_NOTES = [32 - r * 8 + c for r in range(5) for c in range(8)]
SLOT_CHANNELS = [0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 13, 14, 15]

SHIFT_SCALE_ROOT = 36
SHIFT_SCALE_STEPS = [0, 2, 3, 5, 7, 8, 10, 12, 14, 15, 17, 19, 20, 22, 24]
SHIFT_NOTE_PADS = SLOT_NOTE_ORDER + DRUM_NOTE_ORDER

SOUNDFONT_CANDIDATES = [
    "/usr/share/sounds/sf2/FluidR3_GM.sf2",
    "/usr/share/sounds/sf2/FluidR3_GM.sf2.fluid",
    "/usr/share/soundfonts/FluidR3_GM.sf2",
    "/usr/share/soundfonts/default-GM.sf2",
]

REVERB_KNOB_CC = 52
MAIN_VOLUME_KNOB_CC = 53


def load_banks(path):
    slots = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            key = (int(row["bank"]), int(row["note"]))
            slots[key] = (int(row["channel"]), int(row["program"]), row["name"])
    return slots


def load_drums(path):
    drums = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            drums[int(row["note"])] = (int(row["drum_note"]), row["name"])
    return drums


def load_presets(path):
    """Unified preset format: drum hits and melodic notes share one table.

    Columns: preset, name, offset, kind (drum|note), channel, note, velocity.
    `offset` is 0.0-1.0 of the loop for drums, seconds for note presets.
    Legacy files without `kind` are treated as drum patterns.
    """
    presets = {}
    if not os.path.exists(path):
        return presets
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            key = int(row["preset"])
            kind = row.get("kind") or "drum"
            note = row.get("note") or row.get("drum_note") or "0"
            note = int(note)
            events = [(float(row["offset"]), kind,
                       int(row.get("channel", 0)), note,
                       int(row["velocity"]))]
            presets.setdefault(key, []).extend(events)
    for key in presets:
        presets[key].sort(key=lambda ev: ev[0])
    return presets


def load_loops(path):
    """Saved loops: slot, length, offset_seconds, message (space-separated bytes)."""
    loops = {}
    if not os.path.exists(path):
        return loops
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            slot = int(row["slot"])
            message = [int(b) for b in row["message"].split()]
            loops.setdefault(slot, {"length": float(row["length"]), "events": []})
            loops[slot]["events"].append((float(row["offset"]), message))
    return loops


def save_loops(path, loop_slots):
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["slot", "length", "offset", "message"])
        for slot in loop_slots:
            if slot.state == "empty" or not slot.events:
                continue
            for offset, message in slot.events:
                writer.writerow([slot.index, f"{slot.length:.3f}",
                                  f"{offset:.3f}", " ".join(str(b) for b in message)])


def load_cc_config(path):
    actions = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            if row["type"] != "CC":
                continue
            actions[(int(row["channel"]), int(row["number"]))] = {
                "control": row["control"],
                "function": row["function"],
                "param1": row["param1"],
                "param2": row["param2"],
            }
    return actions


def find_port(ports, needle):
    for i, name in enumerate(ports):
        if needle.lower() in name.lower():
            return i
    return None


def find_soundfont():
    for path in SOUNDFONT_CANDIDATES:
        if os.path.exists(path):
            return path
    result = subprocess.run(
        ["find", os.path.expanduser("~"), "-name", "*.sf2", "-print", "-quit"],
        capture_output=True, text=True, timeout=20)
    for line in result.stdout.splitlines():
        if line.strip():
            return line.strip()
    return None


class LoopSlot:
    def __init__(self, index):
        self.index = index
        self.state = "empty"
        self.events = []
        self.length = LOOP_LENGTH
        self.start_time = 0.0
        self.stop_flag = threading.Event()
        self.thread = None

    def begin_record(self):
        self.state = "recording"
        self.events = []
        self.start_time = time.monotonic()

    def finish_record(self):
        if self.state != "recording":
            return False
        self.length = max(0.25, time.monotonic() - self.start_time)
        if self.events:
            self.state = "stopped"
            return True
        self.state = "empty"
        return False

    def start_playback(self, engine):
        self.stop_flag.clear()
        self.state = "playing"
        self.thread = threading.Thread(target=self._play, args=(engine,), daemon=True)
        self.thread.start()

    def stop_playback(self):
        self.stop_flag.set()
        self.state = "stopped" if self.events else "empty"

    def clear(self):
        self.stop_flag.set()
        self.state = "empty"
        self.events = []

    def _play(self, engine):
        while not self.stop_flag.is_set():
            cycle_start = time.monotonic()
            for at, message in self.events:
                delay = at - (time.monotonic() - cycle_start)
                if delay > 0 and self.stop_flag.wait(delay):
                    return
                if self.stop_flag.is_set():
                    return
                engine.send_synth(message)
            if self.stop_flag.wait(max(0.0, self.length - (time.monotonic() - cycle_start))):
                return


class PresetPlayer:
    """Plays a preset: drum events use offset 0.0-1.0 of LOOP_LENGTH,
    note events use absolute seconds. Length comes from the preset itself
    (LOOP_LENGTH for drum patterns, max note offset for melodies)."""

    def __init__(self, events, name, length=None):
        self.name = name
        self.events = events
        kinds = {kind for _, kind, _, _, _ in events}
        if length is None:
            if kinds == {"drum"}:
                length = LOOP_LENGTH
            else:
                length = max((at for at, kind, _, _, _ in events
                              if kind == "note"), default=LOOP_LENGTH) + 0.05
        self.length = length
        self.stop_flag = threading.Event()
        self.thread = None

    def start(self, send):
        self.stop_flag.clear()
        self.thread = threading.Thread(target=self._run, args=(send,), daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_flag.set()

    def _run(self, send):
        while not self.stop_flag.is_set():
            cycle_start = time.monotonic()
            for at, kind, channel, note, velocity in self.events:
                if kind == "drum":
                    delay = at * LOOP_LENGTH - (time.monotonic() - cycle_start)
                    if delay > 0 and self.stop_flag.wait(delay):
                        return
                    if self.stop_flag.is_set():
                        return
                    send([0x90 | DRUM_CHANNEL, note, velocity])
                    send([0x80 | DRUM_CHANNEL, note, 0])
                else:
                    delay = at - (time.monotonic() - cycle_start)
                    if delay > 0 and self.stop_flag.wait(delay):
                        return
                    if self.stop_flag.is_set():
                        return
                    send([0x90 | channel, note, velocity])
            if self.stop_flag.wait(max(0.0, self.length - (time.monotonic() - cycle_start))):
                return


class Engine:
    def __init__(self, banks, drums, presets, cc_actions):
        self.banks = banks
        self.drums = drums
        self.channel_volume = {c: 100 for c in SLOT_CHANNELS + [DRUM_CHANNEL]}
        self.main_volume_value = 100
        self.presets = presets
        self.cc_actions = cc_actions
        self.bank = 0
        self.shift_held = False
        self.selected_note = None
        self.key_channel = SLOT_CHANNELS[0]
        self.loops = [LoopSlot(i) for i in range(LOOP_SLOTS)]
        self.preset_players = [None] * PRESET_COUNT
        self.note_to_loop = {n: i for i, n in enumerate(LOOP_NOTE_ORDER)}
        self.note_to_preset = {n: i for i, n in enumerate(PRESET_NOTE_ORDER)}
        self.note_to_slot = {n: i for i, n in enumerate(SLOT_NOTE_ORDER)}
        self.note_to_scale = {}
        for i, note in enumerate(SHIFT_NOTE_PADS):
            if i < len(SHIFT_SCALE_STEPS):
                self.note_to_scale[note] = SHIFT_SCALE_ROOT + SHIFT_SCALE_STEPS[i]
        self.midi_in = None
        self.leds = None
        self.midi_out = rtmidi.MidiOut()
        self.midi_out.set_client_name("APC Key 25 Engine")
        self.led_out = rtmidi.MidiOut()
        self.led_out.set_client_name("APC Key 25 LEDs")
        self._synth_process = None
        self._open_output()
        self._open_led_output()
        self._init_synth()
        self.load_saved_loops()
        self.refresh_leds()

    def _open_output(self):
        idx = find_port(self.midi_out.get_ports(), "fluid")
        if idx is None:
            idx = find_port(self.midi_out.get_ports(), "qsynth")
        if idx is None:
            idx = self._start_fluidsynth()
        if idx is None:
            raise RuntimeError("No synth found. Start Qsynth or install FluidSynth with a soundfont.")
        port_name = self.midi_out.get_ports()[idx]
        self.midi_out.open_port(idx)
        print(f"Output: {port_name}")

    def _open_led_output(self):
        idx = find_port(self.led_out.get_ports(), "apc key 25")
        if idx is None:
            print("No APC output port found; LED feedback disabled.")
            self.led_out = None
            self.leds = LEDManager(None)
            return
        self.led_out.open_port(idx)
        self.leds = LEDManager(self.led_out)
        print(f"LED output: {self.led_out.get_ports()[idx]}")

    def _start_fluidsynth(self):
        soundfont = find_soundfont()
        if not soundfont:
            return None
        print(f"No running synth. Starting FluidSynth with {soundfont}")
        self._synth_process = subprocess.Popen(
            ["fluidsynth", "-a", "pulseaudio", "-m", "alsa_seq", "-g", "0.5", soundfont],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            time.sleep(0.2)
            idx = find_port(self.midi_out.get_ports(), "fluid")
            if idx is not None:
                return idx
        return None

    def _init_synth(self):
        for channel in SLOT_CHANNELS + [DRUM_CHANNEL]:
            self.send_synth([0xB0 | channel, 7, 100])
        self.set_reverb(64)
        channel, program, name = self.banks[(0, SLOT_NOTE_ORDER[0])]
        self.send_synth([0xC0 | channel, program])
        self.selected_note = SLOT_NOTE_ORDER[0]
        print(f"Ready: keys play '{name}' (slot 1, bank A)")

    def send_synth(self, message):
        self.midi_out.send_message(message)

    def set_reverb(self, value):
        amount = int(value * 40 / 127)
        depth = int(value * 100 / 127)
        self.send_synth([0xB0, 91, amount])
        for channel in SLOT_CHANNELS + [DRUM_CHANNEL]:
            self.send_synth([0xB0 | channel, 91, depth])

    def set_main_volume(self, value):
        self.main_volume_value = value
        for channel, vol in self.channel_volume.items():
            scaled = vol * value // 127
            self.send_synth([0xB0 | channel, 7, scaled])

    def set_led(self, note, color):
        self.leds.set_base(note, color)

    def refresh_leds(self):
        base = LEDManager.BANK_COLORS[self.bank]
        selected = LEDManager.BANK_SELECTED_COLORS[self.bank]
        for note in self.note_to_slot:
            self.leds.set_base(note, selected if note == self.selected_note else base)
        for note in self.drums:
            self.leds.set_base(note, LEDManager.COLOR_DRUM)
        for note, slot in self.note_to_loop.items():
            state = self.loops[slot].state
            if state == "recording":
                self.leds.set_base(note, LEDManager.COLOR_RECORDING)
            elif state == "playing":
                self.leds.set_base(note, LEDManager.COLOR_PLAYING)
            elif state == "stopped":
                self.leds.set_base(note, LEDManager.COLOR_STOPPED)
            else:
                self.leds.set_base(note, LEDManager.COLOR_OFF)
        for note, index in self.note_to_preset.items():
            if self.preset_players[index] is not None:
                self.leds.set_base(note, LEDManager.COLOR_PLAYING)
            else:
                self.leds.set_base(note, LEDManager.COLOR_PRESET)

    def all_sounds_off(self):
        for channel in SLOT_CHANNELS + [DRUM_CHANNEL]:
            self.send_synth([0xB0 | channel, 123, 0])

    def handle_midi(self, message, data=None):
        if not message:
            return
        if isinstance(message, tuple):
            message = message[0]
        status = message[0] & 0xF0
        channel = message[0] & 0x0F
        if status in (0x90, 0x80):
            velocity = message[2] if len(message) > 2 else 0
            is_on = status == 0x90 and velocity > 0
            self._handle_note(channel, message[1], is_on, velocity)
        elif status == 0xB0:
            self._handle_cc(channel, message[1], message[2])

    def _handle_note(self, channel, note, is_on, velocity):
        if channel == KEYS_INPUT_CHANNEL:
            self._handle_key(note, is_on, velocity)
            return
        if channel != 0:
            return
        if self.shift_held and note in self.note_to_scale:
            self._handle_shift_note(note, is_on, velocity)
            return
        if note in self.note_to_slot:
            if is_on:
                self._select_instrument(note)
            return
        if note in self.drums:
            drum_note, name = self.drums[note]
            out_status = (0x90 if is_on else 0x80) | DRUM_CHANNEL
            self.send_synth([out_status, drum_note, velocity])
            if is_on:
                self.leds.flash(note, LEDManager.COLOR_DRUM_HIT)
            return
        if note in self.note_to_loop:
            if is_on:
                self._handle_loop_press(note)
            return
        if note in self.note_to_preset:
            if is_on:
                self._handle_preset_press(note)
            return
        if note in (64, 65) and is_on:
            self._switch_bank(1 if note == 65 else -1)
        elif note == 91 and is_on:
            self._toggle_transport()
        elif note == 93 and is_on:
            self.save_all_loops()
        elif note == 98:
            self.shift_held = is_on
            if not is_on:
                self._release_all_shift_notes()
            print("Shift: held" if is_on else "Shift: released")
        elif is_on and note in (66, 67, 68, 69, 70, 71, 81, 82, 83, 84, 85, 86):
            print(f"Button {note}: reserved")

    def _handle_key(self, note, is_on, velocity):
        out_status = (0x90 if is_on else 0x80) | self.key_channel
        message = [out_status, note, velocity]
        self.send_synth(message)
        now = time.monotonic()
        for slot in self.loops:
            if slot.state == "recording":
                slot.events.append((now - slot.start_time, message))

    def _handle_shift_note(self, pad_note, is_on, velocity):
        note = self.note_to_scale[pad_note]
        out_status = (0x90 if is_on else 0x80) | self.key_channel
        message = [out_status, note, velocity]
        self.send_synth(message)
        now = time.monotonic()
        for slot in self.loops:
            if slot.state == "recording":
                slot.events.append((now - slot.start_time, message))

    def _release_all_shift_notes(self):
        for pad_note in self.note_to_scale:
            note = self.note_to_scale[pad_note]
            self.send_synth([0x80 | self.key_channel, note, 0])

    def _select_instrument(self, note):
        slot = self.note_to_slot[note]
        channel, program, name = self.banks[(self.bank, note)]
        self.key_channel = channel
        self.send_synth([0xC0 | channel, program])
        self.selected_note = note
        print(f"Bank {'ABC'[self.bank]} slot {slot + 1}: {name} (ch{channel + 1})")
        self.refresh_leds()

    def _switch_bank(self, direction):
        self.bank = (self.bank + direction) % 3
        channel, program, name = self.banks[(self.bank, SLOT_NOTE_ORDER[0])]
        self.key_channel = channel
        self.selected_note = SLOT_NOTE_ORDER[0]
        self.send_synth([0xC0 | channel, program])
        print(f"Instrument bank: {'ABC'[self.bank]} ({name})")
        self.refresh_leds()

    def _handle_loop_press(self, note):
        slot = self.loops[self.note_to_loop[note]]
        if self.shift_held:
            slot.clear()
            print(f"Loop {slot.index + 1}: cleared")
        elif slot.state == "empty":
            slot.begin_record()
            print(f"Loop {slot.index + 1}: RECORDING (play keys now)")
            threading.Timer(LOOP_LENGTH, self._finish_recording, args=[slot]).start()
        elif slot.state == "recording":
            self._finish_recording(slot)
        elif slot.state == "playing":
            slot.stop_playback()
            print(f"Loop {slot.index + 1}: stopped")
        elif slot.state == "stopped":
            slot.start_playback(self)
            print(f"Loop {slot.index + 1}: playing")
        self.refresh_leds()

    def _finish_recording(self, slot):
        if slot.state != "recording":
            return
        if slot.finish_record():
            slot.start_playback(self)
            print(f"Loop {slot.index + 1}: playing ({len(slot.events)} events)")
        else:
            print(f"Loop {slot.index + 1}: nothing recorded")
        self.refresh_leds()

    def _handle_preset_press(self, note):
        index = self.note_to_preset[note]
        player = self.preset_players[index]
        if player is not None:
            player.stop()
            self.preset_players[index] = None
            print(f"Preset {index + 1}: stopped")
        else:
            events = self.presets.get(index + 1, [])
            if not events:
                print(f"Preset {index + 1}: no pattern defined")
                return
            name = PRESET_NAMES.get(index + 1, f"Preset {index + 1}")
            player = PresetPlayer(events, name)
            self.preset_players[index] = player
            player.start(self.send_synth)
            print(f"Preset {index + 1}: {name} playing")
        self.refresh_leds()

    def save_all_loops(self):
        save_loops(LOOPS_FILE, self.loops)
        count = sum(1 for s in self.loops if s.events)
        print(f"Saved {count} loops to {os.path.basename(LOOPS_FILE)}")

    def load_saved_loops(self):
        saved = load_loops(LOOPS_FILE)
        for slot, data in saved.items():
            if slot >= LOOP_SLOTS:
                continue
            self.loops[slot].events = data["events"]
            self.loops[slot].length = data["length"]
            self.loops[slot].state = "stopped"
        if saved:
            print(f"Loaded {len(saved)} saved loops from {os.path.basename(LOOPS_FILE)}")
        self.refresh_leds()

    def _toggle_transport(self):
        stopped_any = False
        for slot in self.loops:
            if slot.state == "playing":
                slot.stop_playback()
                stopped_any = True
        for player in self.preset_players:
            if player is not None:
                player.stop()
        self.preset_players = [None] * PRESET_COUNT
        if stopped_any:
            print("Transport: STOP")
        else:
            started = False
            for slot in self.loops:
                if slot.state == "stopped":
                    slot.start_playback(self)
                    started = True
            print("Transport: PLAY" if started else "Transport: no loops to play")
        self.refresh_leds()

    def _handle_cc(self, channel, controller, value):
        if controller == REVERB_KNOB_CC:
            self.set_reverb(value)
            print(f"Reverb: {value}")
            return
        if controller == MAIN_VOLUME_KNOB_CC:
            self.set_main_volume(value)
            print(f"Main volume: {value}")
            return
        action = self.cc_actions.get((channel, controller))
        if action is None:
            return
        fn = action["function"]
        if fn == "sustain":
            self.send_synth([0xB0 | self.key_channel, 64, value])
        elif fn == "knob_volume":
            target = (int(action["param1"]) - 1) & 0x0F
            self.channel_volume[target] = value
            self.send_synth([0xB0 | target, 7, value * self.main_volume_value // 127])
            print(f"Volume ch{action['param1']}: {value}")
        elif fn == "knob_pan":
            target = (int(action["param1"]) - 1) & 0x0F
            self.send_synth([0xB0 | target, 10, value])
            print(f"Pan ch{action['param1']}: {value}")

    def close(self):
        try:
            self.all_sounds_off()
            self.midi_out.close_port()
            if self.led_out:
                self.led_out.close_port()
        finally:
            if self._synth_process:
                self._synth_process.terminate()
                self._synth_process.wait()


PRESET_NAMES = {
    1: "Four on Floor",
    2: "Hypnotic",
    3: "Offbeat",
    4: "Rolling",
    5: "Break",
}


def main():
    banks = load_banks(BANKS_FILE)
    drums = load_drums(DRUMS_FILE)
    presets = load_presets(PRESETS_FILE)
    cc_actions = load_cc_config(CC_CONFIG_FILE)
    print(f"Loaded {len(banks)} instrument slots, {len(drums)} drum pads, "
          f"{len(presets)} presets, {len(cc_actions)} CC assignments")

    midi_in = rtmidi.MidiIn()
    idx = find_port(midi_in.get_ports(), "APC Key 25")
    if idx is None:
        print("APC Key 25 not found. Plug it in and retry.")
        sys.exit(1)
    port_name = midi_in.get_ports()[idx]
    midi_in.open_port(idx)
    print(f"Input: {port_name}")

    engine = Engine(banks, drums, presets, cc_actions)
    midi_in.set_callback(engine.handle_midi)
    print("Rec button: save all recorded loops to apc_loops.csv")
    print("Cols 1-4 = instruments (Shift+Up/Down = bank), col 5 = drums, "
          "cols 6-7 = loops, col 8 = presets. Hold Shift + pads = play notes. Ctrl+C to quit.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        midi_in.close_port()
        engine.close()


if __name__ == "__main__":
    main()
