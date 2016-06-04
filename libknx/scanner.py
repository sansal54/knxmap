import asyncio
import argparse
import binascii
import collections
import codecs
import logging
import os
import socket
import struct
import sys
import time
try:
    # Python 3.4
    from asyncio import JoinableQueue as Queue
except ImportError:
    # Python 3.5 renamed it to Queue
    from asyncio import Queue

from .core import *
from .messages import *
from .gateway import *
from .bus import *
from .manufacturers import *

__all__ = ['KnxScanner']

LOGGER = logging.getLogger(__name__)

KnxTargetReport = collections.namedtuple(
    'KnxTargetReport',
    ['host',
    'port',
    'mac_address',
    'knx_address',
    'device_serial',
    'friendly_name',
    'device_status',
    'knx_medium',
    'project_install_identifier',
    'supported_services',
    'bus_devices'])


KnxBusTargetReport = collections.namedtuple(
    'KnxBusTargetReport',
    ['address',
    'type',
     'device_serial',
     'manufacturer'])


class KnxScanner:
    """The main scanner instance that takes care of scheduling workers for the targets."""
    def __init__(self, targets=None, bus_targets=None, max_workers=100, loop=None, ):
        self.loop = loop or asyncio.get_event_loop()
        self.max_workers = max_workers
        # the Queue contains all targets
        self.q = Queue(loop=self.loop)
        self.bus_queues = dict()
        self.bus_q = Queue(loop=self.loop)
        self._bus_targets = bus_targets
        self.bus_protocols = list()

        if targets:
            self.set_targets(targets)
        else:
            self.targets = set()

        if bus_targets:
            self.set_bus_targets(bus_targets)
        else:
            self.bus_targets = set()

        self.alive_targets = set()
        self.knx_gateways = list()
        self.bus_devices = set()

        # save some timing information
        self.t0 = time.time()
        self.t1 = None

    def set_targets(self, targets):
        #assert isinstance(targets, set)
        self.targets = targets
        for target in self.targets:
            self.add_target(target)

    def add_target(self, target):
        self.q.put_nowait(target)

    def add_bus_queue(self, gateway):
        self.bus_queues[gateway] = Queue(loop=self.loop)
        for target in self._bus_targets:
            self.bus_queues[gateway].put_nowait(target)

    def set_bus_targets(self, targets):
        self.bus_targets = targets
        for target in self.bus_targets:
            self.add_bus_target(target)

    def add_bus_target(self, target):
        self.bus_q.put_nowait(target)

    @asyncio.coroutine
    def knx_bus_worker(self, transport, protocol, queue):
        """A worker for communicating with devices on the bus."""
        try:
            while True:
                target = queue.get_nowait()
                LOGGER.info('BUS: target: {}'.format(target))
                if not protocol.tunnel_established:
                    LOGGER.error('KNX tunnel is not open!')
                    return

                tunnel_request = protocol.make_tunnel_request(target)
                tunnel_request.unnumbered_control_data('CONNECT')
                alive = yield from protocol.send_data(tunnel_request.get_message(), target)

                if alive:
                    # DeviceDescriptorRead
                    tunnel_request = protocol.make_tunnel_request(target)
                    tunnel_request.a_device_descriptor_read()
                    descriptor = yield from protocol.send_data(tunnel_request.get_message(), target)


                    # NCD
                    tunnel_request = protocol.make_tunnel_request(target)
                    tunnel_request.numbered_control_data('ACK')
                    ret = yield from protocol.send_data(tunnel_request.get_message(), target)

                    if not ret:
                        LOGGER.error('ERROR OCCURED')

                    dev_desc = data = struct.unpack('!H', descriptor.body.get('cemi').get('data'))[0]
                    manufacturer = None
                    serial = None

                    if dev_desc > 0x13:
                        # PropertyValueRead
                        tunnel_request = protocol.make_tunnel_request(target)
                        tunnel_request.a_property_value_read(
                            sequence=1, object_index=0, property_id=DEVICE_OBJECTS.get('PID_MANUFACTURER_ID'))
                        manufacturer = yield from protocol.send_data(tunnel_request.get_message(), target)

                        # NCD
                        tunnel_request = protocol.make_tunnel_request(target)
                        tunnel_request.numbered_control_data('ACK', sequence=1)
                        ret = yield from protocol.send_data(tunnel_request.get_message(), target)

                        if not ret:
                            manufacturer = 'COULD NOT READ MANUFACTURER'
                        else:
                            if isinstance(manufacturer, (str, bytes)):
                                manufacturer = struct.unpack('!H', manufacturer)[0]
                                manufacturer = get_manufacturer_by_id(manufacturer)

                        # PropertyValueRead
                        tunnel_request = protocol.make_tunnel_request(target)
                        tunnel_request.a_property_value_read(
                            sequence=2, object_index=0, property_id=DEVICE_OBJECTS.get('PID_SERIAL_NUMBER'))
                        serial = yield from protocol.send_data(tunnel_request.get_message(), target)

                        # NCD
                        tunnel_request = protocol.make_tunnel_request(target)
                        tunnel_request.numbered_control_data('ACK', sequence=2)
                        ret = yield from protocol.send_data(tunnel_request.get_message(), target)

                        if not ret:
                            serial = 'COULD NOT READ SERIAL'
                        else:
                            if isinstance(serial, (str, bytes)):
                                serial = codecs.encode(serial, 'hex').decode().upper()

                    if descriptor:
                        t = KnxBusTargetReport(
                            address=target,
                            type=DEVICE_DESCRIPTORS.get(dev_desc) or 'Unknown',
                            device_serial=serial or 'Unavailable',
                            manufacturer=manufacturer or 'Unknown')

                        self.bus_devices.add(t)

                tunnel_request = protocol.make_tunnel_request(target)
                tunnel_request.unnumbered_control_data('DISCONNECT')
                yield from protocol.send_data(tunnel_request.get_message(), target)



                queue.task_done()
        except asyncio.CancelledError:
            pass
        except asyncio.QueueEmpty:
            pass

    @asyncio.coroutine
    def bus_scan(self, knx_gateway):
        self.add_bus_queue(knx_gateway.host)
        queue = self.bus_queues.get(knx_gateway.host)
        LOGGER.info('Scanning {} bus device(s) on {}'.format(queue.qsize(), knx_gateway.host))
        future = asyncio.Future()
        bus_con = KnxTunnelConnection(future)
        transport, bus_protocol = yield from self.loop.create_datagram_endpoint(
            lambda: bus_con, remote_addr=(knx_gateway.host, knx_gateway.port))
        self.bus_protocols.append(bus_protocol)

        # make sure the tunnel has been established
        connected = yield from future
        if connected:
            workers = [asyncio.Task(self.knx_bus_worker(transport, bus_protocol, queue), loop=self.loop)]
            self.t0 = time.time()
            yield from queue.join()
            self.t1 = time.time()
            for w in workers:
                w.cancel()
            bus_protocol.knx_tunnel_disconnect()

        for i in self.bus_devices:
            knx_gateway.bus_devices.append(i)

        LOGGER.info('Bus scan took {} seconds'.format(self.t1 - self.t0))

    @asyncio.coroutine
    def knx_search_worker(self):
        """Send a KnxDescription request to see if target is a KNX device."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setblocking(0)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, struct.pack('256s', str.encode(self.iface)))

            protocol = KnxGatewaySearch()
            waiter = asyncio.Future(loop=self.loop)
            transport = self.loop._make_datagram_transport(
                sock, protocol, ('224.0.23.12', 3671), waiter)

            try:
                # Wait until connection_made() has been called on the transport
                yield from waiter
            except:
                LOGGER.error('Creating multicast transport failed!')
                transport.close()
                return

            # Wait SEARCH_TIMEOUT seconds for responses to our multicast packets
            yield from asyncio.sleep(self.search_timeout)

            if protocol.responses:
                # If protocol received SEARCH_RESPONSE packets, print them
                for response in protocol.responses:
                    peer = response[0]
                    response = response[1]

                    t = KnxTargetReport(
                        host=peer[0],
                        port=peer[1],
                        mac_address=response.body.get('dib_dev_info').get('knx_mac_address'),
                        knx_address=response.body.get('dib_dev_info').get('knx_address'),
                        device_serial=response.body.get('dib_dev_info').get('knx_device_serial'),
                        friendly_name=response.body.get('dib_dev_info').get('device_friendly_name'),
                        device_status=response.body.get('dib_dev_info').get('device_status'),
                        knx_medium=response.body.get('dib_dev_info').get('knx_medium'),
                        project_install_identifier=response.body.get('dib_dev_info').get('project_install_identifier'),
                        supported_services=[
                            KNX_SERVICES[k] for k, v in
                            response.body.get('dib_supp_sv_families').get('families').items()],
                        bus_devices=[])

                    self.knx_gateways.append(t)
        except asyncio.CancelledError:
            pass

    @asyncio.coroutine
    def search_gateways(self):
        self.t0 = time.time()
        yield from asyncio.ensure_future(asyncio.Task(self.knx_search_worker(), loop=self.loop))
        self.t1 = time.time()
        LOGGER.info('Scan took {} seconds'.format(self.t1 - self.t0))

    @asyncio.coroutine
    def knx_description_worker(self):
        """Send a KnxDescription request to see if target is a KNX device."""
        try:
            while True:
                target = self.q.get_nowait()
                LOGGER.debug('Scanning {}'.format(target))
                future = asyncio.Future()
                description = KnxGatewayDescription(future)
                yield from self.loop.create_datagram_endpoint(
                    lambda: description,
                    remote_addr=target)
                response = yield from future
                if response:
                    self.alive_targets.add(target)

                    t = KnxTargetReport(
                        host=target[0],
                        port=target[1],
                        mac_address=response.body.get('dib_dev_info').get('knx_mac_address'),
                        knx_address=response.body.get('dib_dev_info').get('knx_address'),
                        device_serial=response.body.get('dib_dev_info').get('knx_device_serial'),
                        friendly_name=response.body.get('dib_dev_info').get('device_friendly_name'),
                        device_status=response.body.get('dib_dev_info').get('device_status'),
                        knx_medium=response.body.get('dib_dev_info').get('knx_medium'),
                        project_install_identifier=response.body.get('dib_dev_info').get('project_install_identifier'),
                        supported_services=[
                            KNX_SERVICES[k] for k,v in
                            response.body.get('dib_supp_sv_families').get('families').items()],
                        bus_devices=[])

                    self.knx_gateways.append(t)
                self.q.task_done()
        except asyncio.CancelledError:
            pass
        except asyncio.QueueEmpty:
            pass

    @asyncio.coroutine
    def scan(self, targets=None, search_mode=False, search_timeout=5, iface=None,
             bus_mode=False, bus_monitor_mode=False, group_monitor_mode=False):
        """The function that will be called by run_until_complete(). This is the main coroutine."""
        if targets:
            self.set_targets(targets)

        if search_mode:
            self.iface = iface
            self.search_timeout = search_timeout
            yield from self.search_gateways()
            for t in self.knx_gateways:
                self.print_knx_target(t)

            LOGGER.info('Searching done')

        elif bus_monitor_mode or group_monitor_mode:
            LOGGER.info('Starting bus monitor')
            future = asyncio.Future()
            bus_con = KnxBusMonitor(future, group_monitor=group_monitor_mode)
            transport, self.bus_protocol = yield from self.loop.create_datagram_endpoint(
                lambda: bus_con,
                remote_addr=list(self.targets)[0])
            yield from future

            LOGGER.info('Stopping bus monitor')

        else:
            workers = [asyncio.Task(self.knx_description_worker(), loop=self.loop)
                       for _ in range(self.max_workers if len(self.targets) > self.max_workers else len(self.targets))]

            self.t0 = time.time()
            yield from self.q.join()
            self.t1 = time.time()
            for w in workers:
                w.cancel()

            if bus_mode and self.knx_gateways:
                bus_scanners = [asyncio.Task(self.bus_scan(g), loop=self.loop) for g in self.knx_gateways]
                yield from asyncio.wait(bus_scanners)
            else:
                LOGGER.info('Scan took {} seconds'.format(self.t1 - self.t0))

            for t in self.knx_gateways:
                self.print_knx_target(t)

    @staticmethod
    def print_knx_target(knx_target):
        """Print a target of type KnxTargetReport in a well formatted way."""
        # TODO: make this better, and prettier.
        out = dict()
        out[knx_target.host] = collections.OrderedDict()
        o = out[knx_target.host]

        o['Port'] = knx_target.port
        o['MAC Address'] = knx_target.mac_address
        o['KNX Bus Address'] = knx_target.knx_address
        o['KNX Device Serial'] = knx_target.device_serial
        o['KNX Medium'] = KNX_MEDIUMS.get(knx_target.knx_medium)
        o['Device Friendly Name'] = binascii.b2a_qp(knx_target.friendly_name.strip())
        o['Device Status'] = knx_target.device_status
        o['Project Install Identifier'] = knx_target.project_install_identifier
        o['Supported Services'] = knx_target.supported_services
        if knx_target.bus_devices:
            o['Bus Devices'] = list()
            for d in knx_target.bus_devices:
                _d = dict()
                _d[d.address] = collections.OrderedDict()
                _d[d.address]['Type'] = d.type
                _d[d.address]['Device Serial'] = d.device_serial
                _d[d.address]['Manufacturer'] = d.manufacturer
                o['Bus Devices'].append(_d)

        print()

        def print_fmt(d, indent=0):
            for key, value in d.items():
                if indent is 0:
                    print('   ' * indent + str(key))
                elif isinstance(value, (dict, collections.OrderedDict)):
                    print('   ' * indent + str(key) + ': ')
                else:
                    print('   ' * indent + str(key) + ': ', end="", flush=True)

                if key == 'Bus Devices':
                    print()
                    for i in value:
                        print_fmt(i, indent + 1)
                elif isinstance(value, list):
                    for i, v in enumerate(value):
                        if i is 0:
                            print()
                        print('   ' * (indent + 1) + str(v))
                elif isinstance(value, (dict, collections.OrderedDict)):
                    print_fmt(value, indent + 1)
                else:
                    print(value)

        print_fmt(out)
        print()
