#!/usr/bin/env python3
import argparse
import csv
import json
import os
import threading
import time

import fluidsynth
import mido
import rtmidi

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(BASE_DIR, "apcv2_settings.json")
BANKS_FILE = os.path.join(BASE_DIR, "apc_banks.csv")
DRUMS_FILE = os.path.join(BASE_DIR, "apc_drums.csv")
CC_CONFIG_FILE = os.path.join(BASE_DIR, "apc_config.csv")
LOOPS_FILE = os.path.join(BASE_DIR, "apcv2_loops.json")

KEYS_INPUT_CHANNEL = 1
DRUM_CHANNEL = 9
BEATS_PER_BAR = 4
LOOKAHEAD = 0.020

SLOT_NOTE_ORDER = [32 - r * 8 + c for r in range(5) for c in range(4)]
LOOP_NOTE_ORDER = [39 - r * 8 for r in range(5)]
PRELOAD_NOTE_ORDER = [c - r * 8 for r in range(5) for c in (36, 37, 38)]
SLOT_CHANNELS = [0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 13, 14, 15]
METRONOME_CHANNEL = 12
METRONOME_PROGRAM = 0
METRONOME_ACCENT_NOTE = 76
METRONOME_CLICK_NOTE = 77

BANK_COLOR = [25, 45, 61]
DRUM_COLOR = 9
COLOR_RECORDING = 3
COLOR_PLAYING = 20
COLOR_STOPPED = 5
COLOR_OFF = 0

DEFAULT_SETTINGS = {
    "soundfont": "soundfonts/Live_Party_SoundFont__Techno_.sf2",
    "bpm": 128,
    "gain": 0.6,
    "audio_driver": "alsa",
    "reverb": 0.25,
    "chorus": 0.0,
}


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE) as f:
            settings.update(json.load(f))
    return settings


def load_banks(path):
    slots = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            slots[(int(row["bank"]), int(row["note"]))] = (
                int(row["channel"]), int(row["program"]), row["name"])
    return slots


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


def resolve_soundfont(settings):
    candidates = [settings["soundfont"]]
    for path in candidates:
        if os.path.isabs(path) and os.path.exists(path):
            return path
        joined = os.path.join(BASE_DIR, path)
        if os.path.exists(joined):
            return joined
        expanded = os.path.expanduser(path)
        if os.path.exists(expanded):
            return expanded
    return None


