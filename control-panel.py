#!/usr/bin/env python3
import argparse
import mido
import serial
import sys
from enum import Enum, IntEnum

COLUMNS = 16
ROWS = 2

active_screen = None

mdevice = None
idevice = sys.stdin
ddevice = sys.stdout

class AntonijnSysexEvent(IntEnum):
    TRANSPOSE = 0x01
    TEMPERAMENT = 0x02
    TUNING = 0x03
    INSTRUMENT = 0x04
    METRONOME_BPM = 0x05
    METRONOME_MEASURE = 0x06

    GRANDORGUE_READY = 0x100
    GRANDORGUE_GAIN = 0x101
    GRANDORGUE_POLYPHONY = 0x102

    GRANDORGUE_START_RECORDING = 0x141
    GRANDORGUE_STOP_RECORDING = 0x142
    GRANDORGUE_START_METRONOME = 0x143
    GRANDORGUE_STOP_METRONOME = 0x144

    def midi_message(self, value):
        if value < 0:
            value = 0x10000 + value
        s = f'JOHANNUSANTONIJN{self.value:04x}{value:04x}'
        return mido.Message('sysex', data=s.encode())

def format_arrows(text, left=True, right=True):
    text_width = COLUMNS - 2
    if right:
        text_width -= 2

    # Truncate
    text = text[:text_width]

    if right:
        # Only pad when there is a right arrow
        text = f'{text:{text_width}} >'

    return ('< ' if left else '  ') + text

def ddevice_writeln(text):
    data = (text[:COLUMNS] + '\n').encode('utf-8')
    ddevice.write(data)

def wait_for_ready():
    ddevice_writeln('Loading...')
    ddevice_writeln('')

    # Wait for ready event
    ready_msg = AntonijnSysexEvent.GRANDORGUE_READY.midi_message(0)
    while True:
        msg = mdevice.receive()
        if msg.type == 'sysex' and msg.data == ready_msg.data:
            break

    # We ignore any key presses made while showing the loading screen
    if isinstance(idevice, serial.Serial):
        idevice.reset_input_buffer()

def chain_screens(screens):
    for i in range(1, len(screens)):
        screens[i - 1].adjacent[Key.RIGHT] = screens[i]
        screens[i].adjacent[Key.LEFT] = screens[i - 1]

class Key(Enum):
    LEFT = 1
    DOWN = 2
    UP = 3
    RIGHT = 4

class Screen:
    def __init__(self):
        self.adjacent = {}

    def process_key(self, key):
        global active_screen
        new_screen = self.adjacent.get(key, self)
        if active_screen != new_screen:
            active_screen = new_screen
            active_screen.redraw()

    def redraw(self):
        pass

    def reset(self):
        pass

class IntSelect(Screen):
    active = False

    def __init__(self, name, default, minimum, maximum, stride=1, unit_suffix=''):
        super().__init__()
        self.value = default
        self.default = default
        self.name = name
        self.minimum = minimum
        self.maximum = maximum
        self.stride = stride
        self.unit_suffix = unit_suffix
        self.on_update = lambda x: None

    def process_key(self, key):
        if self.active:
            new_value = self.value

            if key == Key.UP:
                self.active = False
                self.redraw()
            elif key == Key.LEFT:
                new_value = max(self.value - self.stride, self.minimum)
            elif key == Key.RIGHT:
                new_value = min(self.value + self.stride, self.maximum)

            if self.value != new_value:
                self.value = new_value
                self.on_update(new_value)
                self.redraw()
        else:
            if key in (Key.LEFT, Key.RIGHT):
                super().process_key(key)
            elif key == Key.DOWN:
                self.active = True
                self.redraw()

    def format_option(self):
        return f'{self.value}{self.unit_suffix}'

    def redraw(self):
        left = not self.active and (Key.LEFT in self.adjacent)
        right = not self.active and (Key.RIGHT in self.adjacent)
        ddevice_writeln(format_arrows(self.name, left, right))

        left = self.active and self.value > self.minimum
        right = self.active and self.value < self.maximum
        ddevice_writeln(format_arrows(self.format_option(), left, right))

    def reset(self):
        self.value = self.default

class EnumSelect(IntSelect):
    def __init__(self, name, options, default=0):
        super().__init__(name, default, 0, len(options) - 1)
        self.options = options

    def format_option(self):
        return self.options[self.value]

