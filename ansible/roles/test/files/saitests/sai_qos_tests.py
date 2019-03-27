"""
SONiC Dataplane Qos tests
"""

import time
import logging
import ptf.packet as scapy
import socket
import ptf.dataplane as dataplane
import sai_base_test
import operator
import sys
from ptf.testutils import (ptf_ports,
                           simple_arp_packet,
                           send_packet,
                           simple_tcp_packet)
from ptf.mask import Mask
from switch import (switch_init,
                    sai_thrift_create_scheduler_profile,
                    sai_thrift_clear_all_counters,
                    sai_thrift_read_port_counters,
                    sai_port_list,
                    port_list)
from switch_sai_thrift.ttypes import (sai_thrift_attribute_value_t,
                                      sai_thrift_attribute_t)
from switch_sai_thrift.sai_headers import (SAI_PORT_ATTR_QOS_SCHEDULER_PROFILE_ID,
                                           SAI_PORT_ATTR_PKT_TX_ENABLE)

# Counters
# The index number comes from the append order in sai_thrift_read_port_counters
EGRESS_DROP = 0
INGRESS_DROP = 1
PFC_PRIO_3 = 5
PFC_PRIO_4 = 6
TRANSMITTED_OCTETS = 10
TRANSMITTED_PKTS = 11
QUEUE_0 = 0
QUEUE_1 = 1
QUEUE_2 = 2
QUEUE_3 = 3
QUEUE_4 = 4
QUEUE_5 = 5
QUEUE_6 = 6

# Constants
STOP_PORT_MAX_RATE = 1
RELEASE_PORT_MAX_RATE = 0
ECN_INDEX_IN_HEADER = 53 # Fits the ptf hex_dump_buffer() parse function
DSCP_INDEX_IN_HEADER = 52 # Fits the ptf hex_dump_buffer() parse function


class ARPpopulate(sai_base_test.ThriftInterfaceDataPlane):
    def runTest(self):
        router_mac = self.test_params['router_mac']
        # ARP Populate
        index = 0
        for port in ptf_ports():
            arpreq_pkt = simple_arp_packet(
                          eth_dst='ff:ff:ff:ff:ff:ff',
                          eth_src=self.dataplane.get_mac(port[0],port[1]),
                          arp_op=1,
                          ip_snd='10.0.0.%d' % (index * 2 + 1),
                          ip_tgt='10.0.0.%d' % (index * 2),
                          hw_snd=self.dataplane.get_mac(port[0], port[1]),
                          hw_tgt='00:00:00:00:00:00')
            send_packet(self, port[1], arpreq_pkt)
            index += 1

class ReleaseAllPorts(sai_base_test.ThriftInterfaceDataPlane):
    def runTest(self):
        switch_init(self.client)

        asic_type = self.test_params['sonic_asic_type']

        if asic_type == 'mellanox':
            sched_prof_id=sai_thrift_create_scheduler_profile(self.client, RELEASE_PORT_MAX_RATE)
            attr_value = sai_thrift_attribute_value_t(oid=sched_prof_id)
            attr = sai_thrift_attribute_t(id=SAI_PORT_ATTR_QOS_SCHEDULER_PROFILE_ID, value=attr_value)
        else:
            # Pause egress of dut xmit port
            attr_value = sai_thrift_attribute_value_t(booldata=0)
            attr = sai_thrift_attribute_t(id=SAI_PORT_ATTR_PKT_TX_ENABLE, value=attr_value)

        for port in sai_port_list:
            self.client.sai_thrift_set_port_attribute(port, attr)

class DscpMappingPB(sai_base_test.ThriftInterfaceDataPlane):
    def runTest(self):
        switch_init(self.client)

        router_mac = self.test_params['router_mac']        
        dst_port_id = int(self.test_params['dst_port_id'])
        dst_port_ip = self.test_params['dst_port_ip']
        dst_port_mac = self.dataplane.get_mac(0, dst_port_id)
        src_port_id = int(self.test_params['src_port_id'])
        src_port_ip = self.test_params['src_port_ip']
        src_port_mac = self.dataplane.get_mac(0, src_port_id)
        exp_ip_id = 101
        exp_ttl = 63

        # Get a snapshot of counter values
        # port_results is not of our interest here
        port_results, queue_results_base = sai_thrift_read_port_counters(self.client, port_list[dst_port_id])

        # DSCP Mapping test
        try:
            for dscp in range(0,64):
                tos = dscp << 2
                pkt = simple_tcp_packet(eth_dst=router_mac,
                                        eth_src=src_port_mac,
                                        ip_src=src_port_ip,
                                        ip_dst=dst_port_ip,
                                        ip_tos=tos,
                                        ip_id=exp_ip_id,
                                        ip_ttl=64)

                send_packet(self, src_port_id, pkt)

                dscp_received = False

                while not dscp_received:
                    result = self.dataplane.poll(device_number=0, port_number=dst_port_id, timeout=3)
                    if isinstance(result, self.dataplane.PollFailure):
                        self.fail("Expected packet was not received on port %d.\n%s"
                            % (dst_port_id, result.format()))
                    recv_pkt = scapy.Ether(result.packet)

                    # Verify dscp flag
                    try:
                        dscp_received = recv_pkt.payload.tos == tos and recv_pkt.payload.src == src_port_ip and recv_pkt.payload.dst == dst_port_ip and \
                            recv_pkt.payload.ttl == exp_ttl and recv_pkt.payload.id == exp_ip_id
                    except AttributeError:
                        continue

            # Read Counters
            port_results, queue_results = sai_thrift_read_port_counters(self.client, port_list[dst_port_id])

            # According to SONiC configuration all dscp are classified to queue 0 except:
            # dscp 3 -> queue 3
            # dscp 4 -> queue 4
            # dscp 8 -> queue 1
            # So for the 64 pkts sent the mapping should be -> 61 queue 0, and 1 for queue1, queue3 and queue4
            # Check results
            assert(queue_results[QUEUE_0] == 60 + queue_results_base[QUEUE_0])
            assert(queue_results[QUEUE_1] == 1 + queue_results_base[QUEUE_1])
            assert(queue_results[QUEUE_3] == 1 + queue_results_base[QUEUE_3])
            assert(queue_results[QUEUE_4] == 1 + queue_results_base[QUEUE_4])
            assert(queue_results[QUEUE_5] == 1 + queue_results_base[QUEUE_5])

        finally:
            print "END OF TEST"

