# Copyright (c) 2021-2023, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import copy
import logging
import os
import random
import threading
import time
import traceback
import uuid
from typing import Dict, List, Union
from urllib.parse import urlparse

from nvflare.fuel.f3.cellnet.connector_manager import ConnectorManager
from nvflare.fuel.f3.cellnet.defs import (
    AbortRun,
    AuthenticationError,
    CellPropertyKey,
    InvalidRequest,
    InvalidSession,
    MessageHeaderKey,
    MessagePropKey,
    MessageType,
    ReturnCode,
    ReturnReason,
    ServiceUnavailable,
)
from nvflare.fuel.f3.cellnet.fqcn import FQCN, FqcnInfo, same_family
from nvflare.fuel.f3.cellnet.utils import decode_payload, encode_payload, format_log_message, make_reply, new_message
from nvflare.fuel.f3.comm_config import CommConfigurator
from nvflare.fuel.f3.communicator import Communicator, MessageReceiver
from nvflare.fuel.f3.connection import Connection
from nvflare.fuel.f3.drivers.driver_params import DriverParams
from nvflare.fuel.f3.endpoint import Endpoint, EndpointMonitor, EndpointState
from nvflare.fuel.f3.message import Message
from nvflare.fuel.f3.stats_pool import StatsPoolManager

_CHANNEL = "cellnet.channel"
_TOPIC_BULK = "bulk"
_TOPIC_BYE = "bye"

_ONE_MB = 1024 * 1024


class TargetMessage:
    def __init__(
        self,
        target: str,
        channel: str,
        topic: str,
        message: Message,
    ):
        self.target = target
        self.channel = channel
        self.topic = topic
        self.message = message

    def to_dict(self):
        return {
            "target": self.target,
            "channel": self.channel,
            "topic": self.topic,
            "message": {"headers": dict(self.message.headers), "payload": self.message.payload},
        }

    @staticmethod
    def from_dict(d: dict):
        msg_dict = d.get("message")
        msg = new_message(headers=msg_dict.get("headers"), payload=msg_dict.get("payload"))
        return TargetMessage(target=d.get("target"), channel=d.get("channel"), topic=d.get("topic"), message=msg)


class CellAgent:
    """
    A CellAgent represents a cell in another cell.
    """

    def __init__(self, fqcn: str, endpoint: Endpoint):
        """

        Args:
            fqcn: FQCN of the cell represented
        """
        err = FQCN.validate(fqcn)
        if err:
            raise ValueError(f"Invalid FQCN '{fqcn}': {err}")

        self.info = FqcnInfo(FQCN.normalize(fqcn))
        self.endpoint = endpoint

    def get_fqcn(self):
        return self.info.fqcn


class _CB:
    def __init__(self, cb, args, kwargs):
        self.cb = cb
        self.args = args
        self.kwargs = kwargs


class _Registry:
    def __init__(self):
        self.reg = {}  # channel/topic => _CB

    @staticmethod
    def _item_key(channel: str, topic: str) -> str:
        return f"{channel}:{topic}"

    def set(self, channel: str, topic: str, items):
        key = self._item_key(channel, topic)
        self.reg[key] = items

    def append(self, channel: str, topic: str, items):
        key = self._item_key(channel, topic)
        item_list = self.reg.get(key)
        if not item_list:
            item_list = []
            self.reg[key] = item_list
        item_list.append(items)

    def find(self, channel: str, topic: str):
        items = self.reg.get(self._item_key(channel, topic))
        if not items:
            # try topic * in channel
            items = self.reg.get(self._item_key(channel, "*"))

        if not items:
            # try topic * in channel *
            items = self.reg.get(self._item_key("*", "*"))

        return items


class _Waiter(threading.Event):
    def __init__(self, targets: List[str]):
        super().__init__()
        self.targets = [x for x in targets]
        self.reply_time = {}  # target_id => reply recv timestamp
        self.send_time = time.time()
        self.id = str(uuid.uuid4())
        self.received_replies = {}


class _BulkSender:
    def __init__(self, cell, target: str, max_queue_size):
        self.cell = cell
        self.target = target
        self.max_queue_size = max_queue_size
        self.messages = []
        self.last_send_time = 0
        self.lock = threading.Lock()
        self.logger = logging.getLogger(self.__class__.__name__)

    def queue_message(self, channel: str, topic: str, message: Message):
        encode_payload(message)
        with self.lock:
            tm = TargetMessage(target=self.target, channel=channel, topic=topic, message=message)
            self.messages.append(tm)
            self.logger.debug(f"{self.cell.get_fqcn()}: bulk sender {self.target} queue size {len(self.messages)}")

    def send(self):
        with self.lock:
            num_msgs = len(self.messages)
            if num_msgs == 0:
                return

            if num_msgs <= self.max_queue_size:
                messages_to_send = self.messages
                self.messages = []
            else:
                messages_to_send = self.messages[: self.max_queue_size]
                self.messages = self.messages[self.max_queue_size :]

        self.logger.debug(
            f"{self.cell.get_fqcn()}: bulk sender {self.target} sending bulk size {len(messages_to_send)}"
        )
        tms = [m.to_dict() for m in messages_to_send]
        bulk_msg = new_message(payload=tms)
        send_errs = self.cell.fire_and_forget(
            channel=_CHANNEL, topic=_TOPIC_BULK, targets=[self.target], message=bulk_msg
        )
        if send_errs[self.target]:
            self.logger.error(
                f"{self.cell.get_fqcn()}: can't send bulk message ({len(messages_to_send)}) to {self.target}: "
                f"{send_errs[self.target]}"
            )
        else:
            self.logger.debug(f"{self.cell.get_fqcn()}: sent bulk messages ({len(messages_to_send)}) to {self.target}")
        self.last_send_time = time.time()


def _validate_url(url: str) -> bool:
    if not isinstance(url, str) or not url:
        return False
    result = urlparse(url)
    if not result.scheme or not result.netloc:
        return False
    return True


class _CounterName:

    LATE = "late"
    SENT = "sent"
    RETURN = "return"
    FORWARD = "forward"
    RECEIVED = "received"
    REPLIED = "replied"
    REPLY_NONE = "no_reply:none"
    NO_REPLY_LATE = "no_reply:late"
    REPLY_NOT_EXPECTED = "no_reply_expected"
    REQ_FILTER_ERROR = "req_filter_error"
    REP_FILTER_ERROR = "rep_filter_error"


