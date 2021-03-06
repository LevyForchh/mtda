# System imports
import bz2
import configparser
import daemon
import gevent
import importlib
import os
import signal
import sys
import time
import zlib
import zmq

# Local imports
from   mtda.console.input import ConsoleInput
from   mtda.console.logger import ConsoleLogger
from   mtda.console.remote_output import RemoteConsoleOutput
import mtda.power.controller

class MentorTestDeviceAgent:

    def __init__(self):
        self.config_files = [ 'mtda.ini' ]
        self.console = None
        self.console_logger = None
        self.console_input = None
        self.console_output = None
        self.power_controller = None
        self.sdmux_controller = None
        self._sd_bytes_written = 0
        self._sd_mounted = False
        self._sd_opened = False
        self.blksz = 65536
        self.bz2dec = None
        self.zdec = None
        self.fbintvl = 8 # Feedback interval
        self.usb_switches = []
        self.ctrlport = 5556
        self.conport = 5557
        self.is_remote = False
        self.is_server = False
        self.remote = None
        self._lock_owner = None
        self._lock_expiry = None
        self._lock_timeout = 5 # Lock timeout (in minutes)

        # Config file in $HOME/.mtda/config
        home = os.getenv('HOME', '')
        if home != '':
            self.config_files.append(os.path.join(home, '.mtda', 'config'))

        # Config file in /etc/mtda/config
        if os.path.exists('/etc'):
            self.config_files.append(os.path.join('/etc', 'mtda', 'config'))

    def console_getkey(self):
        if self.console_input is None:
            self.console_input = ConsoleInput()
            self.console_input.start()
        return self.console_input.getkey()

    def console_clear(self, session=None):
        self._check_expired(session)
        if self.console_locked(session):
            return None
        if self.console_logger is not None:
            return self.console_logger.clear()
        else:
            return None

    def console_flush(self, session=None):
        self._check_expired(session)
        if self.console_locked(session):
            return None
        if self.console_logger is not None:
            return self.console_logger.flush()
        else:
            return None

    def console_head(self, session=None):
        self._check_expired(session)
        if self.console_logger is not None:
            return self.console_logger.head()
        else:
            return None

    def console_lines(self, session=None):
        self._check_expired(session)
        if self.console_logger is not None:
            return self.console_logger.lines()
        else:
            return None

    def console_locked(self, session=None):
        self._check_expired(session)
        if self._check_locked(session):
            return True
        return False

    def console_print(self, data, session=None):
        self._check_expired(session)
        if self.console_logger is not None:
            return self.console_logger.print(data)
        else:
            return None

    def console_prompt(self, newPrompt=None, session=None):
        self._check_expired(session)
        if self.console_locked(session):
            return None
        if self.console_logger is not None:
            return self.console_logger.prompt(newPrompt)
        else:
            return None

    def console_remote(self, host):
        if self.is_remote == True:
            # Create and start our remote console
            self.console_output = RemoteConsoleOutput(host, self.conport)
            self.console_output.start()

    def console_run(self, cmd, session=None):
        self._check_expired(session)
        if self.console_locked(session):
            return None
        if self.console_logger is not None:
            return self.console_logger.run(cmd)
        else:
            return None

    def console_send(self, data, raw=False, session=None):
        self._check_expired(session)
        if self.console_locked(session):
            return None
        if self.console_logger is not None:
            return self.console_logger.write(data, raw)
        else:
            return None

    def console_tail(self, session=None):
        self._check_expired(session)
        if self.console_locked(session):
            return None
        if self.console_logger is not None:
            return self.console_logger.tail()
        else:
            return None

    def power_locked(self, session=None):
        self._check_expired(session)
        if self._check_locked(session):
            return True
        if self.power_controller is None:
            return True
        return False

    def sd_bytes_written(self, session=None):
        self._check_expired(session)
        return self._sd_bytes_written

    def sd_close(self, session=None):
        self._check_expired(session)
        if self.sdmux_controller is None:
            return False
        self.bz2dec = None
        self.zdec = None
        if self._sd_opened == True:
            self._sd_opened = not self.sdmux_controller.close()
        return (self._sd_opened == False)

    def sd_locked(self, session=None):
        self._check_expired(session)
        if self._check_locked(session):
            return True
        # Cannot swap the SD card between the host and target
        # without a SDMux
        if self.sdmux_controller is None:
            return True
        # We also need a power controller to be safe
        if self.power_controller is None:
            return True
        # The target shall be OFF
        if self.target_status() != "OFF":
            return True
        # Lastly, the SD shall not be opened
        if self._sd_opened == True:
            return True
        # We may otherwise swap our SD card
        return False

    def sd_mount(self, part=None, session=None):
        self._check_expired(session)
        if self._sd_mounted == True:
            return True
        if self.sdmux_controller is None:
            return False
        status = self.sdmux_controller.mount(part)
        self._sd_mounted = (status == True)
        return status

    def sd_update(self, dst, offset, data, session=None):
        self._check_expired(session)
        if self.sdmux_controller is None:
            return -1
        if offset == 0:
            self._sd_bytes_written = 0
        result = self.sdmux_controller.update(dst, offset, data)
        if result > 0:
            self._sd_bytes_written = self._sd_bytes_written + result
        return result

    def sd_open(self, session=None):
        self._check_expired(session)
        if self.sdmux_controller is None:
            return False
        self.sd_close()
        self._sd_bytes_written = 0
        status = self.sdmux_controller.open()
        self._sd_opened = (status == True)
        return status

    def sd_status(self, session=None):
        self._check_expired(session)
        if self.sdmux_controller is None:
            return "???"
        status = self.sdmux_controller.status()
        return status

    def _sd_write_bz2(self, data):
        # Decompress and write the newly received data
        uncompressed = self.bz2dec.decompress(data, self.blksz)
        status = self.sdmux_controller.write(uncompressed)
        if status == False:
            return -1
        self._sd_bytes_written += len(uncompressed)

        # Check if we can write more data without further input
        if self.bz2dec.needs_input == False:
            return 0

        # Data successfully uncompressed and written to SD
        return self.blksz

    def sd_write_bz2(self, data, session=None):
        self._check_expired(session)
        if self.sdmux_controller is None:
            return -1

        # Create a bz2 decompressor when called for the first time
        if self.bz2dec is None:
            self.bz2dec = bz2.BZ2Decompressor()

        cont = True
        start = time.monotonic()
        status = -1

        while cont == True:
            # Decompress and write newly received data
            try:
                # Uncompress and write data
                status = self._sd_write_bz2(data)
                if status != 0:
                    # Either got an error or needing more data; escape from
                    # this loop to provide feedback
                    cont = False
                else:
                    # Check if this loop has been running for quite some time,
                    # in which case we would to give our client an update
                    now = time.monotonic()
                    if (now - start) >= self.fbintvl:
                        cont = False
                    # If we should continue and do not need more data at this time,
                    # use an empty buffer for the next iteration
                    elif status == 0:
                        data = b''
            except EOFError:
                # Handle multi-streams: create a new decompressor and we will start
                # with data unused from the previous decompressor
                data = self.bz2dec.unused_data
                self.bz2dec = bz2.BZ2Decompressor()
                cont = (len(data) > 0) # loop only if we have unused data
                status = 0             # we do not need more input data
        return status

    def _sd_write_gz(self, data):
        # Decompress and write the newly received data
        uncompressed = self.zdec.decompress(data, self.blksz)
        status = self.sdmux_controller.write(uncompressed)
        if status == False:
            return -1
        self._sd_bytes_written += len(uncompressed)

        # Check if we can write more data without further input
        if len(self.zdec.unconsumed_tail) > 0:
            return 0

        # Data successfully uncompressed and written to SD
        return self.blksz

    def sd_write_gz(self, data, session=None):
        self._check_expired(session)
        if self.sdmux_controller is None:
            return -1

        # Create a zlib decompressor when called for the first time
        if self.zdec is None:
            self.zdec = zlib.decompressobj(16+zlib.MAX_WBITS)

        # Check if we should use unconsumed data from the previous call
        if len(data) == 0:
            data = self.zdata

        cont = True
        start = time.monotonic()
        status = -1

        while cont == True:
            # Decompress and write newly received data
            status = self._sd_write_gz(data)
            self.zdata = None
            if status != 0:
                # Either got an error or needing more data; escape from
                # this loop to provide feedback
                cont = False
            else:
                # If we should continue and do not need more data at this time,
                # use the unconsumed data for the next iteration
                data = self.zdec.unconsumed_tail
                # Check if this loop has been running for quite some time,
                # in which case we would to give our client an update
                now = time.monotonic()
                if (now - start) >= self.fbintvl:
                    self.zdata = data
                    cont = False
        return status

    def sd_write_raw(self, data, session=None):
        self._check_expired(session)
        if self.sdmux_controller is None:
            return -1
        status = self.sdmux_controller.write(data)
        if status == False:
            return -1
        self._sd_bytes_written += len(data)
        return self.blksz

    def sd_to_host(self, session=None):
        self._check_expired(session)
        if self.sd_locked(session) == False:
            return self.sdmux_controller.to_host()
        return False

    def sd_to_target(self, session=None):
        self._check_expired(session)
        if self.sd_locked(session) == False:
            self.sd_close()
            return self.sdmux_controller.to_target()
        return False

    def sd_toggle(self, session=None):
        self._check_expired(session)
        if self.sd_locked(session) == False:
            status = self.sd_status(session)
            if status == self.sdmux_controller.SD_ON_HOST:
                self.sdmux_controller.to_target()
            elif status == self.sdmux_controller.SD_ON_TARGET:
                self.sdmux_controller.to_host()
        status = self.sd_status(session)
        return status

    def toggle_timestamps(self):
        if self.console_logger is not None:
            return self.console_logger.toggle_timestamps()
        else:
            print("no console configured/found!", file=sys.stderr)
            return None

    def target_lock(self, session):
        self._check_expired(session)
        owner = self.target_owner()
        if owner is None or owner == session:
            self._lock_owner = session
            return True
        return False

    def target_locked(self, session):
        self._check_expired(session)
        return self._check_locked(session)

    def target_owner(self):
        return self._lock_owner

    def target_on(self, session=None):
        if self.console_logger is not None:
           self.console_logger.resume()
        self._check_expired(session)
        if self.power_locked(session) == False:
            return self.power_controller.on()
        return False

    def target_off(self, session=None):
        self._check_expired(session)
        if self.power_locked(session) == False:
            status = self.power_controller.off()
            if self.console_logger is not None:
                self.console_logger.reset_timer()
            if status == True:
                self.console_logger.pause()
            return status
        return False

    def target_status(self, session=None):
        self._check_expired(session)
        if self.power_controller is None:
            return "???"
        return self.power_controller.status()

    def target_toggle(self, session=None):
        self._check_expired(session)
        if self.power_locked(session) == False:
            status = self.power_controller.toggle()
            if self.console_logger is not None:
                if status == self.power_controller.POWER_OFF:
                    self.console_logger.resume()
                if status == self.power_controller.POWER_OFF:
                    self.console_logger.pause()
                    self.console_logger.reset_timer()
            return status
        return self.power_controller.POWER_LOCKED

    def target_unlock(self, session):
        self._check_expired(session)
        if self.target_owner() == session:
            self._lock_owner = None
            return True
        return False

    def usb_find_by_class(self, className, session=None):
        self._check_expired(session)
        ports = len(self.usb_switches)
        ndx = 0
        while ndx < ports:
            usb_switch = self.usb_switches[ndx]
            if usb_switch.className == className:
                return usb_switch
            ndx = ndx + 1
        return None

    def usb_has_class(self, className, session=None):
        self._check_expired(session)
        usb_switch = self.usb_find_by_class(className, session)
        return usb_switch is not None

    def usb_off(self, ndx, session=None):
        self._check_expired(session)
        try:
            if ndx > 0:
                usb_switch = self.usb_switches[ndx-1]
                usb_switch.off()
        except IndexError:
            print("invalid USB switch #" + str(ndx), file=sys.stderr)

    def usb_off_by_class(self, className, session=None):
        self._check_expired(session)
        usb_switch = self.usb_find_by_class(className, session)
        if usb_switch is not None:
            return usb_switch.off()
        return False

    def usb_on(self, ndx, session=None):
        self._check_expired(session)
        try:
            if ndx > 0:
                usb_switch = self.usb_switches[ndx-1]
                usb_switch.on()
        except IndexError:
            print("invalid USB switch #" + str(ndx), file=sys.stderr)

    def usb_on_by_class(self, className, session=None):
        self._check_expired(session)
        usb_switch = self.usb_find_by_class(className, session)
        if usb_switch is not None:
            return usb_switch.on()
        return False

    def usb_ports(self, session=None):
        self._check_expired(session)
        return len(self.usb_switches)

    def usb_status(self, ndx, session=None):
        self._check_expired(session)
        try:
            if ndx > 0:
                usb_switch = self.usb_switches[ndx-1]
                status = usb_switch.status()
                if status == usb_switch.POWERED_OFF:
                    return "OFF"
                elif status == usb_switch.POWERED_ON:
                    return "ON"
                else:
                    return "???"
        except IndexError:
            print("invalid USB switch #" + str(ndx), file=sys.stderr)
            return "ERR"
        return "???"

    def usb_toggle(self, ndx, session=None):
        self._check_expired(session)
        try:
            if ndx > 0:
                usb_switch = self.usb_switches[ndx-1]
                usb_switch.toggle()
        except IndexError:
            print("invalid USB switch #" + str(ndx), file=sys.stderr)

    def load_config(self, remote=None, is_server=False):
        self.remote = remote
        self.is_remote = remote is not None
        self.is_server = is_server
        parser = configparser.ConfigParser()
        configs_found = parser.read(self.config_files)
        if parser.has_section('remote'):
            self.load_remote_config(parser)
        if self.is_remote == False:
            if parser.has_section('console'):
                self.load_console_config(parser)
            if parser.has_section('power'):
                self.load_power_config(parser)
            if parser.has_section('sdmux'):
                self.load_sdmux_config(parser)
            if parser.has_section('usb'):
                self.load_usb_config(parser)

    def load_console_config(self, parser):
        try:
            # Get variant
            variant = parser.get('console', 'variant')
            # Try loading its support class
            mod = importlib.import_module("mtda.console." + variant)
            factory = getattr(mod, 'instantiate')
            self.console = factory()
            # Configure the console
            self.console.configure(dict(parser.items('console')))
        except configparser.NoOptionError:
            print('console variant not defined!', file=sys.stderr)
        except ImportError:
            print('console "%s" could not be found/loaded!' % (variant), file=sys.stderr)
    
    def load_power_config(self, parser):
        try:
            # Get variant
            variant = parser.get('power', 'variant')
            # Try loading its support class
            mod = importlib.import_module("mtda.power." + variant)
            factory = getattr(mod, 'instantiate')
            self.power_controller = factory()
            # Configure the power controller
            self.power_controller.configure(dict(parser.items('power')))
        except configparser.NoOptionError:
            print('power controller variant not defined!', file=sys.stderr)
        except ImportError:
            print('power controller "%s" could not be found/loaded!' % (variant), file=sys.stderr)
    
    def load_sdmux_config(self, parser):
        try:
            # Get variant
            variant = parser.get('sdmux', 'variant')
            # Try loading its support class
            mod = importlib.import_module("mtda.sdmux." + variant)
            factory = getattr(mod, 'instantiate')
            self.sdmux_controller = factory()
            # Configure the sdmux controller
            self.sdmux_controller.configure(dict(parser.items('sdmux')))
        except configparser.NoOptionError:
            print('sdmux controller variant not defined!', file=sys.stderr)
        except ImportError:
            print('power controller "%s" could not be found/loaded!' % (variant), file=sys.stderr)

    def load_remote_config(self, parser):
        self.conport = int(parser.get('remote', 'console', fallback=self.conport))
        self.ctrlport = int(parser.get('remote', 'control', fallback=self.ctrlport))
        if self.is_server == False:
            if self.remote is None:
                # Load remote setting from the configuration
                self.remote = parser.get('remote', 'host', fallback=self.remote)
                # Allow override from the environment
                self.remote = os.getenv('MTDA_REMOTE', self.remote)
        else:
            self.remote = None
        self.is_remote = self.remote is not None

    def load_usb_config(self, parser):
        try:
            # Get number of ports
            usb_ports = int(parser.get('usb', 'ports'))
            for port in range(0, usb_ports):
                port = port + 1
                section = "usb" + str(port)
                if parser.has_section(section):
                    self.load_usb_port_config(parser, section)
        except configparser.NoOptionError:
            usb_ports = 0
    
    def load_usb_port_config(self, parser, section):
        try:
            # Get attributes
            className = parser.get(section, 'class', fallback="")
            variant = parser.get(section, 'variant')

            # Try loading its support class
            mod = importlib.import_module("mtda.usb." + variant)
            factory = getattr(mod, 'instantiate')
            usb_switch = factory()

            # Configure and probe the USB switch
            usb_switch.configure(dict(parser.items(section)))
            usb_switch.probe()

            # Store other attributes
            usb_switch.className = className

            # Add this USB switch
            self.usb_switches.append(usb_switch)
        except configparser.NoOptionError:
            print('usb switch variant not defined!', file=sys.stderr)
        except ImportError:
            print('usb switch "%s" could not be found/loaded!' % (variant), file=sys.stderr)

    def start(self):
        if self.is_remote == True:
            return True

        # Probe the specified power controller
        if self.power_controller is not None:
            status = self.power_controller.probe()
            if status == False:
                print('Probe of the Power Controller failed!', file=sys.stderr)
                return False

        # Probe the specified sdmux controller
        if self.sdmux_controller is not None:
            status = self.sdmux_controller.probe()
            if status == False:
                print('Probe of the SDMUX Controller failed!', file=sys.stderr)
                return False

        if self.console is not None:
            # Create a publisher
            if self.is_server == True:
                context = zmq.Context()
                socket = context.socket(zmq.PUB)
                socket.bind("tcp://*:%s" % self.conport)
            else:
                socket = None

            # Create and start console logger
            self.console.probe()
            self.console_logger = ConsoleLogger(self.console, socket, self.power_controller)
            self.console_logger.start()

        return True

    def _check_expired(self, session):
        if self._lock_owner:
            now = time.monotonic()
            if session == self._lock_owner:
                self._lock_expiry = now + (self._lock_timeout * 60)
            elif now >= self._lock_expiry:
                self._lock_owner = None

    def _check_locked(self, session):
        owner = self.target_owner()
        if owner is None:
            return False
        status = False if session == owner else True
        return status
