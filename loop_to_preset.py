"""Convert a saved loop (apc_loops.csv) into a melodic preset (apc_presets.csv).

Usage:
  python loop_to_preset.py <slot> <preset_number> [name]

Example: turn loop slot 3 into preset 6, named "My Bassline":
  python loop_to_preset.py 3 6 "My Bassline"

Run while the engine is stopped. Presets become available on the
preset pads (col 8) the next time you start apc_engine.py, or immediately
after restarting it.
"""

import csv
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOOPS_FILE = os.path.join(BASE_DIR, "apc_loops.csv")
PRESETS_FILE = os.path.join(BASE_DIR, "apc_presets.csv")


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    slot = int(sys.argv[1])
    preset = int(sys.argv[2])
    name = sys.argv[3] if len(sys.argv) > 3 else f"Loop {slot}"

    if not 1 <= preset <= 5:
        print("Preset number must be 1-5 (there are 5 preset pads, col 8).")
        print("Pick an existing preset to overwrite it, or edit apc_presets.csv.")
        sys.exit(1)

    if not os.path.exists(LOOPS_FILE):
        print("No apc_loops.csv found. Record loops and press Rec to save first.")
        sys.exit(1)

    events = []
    length = None
    with open(LOOPS_FILE, newline="") as f:
        for row in csv.DictReader(f):
            if int(row["slot"]) != slot:
                continue
            length = float(row["length"])
            message = [int(b) for b in row["message"].split()]
            status = message[0] & 0xF0
            if status not in (0x90, 0x80):
                continue
            channel = message[0] & 0x0F
            velocity = message[2] if status == 0x90 else 0
            events.append((float(row["offset"]), "note", channel,
                           message[1], velocity))
    if not events:
        print(f"Loop slot {slot} is empty or not saved.")
        sys.exit(1)

    with open(PRESETS_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        for offset, kind, channel, note, velocity in events:
            writer.writerow([preset, name, f"{offset:.3f}", kind,
                             channel, note, velocity])
    print(f"Added loop {slot} as preset {preset} '{name}' "
          f"({len(events)} events, {length:.2f}s loop)")
    print(f"Restart apc_engine.py to load it on preset pad {preset}.")


if __name__ == "__main__":
    main()
