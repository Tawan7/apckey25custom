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
PRELOAD_FILE = os.path.join(BASE_DIR, "apcv2_preload.json")

# APC Key 25 MK1 input channels (verified by midi_monitor)
KEYS_INPUT_CHANNEL = 0    # keyboard keys
PADS_CHANNEL = 1         # pads, buttons, knobs
SUSTAIN_CHANNEL = 2       # sustain pedal

# Synth channel assignment
DRUM_CHANNEL = 9
METRONOME_CHANNEL = 14
LIVE_CHANNEL = 15
LOOP_POOL = [1, 2, 3, 4, 5, 6, 7, 8, 11, 12, 13]

BEATS_PER_BAR = 4

# Pad notes (MK1, channel 1)
SLOT_NOTE_ORDER = [32 - r * 8 + c for r in range(5) for c in range(4)]   # cols 1-4
DRUM_NOTE_ORDER = SLOT_NOTE_ORDER[:15]
PRELOAD_NOTE_ORDER = [36 - r * 8 for r in range(5)]                      # col 5
RECORD_NOTE_ORDER = [c - r * 8 for r in range(5) for c in (37, 38, 39)]  # cols 6-8

BANK_COLOR = [25, 45, 61]
DRUM_COLOR = 9
PRELOAD_COLOR = 46
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
    "preload_program": 38,
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
                int(row["program"]), row["name"])
    return slots


def load_drums(path):
    drums = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            drums.append((int(row["drum_note"]), row["name"]))
    return drums


def load_cc_config(path):
    actions = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            if row["type"] != "CC":
                continue
            actions[(int(row["channel"]), int(row["number"]))] = {
                "function": row["function"],
                "param1": row["param1"],
            }
    return actions


def resolve_soundfont(settings):
    for path in [settings["soundfont"]]:
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


class ChannelPool:
    def __init__(self):
        self.free = list(LOOP_POOL)
        self.lock = threading.Lock()

    def acquire(self):
        with self.lock:
            return self.free.pop(0) if self.free else None

    def release(self, channel):
        if channel is not None:
            with self.lock:
                if channel not in self.free:
                    self.free.append(channel)


