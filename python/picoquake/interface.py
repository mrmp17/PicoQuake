import time
from venv import logger
from serial import Serial, SerialException
from serial.tools.list_ports import comports
from queue import Empty, Queue
from time import sleep, time
from threading import Thread, Event
from typing import List
import logging
import struct
from datetime import datetime

from cobs import cobs

from .msg import messages_pb2
from .configuration import *
from .data import *

VID = 0x2E8A
PID = 0xA
MANUFACTURER = "PLab"
PRODUCT = "PicoQuake"

STATUS_TIMEOUT = 2.0

logger = logging.getLogger(__name__)
logger.setLevel(logging.NOTSET)


class HandshakeError(Exception):
    pass


class ConnectionError(Exception):
    pass


class DeviceNotFound(Exception):
    pass


class DecodeError(Exception):
    pass


class DeviceError(Exception):
    
    def __init__(self, error_code: int):
        self.error_code = error_code
        super().__init__(f"Device error: {error_code}")


class PicoQuake:

    def __init__(self, short_id: str | None = None, port: str | None = None):
        if port is not None:
            self._port = port
        elif short_id is not None:
            if not (isinstance(short_id, str) and len(short_id) == 4):
                raise ValueError("Short ID must be a 4-character string")
            self._port = self._find_port(short_id)
            if self._port is None:
                raise DeviceNotFound(f"Device with short ID {short_id} not found")
        else:
            raise ValueError("Either short_id or port must be specified")

        self.device_info: DeviceInfo | None = None

        self._config = Config(DataRate.hz_100, Filter.hz_42, AccRange.g_2, GyroRange.dps_250)
        self._continuos_mode = False
        self._acquire_n_samples = 0
        self._is_sampling = False
        self._sample_list: List[IMUSample] = []
        self._last_sample: IMUSample | None = None

        self._out_packet_queue = Queue()
        self._in_message_queue = Queue()
        self._stop_event = Event()
        
        self._serial_thread = Thread(target=self._serial_worker, daemon=True)
        self._handler_thread = Thread(target=self._handler, daemon=True)

        self._device_status = Status(State.IDLE, 0, 0, 0)
        self._last_status_time = time()

        self._serial_thread.start()
        self._handler_thread.start()
        self.started = True

        self._handshake()
        logger.info(f"Connected to: {self.device_info}")

    @staticmethod
    def handle_exceptions(func):
        def wrapper(self, *args, **kwargs):
            try:
                return func(self, *args, **kwargs)
            except Exception as e:
                self._stop()
                raise e
        return wrapper

    def configure(self, data_rate: DataRate, filter_hz: Filter,
                  acc_range: AccRange, gyro_range: GyroRange):
        self._config = Config(data_rate, filter_hz, acc_range, gyro_range)
        logger.info(f"Configuration set: {self._config}")

    def configure_approx(self, data_rate: float, filter_hz: float,
                         acc_range: float, gyro_range: float):
        self.configure(DataRate.find_closest(data_rate),
                       Filter.find_closest(filter_hz),
                       AccRange.find_closest(acc_range),
                       GyroRange.find_closest(gyro_range))
        
    @property
    def config(self) -> Config:
        return self._config

    def stop(self):
        self._stop()
        self._serial_thread.join()
        self._handler_thread.join()

    def acquire(self, seconds: float = 0, n_samples: int = 0,
                block: bool = True, timeout: float = 0) -> AcquisitionResult | IMUSample | None:
        if not self._continuos_mode:
            if seconds == 0 and n_samples == 0:
                raise ValueError("Either seconds or n_samples must be specified,"
                                 "or use start_continuos() before acquire()")
            if seconds > 0:
                n_samples = int(seconds * self._config.data_rate.param_value)

            logger.info(f"Acquiring {n_samples} samples...")
            self._acquire_n_samples = n_samples
            start_t = datetime.now()
            self._start_sampling()
            while self._is_sampling:
                if len(self._sample_list) >= n_samples:
                    self._stop_sampling()
                    break
                sleep(0.001)
            stop_t = datetime.now()
            logger.info("Acquisition DONE.")
            return AcquisitionResult(samples=self._sample_list[0:n_samples],
                                     device=self.device_info,
                                     config=self._config,
                                     start_time=start_t,
                                     end_time=stop_t)
        else:
            if block:
                start_time = time()
                while self._last_sample is None:
                    if timeout > 0 and time() - start_time > timeout:
                        break
                    sleep(0.001)
            return self._last_sample

    def start_continuos(self):
        self._continuos_mode = True
        self._start_sampling()
        logger.info("Continuos mode started")

    def stop_continuos(self):
        self._continuos_mode = False
        self._stop_sampling()
        self._last_sample = None
        logger.info("Continuos mode stopped")

    def _find_port(self, short_id: str) -> str | None:
        ports = comports()
        for p in ports:
            logger.debug(f"Found port: {p.device}, pid: {p.pid}, vid: {p.vid}, sn: {p.serial_number}")
            if p.vid == VID and p.pid == PID and p.serial_number:
                if DeviceInfo.unique_id_to_short_id(p.serial_number) == short_id.upper():
                    return p.device
        return None

    @handle_exceptions
    def _handshake(self, timeout: float = 2.0):
        self._send_command(CommandID.HANDSHAKE)
        start_time = time()
        while self.device_info is None:
            sleep(0.001)
            if time() - start_time > timeout:
                self._stop()
                raise HandshakeError("Timeout")

    def _start_sampling(self):
        logger.debug("Starting sampling...")
        self._sample_list = []
        self._last_sample = None
        self._send_command(CommandID.START_SAMPLING, self._config)
        self._is_sampling = True

    def _stop_sampling(self):
        logger.debug("Stopping sampling...")
        self._send_command(CommandID.STOP_SAMPLING, self._config) 
        self._is_sampling = False

    def _send_command(self, cmd_id: CommandID, config: Config | None = None):
        msg = messages_pb2.Command()
        msg.id = cmd_id.value
        if config is not None:
            msg.filter_config = config.filter.index
            msg.data_rate = config.data_rate.index
            msg.acc_range = config.acc_range.index
            msg.gyro_range = config.gyro_range.index
        packet = cobs.encode(msg.SerializeToString())
        packet = bytes([0x00]) + bytes([PacketID.COMMAND.value]) + packet + bytes([0x00])
        self._out_packet_queue.put_nowait(packet)
        logger.debug(f"Command sent: {cmd_id.name}")

    def _stop(self):
        if not self.started:
            return
        if self._is_sampling:
            self._stop_sampling()
        self._continuos_mode = False
        self._stop_event.set()
        logger.info("Device stopped")

    @handle_exceptions
    def _handler(self):
        while not self._stop_event.is_set():
            # process messages
            try:
                msg = self._in_message_queue.get(timeout=0.01)
            except Empty:
                pass
            else:
                if isinstance(msg, IMUSample):
                    imu_data = msg
                    if self._continuos_mode:
                        self._last_sample = imu_data
                    else:
                        self._sample_list.append(imu_data)
                elif isinstance(msg, messages_pb2.Status):
                    status = Status(State(msg.state), msg.temperature,
                                    msg.missed_samples, msg.error_code)
                    self.device_status = status
                    self._last_status_time = time()
                    # logger.debug(f"Device status: {status}")
                    if status.state == State.ERROR.value:
                        raise DeviceError(status.error_code)
                elif isinstance(msg, messages_pb2.DeviceInfo):
                    self.device_info = DeviceInfo(msg.unique_id.hex().upper(),
                                                  msg.firmware.decode("utf-8"))

            # check device status
            if time() - self._last_status_time > STATUS_TIMEOUT:
                raise ConnectionError("Connection lost")

    @handle_exceptions
    def _serial_worker(self):
        logger.debug(f"Connecting on port {self._port} ...")
        try:
            ser = Serial(self._port, timeout=0.001)
        except SerialException:
            raise ConnectionError(f"Could not connect to port {self._port}")
        data = b''
        in_buffer = b''
        receiving_packet = False
        while not self._stop_event.is_set():
            # receive
            data = ser.read(1)
            if len(data) > 0:
                if data == bytes([0x00]):
                    if receiving_packet:
                        if len(in_buffer) > 0:
                            # stop flag, end of packet
                            try:
                                self._in_message_queue.put_nowait(self._decode_packet(in_buffer))
                            except Exception as e:
                                logger.error(f"Decode error: {e}")
                            in_buffer = b''
                            receiving_packet = False
                        else:
                            # empty packet, treat as new start flag
                            pass
                    else:
                        # start flag
                        receiving_packet = True
                elif receiving_packet:
                    in_buffer += data
            
            # send
            try:
                packet = self._out_packet_queue.get_nowait()
                ser.write(packet)
            except Empty:
                pass
        ser.flush()
        ser.close()

    def _decode_packet(self, packet: bytes):
        packet_id = PacketID(packet[0])
        decoded = cobs.decode(packet[1:])
        if packet_id == PacketID.IMU_DATA:
            unpacked_data = struct.unpack('<Qffffff', decoded)
            msg = IMUSample(unpacked_data[0],
                            unpacked_data[1],
                            unpacked_data[2],
                            unpacked_data[3],
                            unpacked_data[4],
                            unpacked_data[5],
                            unpacked_data[6])
        elif packet_id == PacketID.STATUS:
            msg = messages_pb2.Status.FromString(decoded)
        elif packet_id == PacketID.DEVICE_INFO:
            msg = messages_pb2.DeviceInfo.FromString(decoded)
        return msg