class OnOffSelect(Screen):
    active = False

    def __init__(self, name, on_text, off_text, on_msg, off_msg):
        super().__init__()
        self.name = name
        self.on_text = on_text
        self.off_text = off_text
        self.on_msg = on_msg
        self.off_msg = off_msg

    def process_key(self, key):
        if self.active:
            if key == Key.UP:
                self.active = False
                mdevice.send(self.off_msg)
                self.redraw()
        else:
            if key in (Key.LEFT, Key.RIGHT):
                super().process_key(key)
            elif key == Key.DOWN:
                self.active = True
                mdevice.send(self.on_msg)
                self.redraw()

    def redraw(self):
        left = not self.active and (Key.LEFT in self.adjacent)
        right = not self.active and (Key.RIGHT in self.adjacent)
        ddevice_writeln(format_arrows(self.name, left, right))
        ddevice_writeln(self.off_text if self.active else self.on_text)

def read_escape_triplet(timeout):
    idevice.timeout = timeout
    while True:
        bs = [b'', b'', b'']
        full = b''
        pattern = b'\x1b['
        for i in range(len(bs)):
            s = idevice.read()
            if s == b'':
                # Timeout reached
                return b''
            full += s
            if not pattern.startswith(full[:len(pattern)]):
                break
        else:
            return full

reset_on_reload = []

instruments = [
    'Modern Organ',
    'Positif',
]
instrument = EnumSelect('Instrument', instruments)
def on_instrument_update(value):
    mdevice.send(AntonijnSysexEvent.INSTRUMENT.midi_message(value))
    wait_for_ready()
    for screen in reset_on_reload:
        screen.reset()
instrument.on_update = on_instrument_update

temperaments = [
    'Original',
    'Equal',
    '1/4 Meantone',
    '1/5 Meantone',
    '1/6 Meantone',
    '2/7 Meantone',
    'Werckmeister',
    'Pythagorean',
    'Pyth. (B-F#)',
]
temperament = EnumSelect('Temperament', temperaments, default=1)
temperament.on_update = lambda value: mdevice.send(AntonijnSysexEvent.TEMPERAMENT.midi_message(value))
reset_on_reload.append(temperament)

transpose = IntSelect('Transpose', 0, -11, 11)
transpose.on_update = lambda value: mdevice.send(AntonijnSysexEvent.TRANSPOSE.midi_message(value))

recorder = OnOffSelect(
    'Record audio',
    '  START REC',
    '^ STOP REC',
    AntonijnSysexEvent.GRANDORGUE_START_RECORDING.midi_message(0),
    AntonijnSysexEvent.GRANDORGUE_STOP_RECORDING.midi_message(0),
)

met_bpm = IntSelect('Metron. BPM', 80, 1, 500)
met_bpm.on_update = lambda value: mdevice.send(AntonijnSysexEvent.METRONOME_BPM.midi_message(value))
reset_on_reload.append(met_bpm)

met_div = IntSelect('Metron. div.', 4, 0, 32)
met_div.on_update = lambda value: mdevice.send(AntonijnSysexEvent.METRONOME_MEASURE.midi_message(value))
reset_on_reload.append(met_div)

met = OnOffSelect(
    'Metronome',
    '  START',
    '^ STOP',
    AntonijnSysexEvent.GRANDORGUE_START_METRONOME.midi_message(0),
    AntonijnSysexEvent.GRANDORGUE_STOP_METRONOME.midi_message(0),
)

chain_screens([instrument, temperament, transpose, recorder, met_bpm, met_div, met])

keymap = {
    b'\x1b[A': Key.UP,
    b'\x1b[B': Key.DOWN,
    b'\x1b[C': Key.RIGHT,
    b'\x1b[D': Key.LEFT,
}

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description='Virtual MIDI device and control panel driver.',)
    parser.add_argument('--midi-name', default='Control Panel')
    parser.add_argument('--blank-after', type=int, default=120)
    parser.add_argument('--tty')
    args = parser.parse_args()

    mdevice = mido.open_ioport(args.midi_name, virtual=True)
    if args.tty:
        ddevice = serial.Serial(args.tty)
        idevice = ddevice

    wait_for_ready()

    active_screen = instrument
    active_screen.redraw()
    while True:
        # All key presses are handled in this loop
        s = read_escape_triplet(args.blank_after)
        if s == b'':
            # Waiting mode
            # Clear display
            ddevice_writeln('')
            ddevice_writeln('')

            # Wait for input
            read_escape_triplet(None)
            active_screen.redraw()
        elif s in keymap:
            active_screen.process_key(keymap[s])
