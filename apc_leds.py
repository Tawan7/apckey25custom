import threading


class LEDManager:
    """Single source of truth for pad LED colors.

    State philosophy:
      - dim category color = pad exists but idle
      - bright version     = selected / active
      - green              = something is currently playing
      - red                = recording
      - off                = empty
    A pad can flash (e.g. drum hit) without losing its base color.
    """

    BANK_COLORS = [25, 45, 61]
    BANK_SELECTED_COLORS = [33, 53, 69]
    COLOR_DRUM = 1
    COLOR_DRUM_HIT = 20
    COLOR_RECORDING = 3
    COLOR_PLAYING = 20
    COLOR_STOPPED = 5
    COLOR_PRESET = 57
    COLOR_OFF = 0

    def __init__(self, led_out):
        self.led_out = led_out
        self.base = {}
        self.flash_timers = {}
        self.lock = threading.Lock()

    def _send(self, note, color):
        if self.led_out is None:
            return
        try:
            self.led_out.send_message([0x90, note, color])
        except Exception:
            pass

    def set_base(self, note, color):
        with self.lock:
            if self.base.get(note) == color:
                return
            self.base[note] = color
            flashing = self.flash_timers.get(note) is not None
        if not flashing:
            self._send(note, color)

    def flash(self, note, color, duration=0.12):
        if self.led_out is None:
            return
        with self.lock:
            old = self.flash_timers.get(note)
            if old is not None:
                old.cancel()
            self._send(note, color)
            timer = threading.Timer(duration, self._end_flash, args=(note,))
            self.flash_timers[note] = timer
        timer.start()

    def _end_flash(self, note):
        with self.lock:
            self.flash_timers[note] = None
            color = self.base.get(note, 0)
        self._send(note, color)

    def all_off(self, notes):
        for note in notes:
            self.set_base(note, self.COLOR_OFF)
