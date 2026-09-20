import sys
import time
import rtmidi

NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']


def describe(msg):
    status = msg[0] & 0xF0
    channel = (msg[0] & 0x0F) + 1
    if status in (0x90, 0x80):
        kind = 'Note On ' if (status == 0x90 and (len(msg) < 3 or msg[2] > 0)) else 'Note Off'
        note = msg[1]
        name = f"{NOTE_NAMES[note % 12]}{note // 12 - 1}"
        vel = msg[2] if len(msg) > 2 else 0
        return f"{kind:<9} ch={channel:<2} note={note:<3} ({name:<4}) velocity={vel}"
    if status == 0xB0:
        return f"CC        ch={channel:<2} controller={msg[1]:<3} value={msg[2]}"
    if status == 0xC0:
        return f"ProgChange ch={channel:<2} program={msg[1]}"
    return f"Other     status=0x{msg[0]:02X} data={[hex(b) for b in msg[1:]]}"


def main():
    midi_in = rtmidi.MidiIn()
    ports = midi_in.get_ports()
    if not ports:
        print("No MIDI input ports found. Is the APC Key 25 plugged in?")
        sys.exit(1)

    print("Available input ports:")
    for i, p in enumerate(ports):
        print(f"  [{i}] {p}")

    idx = None
    for i, p in enumerate(ports):
        if 'APC' in p.upper():
            idx = i
            break
    if idx is None:
        idx = 0
    print(f"\nListening on: [{idx}] {ports[idx]}")
    print("Press every pad, key, button, and turn every knob once.")
    print("Ctrl+C to stop.\n")

    midi_in.open_port(idx)
    midi_in.ignore_types(False, False, False)
    start = time.time()
    try:
        while True:
            msg = midi_in.get_message()
            if msg:
                data, delta = msg
                t = time.time() - start
                print(f"[{t:7.2f}s] {describe(data)}")
            time.sleep(0.005)
    except KeyboardInterrupt:
        print("\nDone.")
    finally:
        midi_in.close_port()


if __name__ == '__main__':
    main()