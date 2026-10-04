# APC Key 25 Custom

Custom MIDI engine for the AKAI APC Key 25, built for live techno without Ableton.
Keys, pads and knobs drive FluidSynth/Qsynth directly (multi-timbral, 16 channels).

## Layout

```
        C1   C2   C3   C4   C5   C6   C7      C8
Row 1   32   33   34   35 | 36   37 | 38    | 39   <- pattern/rec 1
Row 2   24   25   26   27 | 28   29 | 30    | 31
Row 3   16   17   18   19 | 20   21 | 22    | 23
Row 4    8    9   10   11 | 12   13 | 14    | 15
Row 5    0    1    2    3 |  4    5 |  6    |  7
        └─ INSTRUMENTS ─┘└ DRUMS ┘└PATTERNS┘└REC SLOTS┘
```

Pages (Arrow Down/Up): **A = BASSES** (green), **B = LEADS** (amber),
**C = PADS/FX** (red). Each page uses its own set of synth channels, so a
bass from page A and a lead from page B layer together without conflict.

- **Instrument pads (cols 1-4):** 20 slots. Each slot has its own synth channel,
  so pressing an instrument pad only routes your keyboard to that instrument —
  nothing already playing changes. The **currently selected slot lights brighter**
  in the bank color.
- **3 banks (pages):** Arrow Down = next bank, Arrow Up = previous. Each bank lights the
  instrument pads in its own LED color (A/B/C), so you always know which page you're on.
- **Octave:** the APC's Octave Down/Up buttons (under the keys) shift your playing
  range -3..+3 octaves. Look at the console for the current value. Note: this uses
  CC 58/59, which the original APC Key 25 sends for octave buttons — verify with
  `midi_monitor.py` and adjust OCTAVE_CC_DOWN/OCTAVE_CC_UP if needed.
- **Drum pads (cols 5-6):** 10 GM percussion sounds on MIDI ch10.
- **Pattern pads (col 7):** 5 built-in techno patterns (four-on-the-floor,
  kick+clap, hypnotic hats, snare roll, sparse atmo). Press to toggle
  on/off. They follow LOOP_LENGTH (set 240/BPM for your tempo).
- **Record pads (col 8):** 5 recordable loop slots, one per row.
  - Press = start recording (fixed 4-beat window at your tempo; press again to stop early)
  - Your **keyboard notes AND drum pad hits** are both captured into the loop
  - Automatically starts playing when recording finishes
  - Press again = stop / resume
  - **Shift + pad = clear** the loop
  - LEDs: red = recording, green = playing, dim = recorded but stopped, off = empty
- **Play/Pause button:** stops all playing loops / resumes all stopped loops.
- **Knobs 1-4:** volume/pan for keys and drums channels. **Sustain pedal** works.

## Files

- `apc_engine.py` — the engine (run this)
- `apc_banks.csv` — 3 banks x 20 instrument slots (bank, pad note, channel, GM program, name)
- `apc_drums.csv` — pad note -> GM drum note mapping
- `apc_config.csv` — knobs and sustain assignments
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
BANK_COLOR = [1, 5, 3]      # banks: green, amber, red (APC Key 25 mk1 tri-color pads)
COLOR_RECORDING = 4  # red blink
COLOR_PLAYING = 1    # green
COLOR_STOPPED = 5    # amber
```

## Tuning the sounds

Edit `apc_banks.csv`: each row is `bank, pad note, synth channel, GM program, name`.
Give every slot its own channel so instruments layer instead of replacing
each other. Channels 10 (index 9) is reserved for drums.

Loop length is `LOOP_LENGTH` seconds at the top of `apc_engine.py` (default 4.0,
one bar at ~120 BPM; set it to match your tempo: 240 / BPM).
