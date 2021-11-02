import logging
import os
import re
import serial
import socket
import time

from .becker_helper import finalize_code
from .becker_helper import generate_code
from .becker_helper import BeckerConnectionError
from .database import Database

COMMAND_UP = 0x20
COMMAND_UP2 = 0x21  # move up
COMMAND_UP3 = 0x22  # move up
COMMAND_UP4 = 0x23  # move up
COMMAND_UP5 = 0x24  # intermediate position "up"
COMMAND_DOWN = 0x40
COMMAND_DOWN2 = 0x41  # move down
COMMAND_DOWN3 = 0x42  # move down
COMMAND_DOWN4 = 0x43  # move down
COMMAND_DOWN5 = 0x44  # intermediate position "down" (sun protection)
COMMAND_HALT = 0x10
COMMAND_PAIR = 0x80  # pair button press
COMMAND_PAIR2 = 0x81  # pair button pressed for 3 seconds (without releasing)
COMMAND_PAIR3 = 0x82  # pair button pressed for 6 seconds (without releasing)
COMMAND_PAIR4 = 0x83  # pair button pressed for 10 seconds (without releasing)

COMMAND_CLEARPOS = 0x90
COMMAND_CLEARPOS2 = 0x91
COMMAND_CLEARPOS3 = 0x92
COMMAND_CLEARPOS4 = 0x93

DEFAULT_DEVICE_NAME = '/dev/serial/by-id/usb-BECKER-ANTRIEBE_GmbH_CDC_RS232_v125_Centronic-if00'
DEFAULT_DATABASE_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'centronic-stick.db')

logging.basicConfig()
_LOGGER = logging.getLogger(__name__)


