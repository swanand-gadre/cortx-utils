#!/usr/bin/env python3

# CORTX-Py-Utils: CORTX Python common library.
# Copyright (c) 2021 Seagate Technology LLC and/or its Affiliates
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
# For any questions about this software or licensing,
# please email opensource@seagate.com or cortx-questions@seagate.com.

import time
import errno

from cortx.utils.log import Log
from confluent_kafka import Producer, Consumer, KafkaError, KafkaException
from confluent_kafka.admin import AdminClient, ConfigResource, NewTopic, \
    NewPartitions
from cortx.utils.message_bus.error import MessageBusError
from cortx.utils.message_bus.message_broker import MessageBroker
from cortx.utils import errors


class KafkaMessageBroker(MessageBroker):
    """ Kafka Server based message broker implementation """

    name = 'kafka'

    # Retention period in Milliseconds
    _default_msg_retention_period = 604800000
    _min_msg_retention_period = 1

    # Maximum retry count
    _max_config_retry_count = 3
    _max_purge_retry_count = 5
    _max_list_message_type_count = 15
    _config_prop_map = {
        'expire_time_ms': 'retention.ms',
        'data_limit_bytes': 'segment.bytes',
        'file_delete_ms': 'file.delete.delay.ms',
        }

    def __init__(self, broker_conf: dict):
        """ Initialize Kafka based Configurations """
        super().__init__(broker_conf)
        Log.debug(f"KafkaMessageBroker: initialized with broker " \
            f"configurations broker_conf: {broker_conf}")
        self._clients = {'admin': {}, 'producer': {}, 'consumer': {}}

        # Polling timeout
        self._recv_message_timeout = \
            broker_conf['message_bus']['receive_timeout']
        # Socket timeout
        self._controller_socket_timeout = \
            broker_conf['message_bus']['socket_timeout']
        # Message timeout
        self._send_message_timeout = \
            broker_conf['message_bus']['send_timeout']

    def init_client(self, client_type: str, **client_conf: dict):
        """ Obtain Kafka based Producer/Consumer """
        Log.debug(f"initializing client_type: {client_type}," \
            f" **kwargs {client_conf}")
        # Validate and return if client already exists
        if client_type not in self._clients.keys():
            Log.error(f"MessageBusError: Invalid client type " \
                f"{errors.ERR_INVALID_CLIENT_TYPE}, {client_type}")
            raise MessageBusError(errors.ERR_INVALID_CLIENT_TYPE, \
                "Invalid client type %s", client_type)

        if client_conf['client_id'] in self._clients[client_type].keys():
            if self._clients[client_type][client_conf['client_id']] != {}:
                # Check if message_type exists to send/receive
                client = self._clients[client_type][client_conf['client_id']]
                available_message_types = client.list_topics().topics.keys()
                if client_type == 'producer':
                    if client_conf['message_type'] not in \
                        available_message_types:
                        Log.error(f"MessageBusError: message_type " \
                            f"{client_conf['message_type']} not found in " \
                            f"{available_message_types} for {client_type}")
                        raise MessageBusError(errno.EINVAL, "Unknown Topic or \
                            Partition. %s", KafkaError(3))
                elif client_type == 'consumer':
                    if not any(each_message_type in available_message_types for\
                        each_message_type in client_conf['message_types']):
                        Log.error(f"MessageBusError: message_type " \
                            f"{client_conf['message_types']} not found in " \
                            f"{available_message_types} for {client_type}")
                        raise MessageBusError(errno.EINVAL, "Unknown Topic or \
                            Partition. %s", KafkaError(3))
                return

        kafka_conf = {}
        kafka_conf['bootstrap.servers'] = self._servers
        kafka_conf['client.id'] = client_conf['client_id']
        kafka_conf['error_cb'] = self._error_cb

        if client_type == 'admin' or client_type == 'producer':
                kafka_conf['socket.timeout.ms'] = self._controller_socket_timeout
                admin = AdminClient(kafka_conf)
                self._clients['admin'][client_conf['client_id']] = admin

        if client_type == 'producer':
            kafka_conf['message.timeout.ms'] = self._send_message_timeout
            producer = Producer(**kafka_conf)
            self._clients[client_type][client_conf['client_id']] = producer

            self._resource = ConfigResource('topic', \
                client_conf['message_type'])
            admin = self._clients['admin'][client_conf['client_id']]
            conf = admin.describe_configs([self._resource])
            default_configs = list(conf.values())[0].result()
            for params in ['retention.ms']:
                if params not in default_configs:
                    Log.error(f"MessageBusError: Missing required config" \
                        f" parameter {params}. for client type {client_type}")
                    raise MessageBusError(errno.ENOKEY, \
                        "Missing required config parameter %s. for " +\
                        "client type %s", params, client_type)

            self._saved_retention = int(default_configs['retention.ms']\
                .__dict__['value'])

            # Set retention to default if the value is 1 ms
            self._saved_retention = self._default_msg_retention_period if \
                self._saved_retention == self._min_msg_retention_period else \
                int(default_configs['retention.ms'].__dict__['value'])

        elif client_type == 'consumer':
            for entry in ['offset', 'consumer_group', 'message_types', \
                'auto_ack', 'client_id']:
                if entry not in client_conf.keys():
                    Log.error(f"MessageBusError: Could not find entry "\
                        f"{entry} in conf keys for client type {client_type}")
                    raise MessageBusError(errno.ENOENT, "Could not find " +\
                        "entry %s in conf keys for client type %s", entry, \
                        client_type)

            kafka_conf['enable.auto.commit'] = client_conf['auto_ack']
            kafka_conf['auto.offset.reset'] = client_conf['offset']
            kafka_conf['group.id'] = client_conf['consumer_group']

            consumer = Consumer(**kafka_conf)
            consumer.subscribe(client_conf['message_types'])
            self._clients[client_type][client_conf['client_id']] = consumer

    def _task_status(self, tasks: dict, method: str):
        """ Check if the task is completed successfully """
        for task in tasks.values():
            try:
                task.result()  # The result itself is None
            except Exception as e:
                Log.error(f"MessageBusError: {errors.ERR_OP_FAILED}." \
                    f" Admin operation fails for {method}. {e}")
                raise MessageBusError(errors.ERR_OP_FAILED, \
                    "Admin operation fails for %s. %s", method, e)

    def _get_metadata(self, admin: object):
        """ To get the metadata information of message type """
        try:
            message_type_metadata = admin.list_topics().__dict__
            return message_type_metadata['topics']
        except KafkaException as e:
            Log.error(f"MessageBusError: {errors.ERR_OP_FAILED}. " \
                f"list_topics() failed. {e} Check if Kafka service is " \
                f"running successfully")
            raise MessageBusError(errors.ERR_OP_FAILED, "list_topics() " +\
                "failed. %s. Check if Kafka service is running successfully", e)
        except Exception as e:
            Log.error(f"MessageBusError: {errors.ERR_OP_FAILED}. " \
                f"list_topics() failed. {e} Check if Kafka service is " \
                f"running successfully")
            raise MessageBusError(errors.ERR_OP_FAILED, "list_topics() " + \
                "failed. %s. Check if Kafka service is running successfully", e)

    @staticmethod
    def _error_cb(err):
        """ Callback to check if all brokers are down """
        if err.code() == KafkaError._ALL_BROKERS_DOWN:
            raise MessageBusError(errors.ERR_SERVICE_UNAVAILABLE, \
                "Kafka service(s) unavailable. %s", err)

    def list_message_types(self, admin_id: str) -> list:
        """
        Returns a list of existing message types.

        Parameters:
        admin_id        A String that represents Admin client ID.

        Return Value:
        Returns list of message types e.g. ["topic1", "topic2", ...]
        """
        admin = self._clients['admin'][admin_id]
        return list(self._get_metadata(admin).keys())

    def register_message_type(self, admin_id: str, message_types: list, \
        partitions: int):
        """
        Creates a list of message types.

        Parameters:
        admin_id        A String that represents Admin client ID.
        message_types   This is essentially equivalent to the list of
                        queue/topic name. For e.g. ["Alert"]
        partitions      Integer that represents number of partitions to be
                        created.
        """
        Log.debug(f"Register message type {message_types} using {admin_id}" \
            f" with {partitions} partitions")
        admin = self._clients['admin'][admin_id]
        new_message_type = [NewTopic(each_message_type, \
            num_partitions=partitions) for each_message_type in message_types]
        created_message_types = admin.create_topics(new_message_type)
        self._task_status(created_message_types, method='register_message_type')

        for each_message_type in message_types:
            for list_retry in range(1, self._max_list_message_type_count+2):
                if each_message_type not in \
                    list(self._get_metadata(admin).keys()):
                    if list_retry > self._max_list_message_type_count:
                        Log.error(f"MessageBusError: Timed out after retry " \
                            f"{list_retry} while creating message_type " \
                            f"{each_message_type}")
                        raise MessageBusError(errno.ETIMEDOUT, "Timed out " +\
                            "after retry %d while creating message_type %s.", \
                            list_retry, each_message_type)
                    time.sleep(list_retry*1)
                    continue
                else:
                    break

    def deregister_message_type(self, admin_id: str, message_types: list):
        """
        Deletes a list of message types.

        Parameters:
        admin_id        A String that represents Admin client ID.
        message_types   This is essentially equivalent to the list of
                        queue/topic name. For e.g. ["Alert"]
        """
        Log.debug(f"Deregister message type {message_types} using {admin_id}")
        admin = self._clients['admin'][admin_id]
        deleted_message_types = admin.delete_topics(message_types)
        self._task_status(deleted_message_types, \
            method='deregister_message_type')

        for each_message_type in message_types:
            for list_retry in range(1, self._max_list_message_type_count+2):
                if each_message_type in list(self._get_metadata(admin).keys()):
                    if list_retry > self._max_list_message_type_count:
                        Log.error(f"MessageBusError: Timed out after " \
                            f"{list_retry} retry to delete message_type " \
                            f"{each_message_type}")
                        raise MessageBusError(errno.ETIMEDOUT, \
                            "Timed out after %d retry to delete message_type" +\
                            "%s.", list_retry, each_message_type)
                    time.sleep(list_retry*1)
                    continue
                else:
                    break

    def add_concurrency(self, admin_id: str, message_type: str, \
        concurrency_count: int):
        """
        Increases the partitions for a message type.

        Parameters:
        admin_id            A String that represents Admin client ID.
        message_type        This is essentially equivalent to queue/topic name.
                            For e.g. "Alert"
        concurrency_count   Integer that represents number of partitions to be
                            increased.

        Note:  Number of partitions for a message type can only be increased,
               never decreased
        """
        Log.debug(f"Adding concurrency count {concurrency_count} for message" \
            f" type {message_type} with admin id {admin_id}")
        admin = self._clients['admin'][admin_id]
        new_partition = [NewPartitions(message_type, \
            new_total_count=concurrency_count)]
        partitions = admin.create_partitions(new_partition)
        self._task_status(partitions, method='add_concurrency')

        # Waiting for few seconds to complete the partition addition process
        for list_retry in range(1, self._max_list_message_type_count+2):
            if concurrency_count != len(self._get_metadata(admin)\
                [message_type].__dict__['partitions']):
                if list_retry > self._max_list_message_type_count:
                    Log.error(f"MessageBusError: Exceeded retry count " \
                        f"{list_retry} for creating partitions for " \
                        f"message_type {message_type}")
                    raise MessageBusError(errno.E2BIG, "Exceeded retry count" +\
                        " %d for creating partitions for message_type" +\
                        " %s.", list_retry, message_type)
                time.sleep(list_retry*1)
                continue
            else:
                break
        Log.debug(f"Successfully Increased the partitions for a " \
            f"{message_type} to {concurrency_count}")

    @staticmethod
    def delivery_callback(err, _):
        if err:
            raise MessageBusError(errno.ETIMEDOUT, "Message delivery failed. \
                %s", err)

    def send(self, producer_id: str, message_type: str, method: str, \
        messages: list, timeout=0.1):
        """
        Sends list of messages to Kafka cluster(s)

        Parameters:
        producer_id     A String that represents Producer client ID.
        message_type    This is essentially equivalent to the
                        queue/topic name. For e.g. "Alert"
        method          Can be set to "sync" or "async"(default).
        messages        A list of messages sent to Kafka Message Server
        """
        Log.debug(f"Producer {producer_id} sending list of messages " \
            f"{messages} of message type {message_type} to kafka server" \
            f" with method {method}")
        producer = self._clients['producer'][producer_id]
        if producer is None:
            Log.error(f"MessageBusError: " \
                f"{errors.ERR_SERVICE_NOT_INITIALIZED}. Producer: " \
                f"{producer_id} is not initialized")
            raise MessageBusError(errors.ERR_SERVICE_NOT_INITIALIZED,\
                "Producer %s is not initialized", producer_id)

        for message in messages:
            producer.produce(message_type, bytes(message, 'utf-8'), \
                callback=self.delivery_callback)
            if method == 'sync':
                producer.flush()
            else:
                producer.poll(timeout=timeout)
        Log.debug("Successfully Sent list of messages to Kafka cluster")

    def delete(self, admin_id: str, message_type: str):
        """
        Deletes all the messages of given message_type

        Parameters:
        message_type    This is essentially equivalent to the
                        queue/topic name. For e.g. "Alert"
        """
        admin = self._clients['admin'][admin_id]
        Log.debug(f"Removing all messages from kafka cluster for message " \
            f"type {message_type} with admin id {admin_id}")

        for tuned_retry in range(self._max_config_retry_count):
            self._resource.set_config('retention.ms', \
                self._min_msg_retention_period)
            tuned_params = admin.alter_configs([self._resource])
            if list(tuned_params.values())[0].result() is not None:
                if tuned_retry > 1:
                    Log.error(f"MessageBusError: {errors.ERR_OP_FAILED} " \
                        f"alter_configs() for resource {self._resource} " \
                        f"failed using admin {admin} for message type " \
                        f"{message_type}")
                    raise MessageBusError(errors.ERR_OP_FAILED, \
                        "alter_configs() for resource %s failed using admin" +\
                        "%s for message type %s", self._resource, admin,\
                        message_type)
                continue
            else:
                break

        # Sleep for a second to delete the messages
        time.sleep(1)

        for default_retry in range(self._max_config_retry_count):
            self._resource.set_config('retention.ms', self._saved_retention)
            default_params = admin.alter_configs([self._resource])
            if list(default_params.values())[0].result() is not None:
                if default_retry > 1:
                    Log.error(f"MessageBusError: {errno.ENOKEY} Unknown " \
                        f"configuration for message type {message_type}.")
                    raise MessageBusError(errno.ENOKEY, "Unknown " +\
                        "configuration for message type %s.", message_type)
                continue
            else:
                break
        Log.debug(f"Successfully deleted all the messages of message_type: " \
            f"{message_type}")
        return 0

    def receive(self, consumer_id: str, timeout: float = None) -> list:
        """
        Receives list of messages from Kafka Message Server

        Parameters:
        consumer_id     Consumer ID for which messages are to be retrieved
        timeout         Time in seconds to wait for the message. Timeout of 0
                        will lead to blocking indefinitely for the message
        """
        blocking = False

        consumer = self._clients['consumer'][consumer_id]
        Log.debug(f"Receiving list of messages from kafka Message server of" \
            f" consumer_id {consumer_id}, and timeout is {timeout}")
        if consumer is None:
            Log.error(f"MessageBusError: {errors.ERR_SERVICE_NOT_INITIALIZED}"\
                f" Consumer {consumer_id} is not initialized.")
            raise MessageBusError(errors.ERR_SERVICE_NOT_INITIALIZED, \
                "Consumer %s is not initialized.", consumer_id)

        if timeout is None:
            timeout = self._recv_message_timeout
        if timeout == 0:
            timeout = self._recv_message_timeout
            blocking = True

        try:
            while True:
                msg = consumer.poll(timeout=timeout)
                if msg is None:
                    # if blocking (timeout=0), NoneType messages are ignored
                    if not blocking:
                        return None
                elif msg.error():
                    Log.error(f"MessageBusError: {errors.ERR_OP_FAILED}" \
                        f" poll({timeout}) for consumer {consumer_id} failed" \
                        f" to receive message. {msg.error()}")
                    raise MessageBusError(errors.ERR_OP_FAILED, "poll(%s) " +\
                        "for consumer %s failed to receive message. %s", \
                        timeout, consumer_id, msg.error())
                else:
                    return msg.value()
        except KeyboardInterrupt:
            Log.error(f"MessageBusError: {errno.EINTR} Received Keyboard " \
                f"interrupt while trying to receive message for consumer " \
                f"{consumer_id}")
            raise MessageBusError(errno.EINTR, "Received Keyboard interrupt " +\
                "while trying to receive message for consumer %s", consumer_id)

    def ack(self, consumer_id: str):
        """ To manually commit offset """
        consumer = self._clients['consumer'][consumer_id]
        if consumer is None:
            Log.error(f"MessageBusError: {errors.ERR_SERVICE_NOT_INITIALIZED}"\
                f" Consumer {consumer_id} is not initialized.")
            raise MessageBusError(errors.ERR_SERVICE_NOT_INITIALIZED,\
                "Consumer %s is not initialized.", consumer_id)
        consumer.commit(async=False)

    def _configure_message_type(self, admin_id: str, message_type: str, **kwargs):
        """
        Sets expiration time for individual messages types

        Parameters:
        message_type    This is essentially equivalent to the
                        queue/topic name. For e.g. "Alert"
        Kwargs:
        expire_time_ms  This should be the expire time of message_type
                        in milliseconds.
        data_limit_bytes This should be the max size of log files
                         for individual message_type.
        """
        admin = self._clients['admin'][admin_id]
        Log.debug(f"New configuration for message " \
            f"type {message_type} with admin id {admin_id}")
        # check for message_type exist or not
        message_type_list = self.list_message_types(admin_id)
        if message_type not in message_type_list:
            raise MessageBusError(errno.ENOENT, "Unknown Message type:"+\
                " not listed in %s", message_type_list)
        topic_resource = ConfigResource('topic', message_type)
        for tuned_retry in range(self._max_config_retry_count):
            for key, val in kwargs.items():
                if key not in self._config_prop_map:
                    raise MessageBusError(errno.EINVAL,\
                        "Invalid configuration %s for message_type %s.", key,\
                        message_type)
                topic_resource.set_config(self._config_prop_map[key], val)
            tuned_params = admin.alter_configs([topic_resource])
            if list(tuned_params.values())[0].result() is not None:
                if tuned_retry > 1:
                    Log.error(f"MessageBusError: {errors.ERR_OP_FAILED} " \
                        f"Updating message type expire time by "\
                        f"alter_configs() for resource {topic_resource} " \
                        f"failed using admin {admin} for message type " \
                        f"{message_type}")
                    raise MessageBusError(errors.ERR_OP_FAILED, \
                        "Updating message type expire time by "+\
                        "alter_configs() for resource %s failed using admin" +\
                        "%s for message type %s", topic_resource, admin,\
                        message_type)
                continue
            else:
                break
        Log.debug("Successfully updated message type with new"+\
            " configuration.")
        return 0

    def set_message_type_expire(self, admin_id: str, message_type: str,\
        **kwargs):
        """
        Set message type expire with combinational unit of time and size.
        Kwargs:
        expire_time_ms  This should be the expire time of message_type
                        in milliseconds.
        data_limit_bytes This should be the max size of log files
                         for individual message_type.
        """
        Log.debug(f"Set expiration for message " \
            f"type {message_type} with admin id {admin_id}")

        for config_property in ['expire_time_ms', 'data_limit_bytes']:
            if config_property not in kwargs.keys():
                raise MessageBusError(errno.EINVAL,\
                    "Invalid message_type retention config key %s.", config_property)
        kwargs['file_delete_ms'] = 1
        return self._configure_message_type(admin_id, message_type, **kwargs)
