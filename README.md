# APC Key 25 Custom

Custom MIDI engine for the AKAI APC Key 25, built for live techno without Ableton.
Keys, pads and knobs drive FluidSynth/Qsynth directly (multi-timbral, 16 channels).

## Layout

```
        C1   C2   C3   C4   C5   C6   C7   C8
Row 1   32   33   34   35   36   37   38   39
Row 2   24   25   26   27   28   29   30   31
Row 3   16   17   18   19   20   21   22   23
Row 4    8    9   10   11   12   13   14   15
Row 5    0    1    2    3    4    5    6    7
        └INSTRUMENTS┘ └DRUM┘ └LOOPS┘ └PRESETS┘
```

- **Instrument pads (cols 1-4):** 20 slots, 3 banks (Arrow Down/Up switches bank).
  Press = route your keyboard to that instrument on its own synth channel;
  nothing already playing changes. Each bank has its own LED color.
- **Drum pads (col 5):** 5 core techno drums (Kick, Snare, Closed Hat, Clap,
  Open Hat) on MIDI ch10, velocity-sensitive.
- **Loop pads (cols 6-7):** 10 recordable loop slots.
  - Press = start recording your keyboard (fixed 4-beat window; press again to stop early)
  - Automatically starts playing when recording finishes
  - Press again = stop / resume
  - **Shift + pad = clear** the loop (record a new one right after)
  - LEDs: red = recording, green = playing, dim = recorded but stopped, off = empty
- **Preset pads (col 8):** 5 preset pads — pre-made techno drum patterns
  (Four on Floor, Hypnotic, Offbeat, Rolling, Break) and/or your own saved
  melodies. Press = start, press again = stop. Green LED = playing.
- **Rec button:** saves all recorded loops to `apc_loops.csv`; they reload
  automatically at startup (as stopped loops — press their pad to play).
  Record more and press Rec again to update the file.
- **Shift layer (hold Shift + pad):** the instrument and drum pads (cols 1-5)
  play a 2-octave minor scale on the selected instrument — a 25-pad "keyboard"
  for basslines. Loop and preset pads keep working normally while Shift is held.
- **Play/Pause button:** stops all playing loops and presets / resumes loops.
- **Knobs:**
  1. Keys volume
  2. Drums volume
  3. Keys pan
  4. Drums pan
  5. Reverb amount
  6. Main volume (master, combines with per-channel volumes)
- **Sustain pedal** works.

## Files

- `apc_engine.py` — the engine (run this)
- `apc_banks.csv` — 3 banks x 20 instrument slots (bank, pad note, channel, GM program, name)
- `apc_drums.csv` — 5 drum pads (pad note -> GM drum note + name)
- `apc_presets.csv` — preset patterns (drums and melodies):
  `preset, name, offset, kind(drum|note), channel, note, velocity`.
  Drum offsets are 0.0-1.0 of the bar; note offsets are seconds.
- `apc_loops.csv` — your saved loops (written by the Rec button, auto-loaded)
- `loop_to_preset.py` — turns a saved loop into a preset pad
- `apc_config.csv` — pad, button, knob and sustain assignments
- `midi_monitor.py` — shows raw MIDI from the controller
- `led_test.py` — cycles LED palette colors so you can pick bank colors
- `apc_key_25_remap.py` — legacy standalone remapper (not used by the engine)

## Run

```bash
python apc_engine.py
```

Starts headless FluidSynth with a soundfont if Qsynth isn't already running.

## Tuning the LED colors

The pad LED color is set by the velocity byte of a Note On sent to the APC's
output port (fixed 128-color palette). Run `python led_test.py`, watch the
steps, and note the velocity of a color you like. Then edit the constants at
the top of `apc_engine.py`:

```python
BANK_COLOR = [25, 45, 61]   # banks A, B, C
DRUM_COLOR = 9
COLOR_RECORDING = 3
COLOR_PLAYING = 20
COLOR_STOPPED = 5
COLOR_PRESET = 57
```

## Tuning the sounds

- **Instruments:** edit `apc_banks.csv` — each row is
  `bank, pad note, synth channel, GM program, name`. Give every slot its own
  channel so instruments layer instead of replacing each other. Channel 10
  (index 9) is reserved for drums.
- **Drums:** edit `apc_drums.csv` — map the 5 col-5 pads to any GM percussion note.
- **Presets:** edit `apc_presets.csv` — each row is one hit:
  `preset, name, offset, drum note, velocity`. Offset is 0.0-1.0 within the
  loop (0.25 = beat 2). Add or remove rows freely.

Loop length is `LOOP_LENGTH` seconds at the top of `apc_engine.py` (default 4.0,
one bar at ~120 BPM; set it to match your tempo: 240 / BPM).

## Saving and reusing your creations

1. Record loops as usual (cols 6-7 pads).
2. Press **Rec** — all loops are saved to `apc_loops.csv` and reload next
   time you start the engine.
3. To turn a saved loop into a **preset pad** (col 8), stop the engine and run:

```bash
python loop_to_preset.py <loop slot 1-10> <preset pad 1-5> "Name"
# example: loop slot 3 -> preset pad 2
python loop_to_preset.py 3 2 "My Bassline"
```

The melody keeps its original notes, instruments (channels) and timing.
Restart the engine and the preset pad plays your melody, looping.