# This test is to measure the Xoff threshold, and buffer limit
class PFCtest(sai_base_test.ThriftInterfaceDataPlane):
    def runTest(self):
        time.sleep(5)
        switch_init(self.client)

        # Parse input parameters
        dscp = int(self.test_params['dscp'])
        ecn = int(self.test_params['ecn'])
        router_mac = self.test_params['router_mac']
        pg = int(self.test_params['pg']) + 2 # The pfc counter index starts from index 2 in sai_thrift_read_port_counters
        dst_port_id = int(self.test_params['dst_port_id'])
        dst_port_ip = self.test_params['dst_port_ip']
        dst_port_mac = self.dataplane.get_mac(0, dst_port_id)
        max_buffer_size = int(self.test_params['buffer_max_size'])
        max_queue_size = int(self.test_params['queue_max_size']) 
        src_port_id = int(self.test_params['src_port_id'])
        src_port_ip = self.test_params['src_port_ip']
        src_port_mac = self.dataplane.get_mac(0, src_port_id)
        asic_type = self.test_params['sonic_asic_type']
        pkts_num_leak_out = int(self.test_params['pkts_num_leak_out'])
        pkts_num_trig_pfc = int(self.test_params['pkts_num_trig_pfc'])
        pkts_num_trig_ingr_drp = int(self.test_params['pkts_num_trig_ingr_drp'])

        # Prepare TCP packet data
        tos = dscp << 2
        tos |= ecn
        ttl = 64
        default_packet_length = 64
        pkt = simple_tcp_packet(pktlen=default_packet_length,
                                eth_dst=router_mac,
                                eth_src=src_port_mac,
                                ip_src=src_port_ip,
                                ip_dst=dst_port_ip,
                                ip_tos=tos,
                                ip_ttl=ttl)
        # get a snapshot of counter values at recv and transmit ports
        # queue_counters value is not of our interest here
        recv_counters_base, queue_counters = sai_thrift_read_port_counters(self.client, port_list[src_port_id])
        xmit_counters_base, queue_counters = sai_thrift_read_port_counters(self.client, port_list[dst_port_id])
        # Add slight tolerance in threshold characterization to consider
        # the case that cpu puts packets in the egress queue after we pause the egress
        # or the leak out is simply less than expected as we have occasionally observed
        margin = 2

        if asic_type == 'mellanox':
            # Close DST port
            sched_prof_id = sai_thrift_create_scheduler_profile(self.client, STOP_PORT_MAX_RATE)
            attr_value = sai_thrift_attribute_value_t(oid=sched_prof_id)
            attr = sai_thrift_attribute_t(id=SAI_PORT_ATTR_QOS_SCHEDULER_PROFILE_ID, value=attr_value)
            self.client.sai_thrift_set_port_attribute(port_list[dst_port_id], attr)
        else:
            # Pause egress of dut xmit port
            attr_value = sai_thrift_attribute_value_t(booldata=0)
            attr = sai_thrift_attribute_t(id=SAI_PORT_ATTR_PKT_TX_ENABLE, value=attr_value)
            self.client.sai_thrift_set_port_attribute(port_list[dst_port_id], attr)

        try:
            # send packets short of triggering pfc
            send_packet(self, src_port_id, pkt, pkts_num_leak_out + pkts_num_trig_pfc - 1 - margin)
            # allow enough time for the dut to sync up the counter values in counters_db
            time.sleep(8)
            # get a snapshot of counter values at recv and transmit ports
            # queue counters value is not of our interest here
            recv_counters, queue_counters = sai_thrift_read_port_counters(self.client, port_list[src_port_id])
            xmit_counters, queue_counters = sai_thrift_read_port_counters(self.client, port_list[dst_port_id])
            # recv port no pfc
            assert(recv_counters[pg] == recv_counters_base[pg])
            # recv port no ingress drop
            assert(recv_counters[INGRESS_DROP] == recv_counters_base[INGRESS_DROP])
            # xmit port no egress drop
            assert(xmit_counters[EGRESS_DROP] == xmit_counters_base[EGRESS_DROP])

            # send 1 packet to trigger pfc
            send_packet(self, src_port_id, pkt, 1 + 2 * margin)
            # allow enough time for the dut to sync up the counter values in counters_db
            time.sleep(8)
            # get a snapshot of counter values at recv and transmit ports
            # queue counters value is not of our interest here
            recv_counters_base = recv_counters
            recv_counters, queue_counters = sai_thrift_read_port_counters(self.client, port_list[src_port_id])
            xmit_counters, queue_counters = sai_thrift_read_port_counters(self.client, port_list[dst_port_id])
            # recv port pfc
            assert(recv_counters[pg] > recv_counters_base[pg])
            # recv port no ingress drop
            assert(recv_counters[INGRESS_DROP] == recv_counters_base[INGRESS_DROP])
            # xmit port no egress drop
            assert(xmit_counters[EGRESS_DROP] == xmit_counters_base[EGRESS_DROP])

            # send packets short of ingress drop
            send_packet(self, src_port_id, pkt, pkts_num_trig_ingr_drp - pkts_num_trig_pfc - 1 - 2 * margin)
            # allow enough time for the dut to sync up the counter values in counters_db
            time.sleep(8)
            # get a snapshot of counter values at recv and transmit ports
            # queue counters value is not of our interest here
            recv_counters_base = recv_counters
            recv_counters, queue_counters = sai_thrift_read_port_counters(self.client, port_list[src_port_id])
            xmit_counters, queue_counters = sai_thrift_read_port_counters(self.client, port_list[dst_port_id])
            # recv port pfc
            assert(recv_counters[pg] > recv_counters_base[pg])
            # recv port no ingress drop
            assert(recv_counters[INGRESS_DROP] == recv_counters_base[INGRESS_DROP])
            # xmit port no egress drop
            assert(xmit_counters[EGRESS_DROP] == xmit_counters_base[EGRESS_DROP])

            # send 1 packet to trigger ingress drop
            send_packet(self, src_port_id, pkt, 1 + 2 * margin)
            # allow enough time for the dut to sync up the counter values in counters_db
            time.sleep(8)
            # get a snapshot of counter values at recv and transmit ports
            # queue counters value is not of our interest here
            recv_counters_base = recv_counters
            recv_counters, queue_counters = sai_thrift_read_port_counters(self.client, port_list[src_port_id])
            xmit_counters, queue_counters = sai_thrift_read_port_counters(self.client, port_list[dst_port_id])
            # recv port pfc
            assert(recv_counters[pg] > recv_counters_base[pg])
            # recv port ingress drop
            assert(recv_counters[INGRESS_DROP] > recv_counters_base[INGRESS_DROP])
            # xmit port no egress drop
            assert(xmit_counters[EGRESS_DROP] == xmit_counters_base[EGRESS_DROP])

        finally:
            if asic_type == 'mellanox':
                # Release port
                sched_prof_id = sai_thrift_create_scheduler_profile(self.client,RELEASE_PORT_MAX_RATE)
                attr_value = sai_thrift_attribute_value_t(oid=sched_prof_id)
                attr = sai_thrift_attribute_t(id=SAI_PORT_ATTR_QOS_SCHEDULER_PROFILE_ID, value=attr_value)
                self.client.sai_thrift_set_port_attribute(port_list[dst_port_id],attr)
            else:
                # Resume egress of dur xmit port
                attr_value = sai_thrift_attribute_value_t(booldata=1)
                attr = sai_thrift_attribute_t(id=SAI_PORT_ATTR_PKT_TX_ENABLE, value=attr_value)
                self.client.sai_thrift_set_port_attribute(port_list[dst_port_id], attr)