class Becker:
    """
        Becker Shutter Controller
        =========================

        Use this class to perform operations on your Becker Shutter using a centronic USB Stick
        This class will as well maintain a call increment in an internal database
    """
    def __init__(self, device_name=DEFAULT_DEVICE_NAME, init_dummy=False, database_path=DEFAULT_DATABASE_PATH):
        """
            Create a new instance of the Becker controller

            :param  device_name: The path for the centronic stick (default /dev/serial/by-id/usb-BECKER-ANTRIEBE_GmbH_CDC_RS232_v125_Centronic-if00).
            :param  init_dummy: Boolean that indicate if the database should be initialized with a dummy unit (default False).
            :type device_name: str
            :type init_dummy: bool
        """
        self.is_serial = "/" in device_name
        if self.is_serial and not os.path.exists(device_name):
            raise BeckerConnectionError(device_name + " is not existing")
        self.device = device_name
        self.db = Database(database_path)

        # If no unit is defined create a dummy one
        units = self.db.get_all_units()
        if not units and init_dummy:
            self.db.init_dummy()

        try:
            self._connect()
        except serial.SerialException:
            raise BeckerConnectionError("Error when trying to establish connection using " + device_name)

    def _connect(self):
        if self.is_serial:
            self.s = serial.Serial(self.device, 115200, timeout=1)
            self.write_function = self.s.write
        else:
            if ':' in self.device:
                host, port = self.device.split(':', 1)
            else:
                host = self.device
                port = '5000'
            self.s = socket.create_connection((host, port))
            self.write_function = self._reconnecting_sendall

    def _reconnecting_sendall(self, *args, **kwargs):
        """Wrapper for socker.sendall that reconnects (once) on failure"""

        try:
            return self.s.sendall(*args, **kwargs)
        except OSError:
            # Assume the connection failed, and connect again
            self._connect()
            return self.s.sendall(*args, **kwargs)

    async def write(self, codes):
        for code in codes:
            self.write_function(finalize_code(code))
            time.sleep(0.1)

    async def run_codes(self, channel, unit, cmd, test):
        if unit[2] == 0 and cmd != "TRAIN":
            _LOGGER.error("The unit %s is not configured" % (unit[0]))
            return

        # move up/down dependent on given time
        mt = re.match(r"(DOWN|UP):(\d+)", cmd)

        codes = []
        if cmd == "UP":
            codes.append(generate_code(channel, unit, COMMAND_UP))
        elif cmd == "UP2":
            codes.append(generate_code(channel, unit, COMMAND_UP5))
        elif cmd == "HALT":
            codes.append(generate_code(channel, unit, COMMAND_HALT))
        elif cmd == "DOWN":
            codes.append(generate_code(channel, unit, COMMAND_DOWN))
        elif cmd == "DOWN2":
            codes.append(generate_code(channel, unit, COMMAND_DOWN5))
        elif cmd == "TRAIN":
            codes.append(generate_code(channel, unit, COMMAND_PAIR2))
            unit[1] += 1
            codes.append(generate_code(channel, unit, COMMAND_PAIR2))
            # set unit as configured
            unit[2] = 1
        elif cmd == "CLEARPOS":
            codes.append(generate_code(channel, unit, COMMAND_PAIR))
            unit[1] += 1
            codes.append(generate_code(channel, unit, COMMAND_CLEARPOS))
            unit[1] += 1
            codes.append(generate_code(channel, unit, COMMAND_CLEARPOS2))
            unit[1] += 1
            codes.append(generate_code(channel, unit, COMMAND_CLEARPOS3))
            unit[1] += 1
            codes.append(generate_code(channel, unit, COMMAND_CLEARPOS4))
        elif cmd == "REMOVE":
            codes.append(generate_code(channel, unit, COMMAND_PAIR2))
            unit[1] += 1
            codes.append(generate_code(channel, unit, COMMAND_PAIR2))
            unit[1] += 1
            codes.append(generate_code(channel, unit, COMMAND_PAIR3))
            unit[1] += 1
            codes.append(generate_code(channel, unit, COMMAND_PAIR4))
            unit[2] = 0

        if mt:
            _LOGGER.INFO("Moving %s for %s seconds..." % (mt.group(1), mt.group(2)))
            # move down/up for a specific time
            if mt.group(1) == "UP":
                code = generate_code(channel, unit, COMMAND_UP)
            elif mt.group(1) == "DOWN":
                code = generate_code(channel, unit, COMMAND_DOWN)

            unit[1] += 1
            await self.write([code])

            time.sleep(int(mt.group(2)))

            # stop moving
            code = generate_code(channel, unit, COMMAND_HALT)
            unit[1] += 1
            await self.write([code])
        else:
            unit[1] += 1

        # append the release button code
        #codes.append(generate_code(channel, unit, 0))
        #unit[1] += 1

        await self.write(codes)
        self.db.set_unit(unit, test)

    async def send(self, channel, cmd, test=False):
        b = channel.split(':')
        if len(b) > 1:
            ch = int(b[1])
            un = int(b[0])
        else:
            ch = int(channel)
            un = 1

        if not 1 <= ch <= 7 and ch != 15:
            _LOGGER.error("Channel must be in range of 1-7 or 15")
            return

        if not self.device:
            _LOGGER.error("No device defined")
            return

        if un > 0:
            unit = self.db.get_unit(un)
            await self.run_codes(ch, unit, cmd, test)
        else:
            units = self.db.get_all_units()
            for unit in units:
                await self.run_codes(ch, unit, cmd, test)

    async def move_up(self, channel):
        """
            Send the command to move up for a given channel.

            :param channel: the channel on which the shutter is listening
            :type channel: str
        """
        await self.send(channel, "UP")

    async def move_up_intermediate(self, channel):
        """
            Send the command to move up in the intermediate position for a given channel.

            :param channel: the channel on which the shutter is listening
            :type channel: str
        """
        await self.send(channel, "UP2")

    async def move_down(self, channel):
        """
            Sent the command to move down for a given channel.

            :param channel: the channel on which the shutter is listening
            :type channel: str
        """
        await self.send(channel, "DOWN")

    async def move_down_intermediate(self, channel):
        """
            Send the command to move down in the intermediate position for a given channel.

            :param channel: the channel on which the shutter is listening
            :type channel: str
        """
        await self.send(channel, "DOWN2")

    async def stop(self, channel):
        """
            Send the command to stop for a given channel.

            :param channel: the channel on which the shutter is listening
            :type channel: str
        """
        await self.send(channel, "HALT")

    async def pair(self, channel):
        """
            Initiate the pairing for a given channel.

            :param channel: the channel on which the shutter is listening
            :type channel: str
        """
        await self.send(channel, "TRAIN")

    async def list_units(self):
        """
        Return all configured units as a list.
        """

        return self.db.get_all_units()
