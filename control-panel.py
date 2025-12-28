#!/usr/bin/env python
import asyncio
import aioserial
import functools
import mido
import os
import sys
import tomli_w
import tomllib
from enum import Enum, IntEnum

print = functools.partial(print, flush=True)

COLUMNS = 16
ROWS = 2

using_settings_path = None

active_screen = None

mdevice = None
display_inport = None
display_outport = None
reg_inport = None
reg_outport = None

tty_queue = asyncio.Queue()

selected_stops = set()
active_stops = set()
stops_channel = 0

piston = None
manual_piston = None
reed_cutoff = False

piston_settings = {}



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

def display_outport_writeln(text):
    data = (text[:COLUMNS] + '\n').encode('utf-8')
    display_outport.write(data)

def wait_for_ready():
    display_outport_writeln('Laden...')
    display_outport_writeln('')

    # Wait for ready event
    ready_msg = AntonijnSysexEvent.GRANDORGUE_READY.midi_message(0)
    while True:
        msg = mdevice.receive()
        if msg.type == 'sysex' and msg.data == ready_msg.data:
            break

    # We ignore any key presses made while showing the loading screen
    if isinstance(display_inport, aioserial.AioSerial):
        display_inport.reset_input_buffer()

def chain_screens(screens):
    for i in range(1, len(screens)):
        screens[i - 1].adjacent[Key.RIGHT] = screens[i]
        screens[i].adjacent[Key.LEFT] = screens[i - 1]

def save_user_settings():
    pset = {}
    for pst, st in piston_settings.items():
        pset[pst] = list(st)
    dump_me = {'piston_settings': pset}
    with open(user_settings_path, 'wb') as fd:
        tomli_w.dump(dump_me, fd)

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

    def should_redraw(self):
        return False

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
        display_outport_writeln(format_arrows(self.name, left, right))

        left = self.active and self.value > self.minimum
        right = self.active and self.value < self.maximum
        display_outport_writeln(format_arrows(self.format_option(), left, right))

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
        display_outport_writeln(format_arrows(self.name, left, right))
        display_outport_writeln(self.off_text if self.active else self.on_text)

class PistonSaveScreen(Screen):
    def __init__(self):
        super().__init__()

    def can_save(self):
        return piston is not None and piston != manual_piston

    def process_key(self, key):
        if key in (Key.LEFT, Key.RIGHT):
            super().process_key(key)
        elif key == Key.DOWN and self.can_save():
            piston_settings[piston] = selected_stops.copy()
            print(piston_settings)
            save_user_settings()
            self.redraw()

    def redraw(self):
        left = Key.LEFT in self.adjacent
        right = Key.RIGHT in self.adjacent
        display_outport_writeln(format_arrows('Combinatie', left, right))
        text = ''
        if self.can_save():
            saved = piston_settings.get(piston, None)
            if saved == selected_stops:
                text = f'  {piston} opgeslagen'
            else:
                text = f'  {piston} OPSLAAN?'
        display_outport_writeln(text)

    def should_redraw(self):
        return True


async def read_arrow_keys(timeout):
    display_inport.timeout = timeout
    while True:
        full = b''
        pattern = b'\x1b['
        for _ in range(3):
            s = await display_inport.read_async()
            if s == b'':
                # Timeout reached
                await tty_queue.put(b'sleep\n')
                break
            full += s
            if not pattern.startswith(full[:len(pattern)]):
                break
        else:
            await tty_queue.put(full)

async def read_reg_lines():
    while True:
        await tty_queue.put(await reg_inport.readline_async())

def update_stop_selection(cmd):
    words = cmd.strip().split(' ')
    if words[0] != 'stop':
        raise ValueError()

    if len(words) % 2 != 1:
        raise ValueError()

    for i in range(1, len(words), 2):
        on_or_off = words[i]
        stops = {int(s) for s in words[i + 1].split(',')}
        if on_or_off == 'on':
            selected_stops.update(stops)
        elif on_or_off == 'off':
            selected_stops.difference_update(stops)
        else:
            raise ValueError()


def send_stops(prev_active_stops):
    for stop in active_stops.symmetric_difference(prev_active_stops):
        msg_type = 'note_on' if stop in active_stops else 'note_off'
        mdevice.send(mido.Message(msg_type, note=stop, channel=stops_channel))


reset_on_reload = []

instruments = [
    'Modern orgel',
    'Positief',
]
instrument = EnumSelect('Instrument', instruments)
def on_instrument_update(value):
    mdevice.send(AntonijnSysexEvent.INSTRUMENT.midi_message(value))
    wait_for_ready()
    for screen in reset_on_reload:
        screen.reset()
    send_stops(set())
instrument.on_update = on_instrument_update