# This test looks to measure xon threshold (pg_reset_floor)
class PFCXonTest(sai_base_test.ThriftInterfaceDataPlane):
    def runTest(self):
        time.sleep(5)
        switch_init(self.client)
        last_pfc_counter = 0
        recv_port_counters = []
        transmit_port_counters = []

        # Parse input parameters
        dscp = int(self.test_params['dscp'])
        ecn = int(self.test_params['ecn'])
        router_mac = self.test_params['router_mac']
        max_buffer_size = int(self.test_params['buffer_max_size'])
        pg = int(self.test_params['pg']) + 2 # The pfc counter index starts from index 2 in sai_thrift_read_port_counters
        dst_port_id = int(self.test_params['dst_port_id'])
        dst_port_ip = self.test_params['dst_port_ip']
        dst_port_mac = self.dataplane.get_mac(0, dst_port_id)
        src_port_id = int(self.test_params['src_port_id'])
        src_port_ip = self.test_params['src_port_ip']
        src_port_mac = self.dataplane.get_mac(0, src_port_id)
        asic_type = self.test_params['sonic_asic_type']

        tos = dscp << 2
        tos |= ecn
        ttl = 64

        # TODO: pass in dst_port_id and _ip as a list
        dst_port_2_id = int(self.test_params['dst_port_2_id'])
        dst_port_2_ip = self.test_params['dst_port_2_ip']
        dst_port_2_mac = self.dataplane.get_mac(0, dst_port_2_id)
        dst_port_3_id = int(self.test_params['dst_port_3_id'])
        dst_port_3_ip = self.test_params['dst_port_3_ip']
        dst_port_3_mac = self.dataplane.get_mac(0, dst_port_3_id)
        pkts_num_leak_out = int(self.test_params['pkts_num_leak_out'])
        pkts_num_trig_pfc = int(self.test_params['pkts_num_trig_pfc'])
        pkts_num_dismiss_pfc = int(self.test_params['pkts_num_dismiss_pfc'])
        default_packet_length = 64
        # get a snapshot of counter values at recv and transmit ports
        # queue_counters value is not of our interest here
        recv_counters_base, queue_counters = sai_thrift_read_port_counters(self.client, port_list[src_port_id])
        xmit_counters_base, queue_counters = sai_thrift_read_port_counters(self.client, port_list[dst_port_id])
        xmit_2_counters_base, queue_counters = sai_thrift_read_port_counters(self.client, port_list[dst_port_2_id])
        xmit_3_counters_base, queue_counters = sai_thrift_read_port_counters(self.client, port_list[dst_port_3_id])

        if asic_type == 'mellanox':
            # Stop function of dst xmit ports
            sched_prof_id = sai_thrift_create_scheduler_profile(self.client, STOP_PORT_MAX_RATE)
            attr_value = sai_thrift_attribute_value_t(oid=sched_prof_id)
            attr = sai_thrift_attribute_t(id=SAI_PORT_ATTR_QOS_SCHEDULER_PROFILE_ID, value=attr_value)
            self.client.sai_thrift_set_port_attribute(port_list[dst_port_id], attr)
            self.client.sai_thrift_set_port_attribute(port_list[dst_port_2_id], attr)
            self.client.sai_thrift_set_port_attribute(port_list[dst_port_3_id], attr)
        else:
            # Pause egress of dut xmit ports
            attr_value = sai_thrift_attribute_value_t(booldata=0)
            attr = sai_thrift_attribute_t(id=SAI_PORT_ATTR_PKT_TX_ENABLE, value=attr_value)
            self.client.sai_thrift_set_port_attribute(port_list[dst_port_id], attr)
            self.client.sai_thrift_set_port_attribute(port_list[dst_port_2_id], attr)
            self.client.sai_thrift_set_port_attribute(port_list[dst_port_3_id], attr)

        try:
            # send packets to dst port 0
            pkt = simple_tcp_packet(pktlen=default_packet_length,
                                    eth_dst=router_mac,
                                    eth_src=src_port_mac,
                                    ip_src=src_port_ip,
                                    ip_dst=dst_port_ip,
                                    ip_tos=tos,
                                    ip_ttl=ttl)
            send_packet(self, src_port_id, pkt, pkts_num_leak_out + pkts_num_trig_pfc - pkts_num_dismiss_pfc)
            # send packets to dst port 1
            pkt = simple_tcp_packet(pktlen=default_packet_length,
                                    eth_dst=router_mac,
                                    eth_src=src_port_mac,
                                    ip_src=src_port_ip,
                                    ip_dst=dst_port_2_ip,
                                    ip_tos=tos,
                                    ip_ttl=ttl)
            send_packet(self, src_port_id, pkt, pkts_num_leak_out + pkts_num_dismiss_pfc - 1)
            # send 1 packet to dst port 2
            pkt = simple_tcp_packet(pktlen=default_packet_length,
                                    eth_dst=router_mac,
                                    eth_src=src_port_mac,
                                    ip_src=src_port_ip,
                                    ip_dst=dst_port_3_ip,
                                    ip_tos=tos,
                                    ip_ttl=ttl)
            send_packet(self, src_port_id, pkt, pkts_num_leak_out + 1)

            # allow enough time for the dut to sync up the counter values in counters_db
            time.sleep(8)
            # get a snapshot of counter values at recv and transmit ports
            # queue counters value is not of our interest here
            recv_counters, queue_counters = sai_thrift_read_port_counters(self.client, port_list[src_port_id])
            xmit_counters, queue_counters = sai_thrift_read_port_counters(self.client, port_list[dst_port_id])
            xmit_2_counters, queue_counters = sai_thrift_read_port_counters(self.client, port_list[dst_port_2_id])
            xmit_3_counters, queue_counters = sai_thrift_read_port_counters(self.client, port_list[dst_port_3_id])
            # recv port pfc
            assert(recv_counters[pg] > recv_counters_base[pg])
            # recv port no ingress drop
            assert(recv_counters[INGRESS_DROP] == recv_counters_base[INGRESS_DROP])
            # xmit port no egress drop
            assert(xmit_counters[EGRESS_DROP] == xmit_counters_base[EGRESS_DROP])
            assert(xmit_2_counters[EGRESS_DROP] == xmit_2_counters_base[EGRESS_DROP])
            assert(xmit_3_counters[EGRESS_DROP] == xmit_3_counters_base[EGRESS_DROP])

            if asic_type == 'mellanox':
                # Release dst port 1
                sched_prof_id=sai_thrift_create_scheduler_profile(self.client, RELEASE_PORT_MAX_RATE)
                attr_value = sai_thrift_attribute_value_t(oid=sched_prof_id)
                attr = sai_thrift_attribute_t(id=SAI_PORT_ATTR_QOS_SCHEDULER_PROFILE_ID, value=attr_value)
                self.client.sai_thrift_set_port_attribute(port_list[dst_port_2_id], attr)
            else:
                # Resume egress of dst port 1
                attr_value = sai_thrift_attribute_value_t(booldata=1)
                attr = sai_thrift_attribute_t(id=SAI_PORT_ATTR_PKT_TX_ENABLE, value=attr_value)
                self.client.sai_thrift_set_port_attribute(port_list[dst_port_2_id], attr)

            # allow enough time for the dut to sync up the counter values in counters_db
            time.sleep(8)
            # get a snapshot of counter values at recv and transmit ports
            # queue counters value is not of our interest here
            recv_counters_base = recv_counters
            recv_counters, queue_counters = sai_thrift_read_port_counters(self.client, port_list[src_port_id])
            xmit_counters, queue_counters = sai_thrift_read_port_counters(self.client, port_list[dst_port_id])
            xmit_2_counters, queue_counters = sai_thrift_read_port_counters(self.client, port_list[dst_port_2_id])
            xmit_3_counters, queue_counters = sai_thrift_read_port_counters(self.client, port_list[dst_port_3_id])
            # recv port pfc
            assert(recv_counters[pg] > recv_counters_base[pg])
            # recv port no ingress drop
            assert(recv_counters[INGRESS_DROP] == recv_counters_base[INGRESS_DROP])
            # xmit port no egress drop
            assert(xmit_counters[EGRESS_DROP] == xmit_counters_base[EGRESS_DROP])
            assert(xmit_2_counters[EGRESS_DROP] == xmit_2_counters_base[EGRESS_DROP])
            assert(xmit_3_counters[EGRESS_DROP] == xmit_3_counters_base[EGRESS_DROP])

            if asic_type == 'mellanox':
                # Release dst port 2
                sched_prof_id=sai_thrift_create_scheduler_profile(self.client, RELEASE_PORT_MAX_RATE)
                attr_value = sai_thrift_attribute_value_t(oid=sched_prof_id)
                attr = sai_thrift_attribute_t(id=SAI_PORT_ATTR_QOS_SCHEDULER_PROFILE_ID, value=attr_value)
                self.client.sai_thrift_set_port_attribute(port_list[dst_port_3_id], attr)
            else:
                # Resume egress of dst port 2
                attr_value = sai_thrift_attribute_value_t(booldata=1)
                attr = sai_thrift_attribute_t(id=SAI_PORT_ATTR_PKT_TX_ENABLE, value=attr_value)
                self.client.sai_thrift_set_port_attribute(port_list[dst_port_3_id], attr)

            # allow enough time for the dut to sync up the counter values in counters_db
            time.sleep(8)
            # get new base counter values at recv ports
            # queue counters value is not of our interest here
            recv_counters, queue_counters = sai_thrift_read_port_counters(self.client, port_list[src_port_id])
            assert(recv_counters[INGRESS_DROP] == recv_counters_base[INGRESS_DROP])
            recv_counters_base = recv_counters

            time.sleep(30)
            # get a snapshot of counter values at recv and transmit ports
            # queue counters value is not of our interest here
            recv_counters, queue_counters = sai_thrift_read_port_counters(self.client, port_list[src_port_id])
            xmit_counters, queue_counters = sai_thrift_read_port_counters(self.client, port_list[dst_port_id])
            xmit_2_counters, queue_counters = sai_thrift_read_port_counters(self.client, port_list[dst_port_2_id])
            xmit_3_counters, queue_counters = sai_thrift_read_port_counters(self.client, port_list[dst_port_3_id])
            # recv port no pfc
            assert(recv_counters[pg] == recv_counters_base[pg])
            # recv port no ingress drop
            assert(recv_counters[INGRESS_DROP] == recv_counters_base[INGRESS_DROP])
            # xmit port no egress drop
            assert(xmit_counters[EGRESS_DROP] == xmit_counters_base[EGRESS_DROP])
            assert(xmit_2_counters[EGRESS_DROP] == xmit_2_counters_base[EGRESS_DROP])
            assert(xmit_3_counters[EGRESS_DROP] == xmit_3_counters_base[EGRESS_DROP])

        finally:
            if asic_type == 'mellanox':
                # Release dst ports
                sched_prof_id=sai_thrift_create_scheduler_profile(self.client, RELEASE_PORT_MAX_RATE)
                attr_value = sai_thrift_attribute_value_t(oid=sched_prof_id)
                attr = sai_thrift_attribute_t(id=SAI_PORT_ATTR_QOS_SCHEDULER_PROFILE_ID, value=attr_value)
                self.client.sai_thrift_set_port_attribute(port_list[dst_port_id], attr)
                self.client.sai_thrift_set_port_attribute(port_list[dst_port_2_id], attr)
                self.client.sai_thrift_set_port_attribute(port_list[dst_port_3_id], attr)
            else:
                # Resume egress of dut xmit ports
                attr_value = sai_thrift_attribute_value_t(booldata=1)
                attr = sai_thrift_attribute_t(id=SAI_PORT_ATTR_PKT_TX_ENABLE, value=attr_value)
                self.client.sai_thrift_set_port_attribute(port_list[dst_port_id], attr)
                self.client.sai_thrift_set_port_attribute(port_list[dst_port_2_id], attr)
                self.client.sai_thrift_set_port_attribute(port_list[dst_port_3_id], attr)