class LoopSlot:
    """A loop with its own synth channel, program and bar-quantized lifecycle."""

    def __init__(self, index):
        self.index = index
        self.state = "empty"
        self.events = []
        self.length_beats = BEATS_PER_BAR
        self.start_beat = 0.0
        self.fired_index = 0
        self.channel = None
        self.program = None

    def arm_record(self, start_beat):
        self.state = "armed"
        self.start_beat = start_beat

    def begin_record(self):
        self.state = "recording"
        self.events = []

    def finish_record(self):
        if self.events:
            self.state = "stopped"
            return True
        self.state = "empty"
        self.events = []
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
        self.program = None

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
        self.live_program = None
        self.clock = Clock(settings["bpm"])
        self.pool = ChannelPool()
        self.preload_slots = [LoopSlot(i) for i in range(5)]
        self.record_slots = [LoopSlot(i) for i in range(15)]
        self.note_to_preload = {n: i for i, n in enumerate(PRELOAD_NOTE_ORDER)}
        self.note_to_record = {n: i for i, n in enumerate(RECORD_NOTE_ORDER)}
        self.note_to_bank_slot = {n: i for i, n in enumerate(SLOT_NOTE_ORDER)}
        self.note_to_drum = {n: i for i, n in enumerate(DRUM_NOTE_ORDER)}
        self.metronome = False
        self._click_beat = None
        self.running = True
        self.scheduler = threading.Thread(target=self._schedule, daemon=True)
        self._open_input()
        self._open_led_output()
        self._init_synth()
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
        for channel in LOOP_POOL + [DRUM_CHANNEL, METRONOME_CHANNEL, LIVE_CHANNEL]:
            self.synth.cc(channel, 7, 100)
        self.send_synth([0xC0 | METRONOME_CHANNEL, 0])
        program, name = self.banks[(0, SLOT_NOTE_ORDER[0])]
        self.send_synth([0xC0 | LIVE_CHANNEL, program])
        self.live_program = program
        print(f"Ready: keys play '{name}' (bank A, live ch{LIVE_CHANNEL + 1})")

    def _setting(self, name, value):
        setter = getattr(self.synth, "setting", None)
        if setter is None:
            return
        try:
            setter(name, value)
        except Exception as exc:
            print(f"Cannot set {name}={value}: {exc}")

    def send_synth(self, message):
        status = message[0] & 0xF0
        channel = message[0] & 0x0F
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
        for note in self.note_to_bank_slot:
            self.set_led(note, color)
        for note in self.note_to_preload:
            self.set_led(note, PRELOAD_COLOR)
        for note, index in self.note_to_record.items():
            slot = self.record_slots[index]
            self.set_led(note, self._loop_color(slot))

    @staticmethod
    def _loop_color(slot):
        if slot.state in ("recording", "armed"):
            return COLOR_RECORDING
        if slot.state == "playing":
            return COLOR_PLAYING
        if slot.state == "stopped":
            return COLOR_STOPPED
        return COLOR_OFF

    def all_sounds_off(self):
        for channel in LOOP_POOL + [DRUM_CHANNEL, METRONOME_CHANNEL, LIVE_CHANNEL]:
            self.synth.cc(channel, 123, 0)

    def _schedule(self):
        while self.running:
            now_beat = self.clock.now()
            if self.metronome:
                current = int(now_beat)
                if current != self._click_beat:
                    self._click_beat = current
                    note = (76 if current % BEATS_PER_BAR == 0 else 77)
                    self.send_synth([0x90 | METRONOME_CHANNEL, note, 100])
                    self.send_synth([0x80 | METRONOME_CHANNEL, note, 0])
            for slot in self.preload_slots + self.record_slots:
                if slot.state != "playing":
                    continue
                elapsed = now_beat - slot.start_beat
                if elapsed < 0:
                    continue
                position = elapsed % slot.length_beats
                target = now_beat + 0.02 * self.clock.bpm / 60.0
                while slot.fired_index < len(slot.events):
                    at, message = slot.events[slot.fired_index]
                    if at > target:
                        break
                    fire_time = self.clock.beat_to_time(slot.start_beat + at)
                    delay = fire_time - time.monotonic()
                    if delay <= 0.02:
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
        if channel != PADS_CHANNEL:
            return
        if self.shift_held and note in self.note_to_drum:
            if is_on:
                drum_note, name = self.drums[self.note_to_drum[note]]
                self.send_synth([0x90 | DRUM_CHANNEL, drum_note, velocity])
                print(f"Drum: {name}")
            return
        if note in self.note_to_bank_slot:
            if is_on:
                self._select_instrument(note)
        elif note in self.note_to_preload:
            if is_on:
                self._handle_preload_press(note)
        elif note in self.note_to_record:
            if is_on:
                self._handle_record_press(note)
        elif note in (64, 65) and is_on:
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
        out_status = (0x90 if is_on else 0x80) | LIVE_CHANNEL
        self.send_synth([out_status, note, velocity])
        now_beat = self.clock.now()
        for slot in self.record_slots:
            if slot.state == "recording":
                at = round((now_beat - slot.start_beat) * 16) / 16.0
                slot.events.append((at % slot.length_beats, [out_status, note, velocity]))

    def _select_instrument(self, note):
        program, name = self.banks[(self.bank, note)]
        self.send_synth([0xC0 | LIVE_CHANNEL, program])
        self.live_program = program
        print(f"Bank {'ABC'[self.bank]}: keys play '{name}'")

    def _switch_bank(self, direction):
        self.bank = (self.bank + direction) % 3
        program, name = self.banks[(self.bank, SLOT_NOTE_ORDER[0])]
        self.send_synth([0xC0 | LIVE_CHANNEL, program])
        self.live_program = program
        print(f"Bank {'ABC'[self.bank]} ({name})")
        self.refresh_leds()

    def _handle_preload_press(self, note):
        slot = self.preload_slots[self.note_to_preload[note]]
        if slot.state == "empty":
            print(f"Preload {slot.index + 1}: empty (set apcv2_preload.json)")
            return
        if slot.state == "playing":
            slot.stop_playback()
            print(f"Preload {slot.index + 1}: stopped")
        else:
            slot.start_playback(self.clock.next_bar_beat())
            print(f"Preload {slot.index + 1}: starts on next bar")

    def _handle_record_press(self, note):
        slot = self.record_slots[self.note_to_record[note]]
        if self.shift_held:
            self._release_slot_channel(slot)
            slot.clear()
            print(f"Loop {slot.index + 1}: cleared")
        elif slot.state == "empty":
            start = self.clock.next_bar_beat()
            slot.arm_record(start)
            slot.length_beats = BEATS_PER_BAR
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
        print(f"Loop {slot.index + 1}: RECORDING")
        threading.Timer(
            slot.length_beats * 60.0 / self.clock.bpm,
            self._finish_recording, args=[slot]).start()
        self.refresh_leds()

    def _finish_recording(self, slot):
        if slot.state not in ("recording", "armed"):
            return
        if slot.finish_record():
            self._bake_slot(slot, self.live_program)
            slot.start_playback(self.clock.next_bar_beat())
            print(f"Loop {slot.index + 1}: plays from next bar "
                  f"({len(slot.events)} events, ch{slot.channel + 1})")
        else:
            print(f"Loop {slot.index + 1}: nothing recorded")
        self.refresh_leds()

    def _bake_slot(self, slot, program):
        """Give the loop its own channel + program so live playing can
        never change the sound of a recorded loop."""
        if slot.channel is None:
            slot.channel = self.pool.acquire()
        if slot.channel is None:
            slot.channel = LIVE_CHANNEL
            return
        slot.program = program
        for _, message in slot.events:
            if (message[0] & 0x0F) != DRUM_CHANNEL:
                message[0] = (message[0] & 0xF0) | slot.channel
        self.send_synth([0xC0 | slot.channel, program])

    def _release_slot_channel(self, slot):
        if slot.channel is not None and slot.channel != LIVE_CHANNEL:
            self.synth.cc(slot.channel, 123, 0)
            self.pool.release(slot.channel)
        slot.channel = None
        slot.program = None

    def _toggle_transport(self):
        if any(s.state == "playing" for s in self.record_slots):
            for slot in self.record_slots:
                if slot.state == "playing":
                    slot.stop_playback()
            print("Transport: STOP")
        else:
            started = False
            for slot in self.record_slots:
                if slot.state == "stopped":
                    slot.start_playback(self.clock.next_bar_beat())
                    started = True
            print("Transport: PLAY" if started else "Transport: no loops to play")

    def _stop_all(self):
        for slot in self.record_slots + self.preload_slots:
            if slot.state == "playing":
                slot.stop_playback()
        print("Stop all: every loop stopped")
        self.refresh_leds()

    def _toggle_metronome(self):
        self.metronome = not self.metronome
        print(f"Metronome: {'ON' if self.metronome else 'OFF'}")

    def _handle_cc(self, channel, controller, value):
        action = self.cc_actions.get((channel, controller))
        if action is None:
            return
        fn = action["function"]
        if fn == "sustain":
            self.send_synth([0xB0 | LIVE_CHANNEL, 64, value])
        elif fn == "knob_volume":
            target = (int(action["param1"]) - 1) & 0x0F
            self.send_synth([0xB0 | target, 7, value])
        elif fn == "knob_pan":
            target = (int(action["param1"]) - 1) & 0x0F
            self.send_synth([0xB0 | target, 10, value])
        elif fn == "knob_filter":
            for chan in LOOP_POOL + [DRUM_CHANNEL, LIVE_CHANNEL]:
                self.send_synth([0xB0 | chan, 74, value])
        elif fn == "knob_reverb":
            self._setting("synth.reverb.level", value / 127.0)
        elif fn == "knob_chorus":
            self._setting("synth.chorus.level", value / 127.0)
        elif fn == "knob_master":
            self._setting("synth.gain", 2.5 * value / 127.0)
        elif fn == "knob_tempo":
            bpm = 60 + value / 127.0 * 120
            self.clock.set_bpm(bpm)
            print(f"Tempo: {bpm:.0f} BPM")

    def load_loop_file(self, slot, path, program):
        events, length = load_midi_loop(path)
        if not events:
            print(f"Slot {slot.index + 1}: no notes in {path}")
            return False
        self._release_slot_channel(slot)
        slot.channel = self.pool.acquire()
        baked = []
        for at, message in events:
            if (message[0] & 0x0F) != DRUM_CHANNEL:
                message[0] = (message[0] & 0xF0) | (slot.channel
                                                   if slot.channel is not None
                                                   else LIVE_CHANNEL)
            baked.append((at, message))
        slot.load_events(baked, length)
        slot.program = program
        if slot.channel is not None:
            self.send_synth([0xC0 | slot.channel, program])
        print(f"Slot {slot.index + 1}: loaded {path} "
              f"({len(baked)} events, {length // BEATS_PER_BAR} bars, "
              f"ch{slot.channel + 1 if slot.channel is not None else LIVE_CHANNEL + 1})")
        return True

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
    if not os.path.exists(PRELOAD_FILE):
        return
    with open(PRELOAD_FILE) as f:
        manifest = json.load(f)
    for key, entry in manifest.items():
        try:
            slot_index = int(key) - 1
        except ValueError:
            print(f"Ignoring bad preload slot '{key}'")
            continue
        if not 0 <= slot_index < 5:
            print(f"Preload slot {key} out of range 1-5")
            continue
        if isinstance(entry, dict):
            path, program = entry.get("file"), entry.get(
                "program", engine.settings["preload_program"])
        else:
            path, program = entry, engine.settings["preload_program"]
        resolved = path if os.path.exists(path) else os.path.join(BASE_DIR, path)
        if os.path.exists(resolved):
            engine.load_loop_file(engine.preload_slots[slot_index], resolved, program)
        else:
            print(f"Preload file missing: {path}")