temperaments = [
    'Origineel',
    'Gelijkzw.',
    '1/4 Mdd.toon',
    '1/5 Mdd.toon',
    '1/6 Mdd.toon',
    '2/7 Mdd.toon',
    'Werckmeister',
    'Pyth.',
    'Pyth. (B-F#)',
]
temperament = EnumSelect('Stemming', temperaments, default=1)
temperament.on_update = lambda value: mdevice.send(AntonijnSysexEvent.TEMPERAMENT.midi_message(value))
reset_on_reload.append(temperament)

piston_save = PistonSaveScreen()

transpose = IntSelect('Transpositie', 0, -11, 11)
transpose.on_update = lambda value: mdevice.send(AntonijnSysexEvent.TRANSPOSE.midi_message(value))

recorder = OnOffSelect(
    'Opname',
    '  START',
    '^ STOP',
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
    'Metronoom',
    '  START',
    '^ STOP',
    AntonijnSysexEvent.GRANDORGUE_START_METRONOME.midi_message(0),
    AntonijnSysexEvent.GRANDORGUE_STOP_METRONOME.midi_message(0),
)

chain_screens([instrument, temperament, piston_save, transpose, recorder, met_bpm, met_div, met])

async def main():
    global manual_piston, stops_channel

    user_conf_dir = os.environ.get('XDG_CONFIG_HOME', os.path.join(os.getenv('HOME'), '.config'))
    app_conf_dir = os.path.join(user_conf_dir, 'johannus-control-panel')
    app_conf_dir = os.environ.get('CONTROL_PANEL_CONF', app_conf_dir)

    conf_file = os.path.join(app_conf_dir, 'setup.toml')

    with open(conf_file, 'rb') as fd:
        conf = tomllib.load(fd)
        midi = conf['midi']
        midi_name = midi.get('name', 'Control Panel')
        print('Name:', midi_name)
        stops_channel = midi.get('stops_channel', 0)

        system = conf['system']
        display_tty = system.get('display_tty')
        reg_tty = system.get('reg_tty')

        display = conf['display']
        blank_after = display.get('blank_after', 120)

        organ = conf['organ']
        manual_piston = organ.get('manual_piston_setting')
        reed_stops = set(organ.get('reed_stops'))

    global piston_settings, user_settings_path

    user_settings_path = os.path.join(app_conf_dir, 'user-settings.toml')
    if os.path.exists(user_settings_path):
        with open(user_settings_path, 'rb') as fd:
            conf = tomllib.load(fd)
            for pst, stps in conf['piston_settings'].items():
                piston_settings[pst] = set(stps)

    global mdevice, display_outport, display_inport, reg_outport, reg_inport
    global selected_stops, active_stops, active_screen, piston, reed_cutoff

    mdevice = mido.open_ioport(midi_name, virtual=True)

    display_outport = aioserial.AioSerial(display_tty)
    display_inport = display_outport

    reg_outport = aioserial.AioSerial(reg_tty)
    reg_inport = reg_outport

    wait_for_ready()
    send_stops(set())

    asyncio.create_task(read_arrow_keys(blank_after))
    asyncio.create_task(read_reg_lines())

    screen_asleep = False

    keymap = {
        '\x1b[A': Key.UP,
        '\x1b[B': Key.DOWN,
        '\x1b[C': Key.RIGHT,
        '\x1b[D': Key.LEFT,
    }

    active_screen = instrument
    active_screen.redraw()
    while True:
        cmd = await tty_queue.get()
        cmd = cmd.decode(errors='ignore')
        if cmd == 'sleep\n':
            # Waiting mode
            # Clear display
            display_outport_writeln('')
            display_outport_writeln('')
            screen_asleep = True
        elif cmd in keymap:
            if screen_asleep:
                active_screen.redraw()
                screen_asleep = False
            else:
                active_screen.process_key(keymap[cmd])
        else:
            print('Got command', repr(cmd))

            try:
                if cmd.startswith('stop '):
                    update_stop_selection(cmd)
                elif cmd.startswith('piston '):
                    piston = cmd[7:].strip()
                elif cmd == 'reeds on\n':
                    reed_cutoff = False
                elif cmd == 'reeds off\n':
                    reed_cutoff = True
            except Exception as inst:
                print(inst)

        prev_active_stops = active_stops.copy()

        if piston == manual_piston or piston not in piston_settings:
            active_stops = selected_stops.copy()
        else:
            active_stops = piston_settings[piston].copy()

        if reed_cutoff:
            active_stops.difference_update(reed_stops)

        send_stops(prev_active_stops)

        if not screen_asleep and active_screen.should_redraw():
            active_screen.redraw()

if __name__ == "__main__":
    asyncio.run(main())