class HdrmPoolSizeTest(sai_base_test.ThriftInterfaceDataPlane):
    def runTest(self):
        time.sleep(5)
        switch_init(self.client)

        # Parse input parameters
        dscps = self.test_params['dscps']
        ecn = self.test_params['ecn']
        router_mac = self.test_params['router_mac']
        pgs = [pg + 2 for pg in self.test_params['pgs']] # The pfc counter index starts from index 2 in sai_thrift_read_port_counters
        src_port_ids = self.test_params['src_port_ids']
        src_port_ips = self.test_params['src_port_ips']
        print >> sys.stderr, src_port_ips

        dst_port_id = self.test_params['dst_port_id']
        dst_port_ip = self.test_params['dst_port_ip']
        pgs_num = self.test_params['pgs_num']
        asic_type = self.test_params['sonic_asic_type']
        pkts_num_leak_out = self.test_params['pkts_num_leak_out']
        pkts_num_trig_pfc = self.test_params['pkts_num_trig_pfc']
        pkts_num_hdrm_full = self.test_params['pkts_num_hdrm_full']
        pkts_num_hdrm_partial = self.test_params['pkts_num_hdrm_partial']
        print >> sys.stderr, ("pkts num: leak_out: %d, trig_pfc: %d, hdrm_full: %d, hdrm_partial: %d" % (pkts_num_leak_out, pkts_num_trig_pfc, pkts_num_hdrm_full, pkts_num_hdrm_partial))

        dst_port_mac = self.dataplane.get_mac(0, dst_port_id)
        src_port_macs = [self.dataplane.get_mac(0, ptid) for ptid in src_port_ids]
        margin = 0
        sidx_dscp_pg_tuples = [(sidx, dscp, pgs[pgidx]) for sidx, sid in enumerate(src_port_ids) for pgidx, dscp in enumerate(dscps)]
        assert(len(sidx_dscp_pg_tuples) >= pgs_num)
        print >> sys.stderr, sidx_dscp_pg_tuples

        # get a snapshot of counter values at recv and transmit ports
        # queue_counters value is not of our interest here
        recv_counters_bases = [sai_thrift_read_port_counters(self.client, port_list[sid])[0] for sid in src_port_ids]
        xmit_counters_base, queue_counters = sai_thrift_read_port_counters(self.client, port_list[dst_port_id])

        # Pause egress of dut xmit port
        if asic_type == 'mellanox':
            # Close DST port
            sched_prof_id = sai_thrift_create_scheduler_profile(self.client, STOP_PORT_MAX_RATE)
            attr_value = sai_thrift_attribute_value_t(oid=sched_prof_id)
            attr = sai_thrift_attribute_t(id=SAI_PORT_ATTR_QOS_SCHEDULER_PROFILE_ID, value=attr_value)
            self.client.sai_thrift_set_port_attribute(port_list[dst_port_id], attr)
        else:
            # Pause egress of dut xmit port
            attr_value = sai_thrift_attribute_value_t(booldata=0)
            attr = sai_thrift_attribute_t(id=SAI_PORT_ATTR_PKT_TX_ENABLE, value=attr_value)
            self.client.sai_thrift_set_port_attribute(port_list[dst_port_id], attr)

        try:
            # send packets to leak out
            sidx = 0
            pkt = simple_tcp_packet(pktlen=64,
                        eth_dst=router_mac,
                        eth_src=src_port_macs[sidx],
                        ip_src=src_port_ips[sidx],
                        ip_dst=dst_port_ip,
                        ip_ttl=64)
            send_packet(self, src_port_ids[sidx], pkt, pkts_num_leak_out)

            # send packets to all pgs to fill the service pool
            # and trigger PFC on all pgs
            for i in range(0, pgs_num):
                # Prepare TCP packet data
                tos = sidx_dscp_pg_tuples[i][1] << 2
                tos |= ecn
                ttl = 64
                default_packet_length = 64
                pkt = simple_tcp_packet(pktlen=default_packet_length,
                                        eth_dst=router_mac,
                                        eth_src=src_port_macs[sidx_dscp_pg_tuples[i][0]],
                                        ip_src=src_port_ips[sidx_dscp_pg_tuples[i][0]],
                                        ip_dst=dst_port_ip,
                                        ip_tos=tos,
                                        ip_ttl=ttl)
                send_packet(self, src_port_ids[sidx_dscp_pg_tuples[i][0]], pkt, pkts_num_trig_pfc)

            print >> sys.stderr, "Service pool almost filled"
            # allow enough time for the dut to sync up the counter values in counters_db
            time.sleep(8)

            for i in range(0, pgs_num):
                # Prepare TCP packet data
                tos = sidx_dscp_pg_tuples[i][1] << 2
                tos |= ecn
                ttl = 64
                default_packet_length = 64
                pkt = simple_tcp_packet(pktlen=default_packet_length,
                                        eth_dst=router_mac,
                                        eth_src=src_port_macs[sidx_dscp_pg_tuples[i][0]],
                                        ip_src=src_port_ips[sidx_dscp_pg_tuples[i][0]],
                                        ip_dst=dst_port_ip,
                                        ip_tos=tos,
                                        ip_ttl=ttl)
                pkt_cnt = 0

                recv_counters, queue_counters = sai_thrift_read_port_counters(self.client, port_list[src_port_ids[sidx_dscp_pg_tuples[i][0]]])
                while (recv_counters[sidx_dscp_pg_tuples[i][2]] == recv_counters_bases[sidx_dscp_pg_tuples[i][0]][sidx_dscp_pg_tuples[i][2]]) and (pkt_cnt < 10):
                    send_packet(self, src_port_ids[sidx_dscp_pg_tuples[i][0]], pkt, 1)
                    pkt_cnt += 1
                    # allow enough time for the dut to sync up the counter values in counters_db
                    time.sleep(8)

                    # get a snapshot of counter values at recv and transmit ports
                    # queue_counters value is not of our interest here
                    recv_counters, queue_counters = sai_thrift_read_port_counters(self.client, port_list[src_port_ids[sidx_dscp_pg_tuples[i][0]]])

                if pkt_cnt == 10:
                    sys.exit("Too many pkts needed to trigger pfc: %d" % (pkt_cnt))
                assert(recv_counters[sidx_dscp_pg_tuples[i][2]] > recv_counters_bases[sidx_dscp_pg_tuples[i][0]][sidx_dscp_pg_tuples[i][2]])
                print >> sys.stderr, "%d packets for sid: %d, pg: %d to trigger pfc" % (pkt_cnt, src_port_ids[sidx_dscp_pg_tuples[i][0]], sidx_dscp_pg_tuples[i][2] - 2)

            print >> sys.stderr, "PFC triggered"

            # send packets to all pgs to fill the headroom pool
            for i in range(0, pgs_num):
                # Prepare TCP packet data
                tos = sidx_dscp_pg_tuples[i][1] << 2
                tos |= ecn
                ttl = 64
                default_packet_length = 64
                pkt = simple_tcp_packet(pktlen=default_packet_length,
                                        eth_dst=router_mac,
                                        eth_src=src_port_macs[sidx_dscp_pg_tuples[i][0]],
                                        ip_src=src_port_ips[sidx_dscp_pg_tuples[i][0]],
                                        ip_dst=dst_port_ip,
                                        ip_tos=tos,
                                        ip_ttl=ttl)

                send_packet(self, src_port_ids[sidx_dscp_pg_tuples[i][0]], pkt, pkts_num_hdrm_full if i != pgs_num - 1 else pkts_num_hdrm_partial)
                # allow enough time for the dut to sync up the counter values in counters_db
                time.sleep(8)

                recv_counters, queue_counters = sai_thrift_read_port_counters(self.client, port_list[src_port_ids[sidx_dscp_pg_tuples[i][0]]])
                # assert no ingress drop
                assert(recv_counters[INGRESS_DROP] == recv_counters_bases[sidx_dscp_pg_tuples[i][0]][INGRESS_DROP])

            print >> sys.stderr, "all but the last pg hdrms filled"

            # last pg
            i = pgs_num - 1
            # send 1 packet on last pg to trigger ingress drop
            send_packet(self, src_port_ids[sidx_dscp_pg_tuples[i][0]], pkt, 1 + 2 * margin)
            # allow enough time for the dut to sync up the counter values in counters_db
            time.sleep(8)
            recv_counters, queue_counters = sai_thrift_read_port_counters(self.client, port_list[src_port_ids[sidx_dscp_pg_tuples[i][0]]])
            # assert ingress drop
            assert(recv_counters[INGRESS_DROP] > recv_counters_bases[sidx_dscp_pg_tuples[i][0]][INGRESS_DROP])

            # assert no egress drop at the dut xmit port
            xmit_counters, queue_counters = sai_thrift_read_port_counters(self.client, port_list[dst_port_id])
            assert(xmit_counters[EGRESS_DROP] == xmit_counters_base[EGRESS_DROP])

            print >> sys.stderr, "pg hdrm filled"

        finally:
            if asic_type == 'mellanox':
                # Release port
                sched_prof_id = sai_thrift_create_scheduler_profile(self.client,RELEASE_PORT_MAX_RATE)
                attr_value = sai_thrift_attribute_value_t(oid=sched_prof_id)
                attr = sai_thrift_attribute_t(id=SAI_PORT_ATTR_QOS_SCHEDULER_PROFILE_ID, value=attr_value)
                self.client.sai_thrift_set_port_attribute(port_list[dst_port_id],attr)
            else:
                # Resume egress of dur xmit port
                attr_value = sai_thrift_attribute_value_t(booldata=1)
                attr = sai_thrift_attribute_t(id=SAI_PORT_ATTR_PKT_TX_ENABLE, value=attr_value)
                self.client.sai_thrift_set_port_attribute(port_list[dst_port_id], attr)