def main():
    parser = argparse.ArgumentParser(description="APC Key 25 v2 engine (MK1)")
    parser.add_argument("--bpm", type=float, help="tempo (overrides settings)")
    parser.add_argument("--load", nargs=2, action="append",
                        metavar=("SLOT", "FILE"),
                        help="load a MIDI file into record loop slot 1-15")
    args = parser.parse_args()

    settings = load_settings()
    if args.bpm:
        settings["bpm"] = args.bpm
    banks = load_banks(BANKS_FILE)
    drums = load_drums(DRUMS_FILE)
    cc_actions = load_cc_config(CC_CONFIG_FILE)
    print(f"Loaded {len(banks)} instrument slots, {len(drums)} drums, "
          f"{len(cc_actions)} CC assignments, {settings['bpm']} BPM")

    engine = Engine(settings, banks, drums, cc_actions)
    try:
        load_preload_manifest(engine)
        for slot, path in (args.load or []):
            engine.load_loop_file(engine.record_slots[int(slot) - 1], path,
                                   settings["preload_program"])
        print("Cols 1-4 = instruments (Shift = drums), col 5 = preloads, "
              "cols 6-8 = loop record/play. Shift+loop pad = clear. Ctrl+C to quit.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        engine.close()


if __name__ == "__main__":
    main()
