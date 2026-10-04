import csv
import os
import subprocess
import sys
import threading
import time

import rtmidi

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BANKS_FILE = os.path.join(BASE_DIR, "apc_banks.csv")
DRUMS_FILE = os.path.join(BASE_DIR, "apc_drums.csv")
CC_CONFIG_FILE = os.path.join(BASE_DIR, "apc_config.csv")

KEYS_INPUT_CHANNEL = 1
DRUM_CHANNEL = 9
LOOP_LENGTH = 4.0
LOOP_SLOTS = 5

SLOT_NOTE_ORDER = [32 - r * 8 + c for r in range(5) for c in range(4)]
LOOP_NOTE_ORDER = [39 - r * 8 for r in range(5)]
SLOT_CHANNELS = [0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 13, 14, 15]

BANK_COLOR = [25, 45, 61]
SELECTED_COLOR = [26, 46, 62]
DRUM_COLOR = 9
COLOR_RECORDING = 3
COLOR_PLAYING = 20
COLOR_STOPPED = 5
COLOR_OFF = 0

OCTAVE_CC_DOWN = 58
OCTAVE_CC_UP = 59
OCTAVE_MIN = -3
OCTAVE_MAX = 3

SOUNDFONT_CANDIDATES = [
    "/usr/share/sounds/sf2/FluidR3_GM.sf2",
    "/usr/share/sounds/sf2/FluidR3_GM.sf2.fluid",
    "/usr/share/soundfonts/FluidR3_GM.sf2",
    "/usr/share/soundfonts/default-GM.sf2",
]


def load_banks(path):
    slots = {}
    order = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            key = (int(row["bank"]), int(row["note"]))
            slots[key] = (int(row["channel"]), int(row["program"]), row["name"])
            if int(row["note"]) not in order:
                order.append(int(row["note"]))
    return slots, sorted(order, reverse=True)


def load_drums(path):
    drums = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            drums[int(row["note"])] = (int(row["drum_note"]), row["name"])
    return drums


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
        self.start_time = 0.0
        self.stop_flag = threading.Event()
        self.thread = None

    def begin_record(self):
        self.state = "recording"
        self.events = []
        self.start_time = time.monotonic()

    def finish_record(self):
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
            if self.stop_flag.wait(max(0.0, LOOP_LENGTH - (time.monotonic() - cycle_start))):
                return