# TODO: remove sai_thrift_clear_all_counters and change to use incremental counter values
class DscpEcnSend(sai_base_test.ThriftInterfaceDataPlane):
    def runTest(self):
        switch_init(self.client)

        # Parse input parameters
        dscp = int(self.test_params['dscp'])
        ecn = int(self.test_params['ecn'])
        router_mac = self.test_params['router_mac']
        default_packet_length = 64
        dst_port_id = int(self.test_params['dst_port_id'])
        dst_port_ip = self.test_params['dst_port_ip']
        dst_port_mac = self.dataplane.get_mac(0, dst_port_id)
        src_port_id = int(self.test_params['src_port_id'])
        src_port_ip = self.test_params['src_port_ip']
        src_port_mac = self.dataplane.get_mac(0, src_port_id)
        num_of_pkts = self.test_params['num_of_pkts']
        limit = self.test_params['limit']
        min_limit = self.test_params['min_limit']
        cell_size = self.test_params['cell_size']

        #STOP PORT FUNCTION
        sched_prof_id=sai_thrift_create_scheduler_profile(self.client,STOP_PORT_MAX_RATE)
        attr_value = sai_thrift_attribute_value_t(oid=sched_prof_id)
        attr = sai_thrift_attribute_t(id=SAI_PORT_ATTR_QOS_SCHEDULER_PROFILE_ID, value=attr_value)
        self.client.sai_thrift_set_port_attribute(port_list[dst_port_id], attr)

        # Clear Counters
        sai_thrift_clear_all_counters(self.client)

        #send packets
        try:
            tos = dscp << 2
            tos |= ecn
            ttl = 64
            for i in range(0, num_of_pkts):
                pkt = simple_tcp_packet(pktlen=default_packet_length,
                                    eth_dst=router_mac,
                                    eth_src=src_port_mac,
                                    ip_src=src_port_ip,
                                    ip_dst=dst_port_ip,
                                    ip_tos=tos,
                                    ip_ttl=ttl)
                send_packet(self, 0, pkt)

            leaking_pkt_number = 0
            for (rcv_port_number, pkt_str, pkt_time) in self.dataplane.packets(0, 1):
                leaking_pkt_number += 1
            print "leaking packet %d" % leaking_pkt_number

            # Read Counters
            print "DST port counters: "
            port_counters, queue_counters = sai_thrift_read_port_counters(self.client, port_list[dst_port_id])
            print port_counters
            print queue_counters

            # Clear Counters
            sai_thrift_clear_all_counters(self.client)

            # Set receiving socket buffers to some big value
            for p in self.dataplane.ports.values():
                p.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 41943040)

            # RELEASE PORT
            sched_prof_id=sai_thrift_create_scheduler_profile(self.client,RELEASE_PORT_MAX_RATE)
            attr_value = sai_thrift_attribute_value_t(oid=sched_prof_id)
            attr = sai_thrift_attribute_t(id=SAI_PORT_ATTR_QOS_SCHEDULER_PROFILE_ID, value=attr_value)
            self.client.sai_thrift_set_port_attribute(port_list[dst_port_id],attr)

            # if (ecn == 1) - capture and parse all incoming packets
            marked_cnt = 0
            not_marked_cnt = 0
            if (ecn == 1):
                print ""
                print "ECN capable packets generated, releasing dst_port and analyzing traffic -"

                cnt = 0
                pkts = []
                for i in xrange(num_of_pkts):
                    (rcv_device, rcv_port, rcv_pkt, pkt_time) = dp_poll(self, device_number=0, port_number=dst_port_id, timeout=0.2)
                    if rcv_pkt is not None:
                        cnt += 1
                        pkts.append(rcv_pkt)
                    else:  # Received less packets then expected
                        assert (cnt == num_of_pkts)
                print "    Received packets:    " + str(cnt)

                for pkt_to_inspect in pkts:
                    pkt_str = hex_dump_buffer(pkt_to_inspect)

                    # Count marked and not marked amount of packets
                    if ( (int(pkt_str[ECN_INDEX_IN_HEADER]) & 0x03)  == 1 ):
                        not_marked_cnt += 1
                    elif ( (int(pkt_str[ECN_INDEX_IN_HEADER]) & 0x03) == 3 ):
                        assert (not_marked_cnt == 0)
                        marked_cnt += 1

                print "    ECN non-marked pkts: " + str(not_marked_cnt)
                print "    ECN marked pkts:     " + str(marked_cnt)
                print ""

            time.sleep(5)
            # Read Counters
            print "DST port counters: "
            port_counters, queue_counters = sai_thrift_read_port_counters(self.client, port_list[dst_port_id])
            print port_counters
            print queue_counters
            if (ecn == 0):
                transmitted_data = port_counters[TRANSMITTED_PKTS] * 2 * cell_size #num_of_pkts*pkt_size_in_cells*cell_size
                assert (port_counters[TRANSMITTED_OCTETS] <= limit * 1.05)
                assert (transmitted_data >= min_limit)
                assert (marked_cnt == 0)
            elif (ecn == 1):
                non_marked_data = not_marked_cnt * 2 * cell_size
                assert (non_marked_data <= limit*1.05)
                assert (non_marked_data >= limit*0.95)
                assert (marked_cnt == (num_of_pkts - not_marked_cnt))
                assert (port_counters[EGRESS_DROP]  == 0)
                assert (port_counters[INGRESS_DROP] == 0)

        finally:
            # RELEASE PORT
            sched_prof_id=sai_thrift_create_scheduler_profile(self.client,RELEASE_PORT_MAX_RATE)
            attr_value = sai_thrift_attribute_value_t(oid=sched_prof_id)
            attr = sai_thrift_attribute_t(id=SAI_PORT_ATTR_QOS_SCHEDULER_PROFILE_ID, value=attr_value)
            self.client.sai_thrift_set_port_attribute(port_list[dst_port_id],attr)
            print "END OF TEST"