def load_midi_loop(path, max_events=2000):
    mid = mido.MidiFile(path)
    events = []
    for track in mid.tracks:
        ticks = 0.0
        tempo = 500000
        for msg in track:
            ticks += msg.time
            if msg.type == "set_tempo":
                tempo = msg.tempo
            if msg.type not in ("note_on", "note_off"):
                continue
            beat = ticks * tempo / (mid.ticks_per_beat * 60_000_000) * 60.0
            beat = round(beat * 4) / 4.0
            if msg.type == "note_off" or msg.velocity == 0:
                status = 0x80 | (msg.channel & 0x0F)
            else:
                status = 0x90 | (msg.channel & 0x0F)
            events.append((beat, [status, msg.note & 0x7F, msg.velocity & 0x7F]))
    events.sort(key=lambda e: e[0])
    if not events:
        return [], BEATS_PER_BAR
    length = events[-1][0]
    bars = max(1, int(length // BEATS_PER_BAR) + (1 if length % BEATS_PER_BAR else 0))
    return events[:max_events], bars * BEATS_PER_BAR


class Clock:
    def __init__(self, bpm):
        self.lock = threading.Lock()
        self.bpm = bpm
        self.origin = time.monotonic()
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def now(self):
        with self.lock:
            return (time.monotonic() - self.origin) * self.bpm / 60.0

    def set_bpm(self, bpm):
        with self.lock:
            now = time.monotonic()
            current = (now - self.origin) * self.bpm / 60.0
            self.bpm = bpm
            self.origin = now - current * 60.0 / bpm

    def next_bar_beat(self):
        return (int(self.now() // BEATS_PER_BAR) + 1) * BEATS_PER_BAR

    def beat_to_time(self, beat):
        with self.lock:
            return self.origin + beat * 60.0 / self.bpm

    def _run(self):
        while self.running:
            time.sleep(0.005)

    def stop(self):
        self.running = False


class LoopSlot:
    def __init__(self, index):
        self.index = index
        self.state = "empty"
        self.events = []
        self.length_beats = BEATS_PER_BAR
        self.start_beat = 0.0
        self.fired_index = 0

    def arm_record(self, start_beat):
        self.state = "armed"
        self.start_beat = start_beat

    def begin_record(self):
        self.state = "recording"
        self.events = []

    def finish_record(self, length_beats):
        if self.events:
            self.length_beats = max(BEATS_PER_BAR, length_beats)
            self.state = "stopped"
            return True
        self.state = "empty"
        return False

    def start_playback(self, start_beat):
        self.state = "playing"
        self.start_beat = start_beat
        self.fired_index = 0

    def stop_playback(self):
        self.state = "stopped" if self.events else "empty"

    def clear(self):
        self.state = "empty"
        self.events = []

    def load_events(self, events, length_beats):
        self.events = events
        self.length_beats = length_beats
        self.state = "stopped"


class Engine:
    def __init__(self, settings, banks, drums, cc_actions):
        self.settings = settings
        self.banks = banks
        self.drums = drums
        self.cc_actions = cc_actions
        self.bank = 0
        self.shift_held = False
        self.selected_note = None
        self.key_channel = SLOT_CHANNELS[0]
        self.clock = Clock(settings["bpm"])
        self.loops = [LoopSlot(i) for i in range(5)]
        self.preload_slots = [LoopSlot(i) for i in range(15)]
        self.note_to_loop = {n: i for i, n in enumerate(LOOP_NOTE_ORDER)}
        self.note_to_preload = {n: i for i, n in enumerate(PRELOAD_NOTE_ORDER)}
        self.note_to_slot = {n: i for i, n in enumerate(SLOT_NOTE_ORDER)}
        self.metronome = False
        self.midi_in = None
        self.led_out = None
        self.running = True
        self.scheduler = threading.Thread(target=self._schedule, daemon=True)
        self._open_input()
        self._open_led_output()
        self._init_synth()
        self._init_metronome()
        self.scheduler.start()
        self.refresh_leds()

    def _open_input(self):
        self.midi_in = rtmidi.MidiIn()
        idx = self._find_port(self.midi_in.get_ports(), "APC Key 25")
        if idx is None:
            raise RuntimeError("APC Key 25 not found. Plug it in and retry.")
        self.midi_in.open_port(idx)
        self.midi_in.set_callback(self.handle_midi)
        print(f"Input: {self.midi_in.get_ports()[idx]}")

    def _open_led_output(self):
        out = rtmidi.MidiOut()
        idx = self._find_port(out.get_ports(), "APC Key 25")
        if idx is None:
            print("No APC output port; LED feedback disabled.")
            return
        out.open_port(idx)
        self.led_out = out
        print(f"LED output: {out.get_ports()[idx]}")

    @staticmethod
    def _find_port(ports, needle):
        for i, name in enumerate(ports):
            if needle.lower() in name.lower():
                return i
        return None

    def _init_synth(self):
        soundfont = resolve_soundfont(self.settings)
        if not soundfont:
            raise RuntimeError(
                "Soundfont not found. Set 'soundfont' in apcv2_settings.json.")
        self.synth = fluidsynth.Synth(gain=self.settings["gain"])
        self.synth.start(driver=self.settings["audio_driver"])
        self.sfid = self.synth.sfload(soundfont, 1)
        if hasattr(self.synth, "reverb"):
            self.synth.reverb(0, self.settings["reverb"], 0, 1)
        else:
            self._setting("synth.reverb.level", float(self.settings["reverb"]))
            self._setting("synth.chorus.level", float(self.settings["chorus"]))
        print(f"Soundfont: {soundfont}")
        for channel in SLOT_CHANNELS:
            self.send_synth([0xB0 | channel, 7, 100])
        self.send_synth([0xB0 | DRUM_CHANNEL, 7, 100])
        channel, program, name = self.banks[(0, SLOT_NOTE_ORDER[0])]
        self.send_synth([0xC0 | channel, program])
        self.selected_note = SLOT_NOTE_ORDER[0]
        print(f"Ready: keys play '{name}' (slot 1, bank A)")

    def _init_metronome(self):
        self.send_synth([0xC0 | METRONOME_CHANNEL, METRONOME_PROGRAM])
        self.send_synth([0xB0 | METRONOME_CHANNEL, 7, 64])

    def _toggle_metronome(self):
        self.metronome = not self.metronome
        print(f"Metronome: {'ON' if self.metronome else 'OFF'}")
        if self.metronome:
            self._click_beat = int(self.clock.now())

    def _stop_all(self):
        for slot in self.loops + self.preload_slots:
            if slot.state == "playing":
                slot.stop_playback()
        print("Stop all: every loop stopped")
        self.refresh_leds()

    def _setting(self, name, value):
        setter = getattr(self.synth, "setting", None)
        if setter is None:
            print(f"Cannot set {name}={value} (no settings API)")
            return
        try:
            setter(name, value)
        except Exception as exc:
            print(f"Cannot set {name}={value}: {exc}")

    def send_synth(self, message):
        status = message[0] & 0xF0
        channel = message[0] & 0x0F
        data = message[1:]
        if status == 0x90 and message[2] > 0:
            self.synth.noteon(channel, message[1], message[2])
        elif status == 0x80 or (status == 0x90 and message[2] == 0):
            self.synth.noteoff(channel, message[1])
        elif status == 0xB0:
            self.synth.cc(channel, message[1], message[2])
        elif status == 0xC0:
            self.synth.program_change(channel, message[1])

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
            self.set_led(note, color)
        for note in self.drums:
            self.set_led(note, DRUM_COLOR)
        for note, slot in self.note_to_loop.items():
            state = self.loops[slot].state
            if state in ("recording", "armed"):
                self.set_led(note, COLOR_RECORDING)
            elif state == "playing":
                self.set_led(note, COLOR_PLAYING)
            elif state == "stopped":
                self.set_led(note, COLOR_STOPPED)
            else:
                self.set_led(note, COLOR_OFF)

    def all_sounds_off(self):
        for channel in SLOT_CHANNELS:
            self.synth.cc(channel, 123, 0)
        self.synth.cc(DRUM_CHANNEL, 123, 0)

    def _schedule(self):
        while self.running:
            now_beat = self.clock.now()
            if self.metronome:
                current = int(now_beat)
                last = getattr(self, "_click_beat", current - 1)
                if current != last:
                    self._click_beat = current
                    note = METRONOME_ACCENT_NOTE if current % BEATS_PER_BAR == 0 else METRONOME_CLICK_NOTE
                    self.send_synth([0x90 | METRONOME_CHANNEL, note, 100])
                    self.send_synth([0x80 | METRONOME_CHANNEL, note, 0])
            for slot in self.loops + self.preload_slots:
                if slot.state != "playing":
                    continue
                elapsed = now_beat - slot.start_beat
                if elapsed < 0:
                    continue
                position = elapsed % slot.length_beats
                target = self.clock.now() + LOOKAHEAD * self.clock.bpm / 60.0
                while slot.fired_index < len(slot.events):
                    at, message = slot.events[slot.fired_index]
                    if at > target:
                        break
                    delay = max(0.0, self.clock.beat_to_time(
                        slot.start_beat + at) - time.monotonic())
                    if delay <= LOOKAHEAD:
                        self.send_synth(message)
                    slot.fired_index += 1
                if slot.fired_index >= len(slot.events) and position < 0.01:
                    slot.fired_index = 0
            time.sleep(0.003)

    def handle_midi(self, message, data=None):
        if not message:
            return
        message = message[0] if isinstance(message, tuple) else message
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
        if self.shift_held and note in self.note_to_preload:
            if is_on:
                self._handle_preload_press(note)
            return
        if note in self.note_to_slot:
            if is_on:
                self._select_instrument(note)
        elif note in self.drums:
            drum_note, _ = self.drums[note]
            out_status = (0x90 if is_on else 0x80) | DRUM_CHANNEL
            self.send_synth([out_status, drum_note, velocity])
        elif note in self.note_to_loop:
            if is_on:
                self._handle_loop_press(note)
        else:
            if note in (64, 65) and is_on:
                self._switch_bank(1 if note == 65 else -1)
            elif note == 91 and is_on:
                self._toggle_transport()
            elif note == 81 and is_on:
                self._stop_all()
            elif note == 93 and is_on:
                self._toggle_metronome()
            elif note == 98:
                self.shift_held = is_on

    def _handle_key(self, note, is_on, velocity):
        out_status = (0x90 if is_on else 0x80) | self.key_channel
        message = [out_status, note, velocity]
        self.send_synth(message)
        now_beat = self.clock.now()
        for slot in self.loops:
            if slot.state == "recording":
                slot.events.append((round((now_beat - slot.start_beat) * 16) / 16.0
                                    % slot.length_beats, message))

    def _handle_preload_press(self, note):
        slot = self.preload_slots[self.note_to_preload[note]]
        if slot.state == "empty":
            print(f"Preload {slot.index + 1}: empty (set it in apcv2_preload.json)")
            return
        if slot.state == "playing":
            slot.stop_playback()
            print(f"Preload {slot.index + 1}: stopped")
        else:
            slot.start_playback(self.clock.next_bar_beat())
            print(f"Preload {slot.index + 1}: starts on next bar")

    def _select_instrument(self, note):
        slot = self.note_to_slot[note]
        channel, program, name = self.banks[(self.bank, note)]
        self.key_channel = channel
        self.send_synth([0xC0 | channel, program])
        self.selected_note = note
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
            start = self.clock.next_bar_beat()
            slot.arm_record(start)
            print(f"Loop {slot.index + 1}: ARMED (recording starts on next bar)")
            threading.Timer(
                max(0.01, self.clock.beat_to_time(start) - time.monotonic()),
                self._begin_recording, args=[slot]).start()
        elif slot.state in ("armed", "recording"):
            self._finish_recording(slot)
        elif slot.state == "playing":
            slot.stop_playback()
            print(f"Loop {slot.index + 1}: stopped")
        elif slot.state == "stopped":
            slot.start_playback(self.clock.next_bar_beat())
            print(f"Loop {slot.index + 1}: starts on next bar")
        self.refresh_leds()

    def _begin_recording(self, slot):
        if slot.state != "armed":
            return
        slot.begin_record()
        print(f"Loop {slot.index + 1}: RECORDING (play keys now)")
        threading.Timer(
            slot.length_beats * 60.0 / self.clock.bpm,
            self._finish_recording, args=[slot]).start()
        self.refresh_leds()

    def _finish_recording(self, slot):
        if slot.state not in ("recording", "armed"):
            return
        if slot.finish_record(slot.length_beats):
            slot.start_playback(self.clock.next_bar_beat())
            print(f"Loop {slot.index + 1}: plays from next bar ({len(slot.events)} events)")
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
                    slot.start_playback(self.clock.next_bar_beat())
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
        elif fn == "knob_pan":
            target = (int(action["param1"]) - 1) & 0x0F
            self.send_synth([0xB0 | target, 10, value])
        elif fn == "knob_filter":
            for channel in SLOT_CHANNELS:
                self.send_synth([0xB0 | channel, 74, value])
            print(f"Master filter cutoff: {value}")
        elif fn == "knob_reverb":
            self._setting("synth.reverb.level", value / 127.0 * 1.0)
        elif fn == "knob_chorus":
            self._setting("synth.chorus.level", value / 127.0)
        elif fn == "knob_master":
            try:
                self.synth.setting("synth.gain", 2.5 * value / 127.0)
            except Exception:
                pass

    def load_loop_file_into(self, slot, path):
        events, length = load_midi_loop(path)
        if not events:
            print(f"Loop {slot.index + 1}: no notes in {path}")
            return False
        slot.load_events(events, length)
        print(f"Slot {slot.index + 1}: loaded {path} "
              f"({len(events)} events, {length // BEATS_PER_BAR} bars)")
        return True

    def load_loop_file(self, slot_index, path):
        return self.load_loop_file_into(self.loops[slot_index], path)

    def close(self):
        self.running = False
        self.clock.stop()
        self.all_sounds_off()
        if self.led_out:
            self.led_out.close_port()
        self.midi_in.close_port()
        time.sleep(0.2)
        self.synth.delete()


def load_preload_manifest(engine):
    path = os.path.join(BASE_DIR, "apcv2_preload.json")
    if not os.path.exists(path):
        return
    with open(path) as f:
        manifest = json.load(f)
    for key, src in manifest.items():
        try:
            slot = int(key) - 1
        except ValueError:
            print(f"Ignoring bad preload slot '{key}'")
            continue
        if not 0 <= slot < 15:
            print(f"Preload slot {key} out of range 1-15")
            continue
        resolved = src if os.path.exists(src) else os.path.join(BASE_DIR, src)
        if os.path.exists(resolved):
            engine.load_loop_file_into(engine.preload_slots[slot], resolved)
        else:
            print(f"Preload file missing: {src}")


def load_loop_manifest(engine):
    if not os.path.exists(LOOPS_FILE):
        return
    with open(LOOPS_FILE) as f:
        manifest = json.load(f)
    for key, path in manifest.items():
        try:
            slot = int(key) - 1
        except ValueError:
            print(f"Ignoring bad loop slot '{key}'")
            continue
        if not 0 <= slot < 5:
            print(f"Loop slot {key} out of range 1-5")
            continue
        resolved = path if os.path.exists(path) else os.path.join(BASE_DIR, path)
        if os.path.exists(resolved):
            engine.load_loop_file(slot, resolved)
        else:
            print(f"Loop file missing: {path}")


def main():
    parser = argparse.ArgumentParser(description="APC Key 25 v2 engine")
    parser.add_argument("--bpm", type=float, help="tempo (overrides settings)")
    parser.add_argument("--load", nargs=2, action="append", metavar=("SLOT", "FILE"),
                        help="load a MIDI file into loop slot 1-5")
    args = parser.parse_args()

    settings = load_settings()
    if args.bpm:
        settings["bpm"] = args.bpm
    banks = load_banks(BANKS_FILE)
    drums = load_drums(DRUMS_FILE)
    cc_actions = load_cc_config(CC_CONFIG_FILE)
    print(f"Loaded {len(banks)} instrument slots, {len(drums)} drum pads, "
          f"{len(cc_actions)} CC assignments, {settings['bpm']} BPM")

    engine = Engine(settings, banks, drums, cc_actions)
    try:
        load_loop_manifest(engine)
        load_preload_manifest(engine)
        for slot, path in (args.load or []):
            engine.load_loop_file(int(slot) - 1, path)
        print("Pads cols 1-4 = instruments, cols 5-7 = drums, col 8 = loops. "
              "Loops start on the bar. Shift+loop pad = clear. Ctrl+C to quit.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        engine.close()


if __name__ == "__main__":
    main()
