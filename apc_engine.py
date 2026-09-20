
import csv
import os
import subprocess
import sys
import time

import rtmidi

KEYS_CHANNEL = 0
DRUMS_CHANNEL = 9
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apc_config.csv")
SOUNDFONT_CANDIDATES = [
    "/usr/share/sounds/sf2/FluidR3_GM.sf2",
    "/usr/share/sounds/sf2/FluidR3_GM.sf2.fluid",
    "/usr/share/soundfonts/FluidR3_GM.sf2",
    "/usr/share/soundfonts/default-GM.sf2",
]


def load_config(path):
    actions = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            key = (row["type"], int(row["channel"]), int(row["number"]))
            actions[key] = {
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


class Engine:
    def __init__(self, config):
        self.config = config
        self.shift_held = False
        self.playing = False
        self.current_instrument = "Acoustic Grand Piano"
        self.midi_out = rtmidi.MidiOut()
        self.midi_out.set_client_name("APC Key 25 Engine")
        self._synth_process = None
        self._open_output()
        self._send_program(0)
        self._send_cc(KEYS_CHANNEL, 7, 100)
        self._send_cc(DRUMS_CHANNEL, 7, 100)

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

    def _send(self, message):
        self.midi_out.send_message(message)

    def _send_cc(self, channel, controller, value):
        self._send([0xB0 | channel, controller, value])

    def _send_program(self, program, channel=KEYS_CHANNEL):
        self._send_cc(channel, 123, 0)
        self._send([0xC0 | channel, program])

    def all_sounds_off(self):
        self._send_cc(KEYS_CHANNEL, 123, 0)
        self._send_cc(DRUMS_CHANNEL, 123, 0)

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
        elif status == 0xC0:
            self._send(message)

    def _handle_note(self, channel, note, is_on, velocity):
        action = self.config.get(("Note", channel, note))
        if channel == 1:
            out_status = (0x90 if is_on else 0x80) | KEYS_CHANNEL
            self._send([out_status, note, velocity])
            return
        if action is None:
            return
        fn = action["function"]
        if fn == "instrument" and is_on:
            self.current_instrument = action["param2"]
            self._send_program(int(action["param1"]))
            print(f"Instrument: {action['param2']}")
        elif fn == "transport_play" and is_on:
            self.playing = not self.playing
            if self.playing:
                print("Transport: PLAY")
            else:
                self.all_sounds_off()
                print("Transport: STOP")
        elif fn == "transport_rec" and is_on:
            print("REC: live looper arrives in v2.2")
        elif fn == "shift":
            self.shift_held = is_on
            print("Shift: held" if is_on else "Shift: released")
        elif fn == "reserved" and is_on:
            print(f"{action['control']}: reserved")
        elif fn in ("loop_pre", "loop_rec") and is_on:
            kind = "pre-made loop" if fn == "loop_pre" else "recordable loop slot"
            print(f"Slot {action['param1']} ({kind}): arrives in v2.1/v2.2")

    def _handle_cc(self, channel, controller, value):
        action = self.config.get(("CC", channel, controller))
        if action is None:
            return
        fn = action["function"]
        if fn == "sustain":
            self._send_cc(KEYS_CHANNEL, 64, value)
        elif fn == "knob_volume":
            channel_0based = (int(action["param1"]) - 1) & 0x0F
            self._send_cc(channel_0based, 7, value)
            print(f"Volume ch{action['param1']}: {value}")
        elif fn == "knob_pan":
            channel_0based = (int(action["param1"]) - 1) & 0x0F
            self._send_cc(channel_0based, 10, value)
            print(f"Pan ch{action['param1']}: {value}")

    def close(self):
        try:
            self.all_sounds_off()
            self.midi_out.close_port()
        finally:
            if self._synth_process:
                self._synth_process.terminate()
                self._synth_process.wait()


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else CONFIG_FILE
    config = load_config(config_path)
    print(f"Loaded {len(config)} control assignments from {config_path}")

    midi_in = rtmidi.MidiIn()
    idx = find_port(midi_in.get_ports(), "APC Key 25")
    if idx is None:
        print("APC Key 25 not found. Plug it in and retry.")
        sys.exit(1)
    port_name = midi_in.get_ports()[idx]
    midi_in.open_port(idx)
    print(f"Input: {port_name}")

    engine = Engine(config)
    midi_in.set_callback(engine.handle_midi)

    current = "Acoustic Grand Piano"
    print(f"Ready. Keys play {current} on channel {KEYS_CHANNEL + 1}.")
    print("Pads cols 1-4 select instruments. Ctrl+C to quit.")

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
