# -*- coding: utf-8 -*-

#
# MIT License
#
# Copyright (c) 2016 Tomas Pavuk <433592@mail.muni.cz>, Institute of Computer Science, Masaryk University
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#

"""
Detects external dns resolvers used in the specified local network.

Usage:
    dns_external_resolvers.py -iz <input-zookeeper-hostname>:<input-zookeeper-port> -it <input-topic>
    -oz <output-zookeeper-hostname>:<output-zookeeper-port> -ot <output-topic> -m <microbatch-duration>
    -w <window-duration> -ln <local-network>/<subnet-mask>

To run this on the Stream4Flow, you need to receive flows by IPFIXCol and make them available via Kafka topic. Then you
can run the application as follows:
    $ ~/applications/run-application.sh ./dns_external_resolvers.py -iz producer:2181 -it ipfix.entry -oz producer:9092
    -ot results.output -ln 10.10.0.0/16
"""

import argparse  # Arguments parser
import time  # Unix time to timestamp conversion

from netaddr import IPNetwork, IPAddress  # Checking if IP is in the network
from modules import kafkaIO  # IO operations with kafka topics
from modules import DNSResponseConverter  # Convert byte array to the IP address
from termcolor import cprint  # Colors in the console output

# Saves attacks in dictionary, so 1 attack is not reported multiple times
detectionsDict = {}
# Remembers when dictionary was cleaned last time
lastCleaning = time.time()


def clean_old_data_from_dictionary(window_duration):
    """
    Clean dictionary of old attacks.

    :param window_duration: time for which if record is older it gets deleted (multiplied by 10)
    """
    global lastCleaning
    current_time = time.time()
    # Clean once a day
    if (lastCleaning + 86400) < current_time:
        lastCleaning = current_time
        for key, value in detectionsDict.items():
            # If timestamp of record + 10times window duration is smaller than current time
            if ((10 * window_duration * 1000) + value[1]) < (int(current_time * 1000)):
                del detectionsDict[key]


def get_output_json(key, value, flows_total):
    """
    Create JSON with correct format.

    :param key: Source ip address
    :param value: Dictionary value for statistic
    :param flows_total: Sum of all flows
    :return: JSON string in desired format
    """
    # Convert Unix time to timestamp
    s, ms = divmod(value[0], 1000)
    timestamp = "%s.%03d" % (time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(s)), ms) + 'Z'

    return "{\"@type\": \"external_dns_resolver\", \"src_ip\": \"" + str(key[0]) + '\"' +\
           ", \"resolver_ip\": \"" + str(key[1]) + '\"' +\
           ", \"flows\": " + str(flows_total) + \
           ", \"flows_increment\": " + str(value[1]) +\
           ", \"timestamp\": \"" + str(timestamp) + "\"}\n"


def process_results(results, producer, output_topic, window_duration):
    """
    Format and report detected records.

    :param results: Detected records
    :param producer: Kafka producer that sends the data to output topic
    :param output_topic: Name of the receiving kafka topic
    :param window_duration: Duration of the window
    """
    output_json = ""
    # Transform given results into the JSON
    for key, value in results.iteritems():
        if key in detectionsDict:
            # If there are additional flows for the attack that was reported.
            if (detectionsDict[key][0] + window_duration * 1000) <= value[0]:
                detectionsDict[key] = (value[0], detectionsDict[key][1] + value[1])
                output_json += get_output_json(key, value, detectionsDict[key][1])
        else:
            detectionsDict[key] = (value[0], value[1])
            output_json += get_output_json(key, value, value[1])

    if output_json:
        # Print data to standard output
        cprint(output_json)

        # Check if dictionary cleaning is necessary
        clean_old_data_from_dictionary(window_duration)

        # Send results to the specified kafka topic
        kafkaIO.send_data_to_kafka(output_json, producer, output_topic)


def get_ip(record, direction):
    """
    Return required IPv4 or IPv6 address (source or destination) from given record.

    :param record: JSON record searched for IP
    :param direction: string from which IP will be searched (e.g. "source" => ipfix.sourceIPv4Address or "destination" => ipfix.destinationIPv4Address)
    :return: value corresponding to the key in the record
    """
    key_name = "ipfix." + direction + "IPv4Address"
    if key_name in record.keys():
        return record[key_name]
    key_name = "ipfix." + direction + "IPv6Address"
    return record[key_name]


