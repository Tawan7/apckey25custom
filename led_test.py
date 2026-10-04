import sys
import time

import rtmidi

PALETTE_STEPS = [0, 1, 2, 3, 4, 5, 6, 7, 13, 25, 45, 61, 127]
PADS = [32, 33, 34, 35, 36, 37, 38, 39, 24, 25, 26, 27, 28, 29, 30, 31]


def main():
    out = rtmidi.MidiOut()
    idx = None
    for i, name in enumerate(out.get_ports()):
        if "apc key 25" in name.lower():
            idx = i
            break
    if idx is None:
        print("APC Key 25 output port not found.")
        sys.exit(1)
    out.open_port(idx)
    print(f"LED test on: {out.get_ports()[idx]}")
    print("Each step lights 16 pads in one palette color for 3 seconds.")
    print("Note the velocity value of the color you want (yellow/blue/magenta),")
    print("then update BANK_COLOR in apc_engine.py. Ctrl+C to stop.\n")
    try:
        for velocity in PALETTE_STEPS:
            for note in PADS:
                out.send_message([0x90, note, velocity])
            print(f"velocity {velocity:3d} -> look at the pads")
            time.sleep(3)
            for note in PADS:
                out.send_message([0x90, note, 0])
    except KeyboardInterrupt:
        for note in PADS:
            out.send_message([0x90, note, 0])
        print("\nDone.")
    finally:
        out.close_port()


if __name__ == "__main__":
    main()