class Cell(MessageReceiver, EndpointMonitor):

    APP_ID = 1
    ERR_TYPE_MSG_TOO_BIG = "MsgTooBig"
    ERR_TYPE_COMM = "CommErr"

    def __init__(
        self,
        fqcn: str,
        root_url: str,
        secure: bool,
        credentials: dict,
        create_internal_listener: bool = False,
        parent_url: str = None,
        max_timeout=3600,
        bulk_check_interval=0.5,
        bulk_process_interval=0.5,
        max_bulk_size=100,
    ):
        """

        Args:
            fqcn: the Cell's FQCN (Fully Qualified Cell Name)
            credentials: credentials for secure connections
            root_url: the URL for backbone external connection
            secure: secure mode or not
            max_timeout: default timeout for send_and_receive
            create_internal_listener: whether to create an internal listener for child cells
            parent_url: url for connecting to parent cell

        FQCN is the names of all ancestor, concatenated with dots.
        Note: internal listener is automatically created for root cells.

        Example:
            server.J12345       (the cell for job J12345 on the server)
            server              (the root cell of server)
            nih_1.J12345        (the cell for job J12345 on client_1's site)
            client_1.J12345.R0  (the cell for rank R0 of J12345 on client_1 site)
            client_1            (he root cell of client_1)

        """
        comm_configurator = CommConfigurator()
        self._name = self.__class__.__name__
        self.logger = logging.getLogger(self._name)
        self.max_msg_size = comm_configurator.get_max_message_size()
        self.comm_configurator = comm_configurator

        err = FQCN.validate(fqcn)
        if err:
            raise ValueError(f"Invalid FQCN '{fqcn}': {err}")

        self.my_info = FqcnInfo(FQCN.normalize(fqcn))
        self.secure = secure
        self.logger.debug(f"{self.my_info.fqcn}: max_msg_size={self.max_msg_size}")

        if not root_url:
            raise ValueError(f"{self.my_info.fqcn}: root_url not provided")

        if self.my_info.is_root and self.my_info.is_on_server:
            if isinstance(root_url, list):
                for url in root_url:
                    if not _validate_url(url):
                        raise ValueError(f"{self.my_info.fqcn}: invalid Root URL '{url}'")
            else:
                if not _validate_url(root_url):
                    raise ValueError(f"{self.my_info.fqcn}: invalid Root URL '{root_url}'")
                root_url = [root_url]
        else:
            if isinstance(root_url, list):
                # multiple urls are available - randomly pick one
                root_url = random.choice(root_url)
                self.logger.info(f"{self.my_info.fqcn}: use Root URL {root_url}")
            if not _validate_url(root_url):
                raise ValueError(f"{self.my_info.fqcn}: invalid Root URL '{root_url}'")

        self.root_url = root_url
        self.create_internal_listener = create_internal_listener
        self.parent_url = parent_url
        self.bulk_check_interval = bulk_check_interval
        self.max_bulk_size = max_bulk_size
        self.bulk_senders = {}
        self.bulk_checker = threading.Thread(target=self._check_bulk)
        self.bulk_lock = threading.Lock()

        self.bulk_process_interval = bulk_process_interval
        self.bulk_messages = []
        self.bulk_msg_lock = threading.Lock()
        self.bulk_processor = threading.Thread(target=self._process_bulk_messages)

        self.agents = {}  # cell_fqcn => CellAgent
        self.agent_lock = threading.Lock()

        self.logger.debug(f"Creating Cell: {self.my_info.fqcn}")

        ep = Endpoint(
            name=fqcn,
            conn_props=credentials,
            properties={
                CellPropertyKey.FQCN: self.my_info.fqcn,
            },
        )

        self.communicator = Communicator(local_endpoint=ep)

        self.endpoint = ep
        self.connector_manager = ConnectorManager(
            communicator=self.communicator, secure=secure, comm_configurator=comm_configurator
        )

        self.communicator.register_message_receiver(app_id=self.APP_ID, receiver=self)
        self.communicator.register_monitor(monitor=self)
        self.req_reg = _Registry()
        self.in_req_filter_reg = _Registry()  # for request received
        self.out_reply_filter_reg = _Registry()  # for reply going out
        self.out_req_filter_reg = _Registry()  # for request sent
        self.in_reply_filter_reg = _Registry()  # for reply received
        self.error_handler_reg = _Registry()
        self.cell_connected_cb = None
        self.cell_connected_cb_args = None
        self.cell_connected_cb_kwargs = None
        self.cell_disconnected_cb = None
        self.cell_disconnected_cb_args = None
        self.cell_disconnected_cb_kwargs = None
        self.message_interceptor = None
        self.message_interceptor_args = None
        self.message_interceptor_kwargs = None

        self.waiters = {}  # req_id => req
        self.stats_lock = threading.Lock()
        self.req_hw = 0
        self.num_sar_reqs = 0  # send-and-receive
        self.num_faf_reqs = 0
        self.num_timeout_reqs = 0

        # req_expiry specifies how long we keep requests in "reqs" table if they are
        # not answered or picked up
        if not max_timeout or max_timeout <= 0:
            max_timeout = 3600  # one hour
        self.max_timeout = max_timeout
        self.asked_to_stop = False
        self.running = False

        # add appropriate drivers based on roles of the cell
        # a cell can have at most two listeners: one for external, one for internal
        self.ext_listeners = {}  # external listeners: url => connector object
        self.ext_listener_lock = threading.Lock()
        self.ext_listener_impossible = False

        self.int_listener = None  # backbone internal listener - only for cells with child cells

        # a cell could have any number of connectors: some for backbone, some for ad-hoc
        self.bb_ext_connector = None  # backbone external connector - only for Client cells
        self.bb_int_connector = None  # backbone internal connector - only for non-root cells

        # ad-hoc connectors: currently only support ad-hoc external connectors
        self.adhoc_connectors = {}  # target cell fqcn => connector
        self.adhoc_connector_lock = threading.Lock()
        self.root_change_lock = threading.Lock()

        self.register_request_cb(channel=_CHANNEL, topic=_TOPIC_BULK, cb=self._receive_bulk_message)
        self.register_request_cb(channel=_CHANNEL, topic=_TOPIC_BYE, cb=self._peer_goodbye)

        self.cleanup_waiter = None
        self.msg_stats_pool = StatsPoolManager.add_time_hist_pool(
            "Request_Response", "Request/response time in secs (sender)", scope=self.my_info.fqcn
        )

        self.req_cb_stats_pool = StatsPoolManager.add_time_hist_pool(
            "Request_Processing",
            "Time spent (secs) by request processing callbacks (receiver)",
            scope=self.my_info.fqcn,
        )

        self.msg_travel_stats_pool = StatsPoolManager.add_time_hist_pool(
            "Msg_Travel", "Time taken (secs) to get here (receiver)", scope=self.my_info.fqcn
        )

        self.sent_msg_size_pool = StatsPoolManager.add_msg_size_pool(
            "Sent_Msg_sizes", "Sizes of messages sent (MBs)", scope=self.my_info.fqcn
        )

        self.received_msg_size_pool = StatsPoolManager.add_msg_size_pool(
            "Received_Msg_Sizes", "Sizes of messages received (MBs)", scope=self.my_info.fqcn
        )

        counter_names = [_CounterName.SENT]
        self.sent_msg_counter_pool = StatsPoolManager.add_counter_pool(
            name="Sent_Msg_Counters",
            description="Result counters of sent messages",
            counter_names=counter_names,
            scope=self.my_info.fqcn,
        )

        counter_names = [_CounterName.RECEIVED]
        self.received_msg_counter_pool = StatsPoolManager.add_counter_pool(
            name="Received_Msg_Counters",
            description="Result counters of received messages",
            counter_names=counter_names,
            scope=self.my_info.fqcn,
        )

    def get_root_url_for_child(self):
        if isinstance(self.root_url, list):
            return self.root_url[0]
        else:
            return self.root_url

    def get_fqcn(self) -> str:
        return self.my_info.fqcn

    def is_cell_reachable(self, target_fqcn: str) -> bool:
        _, ep = self._find_endpoint(target_fqcn)
        return ep is not None

    def is_cell_connected(self, target_fqcn: str) -> bool:
        agent = self.agents.get(target_fqcn)
        return agent is not None

    def is_backbone_ready(self):
        """Check if backbone is ready
        Backbone is the preconfigured network connections, like all the connections from clients to server.
        Adhoc connections are not part of the backbone.
        """
        if not self.running:
            return False

        if self.my_info.is_root:
            if self.my_info.is_on_server:
                # server root - make sure listener is created
                return len(self.ext_listeners) > 0
            else:
                # client root - must be connected to server root
                return self.agents.get(FQCN.ROOT_SERVER) is not None
        else:
            # child cell - must be connected to parent
            parent_fqcn = FQCN.get_parent(self.my_info.fqcn)
            return self.agents.get(parent_fqcn) is not None

    def _set_bb_for_client_root(self):
        self._create_bb_external_connector()
        self._create_internal_listener()

    def _set_bb_for_client_child(self, parent_url: str, create_internal_listener: bool):
        self._create_internal_connector(parent_url)
        if create_internal_listener:
            self._create_internal_listener()

        if self.connector_manager.should_connect_to_server(self.my_info):
            self._create_bb_external_connector()

    def _set_bb_for_server_root(self):
        if isinstance(self.root_url, list):
            for url in self.root_url:
                self.logger.info(f"{self.my_info.fqcn}: creating listener on {url}")
                self._create_external_listener(url)
        else:
            self.logger.info(f"{self.my_info.fqcn}: creating listener on {self.root_url}")
            self._create_external_listener(self.root_url)
        self._create_internal_listener()

    def _set_bb_for_server_child(self, parent_url: str, create_internal_listener: bool):
        self._create_internal_connector(parent_url)
        if create_internal_listener:
            self._create_internal_listener()

    def change_server_root(self, to_url: str):
        """
        Change to a different server url

        Args:
            to_url: the new url of the server root

        Returns:

        """
        self.logger.debug(f"{self.my_info.fqcn}: changing server root to {to_url}")
        with self.root_change_lock:
            if self.my_info.is_on_server:
                # only affect clients
                self.logger.debug(f"{self.my_info.fqcn}: no change - on server side")
                return

            if to_url == self.root_url:
                # already changed
                self.logger.debug(f"{self.my_info.fqcn}: no change - same url")
                return

            self.root_url = to_url
            self.drop_connectors()
            self.drop_agents()

            # recreate backbone connector to the root
            if self.my_info.gen <= 2:
                # self.logger.debug("waiting 5 secs before connecting to new server ...")
                # time.sleep(5.0)
                self.logger.debug(f"{self.my_info.fqcn}: recreating bb_external_connector ...")
                self._create_bb_external_connector()

    def drop_connectors(self):
        # drop connections to all cells on server and their agents
        # drop the backbone connector
        if self.bb_ext_connector:
            self.logger.debug(f"{self.my_info.fqcn}: removing bb_ext_connector ...")
            try:
                self.communicator.remove_connector(self.bb_ext_connector.handle)
            except Exception as ex:
                self.logger.error(f"{self.my_info.fqcn}: error removing bb_ext_connector {ex}")
            self.bb_ext_connector = None

        # drop ad-hoc connectors to cells on server
        with self.adhoc_connector_lock:
            cells_to_delete = []
            for to_cell in self.adhoc_connectors.keys():
                to_cell_info = FqcnInfo(to_cell)
                if to_cell_info.is_on_server:
                    cells_to_delete.append(to_cell)
            for c in cells_to_delete:
                self.logger.debug(f"{self.my_info.fqcn}: removing adhoc connector to {c}")
                connector = self.adhoc_connectors.pop(c, None)
                if connector:
                    try:
                        self.communicator.remove_connector(connector.handle)
                    except Exception as ex:
                        self.logger.error(f"{self.my_info.fqcn}: error removing adhoc connector {ex}")

    def drop_agents(self):
        # drop agents
        with self.agent_lock:
            agents_to_delete = []
            for fqcn, agent in self.agents.items():
                assert isinstance(agent, CellAgent)
                if agent.info.is_on_server:
                    agents_to_delete.append(fqcn)
            for a in agents_to_delete:
                self.logger.debug(f"{self.my_info.fqcn}: removing agent {a}")
                self.agents.pop(a, None)

    def create_internal_listener(self):
        """
        Create the internal listener for child cells of this cell to connect to.

        Returns:

        """
        self._create_internal_listener()

    def get_internal_listener_url(self) -> Union[None, str]:
        """
        Get the cell's internal listener url.
        This method should only be used for cells that need to have child cells.
        The url returned is to be passed to child of this cell to create connection

        Returns: url for child cells to connect

        """
        if not self.int_listener:
            return None
        return self.int_listener.get_connection_url()

    def _add_adhoc_connector(self, to_cell: str, url: str):
        if self.bb_ext_connector:
            # it is possible that the server root offers connect url after the bb_ext_connector is created
            # but the actual connection has not been established.
            # Do not create another adhoc connection to the server!
            if isinstance(self.root_url, str) and url == self.root_url:
                return None

            if isinstance(self.root_url, list) and url in self.root_url:
                return None

        with self.adhoc_connector_lock:
            if to_cell in self.adhoc_connectors:
                return self.adhoc_connectors[to_cell]

            connector = self.connector_manager.get_external_connector(url, adhoc=True)
            self.adhoc_connectors[to_cell] = connector
            if connector:
                self.logger.info(
                    f"{self.my_info.fqcn}: created adhoc connector {connector.handle} to {url} on {to_cell}"
                )
            else:
                self.logger.info(f"{self.my_info.fqcn}: cannot create adhoc connector to {url} on {to_cell}")
            return connector

    def _create_internal_listener(self):
        # internal listener is always backbone
        if not self.int_listener:
            self.int_listener = self.connector_manager.get_internal_listener()
            if self.int_listener:
                self.logger.info(
                    f"{self.my_info.fqcn}: created backbone internal listener "
                    f"for {self.int_listener.get_connection_url()}"
                )
            else:
                raise RuntimeError(f"{self.my_info.fqcn}: cannot create backbone internal listener")
        return self.int_listener

    def _create_external_listener(self, url: str):
        adhoc = len(url) == 0
        if adhoc and not self.connector_manager.adhoc_allowed:
            return None

        with self.ext_listener_lock:
            if url:
                listener = self.ext_listeners.get(url)
                if listener:
                    return listener
            elif len(self.ext_listeners) > 0:
                # no url specified - just pick one if any
                k = random.choice(list(self.ext_listeners))
                return self.ext_listeners[k]

            listener = None
            if not self.ext_listener_impossible:
                self.logger.debug(f"{self.my_info.fqcn}: trying create ext listener: url={url}")
                listener = self.connector_manager.get_external_listener(url, adhoc)
                if listener:
                    if not adhoc:
                        self.logger.info(f"{self.my_info.fqcn}: created backbone external listener for {url}")
                    else:
                        self.logger.info(
                            f"{self.my_info.fqcn}: created adhoc external listener {listener.handle} "
                            f"for {listener.get_connection_url()}"
                        )
                    self.ext_listeners[listener.get_connection_url()] = listener
                else:
                    if not adhoc:
                        raise RuntimeError(
                            f"{os.getpid()}: {self.my_info.fqcn}: "
                            f"cannot create backbone external listener for {url}"
                        )
                    else:
                        self.logger.warning(f"{self.my_info.fqcn}: cannot create adhoc external listener")
                    self.ext_listener_impossible = True
            return listener

    def _create_bb_external_connector(self):
        self.logger.debug(f"{self.my_info.fqcn}: creating connector to {self.root_url}")
        self.bb_ext_connector = self.connector_manager.get_external_connector(self.root_url, False)
        if self.bb_ext_connector:
            self.logger.info(f"{self.my_info.fqcn}: created backbone external connector to {self.root_url}")
        else:
            raise RuntimeError(f"{self.my_info.fqcn}: cannot create backbone external connector to {self.root_url}")

    def _create_internal_connector(self, url: str):
        self.bb_int_connector = self.connector_manager.get_internal_connector(url)
        if self.bb_int_connector:
            self.logger.info(f"{self.my_info.fqcn}: created backbone internal connector to {url} on parent")
        else:
            raise RuntimeError(f"{self.my_info.fqcn}: cannot create backbone internal connector to {url} on parent")

    def set_cell_connected_cb(self, cb, *args, **kwargs):
        """
        Set a callback that is called when an external cell is connected.

        Args:
            cb: the callback function. It must follow the signature of cell_connected_cb_signature.
            *args: args to be passed to the cb.
            **kwargs: kwargs to be passed to the cb

        Returns: None

        """
        if not callable(cb):
            raise ValueError(f"specified cell_connected_cb {type(cb)} is not callable")
        self.cell_connected_cb = cb
        self.cell_connected_cb_args = args
        self.cell_connected_cb_kwargs = kwargs

    def set_cell_disconnected_cb(self, cb, *args, **kwargs):
        """
        Set a callback that is called when an external cell is disconnected.

        Args:
            cb: the callback function. It must follow the signature of cell_disconnected_cb_signature.
            *args: args to be passed to the cb.
            **kwargs: kwargs to be passed to the cb

        Returns: None

        """
        if not callable(cb):
            raise ValueError(f"specified cell_disconnected_cb {type(cb)} is not callable")
        self.cell_disconnected_cb = cb
        self.cell_disconnected_cb_args = args
        self.cell_disconnected_cb_kwargs = kwargs

    def set_message_interceptor(self, cb, *args, **kwargs):
        """
        Set a callback that is called when a message is received or forwarded.

        Args:
            cb: the callback function. It must follow the signature of message_interceptor_signature.
            *args: args to be passed to the cb.
            **kwargs: kwargs to be passed to the cb

        Returns: None

        """
        if not callable(cb):
            raise ValueError(f"specified message_interceptor {type(cb)} is not callable")

        self.message_interceptor = cb
        self.message_interceptor_args = args
        self.message_interceptor_kwargs = kwargs

    def start(self):
        """
        Start the cell after it is fully set up (connectors and listeners are added, CBs are set up)

        Returns:

        """
        if self.my_info.is_on_server:
            if self.my_info.is_root:
                self._set_bb_for_server_root()
            else:
                self._set_bb_for_server_child(self.parent_url, self.create_internal_listener)
        else:
            # client side
            if self.my_info.is_root:
                self._set_bb_for_client_root()
            else:
                self._set_bb_for_client_child(self.parent_url, self.create_internal_listener)

        self.bulk_checker.start()
        self.bulk_processor.start()
        self.communicator.start()
        self.running = True

    def stop(self):
        """
        Cleanup the cell. Once the cell is stopped, it won't be able to send/receive messages.

        Returns:

        """
        if not self.running:
            return

        # notify peers that I am gone
        with self.agent_lock:
            if self.agents:
                targets = [peer_name for peer_name in self.agents.keys()]
                self.logger.debug(f"broadcasting goodbye to {targets}")
                self.broadcast_request(
                    channel=_CHANNEL, topic=_TOPIC_BYE, targets=targets, request=new_message(), timeout=1.0
                )

        self.logger.debug(f"{self.my_info.fqcn}: Closing Cell")
        self.running = False
        self.asked_to_stop = True
        self.bulk_checker.join()
        self.bulk_processor.join()

        try:
            # we can now stop the communicator
            self.communicator.stop()
        except Exception as ex:
            self.logger.error(f"{self.my_info.fqcn}: error stopping Communicator: {ex}")
            self.logger.debug(traceback.format_exc())

        self.logger.debug(f"{self.my_info.fqcn}: CELL closed!")

    def register_request_cb(self, channel: str, topic: str, cb, *args, **kwargs):
        """
        Register a callback for handling request. The CB must follow request_cb_signature.

        Args:
            channel: the channel of the request
            topic: topic of the request
            cb:
            *args:
            **kwargs:

        Returns:

        """
        if not callable(cb):
            raise ValueError(f"specified request_cb {type(cb)} is not callable")
        self.req_reg.set(channel, topic, _CB(cb, args, kwargs))

    def add_incoming_request_filter(self, channel: str, topic: str, cb, *args, **kwargs):
        if not callable(cb):
            raise ValueError(f"specified incoming_request_filter {type(cb)} is not callable")
        self.in_req_filter_reg.append(channel, topic, _CB(cb, args, kwargs))

    def add_outgoing_reply_filter(self, channel: str, topic: str, cb, *args, **kwargs):
        if not callable(cb):
            raise ValueError(f"specified outgoing_reply_filter {type(cb)} is not callable")
        self.out_reply_filter_reg.append(channel, topic, _CB(cb, args, kwargs))

    def add_outgoing_request_filter(self, channel: str, topic: str, cb, *args, **kwargs):
        if not callable(cb):
            raise ValueError(f"specified outgoing_request_filter {type(cb)} is not callable")
        self.out_req_filter_reg.append(channel, topic, _CB(cb, args, kwargs))

    def add_incoming_reply_filter(self, channel: str, topic: str, cb, *args, **kwargs):
        if not callable(cb):
            raise ValueError(f"specified incoming_reply_filter {type(cb)} is not callable")
        self.in_reply_filter_reg.append(channel, topic, _CB(cb, args, kwargs))

    def add_error_handler(self, channel: str, topic: str, cb, *args, **kwargs):
        if not callable(cb):
            raise ValueError(f"specified error_handler {type(cb)} is not callable")
        self.error_handler_reg.set(channel, topic, _CB(cb, args, kwargs))

    def _filter_outgoing_request(self, channel: str, topic: str, request: Message) -> Union[None, Message]:
        cbs = self.out_req_filter_reg.find(channel, topic)
        if not cbs:
            return None
        for _cb in cbs:
            assert isinstance(_cb, _CB)
            reply = self._try_cb(request, _cb.cb, *_cb.args, **_cb.kwargs)
            if reply:
                return reply

    def _try_path(self, fqcn_path: List[str]) -> Union[None, Endpoint]:
        self.logger.debug(f"{self.my_info.fqcn}: trying path {fqcn_path} ...")
        target = FQCN.join(fqcn_path)
        agent = self.agents.get(target, None)
        if agent:
            # there is a direct path to the target call
            self.logger.debug(f"{self.my_info.fqcn}: got cell agent for {target}")
            return agent.endpoint
        else:
            self.logger.debug(f"{self.my_info.fqcn}: no CellAgent for {target}")

        if len(fqcn_path) == 1:
            return None
        return self._try_path(fqcn_path[:-1])

    def _find_endpoint(self, target_fqcn: str) -> (str, Union[None, Endpoint]):
        err = FQCN.validate(target_fqcn)
        if err:
            self.logger.error(f"{self.my_info.fqcn}: invalid target FQCN '{target_fqcn}': {err}")
            return ReturnCode.INVALID_TARGET, None

        try:
            ep = self._try_find_ep(target_fqcn)
            if not ep:
                return ReturnCode.TARGET_UNREACHABLE, None
            return "", ep
        except Exception as ex:
            self.logger.error(f"Error when finding {target_fqcn}: {ex}")
            self.logger.debug(traceback.format_exc())
            return ReturnCode.TARGET_UNREACHABLE, None

    def _try_find_ep(self, target_fqcn: str) -> Union[None, Endpoint]:
        self.logger.debug(f"{self.my_info.fqcn}: finding path to {target_fqcn}")
        if target_fqcn == self.my_info.fqcn:
            # sending request to myself? Not allowed!
            # self.logger.error(f"{self.my_info.fqcn}: sending message to self is not allowed")
            # return None
            return self.endpoint

        target_info = FqcnInfo(target_fqcn)

        # is there a direct path to the target?
        agent = self.agents.get(target_fqcn)
        if agent:
            return agent.endpoint

        if same_family(self.my_info, target_info):
            if FQCN.is_parent(self.my_info.fqcn, target_fqcn):
                self.logger.error(f"{self.my_info.fqcn}: backbone broken: no path to child {target_fqcn}")
                return None
            elif FQCN.is_parent(target_fqcn, self.my_info.fqcn):
                self.logger.error(f"{self.my_info.fqcn}: backbone broken: no path to parent {target_fqcn}")

            self.logger.debug(f"{self.my_info.fqcn}: find path in the same family")
            if FQCN.is_ancestor(self.my_info.fqcn, target_fqcn):
                # I am the ancestor of the target
                self.logger.debug(f"{self.my_info.fqcn}: I'm ancestor of the target {target_fqcn}")
                return self._try_path(target_info.path)
            else:
                # target is my ancestor, or we share the same ancestor - go to my parent!
                self.logger.debug(f"{self.my_info.fqcn}: target {target_fqcn} is or share my ancestor")
                parent_fqcn = FQCN.get_parent(self.my_info.fqcn)
                agent = self.agents.get(parent_fqcn)
                if not agent:
                    self.logger.error(f"{self.my_info.fqcn}: broken backbone - no path to parent {parent_fqcn}")
                    return None
                return agent.endpoint

        # not the same family
        ep = self._try_path(target_info.path)
        if ep:
            return ep

        # cannot find path to the target
        # try the server root
        # we assume that all client roots connect to the server root.
        root_agent = self.agents.get(FQCN.ROOT_SERVER)
        if root_agent:
            return root_agent.endpoint

        # no direct path to the server root
        # let my parent handle it if I have a parent
        if self.my_info.gen > 1:
            parent_fqcn = FQCN.get_parent(self.my_info.fqcn)
            agent = self.agents.get(parent_fqcn)
            if not agent:
                self.logger.error(f"{self.my_info.fqcn}: broken backbone - no path to parent {parent_fqcn}")
                return None
            return agent.endpoint

        self.logger.error(f"{self.my_info.fqcn}: cannot find path to {target_fqcn}")
        return None

    def _send_to_endpoint(self, to_endpoint: Endpoint, message: Message) -> str:
        err = ""
        try:
            encode_payload(message)
            message.set_header(MessageHeaderKey.SEND_TIME, time.time())
            if not message.payload:
                msg_size = 0
            else:
                msg_size = len(message.payload)

            if msg_size > self.max_msg_size:
                err_text = f"message is too big ({msg_size} > {self.max_msg_size}"
                self.logger.error(err_text)
                err = ReturnCode.MSG_TOO_BIG
            else:
                self.communicator.send(to_endpoint, Cell.APP_ID, message)
                self.sent_msg_size_pool.record_value(
                    category=self._stats_category(message), value=self._msg_size_mbs(message)
                )
        except Exception as ex:
            err_text = f"Failed to send message to {to_endpoint.name}: {ex}"
            self.logger.error(err_text)
            self.logger.debug(traceback.format_exc())
            err = ReturnCode.COMM_ERROR
        return err

    def _send_target_messages(
        self,
        target_msgs: Dict[str, TargetMessage],
    ) -> Dict[str, str]:
        if not self.running:
            raise RuntimeError("Messenger is not running")

        send_errs = {}
        reachable_targets = {}  # target fqcn => endpoint
        for t in target_msgs.keys():
            err, ep = self._find_endpoint(t)
            if ep:
                reachable_targets[t] = ep
            else:
                self.logger.error(f"{self.my_info.fqcn}: cannot send to '{t}': {err}")
                send_errs[t] = err

        for t, ep in reachable_targets.items():
            tm = target_msgs[t]
            req = Message(headers=copy.copy(tm.message.headers), payload=tm.message.payload)

            req.add_headers(
                {
                    MessageHeaderKey.CHANNEL: tm.channel,
                    MessageHeaderKey.TOPIC: tm.topic,
                    MessageHeaderKey.ORIGIN: self.my_info.fqcn,
                    MessageHeaderKey.FROM_CELL: self.my_info.fqcn,
                    MessageHeaderKey.MSG_TYPE: MessageType.REQ,
                    MessageHeaderKey.ROUTE: [(self.my_info.fqcn, time.time())],
                    MessageHeaderKey.DESTINATION: t,
                    MessageHeaderKey.TO_CELL: ep.name,
                }
            )

            # invoke outgoing req filters
            req_filters = self.out_req_filter_reg.find(tm.channel, tm.topic)
            if req_filters:
                self.logger.debug(f"{self.my_info.fqcn}: invoking outgoing request filters")
                assert isinstance(req_filters, list)
                for f in req_filters:
                    assert isinstance(f, _CB)
                    r = self._try_cb(req, f.cb, *f.args, **f.kwargs)
                    if r:
                        send_errs[t] = ReturnCode.FILTER_ERROR
                        break
                if send_errs.get(t):
                    # process next target
                    continue

            # is this a direct path?
            ti = FqcnInfo(t)
            allow_adhoc = self.connector_manager.is_adhoc_allowed(ti, self.my_info)
            if allow_adhoc and t != ep.name:
                # Not a direct path since the destination and the next leg are not the same
                if not ti.is_on_server and (self.my_info.is_on_server or self.my_info.fqcn > t):
                    # try to get or create a listener and let the peer know the endpoint
                    listener = self._create_external_listener("")
                    if listener:
                        conn_url = listener.get_connection_url()
                        req.set_header(MessageHeaderKey.CONN_URL, conn_url)
            err = self._send_to_endpoint(ep, req)
            if err:
                self.logger.error(f"{self.my_info.fqcn}: failed to send to endpoint {ep.name}: {err}")
            else:
                self.sent_msg_counter_pool.increment(category=self._stats_category(req), counter_name=_CounterName.SENT)
            send_errs[t] = err
        return send_errs

    def _send_to_targets(
        self,
        channel: str,
        topic: str,
        targets: Union[str, List[str]],
        message: Message,
    ) -> Dict[str, str]:
        if isinstance(targets, str):
            targets = [targets]
        target_msgs = {}
        for t in targets:
            target_msgs[t] = TargetMessage(t, channel, topic, message)
        return self._send_target_messages(target_msgs)

    def send_request(self, channel: str, topic: str, target: str, request: Message, timeout=None) -> Message:
        self.logger.debug(f"{self.my_info.fqcn}: sending request {channel}:{topic} to {target}")
        result = self.broadcast_request(channel, topic, [target], request, timeout)
        assert isinstance(result, dict)
        return result.get(target)

    def broadcast_multi_requests(self, target_msgs: Dict[str, TargetMessage], timeout=None) -> Dict[str, Message]:
        """
        This is the core of the request/response handling. Be extremely careful when making any changes!
        To maximize the communication efficiency, we avoid the use of locks.
        We use a waiter implemented as a Python threading.Event object.
        We create the waiter, send out messages, set up default responses, and set it up to wait for response.
        Once the waiter is triggered from a reply-receiving thread, we process received results.

        HOWEVER, if the network is extremely fast, the response may already be received even before we finish setting
        up the waiter in this thread!

        We had a very mysterious bug that caused a request to be treated as timeout even though the reply is received.
        It was both threads try to set values to "waiter.replies". In case of extremely fast network, the reply
        processing thread set the reply to "waiter.replies", and then overwritten by this thread with a default timeout
        reply.

        To avoid this kind of problems, we now use two sets of values in the waiter object.
        One set is for this thread: targets
        Another set is for the reply processing thread: received_replies, reply_time

        Args:
            target_msgs: messages to be sent
            timeout: timeout value

        Returns: a dict of: target name => reply message

        """
        targets = [t for t in target_msgs]
        self.logger.debug(f"{self.my_info.fqcn}: broadcasting to {targets} ...")
        waiter = _Waiter(targets)
        if waiter.id in self.waiters:
            raise RuntimeError("waiter not unique!")
        self.waiters[waiter.id] = waiter
        now = time.time()
        if not timeout:
            timeout = self.max_timeout
        result = {}
        try:
            for _, tm in target_msgs.items():
                request = tm.message
                request.add_headers(
                    {
                        MessageHeaderKey.REQ_ID: waiter.id,
                        MessageHeaderKey.REPLY_EXPECTED: True,
                        MessageHeaderKey.WAIT_UNTIL: time.time() + timeout,
                    }
                )
            send_errs = self._send_target_messages(target_msgs)
            send_count = 0
            timeout_reply = make_reply(ReturnCode.TIMEOUT)

            # NOTE: it is possible that reply is already received and the waiter is triggered by now!
            # if waiter.received_replies:
            #     self.logger.info(f"{self.my_info.fqcn}: the network is extremely fast - response already received!")

            for t, err in send_errs.items():
                if not err:
                    send_count += 1
                    result[t] = timeout_reply
                else:
                    result[t] = make_reply(rc=err)
                    waiter.reply_time[t] = now

            if send_count > 0:
                self.num_sar_reqs += 1
                num_reqs = len(self.waiters)
                if self.req_hw < num_reqs:
                    self.req_hw = num_reqs

                # wait for reply
                self.logger.debug(f"{self.my_info.fqcn}: set up waiter {waiter.id} to wait for {timeout} secs")
                if not waiter.wait(timeout=timeout):
                    # timeout
                    self.logger.error(f"{self.my_info.fqcn}: timeout on REQ {waiter.id} after {timeout} secs")
                    with self.stats_lock:
                        self.num_timeout_reqs += 1
        except Exception as ex:
            raise ex
        finally:
            self.waiters.pop(waiter.id, None)
            self.logger.debug(f"released waiter on REQ {waiter.id}")
        if waiter.received_replies:
            result.update(waiter.received_replies)
        for t, reply in result.items():
            rc = reply.get_header(MessageHeaderKey.RETURN_CODE, ReturnCode.OK)
            self.sent_msg_counter_pool.increment(category=self._stats_category(reply), counter_name=rc)
        return result

    def broadcast_request(
        self, channel: str, topic: str, targets: Union[str, List[str]], request: Message, timeout=None
    ) -> Dict[str, Message]:
        """
        Send a message over a channel to specified destination cell(s), and wait for reply

        Args:
            channel: channel for the message
            topic: topic of the message
            targets: FQCN of the destination cell(s)
            request: message to be sent
            timeout: how long to wait for replies

        Returns: a dict of: cell_id => reply message

        """
        if isinstance(targets, str):
            targets = [targets]
        target_msgs = {}
        for t in targets:
            target_msgs[t] = TargetMessage(t, channel, topic, request)
        return self.broadcast_multi_requests(target_msgs, timeout)

    def fire_and_forget(
        self, channel: str, topic: str, targets: Union[str, List[str]], message: Message
    ) -> Dict[str, str]:
        """
        Send a message over a channel to specified destination cell(s), and do not wait for replies.

        Args:
            channel: channel for the message
            topic: topic of the message
            targets: one or more destination cell IDs. None means all.
            message: message to be sent

        Returns: None

        """
        message.add_headers({MessageHeaderKey.REPLY_EXPECTED: False})
        return self._send_to_targets(channel, topic, targets, message)

    def queue_message(self, channel: str, topic: str, targets: Union[str, List[str]], message: Message):
        if isinstance(targets, str):
            targets = [targets]

        with self.bulk_lock:
            for t in targets:
                sender = self.bulk_senders.get(t)
                if not sender:
                    sender = _BulkSender(cell=self, target=t, max_queue_size=self.max_bulk_size)
                    self.bulk_senders[t] = sender
                sender.queue_message(channel=channel, topic=topic, message=message)
                self.logger.debug(f"{self.get_fqcn()}: queued msg for {t}")

    def _peer_goodbye(self, request: Message):
        peer_ep = request.get_prop(MessagePropKey.ENDPOINT)
        if not peer_ep:
            self.logger.error(f"{self.my_info.fqcn}: no endpoint prop in message")
            return

        assert isinstance(peer_ep, Endpoint)
        with self.agent_lock:
            self.logger.debug(f"{self.my_info.fqcn}: got goodbye from cell {peer_ep.name}")
            ep = self.agents.pop(peer_ep.name, None)
            if ep:
                self.logger.debug(f"{self.my_info.fqcn}: removed agent for {peer_ep.name}")
            else:
                self.logger.debug(f"{self.my_info.fqcn}: agent for {peer_ep.name} is already gone")

        # ack back
        return new_message()

    def _receive_bulk_message(self, request: Message):
        target_msgs = request.payload
        assert isinstance(target_msgs, list)
        with self.bulk_msg_lock:
            self.bulk_messages.append(request)
            self.logger.debug(f"{self.get_fqcn()}: received bulk msg. Pending size {len(self.bulk_messages)}")

    def _process_bulk_messages(self):
        self.logger.debug(f"{self.get_fqcn()}: processing bulks ...")
        while not self.asked_to_stop:
            self._process_pending_bulks()
            time.sleep(self.bulk_process_interval)

        # process remaining messages if any
        self._process_pending_bulks()

    def _process_pending_bulks(self):
        while True:
            with self.bulk_msg_lock:
                if not self.bulk_messages:
                    return
                bulk = self.bulk_messages.pop(0)
            self._process_one_bulk(bulk)

    def _process_one_bulk(self, bulk_request: Message):
        target_msgs = bulk_request.payload
        assert isinstance(target_msgs, list)
        self.logger.debug(f"{self.get_fqcn()}: processing one bulk size {len(target_msgs)}")
        for tmd in target_msgs:
            assert isinstance(tmd, dict)
            tm = TargetMessage.from_dict(tmd)
            assert isinstance(tm, TargetMessage)
            req = tm.message
            req.add_headers(bulk_request.headers)
            req.add_headers({MessageHeaderKey.TOPIC: tm.topic, MessageHeaderKey.CHANNEL: tm.channel})
            origin = bulk_request.get_header(MessageHeaderKey.ORIGIN, "")
            self.logger.debug(f"{self.get_fqcn()}: bulk item: {req.headers}")
            self._process_request(origin=origin, message=req)

    def fire_multi_requests_and_forget(self, target_msgs: Dict[str, TargetMessage]) -> Dict[str, str]:
        for _, tm in target_msgs.items():
            request = tm.message
            request.add_headers(
                {
                    MessageHeaderKey.REPLY_EXPECTED: False,
                }
            )
        return self._send_target_messages(target_msgs)

    def send_reply(self, reply: Message, to_cell: str, for_req_ids: List[str]) -> str:
        """
        Send a reply to respond to one or more requests.
        This is useful if the request receiver needs to delay its reply as follows:
        - When a request is received, if it's not ready to reply (e.g. waiting for additional requests from
         other cells), simply remember the REQ_ID and returns None;
        - The receiver may queue up multiple such requests
        - When ready, call this method to send the reply for all the queued requests

        Args:
            reply:
            to_cell:
            for_req_ids:

        Returns: an error message if any

        """
        reply.add_headers(
            {
                MessageHeaderKey.FROM_CELL: self.my_info.fqcn,
                MessageHeaderKey.ORIGIN: self.my_info.fqcn,
                MessageHeaderKey.ROUTE: [(self.my_info.fqcn, time.time())],
                MessageHeaderKey.DESTINATION: to_cell,
                MessageHeaderKey.REQ_ID: for_req_ids,
                MessageHeaderKey.MSG_TYPE: MessageType.REPLY,
            }
        )

        err, ep = self._find_endpoint(to_cell)
        if err:
            return err
        reply.set_header(MessageHeaderKey.TO_CELL, ep.name)
        return self._send_to_endpoint(ep, reply)

    def _try_cb(self, message, cb, *args, **kwargs):
        try:
            self.logger.debug(f"{self.my_info.fqcn}: calling CB {cb.__name__}")
            return cb(message, *args, **kwargs)
        except ServiceUnavailable:
            return make_reply(ReturnCode.SERVICE_UNAVAILABLE)
        except InvalidSession:
            return make_reply(ReturnCode.INVALID_SESSION)
        except InvalidRequest:
            return make_reply(ReturnCode.INVALID_REQUEST)
        except AuthenticationError:
            return make_reply(ReturnCode.AUTHENTICATION_ERROR)
        except AbortRun:
            return make_reply(ReturnCode.ABORT_RUN)
        except Exception as ex:
            self.logger.error(f"{self.my_info.fqcn}: exception from CB {cb.__name__}: {ex}")
            self.logger.debug(traceback.format_exc())
            return make_reply(ReturnCode.PROCESS_EXCEPTION)

    def process_message(self, endpoint: Endpoint, connection: Connection, app_id: int, message: Message):
        # this is the receiver callback
        try:
            self._process_received_msg(endpoint, connection, message)
        except Exception as ex:
            self.logger.error(f"Error processing received message: {ex}")
            self.logger.debug(traceback.format_exc())

    def _process_request(self, origin: str, message: Message) -> Union[None, Message]:
        self.logger.debug(f"{self.my_info.fqcn}: processing incoming request")
        decode_payload(message)
        # this is a request for me - dispatch to the right CB
        channel = message.get_header(MessageHeaderKey.CHANNEL, "")
        topic = message.get_header(MessageHeaderKey.TOPIC, "")
        _cb = self.req_reg.find(channel, topic)
        if not _cb:
            self.logger.error(f"{self.my_info.fqcn}: no callback for request ({topic}@{channel}) from cell '{origin}'")
            return make_reply(ReturnCode.PROCESS_EXCEPTION, error="no callback")

        # invoke incoming request filters
        req_filters = self.in_req_filter_reg.find(channel, topic)
        if req_filters:
            self.logger.debug(f"{self.my_info.fqcn}: invoking incoming request filters")
            assert isinstance(req_filters, list)
            for f in req_filters:
                assert isinstance(f, _CB)
                reply = self._try_cb(message, f.cb, *f.args, **f.kwargs)
                if reply:
                    return reply

        assert isinstance(_cb, _CB)
        self.logger.debug(f"{self.my_info.fqcn}: calling registered request CB")
        cb_start = time.perf_counter()
        reply = self._try_cb(message, _cb.cb, *_cb.args, **_cb.kwargs)
        cb_end = time.perf_counter()
        self.req_cb_stats_pool.record_value(category=self._stats_category(message), value=cb_end - cb_start)
        if not reply:
            # the CB doesn't have anything to reply
            self.logger.debug("no reply is returned from the CB")
            return None

        if not isinstance(reply, Message):
            self.logger.error(
                f"{self.my_info.fqcn}: bad result from request CB for topic {topic} on channel {channel}: "
                f"expect Message but got {type(reply)}"
            )
            reply = make_reply(ReturnCode.PROCESS_EXCEPTION, error="bad cb result")
        return reply

    def _add_to_route(self, message: Message):
        route = message.get_header(MessageHeaderKey.ROUTE, None)
        if not route:
            route = []
            message.set_header(MessageHeaderKey.ROUTE, route)
        if not isinstance(route, list):
            self.logger.error(
                format_log_message(self.my_info.fqcn, message, "bad route header: expect list but got {type(route)}")
            )
        else:
            route.append((self.my_info.fqcn, time.time()))

    def _forward(self, endpoint: Endpoint, origin: str, destination: str, msg_type: str, message: Message):
        # not for me - need to forward it
        self.logger.debug(f"{self.my_info.fqcn}: forwarding for {origin} to {destination}")
        err, ep = self._find_endpoint(destination)
        if ep:
            self.logger.debug(f"{self.my_info.fqcn}: found next leg {ep.name}")
            message.add_headers({MessageHeaderKey.FROM_CELL: self.my_info.fqcn, MessageHeaderKey.TO_CELL: ep.name})
            self._add_to_route(message)
            err = self._send_to_endpoint(to_endpoint=ep, message=message)
            if not err:
                self.logger.debug(f"{self.my_info.fqcn}: forwarded successfully!")
                return
            else:
                self.logger.error(
                    format_log_message(self.my_info.fqcn, message, f"failed to forward {msg_type}: {err}")
                )
        else:
            # cannot find next leg endpoint
            self.logger.error(format_log_message(self.my_info.fqcn, message, f"cannot forward {msg_type}: no path"))

        if msg_type == MessageType.REQ:
            reply_expected = message.get_header(MessageHeaderKey.REPLY_EXPECTED, False)
            if not reply_expected:
                self.logger.debug(f"{self.my_info.fqcn}: can't forward: drop the message since reply is not expected")
                return

            wait_until = message.get_header(MessageHeaderKey.WAIT_UNTIL, None)
            if isinstance(wait_until, float) and time.time() > wait_until:
                # no need to reply since peer already gave up waiting by now
                self.logger.debug(f"{self.my_info.fqcn}: can't forward: drop the message since too late")
                return

            # tell the requester that message couldn't be delivered
            req_id = message.get_header(MessageHeaderKey.REQ_ID, "")
            reply = make_reply(ReturnCode.COMM_ERROR, error="cannot forward")
            reply.add_headers(
                {
                    MessageHeaderKey.ORIGINAL_HEADERS: message.headers,
                    MessageHeaderKey.FROM_CELL: self.my_info.fqcn,
                    MessageHeaderKey.TO_CELL: endpoint.name,
                    MessageHeaderKey.ORIGIN: self.my_info.fqcn,
                    MessageHeaderKey.DESTINATION: origin,
                    MessageHeaderKey.REQ_ID: [req_id],
                    MessageHeaderKey.MSG_TYPE: MessageType.RETURN,
                    MessageHeaderKey.ROUTE: [(self.my_info.fqcn, time.time())],
                    MessageHeaderKey.RETURN_REASON: ReturnReason.CANT_FORWARD,
                }
            )
            self._send_to_endpoint(endpoint, reply)
            self.logger.debug(f"{self.my_info.fqcn}: sent RETURN message back to {endpoint.name}")
        else:
            # msg_type is either RETURN or REPLY - drop it.
            self.logger.warning(format_log_message(self.my_info.fqcn, message, "dropped forwarded message"))

    @staticmethod
    def _stats_category(message: Message):
        channel = message.get_header(MessageHeaderKey.CHANNEL, "")
        topic = message.get_header(MessageHeaderKey.TOPIC, "")
        return f"{channel}:{topic}"

    def _process_reply(self, origin: str, message: Message, msg_type: str):
        channel = message.get_header(MessageHeaderKey.CHANNEL, "")
        topic = message.get_header(MessageHeaderKey.TOPIC, "")
        now = time.time()
        self.logger.debug(f"{self.my_info.fqcn}: processing reply from {origin} for type {msg_type}")
        decode_payload(message)

        req_ids = message.get_header(MessageHeaderKey.REQ_ID)
        if not req_ids:
            raise RuntimeError(format_log_message(self.my_info.fqcn, message, "reply does not have REQ_ID header"))

        if isinstance(req_ids, str):
            req_ids = [req_ids]

        if not isinstance(req_ids, list):
            raise RuntimeError(
                format_log_message(self.my_info.fqcn, message, f"REQ_ID must be list of ids but got {type(req_ids)}")
            )

        req_destination = origin
        if msg_type == MessageType.RETURN:
            self.logger.error(f"{self.my_info.fqcn}: got a RETURN!")
            self.sent_msg_counter_pool.increment(
                category=self._stats_category(message), counter_name=_CounterName.RETURN
            )
            original_headers = message.get_header(MessageHeaderKey.ORIGINAL_HEADERS, None)
            if not original_headers:
                raise RuntimeError(
                    format_log_message(self.my_info.fqcn, message, "missing ORIGINAL_HEADERS in returned message!")
                )
            req_destination = original_headers.get(MessageHeaderKey.DESTINATION, None)
            if not req_destination:
                raise RuntimeError(
                    format_log_message(self.my_info.fqcn, message, "missing DESTINATION header in original headers")
                )
        else:
            # invoking incoming reply filter
            reply_filters = self.in_reply_filter_reg.find(channel, topic)
            if reply_filters:
                self.logger.debug(f"{self.my_info.fqcn}: invoking incoming reply filters")
                assert isinstance(reply_filters, list)
                for f in reply_filters:
                    assert isinstance(f, _CB)
                    self._try_cb(message, f.cb, *f.args, **f.kwargs)

        for rid in req_ids:
            waiter = self.waiters.get(rid, None)
            if waiter:
                assert isinstance(waiter, _Waiter)
                if req_destination not in waiter.targets:
                    self.logger.error(
                        format_log_message(
                            self.my_info.fqcn, message, f"unexpected reply for {rid} from {req_destination}"
                        )
                    )
                    self.logger.error(f"req_destination='{req_destination}', expecting={waiter.targets}")
                    return
                waiter.received_replies[req_destination] = message
                waiter.reply_time[req_destination] = now
                time_taken = now - waiter.send_time

                self.msg_stats_pool.record_value(category=self._stats_category(message), value=time_taken)

                # all targets replied?
                all_targets_replied = True
                for t in waiter.targets:
                    if not waiter.reply_time.get(t):
                        all_targets_replied = False
                        break

                if all_targets_replied:
                    self.logger.debug(
                        format_log_message(
                            self.my_info.fqcn,
                            message,
                            f"trigger waiter - replies received from {len(waiter.targets)} targets for {rid}",
                        )
                    )
                    waiter.set()  # trigger the waiting requests!
                else:
                    self.logger.debug(
                        format_log_message(
                            self.my_info.fqcn,
                            message,
                            f"waiting - replies not received from {len(waiter.targets)} targets for req {rid}",
                        )
                    )
            else:
                self.logger.error(
                    format_log_message(self.my_info.fqcn, message, f"no waiter for req {rid} - the reply is too late")
                )
                self.sent_msg_counter_pool.increment(
                    category=self._stats_category(message), counter_name=_CounterName.LATE
                )

    @staticmethod
    def _msg_size_mbs(message: Message):
        if message.payload:
            msg_size = len(message.payload)
        else:
            msg_size = 0
        return msg_size / _ONE_MB

    def _process_received_msg(self, endpoint: Endpoint, connection: Connection, message: Message):
        route = message.get_header(MessageHeaderKey.ROUTE)
        if route:
            origin_name = route[0][0]
            t0 = route[0][1]
            time_taken = time.time() - t0
            self.msg_travel_stats_pool.record_value(
                category=f"{origin_name}#{self._stats_category(message)}", value=time_taken
            )

        self.logger.debug(f"{self.my_info.fqcn}: received message: {message.headers}")
        message.set_prop(MessagePropKey.ENDPOINT, endpoint)

        conn_props = connection.get_conn_properties()
        cn = conn_props.get(DriverParams.PEER_CN.value)
        if cn:
            message.set_prop(MessagePropKey.COMMON_NAME, cn)

        msg_type = message.get_header(MessageHeaderKey.MSG_TYPE)
        if not msg_type:
            raise RuntimeError(format_log_message(self.my_info.fqcn, message, "missing MSG_TYPE in received message"))

        origin = message.get_header(MessageHeaderKey.ORIGIN)
        if not origin:
            raise RuntimeError(
                format_log_message(self.my_info.fqcn, message, "missing ORIGIN header in received message")
            )

        # is this msg for me?
        destination = message.get_header(MessageHeaderKey.DESTINATION)
        if not destination:
            raise RuntimeError(
                format_log_message(self.my_info.fqcn, message, "missing DESTINATION header in received message")
            )

        self.received_msg_counter_pool.increment(
            category=self._stats_category(message), counter_name=_CounterName.RECEIVED
        )

        if msg_type == MessageType.REQ and self.message_interceptor is not None:
            reply = self._try_cb(
                message, self.message_interceptor, *self.message_interceptor_args, **self.message_interceptor_kwargs
            )

            if reply:
                self.logger.debug(f"{self.my_info.fqcn}: interceptor stopped message!")
                reply_expected = message.get_header(MessageHeaderKey.REPLY_EXPECTED)
                if not reply_expected:
                    return

                req_id = message.get_header(MessageHeaderKey.REQ_ID, "")
                reply.add_headers(
                    {
                        MessageHeaderKey.ORIGINAL_HEADERS: message.headers,
                        MessageHeaderKey.FROM_CELL: self.my_info.fqcn,
                        MessageHeaderKey.TO_CELL: endpoint.name,
                        MessageHeaderKey.ORIGIN: self.my_info.fqcn,
                        MessageHeaderKey.DESTINATION: origin,
                        MessageHeaderKey.REQ_ID: [req_id],
                        MessageHeaderKey.MSG_TYPE: MessageType.RETURN,
                        MessageHeaderKey.ROUTE: [(self.my_info.fqcn, time.time())],
                        MessageHeaderKey.RETURN_REASON: ReturnReason.INTERCEPT,
                    }
                )
                self._send_reply(reply, endpoint)
                self.logger.debug(f"{self.my_info.fqcn}: returned intercepted message")
                return

        if destination != self.my_info.fqcn:
            # not for me - need to forward it
            self.sent_msg_counter_pool.increment(
                category=self._stats_category(message), counter_name=_CounterName.FORWARD
            )
            self.received_msg_counter_pool.increment(
                category=self._stats_category(message), counter_name=_CounterName.FORWARD
            )
            self._forward(endpoint, origin, destination, msg_type, message)
            return

        self.received_msg_size_pool.record_value(
            category=self._stats_category(message), value=self._msg_size_mbs(message)
        )

        # this message is for me
        self._add_to_route(message)

        # handle ad-hoc
        my_conn_url = None
        if msg_type in [MessageType.REQ, MessageType.REPLY]:
            from_cell = message.get_header(MessageHeaderKey.FROM_CELL)
            oi = FqcnInfo(origin)
            if from_cell != origin and not same_family(oi, self.my_info):
                # this is a forwarded message, so no direct path from the origin to me
                conn_url = message.get_header(MessageHeaderKey.CONN_URL)
                if conn_url:
                    # the origin already has a listener
                    # create an ad-hoc connector to connect to the origin cell
                    self.logger.debug(f"{self.my_info.fqcn}: creating adhoc connector to {origin} at {conn_url}")
                    self._add_adhoc_connector(origin, conn_url)
                elif msg_type == MessageType.REQ:
                    # see whether we can offer a listener
                    allow_adhoc = self.connector_manager.is_adhoc_allowed(oi, self.my_info)
                    if allow_adhoc and not oi.is_on_server and self.my_info.fqcn > origin:
                        self.logger.debug(f"{self.my_info.fqcn}: trying to offer ad-hoc listener to {origin}")
                        listener = self._create_external_listener("")
                        if listener:
                            my_conn_url = listener.get_connection_url()

        if msg_type == MessageType.REQ:
            # this is a request for me - dispatch to the right CB
            channel = message.get_header(MessageHeaderKey.CHANNEL, "")
            topic = message.get_header(MessageHeaderKey.TOPIC, "")
            reply = self._process_request(origin, message)

            if not reply:
                self.logger.debug(f"{self.my_info.fqcn}: don't send response - nothing to send")
                self.received_msg_counter_pool.increment(
                    category=self._stats_category(message), counter_name=_CounterName.REPLY_NONE
                )
                return

            reply_expected = message.get_header(MessageHeaderKey.REPLY_EXPECTED, False)
            if not reply_expected:
                # this is fire and forget
                self.logger.debug(f"{self.my_info.fqcn}: don't send response - request expects no reply")
                self.received_msg_counter_pool.increment(
                    category=self._stats_category(message), counter_name=_CounterName.REPLY_NOT_EXPECTED
                )
                return

            wait_until = message.get_header(MessageHeaderKey.WAIT_UNTIL, None)
            if isinstance(wait_until, float) and time.time() > wait_until:
                # no need to reply since peer already gave up waiting by now
                self.logger.debug(f"{self.my_info.fqcn}: don't send response - reply is too late")
                self.received_msg_counter_pool.increment(
                    category=self._stats_category(message), counter_name=_CounterName.NO_REPLY_LATE
                )
                return

            # send the reply back
            if not reply.headers.get(MessageHeaderKey.RETURN_CODE):
                self.logger.debug(f"{self.my_info.fqcn}: added return code OK")
                reply.set_header(MessageHeaderKey.RETURN_CODE, ReturnCode.OK)

            req_id = message.get_header(MessageHeaderKey.REQ_ID, "")
            reply.add_headers(
                {
                    MessageHeaderKey.CHANNEL: channel,
                    MessageHeaderKey.TOPIC: topic,
                    MessageHeaderKey.FROM_CELL: self.my_info.fqcn,
                    MessageHeaderKey.ORIGIN: self.my_info.fqcn,
                    MessageHeaderKey.DESTINATION: origin,
                    MessageHeaderKey.TO_CELL: endpoint.name,
                    MessageHeaderKey.REQ_ID: req_id,
                    MessageHeaderKey.MSG_TYPE: MessageType.REPLY,
                    MessageHeaderKey.ROUTE: [(self.my_info.fqcn, time.time())],
                }
            )

            if my_conn_url:
                reply.set_header(MessageHeaderKey.CONN_URL, my_conn_url)

            # invoke outgoing reply filters
            reply_filters = self.out_reply_filter_reg.find(channel, topic)
            if reply_filters:
                self.logger.debug(f"{self.my_info.fqcn}: invoking outgoing reply filters")
                assert isinstance(reply_filters, list)
                for f in reply_filters:
                    assert isinstance(f, _CB)
                    r = self._try_cb(reply, f.cb, *f.args, **f.kwargs)
                    if r:
                        reply = r
                        break
            self._send_reply(reply, endpoint)
        else:
            # the message is either a reply or a return for a previous request: handle replies
            self._process_reply(origin, message, msg_type)

    def _send_reply(self, reply: Message, endpoint: Endpoint):
        self.logger.debug(f"{self.my_info.fqcn}: sending reply back to {endpoint.name}")
        self.logger.debug(f"Reply message: {reply.headers}")
        err = self._send_to_endpoint(endpoint, reply)
        if err:
            self.logger.error(f"{self.my_info.fqcn}: error sending reply back to {endpoint.name}: {err}")
            self.received_msg_counter_pool.increment(category=self._stats_category(reply), counter_name=err)
        else:
            self.received_msg_counter_pool.increment(
                category=self._stats_category(reply), counter_name=_CounterName.REPLIED
            )
            rc = reply.get_header(MessageHeaderKey.RETURN_CODE)
            self.received_msg_counter_pool.increment(category=self._stats_category(reply), counter_name=rc)

    def _check_bulk(self):
        while not self.asked_to_stop:
            with self.bulk_lock:
                for _, sender in self.bulk_senders.items():
                    sender.send()
            time.sleep(self.bulk_check_interval)

        # force everything to be flushed
        with self.bulk_lock:
            for _, sender in self.bulk_senders.items():
                sender.send()

    def state_change(self, endpoint: Endpoint):
        self.logger.debug(f"========= {self.my_info.fqcn}: EP {endpoint.name} state changed to {endpoint.state}")
        fqcn = endpoint.name
        if endpoint.state == EndpointState.READY:
            # create the CellAgent for this endpoint
            agent = self.agents.get(fqcn)
            if not agent:
                agent = CellAgent(fqcn, endpoint)
                with self.agent_lock:
                    self.agents[fqcn] = agent
                self.logger.debug(f"{self.my_info.fqcn}: created CellAgent for {fqcn}")
            else:
                self.logger.debug(f"{self.my_info.fqcn}: found existing CellAgent for {fqcn} - shouldn't happen")
                agent.endpoint = endpoint

            if self.cell_connected_cb is not None:
                try:
                    self.logger.debug(f"{self.my_info.fqcn}: calling cell_connected_cb")
                    self.cell_connected_cb(agent, *self.cell_connected_cb_args, **self.cell_connected_cb_kwargs)
                except Exception as ex:
                    self.logger.error(f"{self.my_info.fqcn}: exception in cell_connected_cb: {ex}")
                    self.logger.debug(traceback.format_exc())

        elif endpoint.state in [EndpointState.CLOSING, EndpointState.DISCONNECTED, EndpointState.IDLE]:
            # remove this agent
            with self.agent_lock:
                agent = self.agents.pop(fqcn, None)
                self.logger.debug(f"{self.my_info.fqcn}: removed CellAgent {fqcn}")
            if agent and self.cell_disconnected_cb is not None:
                try:
                    self.logger.debug(f"{self.my_info.fqcn}: calling cell_disconnected_cb")
                    self.cell_disconnected_cb(
                        agent, *self.cell_disconnected_cb_args, **self.cell_disconnected_cb_kwargs
                    )
                except Exception as ex:
                    self.logger.error(f"{self.my_info.fqcn}: exception in cell_disconnected_cb: {ex}")
                    self.logger.debug(traceback.format_exc())

    def get_sub_cell_names(self) -> (List[str], List[str]):
        """
        Get cell FQCNs of all subs, which are children or top-level client cells (if my cell is server).

        Returns: fqcns of child cells, fqcns of top-level client cells
        """
        children = []
        clients = []
        with self.agent_lock:
            for fqcn, agent in self.agents.items():
                if FQCN.is_parent(self.my_info.fqcn, fqcn):
                    children.append(fqcn)
                elif self.my_info.is_root and self.my_info.is_on_server:
                    # see whether the agent is a client cell
                    if agent.info.is_root and not agent.info.is_on_server:
                        clients.append(fqcn)
            return children, clients