class Engine:
    def __init__(self, banks, bank_order, drums, cc_actions):
        self.banks = banks
        self.bank_order = bank_order
        self.drums = drums
        self.cc_actions = cc_actions
        self.bank = 0
        self.shift_held = False
        self.octave = 0
        self.selected_note = None
        self.key_channel = SLOT_CHANNELS[0]
        self.loops = [LoopSlot(i) for i in range(LOOP_SLOTS)]
        self.note_to_loop = {n: i for i, n in enumerate(LOOP_NOTE_ORDER)}
        self.note_to_slot = {n: i for i, n in enumerate(SLOT_NOTE_ORDER)}
        self.midi_in = None
        self.midi_out = rtmidi.MidiOut()
        self.midi_out.set_client_name("APC Key 25 Engine")
        self.led_out = rtmidi.MidiOut()
        self.led_out.set_client_name("APC Key 25 LEDs")
        self._synth_process = None
        self._open_output()
        self._open_led_output()
        self._init_synth()
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
            return
        self.led_out.open_port(idx)
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
        for channel in SLOT_CHANNELS:
            self.send_synth([0xB0 | channel, 7, 100])
        self.send_synth([0xB0 | DRUM_CHANNEL, 7, 100])
        channel, program, name = self.banks[(0, SLOT_NOTE_ORDER[0])]
        self.send_synth([0xC0 | channel, program])
        self.selected_note = SLOT_NOTE_ORDER[0]
        print(f"Ready: keys play '{name}' (slot 1, bank A)")

    def send_synth(self, message):
        self.midi_out.send_message(message)

    def set_led(self, note, color):
        if self.led_out is None:
            return
        try:
            self.led_out.send_message([0x90, note, color])
        except Exception:
            pass

    def refresh_leds(self):
        color = BANK_COLOR[self.bank]
        for note in self.note_to_slot:
            led = SELECTED_COLOR[self.bank] if note == self.selected_note else color
            self.set_led(note, led)
        for note in self.drums:
            self.set_led(note, DRUM_COLOR)
        for note, slot in self.note_to_loop.items():
            state = self.loops[slot].state
            if state == "recording":
                self.set_led(note, COLOR_RECORDING)
            elif state == "playing":
                self.set_led(note, COLOR_PLAYING)
            elif state == "stopped":
                self.set_led(note, COLOR_STOPPED)
            else:
                self.set_led(note, COLOR_OFF)

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
            if channel == 0 and message[1] in (OCTAVE_CC_DOWN, OCTAVE_CC_UP) and message[2] > 0:
                self._shift_octave(-1 if message[1] == OCTAVE_CC_DOWN else 1)
            else:
                self._handle_cc(channel, message[1], message[2])

    def _handle_note(self, channel, note, is_on, velocity):
        if channel == KEYS_INPUT_CHANNEL:
            self._handle_key(note, is_on, velocity)
            return
        if channel == 0 and note in self.note_to_slot:
            if is_on:
                self._select_instrument(note)
            return
        if channel == 0 and note in self.drums:
            drum_note, name = self.drums[note]
            out_status = (0x90 if is_on else 0x80) | DRUM_CHANNEL
            message = [out_status, drum_note, velocity]
            self.send_synth(message)
            now = time.monotonic()
            for slot in self.loops:
                if slot.state == "recording":
                    slot.events.append((now - slot.start_time, message))
            return
        if channel == 0 and note in self.note_to_loop:
            if is_on:
                self._handle_loop_press(note)
            return
        if channel == 0:
            if note in (64, 65) and is_on:
                self._switch_bank(1 if note == 65 else -1)
            elif note == 91 and is_on:
                self._toggle_transport()
            elif note == 98:
                self.shift_held = is_on
                print("Shift: held" if is_on else "Shift: released")
            elif is_on and note in (66, 67, 68, 69, 70, 71, 81, 82, 83, 84, 85, 86, 93):
                print(f"Button {note}: reserved")

    def _handle_key(self, note, is_on, velocity):
        out_status = (0x90 if is_on else 0x80) | self.key_channel
        message = [out_status, note + self.octave * 12, velocity]
        self.send_synth(message)
        now = time.monotonic()
        for slot in self.loops:
            if slot.state == "recording":
                slot.events.append((now - slot.start_time, message))

    def _shift_octave(self, direction):
        new_octave = self.octave + direction
        if new_octave < OCTAVE_MIN or new_octave > OCTAVE_MAX:
            print(f"Octave: already at limit ({self.octave:+d})")
            return
        self.octave = new_octave
        print(f"Octave: {self.octave:+d}")

    def _select_instrument(self, note):
        slot = self.note_to_slot[note]
        channel, program, name = self.banks[(self.bank, note)]
        self.key_channel = channel
        self.send_synth([0xC0 | channel, program])
        self.selected_note = note
        self.refresh_leds()
        print(f"Bank {'ABC'[self.bank]} slot {slot + 1}: {name} (ch{channel + 1})")

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

    def _toggle_transport(self):
        if any(s.state == "playing" for s in self.loops):
            for slot in self.loops:
                if slot.state == "playing":
                    slot.stop_playback()
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
        action = self.cc_actions.get((channel, controller))
        if action is None:
            return
        fn = action["function"]
        if fn == "sustain":
            self.send_synth([0xB0 | self.key_channel, 64, value])
        elif fn == "knob_volume":
            target = (int(action["param1"]) - 1) & 0x0F
            self.send_synth([0xB0 | target, 7, value])
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


def main():
    banks, bank_order = load_banks(BANKS_FILE)
    drums = load_drums(DRUMS_FILE)
    cc_actions = load_cc_config(CC_CONFIG_FILE)
    print(f"Loaded {len(banks)} instrument slots, {len(drums)} drum pads, "
          f"{len(cc_actions)} CC assignments")

    midi_in = rtmidi.MidiIn()
    idx = find_port(midi_in.get_ports(), "APC Key 25")
    if idx is None:
        print("APC Key 25 not found. Plug it in and retry.")
        sys.exit(1)
    port_name = midi_in.get_ports()[idx]
    midi_in.open_port(idx)
    print(f"Input: {port_name}")

    engine = Engine(banks, bank_order, drums, cc_actions)
    midi_in.set_callback(engine.handle_midi)
    print("Keys play the selected slot. Pads cols 1-4 = instruments, "
          "cols 5-7 = drums, col 8 = loop record. Ctrl+C to quit.")

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