class WRRtest(sai_base_test.ThriftInterfaceDataPlane):
    def runTest(self):
        switch_init(self.client)        

        # Parse input parameters
        ecn = int(self.test_params['ecn'])
        router_mac = self.test_params['router_mac']       
        dst_port_id = int(self.test_params['dst_port_id'])
        dst_port_ip = self.test_params['dst_port_ip']
        dst_port_mac = self.dataplane.get_mac(0, dst_port_id)
        src_port_id = int(self.test_params['src_port_id'])
        src_port_ip = self.test_params['src_port_ip']
        src_port_mac = self.dataplane.get_mac(0, src_port_id)
        print >> sys.stderr, "dst_port_id: %d, src_port_id: %d" % (dst_port_id, src_port_id)
        print >> sys.stderr, "dst_port_mac: %s, src_port_mac: %s, src_port_ip: %s, dst_port_ip: %s" % (dst_port_mac, src_port_mac, src_port_ip, dst_port_ip)
        asic_type = self.test_params['sonic_asic_type']
        default_packet_length = 1500
        exp_ip_id = 110
        queue_0_num_of_pkts = int(self.test_params['q0_num_of_pkts'])
        queue_1_num_of_pkts = int(self.test_params['q1_num_of_pkts'])
        queue_2_num_of_pkts = int(self.test_params['q2_num_of_pkts'])
        queue_3_num_of_pkts = int(self.test_params['q3_num_of_pkts'])
        queue_4_num_of_pkts = int(self.test_params['q4_num_of_pkts'])
        queue_5_num_of_pkts = int(self.test_params['q5_num_of_pkts'])
        queue_6_num_of_pkts = int(self.test_params['q6_num_of_pkts'])
        limit = int(self.test_params['limit'])
        pkts_num_leak_out = int(self.test_params['pkts_num_leak_out'])

        if asic_type == 'mellanox':
            # Stop port function
            sched_prof_id=sai_thrift_create_scheduler_profile(self.client, STOP_PORT_MAX_RATE)
            attr_value = sai_thrift_attribute_value_t(oid=sched_prof_id)
            attr = sai_thrift_attribute_t(id=SAI_PORT_ATTR_QOS_SCHEDULER_PROFILE_ID, value=attr_value)
            self.client.sai_thrift_set_port_attribute(port_list[dst_port_id], attr)
        else:
            attr_value = sai_thrift_attribute_value_t(booldata=0)
            attr = sai_thrift_attribute_t(id=SAI_PORT_ATTR_PKT_TX_ENABLE, value=attr_value)
            self.client.sai_thrift_set_port_attribute(port_list[dst_port_id], attr)

        # Send packets to leak out
        pkt = simple_tcp_packet(pktlen=64,
                    eth_dst=router_mac if router_mac != '' else dst_port_mac,
                    eth_src=src_port_mac,
                    ip_src=src_port_ip,
                    ip_dst=dst_port_ip,
                    ip_ttl=64)
        send_packet(self, src_port_id, pkt, pkts_num_leak_out)

        # Get a snapshot of counter values
        port_counters_base, queue_counters_base = sai_thrift_read_port_counters(self.client, port_list[dst_port_id])

        # Send packets to each queue based on dscp field
        dscp = 8
        tos = dscp << 2
        tos |= ecn
        pkt = simple_tcp_packet(pktlen=default_packet_length,
                    eth_dst=router_mac if router_mac != '' else dst_port_mac,
                    eth_src=src_port_mac,
                    ip_src=src_port_ip,
                    ip_dst=dst_port_ip,
                    ip_tos=tos,
                    ip_id=exp_ip_id,
                    ip_ttl=64)
        send_packet(self, src_port_id, pkt, queue_0_num_of_pkts)

        dscp = 0
        tos = dscp << 2
        tos |= ecn
        pkt = simple_tcp_packet(pktlen=default_packet_length,
                    eth_dst=router_mac if router_mac != '' else dst_port_mac,
                    eth_src=src_port_mac,
                    ip_src=src_port_ip,
                    ip_dst=dst_port_ip,
                    ip_tos=tos,
                    ip_id=exp_ip_id,
                    ip_ttl=64)
        send_packet(self, src_port_id, pkt, queue_1_num_of_pkts)

        dscp = 5
        tos = dscp << 2
        tos |= ecn
        pkt = simple_tcp_packet(pktlen=default_packet_length,
                    eth_dst=router_mac if router_mac != '' else dst_port_mac,
                    eth_src=src_port_mac,
                    ip_src=src_port_ip,
                    ip_dst=dst_port_ip,
                    ip_tos=tos,
                    ip_id=exp_ip_id,
                    ip_ttl=64)
        send_packet(self, src_port_id, pkt, queue_2_num_of_pkts)

        dscp = 3
        tos = dscp << 2
        tos |= ecn
        pkt = simple_tcp_packet(pktlen=default_packet_length,
                    eth_dst=router_mac if router_mac != '' else dst_port_mac,
                    eth_src=src_port_mac,
                    ip_src=src_port_ip,
                    ip_dst=dst_port_ip,
                    ip_tos=tos,
                    ip_id=exp_ip_id,
                    ip_ttl=64)
        send_packet(self, src_port_id, pkt, queue_3_num_of_pkts)

        dscp = 4
        tos = dscp << 2
        tos |= ecn
        pkt = simple_tcp_packet(pktlen=default_packet_length,
                    eth_dst=router_mac if router_mac != '' else dst_port_mac,
                    eth_src=src_port_mac,
                    ip_src=src_port_ip,
                    ip_dst=dst_port_ip,
                    ip_tos=tos,
                    ip_id=exp_ip_id,
                    ip_ttl=64)
        send_packet(self, src_port_id, pkt, queue_4_num_of_pkts)

        dscp = 46
        tos = dscp << 2
        tos |= ecn
        pkt = simple_tcp_packet(pktlen=default_packet_length,
                    eth_dst=router_mac if router_mac != '' else dst_port_mac,
                    eth_src=src_port_mac,
                    ip_src=src_port_ip,
                    ip_dst=dst_port_ip,
                    ip_tos=tos,
                    ip_id=exp_ip_id,
                    ip_ttl=64)
        send_packet(self, src_port_id, pkt, queue_5_num_of_pkts)

        dscp = 48
        tos = dscp << 2
        tos |= ecn
        pkt = simple_tcp_packet(pktlen=default_packet_length,
                    eth_dst=router_mac if router_mac != '' else dst_port_mac,
                    eth_src=src_port_mac,
                    ip_src=src_port_ip,
                    ip_dst=dst_port_ip,
                    ip_tos=tos,
                    ip_id=exp_ip_id,
                    ip_ttl=64)
        send_packet(self, src_port_id, pkt, queue_6_num_of_pkts)

        # Set receiving socket buffers to some big value
        for p in self.dataplane.ports.values():
            p.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 41943040)

        # Release port
        if asic_type == 'mellanox':
            sched_prof_id=sai_thrift_create_scheduler_profile(self.client, RELEASE_PORT_MAX_RATE)
            attr_value = sai_thrift_attribute_value_t(oid=sched_prof_id)
            attr = sai_thrift_attribute_t(id=SAI_PORT_ATTR_QOS_SCHEDULER_PROFILE_ID, value=attr_value)
            self.client.sai_thrift_set_port_attribute(port_list[dst_port_id], attr)
        else:
            # Resume egress of dut xmit port
            attr_value = sai_thrift_attribute_value_t(booldata=1)
            attr = sai_thrift_attribute_t(id=SAI_PORT_ATTR_PKT_TX_ENABLE, value=attr_value)
            self.client.sai_thrift_set_port_attribute(port_list[dst_port_id], attr)

        cnt = 0
        pkts = []
        recv_pkt = scapy.Ether()

        while recv_pkt:
            received = self.dataplane.poll(device_number=0, port_number=dst_port_id, timeout=2)
            if isinstance(received, self.dataplane.PollFailure):
                recv_pkt = None
                break
            recv_pkt = scapy.Ether(received.packet)

            try:
                if recv_pkt.payload.src == src_port_ip and recv_pkt.payload.dst == dst_port_ip and recv_pkt.payload.id == exp_ip_id:
                    cnt += 1
                    pkts.append(recv_pkt)
            except AttributeError:
                continue

        queue_pkt_counters = [0] * 49
        queue_num_of_pkts  = [0] * 49
        queue_num_of_pkts[8]  = queue_0_num_of_pkts
        queue_num_of_pkts[0]  = queue_1_num_of_pkts
        queue_num_of_pkts[5]  = queue_2_num_of_pkts
        queue_num_of_pkts[3]  = queue_3_num_of_pkts
        queue_num_of_pkts[4]  = queue_4_num_of_pkts
        queue_num_of_pkts[46] = queue_5_num_of_pkts
        queue_num_of_pkts[48] = queue_6_num_of_pkts
        total_pkts = 0

        for pkt_to_inspect in pkts:
            dscp_of_pkt = pkt_to_inspect.payload.tos >> 2
            total_pkts += 1

            # Count packet ordering

            queue_pkt_counters[dscp_of_pkt] += 1
            if queue_pkt_counters[dscp_of_pkt] == queue_num_of_pkts[dscp_of_pkt]:
                 assert((queue_0_num_of_pkts + queue_1_num_of_pkts + queue_2_num_of_pkts + queue_3_num_of_pkts + queue_4_num_of_pkts + queue_5_num_of_pkts + queue_6_num_of_pkts) - total_pkts < limit)

            print >> sys.stderr, queue_pkt_counters

        # Read counters
        print "DST port counters: "
        port_counters, queue_counters = sai_thrift_read_port_counters(self.client, port_list[dst_port_id])
        print >> sys.stderr, map(operator.sub, queue_counters, queue_counters_base)

        # All packets sent should be received intact
        assert(queue_0_num_of_pkts + queue_1_num_of_pkts + queue_2_num_of_pkts + queue_3_num_of_pkts + queue_4_num_of_pkts + queue_5_num_of_pkts + queue_6_num_of_pkts == total_pkts)

