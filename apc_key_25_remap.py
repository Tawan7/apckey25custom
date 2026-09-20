import rtmidi
import time
import csv
from collections import defaultdict

# Load the MIDI mapping from CSV
def load_midi_mapping(csv_file):
    note_map = defaultdict(dict)
    try:
        with open(csv_file, mode='r') as file:
            reader = csv.DictReader(file)
            for row in reader:
                if row['type'] == 'Note':
                    midi_note = int(row['midi_note'])
                    note_map[midi_note]['remap_to_note'] = int(row['remap_to_note'])
                    note_map[midi_note]['remap_to_channel'] = int(row['remap_to_channel'])
        print(f"Loaded {len(note_map)} MIDI note mappings from {csv_file}")
    except Exception as e:
        print(f"Error loading {csv_file}: {e}")
    return note_map

# Initialize MIDI in/out
midi_in = rtmidi.MidiIn()
midi_out = rtmidi.MidiOut()

# Set client names for debugging
midi_in.set_client_name("APC Key 25 Remapper In")
midi_out.set_client_name("APC Key 25 Remapper Out")

# Open ports (adjust indices based on `aconnect -l`)
try:
    midi_in.open_virtual_port("APC Key 25 Remapper In")
    midi_out.open_virtual_port("APC Key 25 Remapper Out")
    print("Virtual MIDI ports created.")
except Exception as e:
    print(f"Error creating virtual MIDI ports: {e}")

# Load the MIDI mapping
note_map = load_midi_mapping('/home/piakai/apc_key_25_mapping.csv')

print("APC Key 25 MIDI Remapper Running. Press Ctrl+C to stop.")

try:
    while True:
        msg = midi_in.get_message()
        if msg:
            message, _ = msg
            # Handle Note On/Off
            if message[0] in [0x90, 0x80]:  # Note On/Off
                status = message[0]
                channel = status & 0x0F
                note = message[1]
                velocity = message[2]

                if note in note_map:
                    remap = note_map[note]
                    new_note = remap['remap_to_note']
                    new_channel = remap['remap_to_channel']
                    new_status = (status & 0xF0) | new_channel  # Preserve On/Off, change channel
                    new_msg = [new_status, new_note, velocity]
                    midi_out.send_message(new_msg)
                    print(f"Remapped: Ch {channel}, Note {note} -> Ch {new_channel}, Note {new_note}")
                else:
                    midi_out.send_message(message)
                    print(f"Forwarded: Ch {channel}, Note {note}, Velocity {velocity}")
            # Handle Control Change (CC) - Optional for later
            elif message[0] == 0xB0:
                midi_out.send_message(message)
                print(f"Forwarded CC: Ch {message[0] & 0x0F}, CC {message[1]}, Value {message[2]}")
        time.sleep(0.01)
except KeyboardInterrupt:
    print("Stopping MIDI Remapper.")
finally:
    midi_in.close_port()
    midi_out.close_port()