def get_external_dns_resolvers(dns_input_stream, all_data_stream, window_duration, window_slide):
    """
    Gets used external dns resolvers from input stream

    :param dns_input_stream: Input flows
    :param all_data_stream: All incoming flows
    :param window_duration: Length of the window in seconds
    :param window_slide: Length of the window slide in seconds
    :return: Detected external resolvers
    """
    dns_resolved = dns_input_stream\
        .filter(lambda record: record["ipfix.DNSCrrType"] == 1) \
        .map(lambda record: ((get_ip(record, "destination"),
                              DNSResponseConverter.convert_dns_rdata(record["ipfix.DNSRData"], record["ipfix.DNSCrrType"])),
                             (get_ip(record, "source"),
                              record["ipfix.flowStartMilliseconds"]))) \
        .reduceByKey(lambda actual, update: actual) \
        .window(window_duration, window_slide)

    detected_external = all_data_stream\
        .filter(lambda flow_json: flow_json["ipfix.protocolIdentifier"] == 6) \
        .map(lambda record: ((get_ip(record, "source"), get_ip(record, "destination")),
                              record["ipfix.flowStartMilliseconds"])) \
        .join(dns_resolved) \
        .filter(lambda record: abs(record[1][0] - record[1][1][1]) <= 5000) \
        .map(lambda record: ((record[0][0], record[1][1][0]), (record[1][1][1], 1))) \
        .reduceByKey(lambda actual, update: (actual[0],
                                             actual[1] + update[1]))

    return detected_external


def get_dns_stream(flows_stream):
    """
    Filter to get only flows containing DNS information.

    :param flows_stream: Input flows
    :return: Flows with DNS information
    """
    return flows_stream.filter(lambda flow_json: ("ipfix.DNSName" in flow_json.keys()))


def get_flows_external_to_local(dns_stream, local_network):
    """
    Filter to contain flows going from the specified local network to the different network.

    :param dns_stream: Input flows
    :param local_network: Local network's address
    :return: Flows coming from local network to external networks
    """
    return dns_stream \
        .filter(lambda dns_json: (IPAddress(get_ip(dns_json, "source")) not in IPNetwork(local_network)) and
                                 (IPAddress(get_ip(dns_json, "destination")) in IPNetwork(local_network)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-iz", "--input_zookeeper", help="input zookeeper hostname:port", type=str, required=True)
    parser.add_argument("-it", "--input_topic", help="input kafka topic", type=str, required=True)
    parser.add_argument("-oz", "--output_zookeeper", help="output zookeeper hostname:port", type=str, required=True)
    parser.add_argument("-ot", "--output_topic", help="output kafka topic", type=str, required=True)
    parser.add_argument("-m", "--microbatch", help="microbatch duration", type=int, required=False, default=10)
    parser.add_argument("-w", "--window", help="analysis window duration", type=int, required=False, default=360)

    # Define Arguments for detection
    parser.add_argument("-ln", "--local_network", help="local network", type=str, required=True)

    # Parse arguments
    args = parser.parse_args()

    # Initialize input stream and parse it into JSON
    ssc, parsed_input_stream = kafkaIO\
        .initialize_and_parse_input_stream(args.input_zookeeper, args.input_topic, args.microbatch)

    # Prepare input stream
    dns_stream = get_dns_stream(parsed_input_stream)
    dns_external_to_local = get_flows_external_to_local(dns_stream, args.local_network)

    # Initialize kafka producer
    kafka_producer = kafkaIO.initialize_kafka_producer(args.output_zookeeper)

    # Calculate and process DNS statistics
    get_external_dns_resolvers(dns_external_to_local, parsed_input_stream, args.window, args.microbatch) \
        .foreachRDD(lambda rdd: process_results(rdd.collectAsMap(), kafka_producer, args.output_topic, args.window))

    # Start Spark streaming context
    kafkaIO.spark_start(ssc)