class LossyQueueTest(sai_base_test.ThriftInterfaceDataPlane):
    def runTest(self):
        switch_init(self.client)

        # Parse input parameters
        dscp = int(self.test_params['dscp'])
        ecn = int(self.test_params['ecn'])
        pg = int(self.test_params['pg']) + 2 # The pfc counter index starts from index 2 in sai_thrift_read_port_counters
        router_mac = self.test_params['router_mac']
        max_buffer_size = int(self.test_params['buffer_max_size'])
        headroom_size = int(self.test_params['headroom_size'])
        dst_port_id = int(self.test_params['dst_port_id'])
        dst_port_ip = self.test_params['dst_port_ip']
        dst_port_mac = self.dataplane.get_mac(0, dst_port_id)
        dst_port_2_id = int(self.test_params['dst_port_2_id'])
        dst_port_2_ip = self.test_params['dst_port_2_ip']
        dst_port_2_mac = self.dataplane.get_mac(0, dst_port_2_id)
        src_port_id = int(self.test_params['src_port_id'])
        src_port_ip = self.test_params['src_port_ip']
        src_port_mac = self.dataplane.get_mac(0, src_port_id)
        asic_type = self.test_params['sonic_asic_type']

        # prepare tcp packet date
        tos = dscp << 2
        tos |= ecn
        ttl = 64

        pkts_num_leak_out = int(self.test_params['pkts_num_leak_out'])
        pkts_num_trig_egr_drp = int(self.test_params['pkts_num_trig_egr_drp'])
        default_packet_length = 64
        pkt = simple_tcp_packet(pktlen=default_packet_length,
                                eth_dst=router_mac,
                                eth_src=src_port_mac,
                                ip_src=src_port_ip,
                                ip_dst=dst_port_ip,
                                ip_tos=tos,
                                ip_ttl=ttl)
        # get a snapshot of counter values at recv and transmit ports
        # queue_counters value is not of our interest here
        recv_counters_base, queue_counters = sai_thrift_read_port_counters(self.client, port_list[src_port_id])
        xmit_counters_base, queue_counters = sai_thrift_read_port_counters(self.client, port_list[dst_port_id])
        # add slight tolerance in threshold characterization to consider
        # the case that cpu puts packets in the egress queue after we pause the egress
        # or the leak out is simply less than expected as we have occasionally observed
        margin = 2

        if asic_type == 'mellanox':
            # Stop port function
            sched_prof_id=sai_thrift_create_scheduler_profile(self.client, STOP_PORT_MAX_RATE)
            attr_value = sai_thrift_attribute_value_t(oid=sched_prof_id)
            attr = sai_thrift_attribute_t(id=SAI_PORT_ATTR_QOS_SCHEDULER_PROFILE_ID, value=attr_value)
            self.client.sai_thrift_set_port_attribute(port_list[dst_port_id], attr)
            self.client.sai_thrift_set_port_attribute(port_list[dst_port_2_id], attr)
        else:
            # Pause egress of dut xmit port
            attr_value = sai_thrift_attribute_value_t(booldata=0)
            attr = sai_thrift_attribute_t(id=SAI_PORT_ATTR_PKT_TX_ENABLE, value=attr_value)
            self.client.sai_thrift_set_port_attribute(port_list[dst_port_id], attr)

        try:
            # send packets short of triggering egress drop
            send_packet(self, src_port_id, pkt, pkts_num_leak_out + pkts_num_trig_egr_drp - 1 - margin)
            # allow enough time for the dut to sync up the counter values in counters_db
            time.sleep(8)
            # get a snapshot of counter values at recv and transmit ports
            # queue counters value is not of our interest here
            recv_counters, queue_counters = sai_thrift_read_port_counters(self.client, port_list[src_port_id])
            xmit_counters, queue_counters = sai_thrift_read_port_counters(self.client, port_list[dst_port_id])
            # recv port no pfc
            assert(recv_counters[pg] == recv_counters_base[pg])
            # recv port no ingress drop
            assert(recv_counters[INGRESS_DROP] == recv_counters_base[INGRESS_DROP])
            # xmit port no egress drop
            assert(xmit_counters[EGRESS_DROP] == xmit_counters_base[EGRESS_DROP])

            # send 1 packet to trigger egress drop
            send_packet(self, src_port_id, pkt, 1 + 2 * margin)
            # allow enough time for the dut to sync up the counter values in counters_db
            time.sleep(8)
            # get a snapshot of counter values at recv and transmit ports
            # queue counters value is not of our interest here
            recv_counters, queue_counters = sai_thrift_read_port_counters(self.client, port_list[src_port_id])
            xmit_counters, queue_counters = sai_thrift_read_port_counters(self.client, port_list[dst_port_id])
            # recv port no pfc
            assert(recv_counters[pg] == recv_counters_base[pg])
            # recv port no ingress drop
            assert(recv_counters[INGRESS_DROP] == recv_counters_base[INGRESS_DROP])
            # xmit port egress drop
            assert(xmit_counters[EGRESS_DROP] > xmit_counters_base[EGRESS_DROP])

        finally:
            if asic_type == 'mellanox':
                # Release ports
                sched_prof_id=sai_thrift_create_scheduler_profile(self.client, RELEASE_PORT_MAX_RATE)
                attr_value = sai_thrift_attribute_value_t(oid=sched_prof_id)
                attr = sai_thrift_attribute_t(id=SAI_PORT_ATTR_QOS_SCHEDULER_PROFILE_ID, value=attr_value)
                self.client.sai_thrift_set_port_attribute(port_list[dst_port_id], attr)
                self.client.sai_thrift_set_port_attribute(port_list[dst_port_2_id], attr)
            else:
                # Resume egress of dut xmit port
                attr_value = sai_thrift_attribute_value_t(booldata=1)
                attr = sai_thrift_attribute_t(id=SAI_PORT_ATTR_PKT_TX_ENABLE, value=attr_value)
                self.client.sai_thrift_set_port_attribute(port_list[dst_port_id], attr)
