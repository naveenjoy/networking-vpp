# Copyright (c) 2016 Cisco Systems, Inc.
# All Rights Reserved
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.


# This is a simple Flask application that provides REST APIs by which
# compute and network services can communicate, plus a REST API for
# debugging using a CLI client.

# Note that it does *NOT* at this point have a persistent database, so
# restarting this process will make Gluon forget about every port it's
# learned, which will not do your system much good (the data is in the
# global 'backends' and 'ports' objects).  This is for simplicity of
# demonstration; we have a second codebase already defined that is
# written to OpenStack endpoint principles and includes its ORM, so
# that work was not repeated here where the aim was to get the APIs
# worked out.  The two codebases will merge in the future.

import distro
import etcd
import json
import os
import re
import sys
from threading import Thread
import time
import traceback
import vpp
from networking_vpp import config_opts
from neutron.agent.linux import bridge_lib
from neutron.agent.linux import ip_lib
from neutron.agent.linux import utils
from neutron.common import constants as n_const
from oslo_config import cfg
from oslo_log import log as logging
from urllib3.exceptions import ReadTimeoutError
from urllib3 import Timeout

LOG = logging.getLogger(__name__)
######################################################################

# This mirrors functionality in Neutron so that we're creating a name
# that Neutron can find for its agents.

DEV_NAME_PREFIX = n_const.TAP_DEVICE_PREFIX

def get_tap_name(uuid):
    return n_const.TAP_DEVICE_PREFIX + uuid[0:11]

# This mirrors functionality in Nova so that we're creating a vhostuser
# name that it will be able to locate

VHOSTUSER_DIR = '/tmp'


def get_vhostuser_name(uuid):
    return os.path.join(VHOSTUSER_DIR, uuid)


def get_distro_family():
    if distro.id() in ['rhel', 'centos', 'fedora']:
        return 'redhat'
    else:
        return distro.id()

def get_qemu_default():
    distro = get_distro_family()
    if distro == 'redhat':
        qemu_user = 'qemu'
        qemu_group = 'qemu'
    elif distro == 'ubuntu':
        qemu_user = 'libvirt-qemu'
        qemu_group = 'libvirtd'
    else:
        # let's just try libvirt-qemu for now, maybe we should instead
        # print error messsage and exit?
        qemu_user = 'libvirt-qemu'
        qemu_group = 'kvm'

    return (qemu_user, qemu_group)

######################################################################

class VPPForwarder(object):
    def __init__(self,
                 physnets,  #physnet_name:interface-name
                 vxlan_src_addr=None,
                 vxlan_bcast_addr=None,
                 vxlan_vrf=None,
                 qemu_user=None,
                 qemu_group=None):

        self.vpp = vpp.VPPInterface(LOG)
        self.physnets = physnets
        self.qemu_user = qemu_user
        self.qemu_group = qemu_group
        # This is the address we'll use if we plan on broadcasting
        # vxlan packets
        self.vxlan_bcast_addr = vxlan_bcast_addr
        self.vxlan_src_addr = vxlan_src_addr
        self.vxlan_vrf = vxlan_vrf
        # Used as a unique number for bridge IDs
        self.next_bridge_id = 5678
        self.networks = {}      #(physnet, type, ID): datastruct
        self.interfaces = {}    #uuid: if idx
        self.nets= {}  #net_uuid:{data} #Used to track network to ports used

    def get_interface(self, phys_net):
        """ Return the trunk interface for VLAN networking corresponding to 
        the physnet mapping """
        intf = self.physnets.get(phys_net, None)
        if intf:
            LOG.debug("Using trunk interface:%s for VLAN networking" % intf)
        else:
            LOG.error("Could not find a VLAN trunk interface" 
                      " for physical network:%s" % phys_net)
        return intf

    def get_vpp_ifidx(self, if_name):
        """Return VPP's interface index value for the network interface"""
        if self.vpp.get_interface(if_name):
            indx = self.vpp.get_interface(if_name).sw_if_index
            LOG.debug("VPP interface:%s has index:%s" % (if_name, indx))
            return int(indx)
        else:
            LOG.error("Error obtaining interface data from vpp"
                      " for interface:%s" % if_name)
            return None

    def new_bridge_domain(self):
        #TODO(najoy): Need to check if the bridge domain exists on the host
        x = self.next_bridge_id
        self.vpp.create_bridge_domain(x)
        self.next_bridge_id += 1
        return x

    def get_vpp_intf(self, if_name):
        return self.vpp.get_interface(if_name)

    # This, here, is us creating a FLAT, VLAN or VxLAN backed network.
    # The network is created lazily when a port bind happens on the host
    def network_on_host(self, 
                        net_uuid, 
                        phys_net=None, 
                        net_type=None, 
                        seg_id=None, 
                        net_name=None):
        """ If only the net_uuid is provided, we try to return the network data
        else the agent will attempt to create a network on the host
        """
        if net_uuid not in self.nets and net_type is not None:
            intf = self.get_interface(phys_net)
            if intf is None:
                LOG.error("Error: creating network as a flat network "
                              "interface is not available on the host")
                return {}
            
            if net_type == 'flat':
                if_upstream = self.get_vpp_ifidx(intf)
                #if_upstream = self.flat_ifidx
                LOG.debug('Adding upstream interface-indx:%s-%s to bridge ' 
                          'for flat networking' % (intf, if_upstream))
            
            elif net_type == 'vlan':
                trunk_ifidx = self.get_vpp_ifidx(intf)
                if trunk_ifidx is None:
                    return {}
                LOG.debug("Activating VPP's Vlan trunk interface: "
                          "%s - index:%d" % (intf, trunk_ifidx))
                self.vpp.ifup(trunk_ifidx)
                if not self.get_vpp_intf('%s.%s' % (intf, seg_id)):
                    if_upstream = self.vpp.create_vlan_subif(trunk_ifidx, 
                                                             int(seg_id))
                else:
                    if_upstream = self.get_vpp_ifidx('%s.%s' % (intf, seg_id))
                    LOG.debug('Adding upstream trunk interface:%s.%s '
                              'to bridge for vlan networking' % (intf, seg_id))
                self.vpp.set_vlan_tag_rewrite(if_upstream)
            else:
                raise Exception('network type %s not supported', net_type)
            self.vpp.ifup(if_upstream)
            # May not remain this way but we use the VLAN ID as the
            # bridge ID; TODO(ijw): bridge ID can already exist, we
            # should check till we find a free one
            id = self.new_bridge_domain()
            self.vpp.add_to_bridge(id, if_upstream)
            #self.networks[(net_type, seg_id)] = id
            self.nets[net_uuid] = {
                               'bridge_domain_id': id,
                               'if_upstream': intf,
                               'if_upstream_idx': if_upstream,
                               'network_type': net_type,
                               'segmentation_id': seg_id,
                               'network_name' : net_name,
                               'interfaces' : set(), #set of bound ports
                                  }
            LOG.debug('Created network UUID:%s-%s ' 
                      % (net_uuid, self.nets[net_uuid]))
        return self.nets.get(net_uuid, {})  

    def delete_network_on_host(self, net_uuid, net_type=None):
        """ Lazily deletes the network resources when the last 
        port is unbound""" 
        net = self.network_on_host(net_uuid)
        if net:
            if net_type is None:
                net_type = net['network_type']
            elif net_type != net['network_type']:
                LOG.error("Delete Network: Type mismatch "
                          "Expecting network type:%s Got:%s" 
                          % (net['network_type'], net_type)
                          )
                return
            bridge_domain = net.get('bridge_domain_id', None)
            if_upstream_idx = net.get('if_upstream_idx', None)
            #(najoy):Do not delete VPP bridge domain due to re-connectivity issues
            # if bridge_domain:
            #     self.vpp.delete_bridge_domain(bridge_domain)
            if if_upstream_idx:
                self.vpp.ifdown(if_upstream_idx)
            self.nets.pop(net_uuid, None)
        else:
            LOG.error("Delete Network: network is unknown "
                             "to agent")

    ###############################
    # Source: Neutron LB Driver
    def _bridge_exists_and_ensure_up(self, bridge_name):
        """Check if the bridge exists and make sure it is up."""
        br = ip_lib.IPDevice(bridge_name)
        br.set_log_fail_as_error(False)
        try:
            # If the device doesn't exist this will throw a RuntimeError
            br.link.set_up()
        except RuntimeError:
            return False
        return True

    def ensure_bridge(self, bridge_name):
        """Create a bridge unless it already exists."""
        # _bridge_exists_and_ensure_up instead of device_exists is used here
        # because there are cases where the bridge exists but it's not UP,
        # for example:
        # 1) A greenthread was executing this function and had not yet executed
        # "ip link set bridge_name up" before eventlet switched to this
        # thread running the same function
        # 2) The Nova VIF driver was running concurrently and had just created
        #    the bridge, but had not yet put it UP
        if not self._bridge_exists_and_ensure_up(bridge_name):
            bridge_device = bridge_lib.BridgeDevice.addbr(bridge_name)
            if bridge_device.setfd(0):
                return
            if bridge_device.disable_stp():
                return
            if bridge_device.disable_ipv6():
                return
            if bridge_device.link.set_up():
                return
        else:
            bridge_device = bridge_lib.BridgeDevice(bridge_name)
        return bridge_device

    # TODO(njoy): make wait_time configurable
    # TODO(ijw): needs to be one thread for all waits
    def add_external_tap(self, device_name, bridge, bridge_name):
        """Add an externally created TAP device to the bridge

        Wait for the external tap device to be created by the DHCP agent.
        When the tap device is ready, add it to bridge Run as a thread
        so REST call can return before this code completes its
        execution.

        """
        wait_time = 60
        found = False
        while wait_time > 0:
            if ip_lib.device_exists(device_name):
                LOG.debug('External tap device %s found!'
                                 % device_name)
                LOG.debug('Bridging tap interface %s on %s'
                                 % (device_name, bridge_name))
                if not bridge.owns_interface(device_name):
                    bridge.addif(device_name)
                else:
                    LOG.debug('Interface: %s is already added '
                                     'to the bridge %s' %
                                     (device_name, bridge_name))
                found = True
                break
            else:
                time.sleep(2)
                wait_time -= 2
        if not found:
            LOG.error('Failed waiting for external tap device:%s'
                             % device_name)

    def create_interface_on_host(self, if_type, uuid, mac):
        if uuid in self.interfaces:
            LOG.debug('port %s repeat binding request - ignored' % uuid)
        else:
            LOG.debug('binding port %s as type %s' % (uuid, if_type))
            # TODO(ijw): naming not obviously consistent with
            # Neutron's naming
            name = uuid[0:11]
            bridge_name = 'br-' + name
            tap_name = 'tap' + name
            if if_type == 'maketap' or if_type == 'plugtap':
                if if_type == 'maketap':
                    iface_idx = self.vpp.create_tap(tap_name, mac)
                    props = {'name': tap_name}
                else:
                    int_tap_name = 'vpp' + name
                    props = {'bridge_name': bridge_name,
                             'ext_tap_name': tap_name,
                             'int_tap_name': int_tap_name
                             }
                    LOG.debug('Creating tap interface %s with mac %s'
                               % (int_tap_name, mac))
                    iface_idx = self.vpp.create_tap(int_tap_name, mac)
                    # TODO(ijw): someone somewhere ought to be sorting
                    # the MTUs out
                    br = self.ensure_bridge(bridge_name)
                    # This is the external TAP device that will be
                    # created by the neutron (q-router/dhcp)agent
                    t = Thread(target=self.add_external_tap,
                               args=(tap_name, br, bridge_name,))
                    t.start()
                    # This is the device that we just created with VPP
                    if not br.owns_interface(int_tap_name):
                        br.addif(int_tap_name)
            elif if_type == 'vhostuser':
                path = get_vhostuser_name(uuid)
                iface_idx = self.vpp.create_vhostuser(path, 
                                                      mac, 
                                                      self.qemu_user,
                                                      self.qemu_group)
                props = {'path': path}
            else:
                raise Exception('unsupported interface type')
            props['bind_type'] = if_type
            props['iface_idx'] = iface_idx
            props['mac'] = mac
            self.interfaces[uuid] = props
        return self.interfaces[uuid]

    def bind_interface_on_host(self, uuid, if_type, mac, 
                               net_type, seg_id, net_id, phys_net):
        #Bind if the network exists else create network and bind
        if self.network_on_host(net_id, phys_net, net_type, seg_id): 
            net_data = self.network_on_host(net_id)
            net_br_idx = net_data['bridge_domain_id']
            props = self.create_interface_on_host(if_type, uuid, mac)
            props['network_id'] = net_id
            iface_idx = props['iface_idx']
            net_data['interfaces'].add(uuid)
            self.vpp.ifup(iface_idx)
            self.vpp.add_to_bridge(net_br_idx, iface_idx)
            LOG.debug('Bound vpp interface with sw_indx:%s '
                       'on bridge domain:%s' % (iface_idx, net_br_idx))
            return props
        else:
            LOG.error('Error:Binding port:%s on network:%s failed' % (uuid, net_id))

    def unbind_interface_on_host(self, uuid):
        if uuid not in self.interfaces:
            LOG.debug("unknown port %s unbinding request - ignored" % uuid)
        else:
            props = self.interfaces[uuid]
            iface_idx = props['iface_idx']
            LOG.debug("unbinding port %s, recorded as type %s " % 
                       (uuid, props['bind_type']))
            # We no longer need this interface.  Specifically if it's
            # a vhostuser interface it's annoying to have it around
            # because the VM's memory (hugepages) will not be
            # released.  So, here, we destroy it.
            if props['bind_type'] == 'vhostuser':
                self.vpp.delete_vhostuser(iface_idx)
            elif props['bind_type'] in ['maketap', 'plugtap']:
                self.vpp.delete_tap(iface_idx)
                if props['bind_type'] == 'plugtap':
                    name = uuid[0:11]
                    bridge_name = 'br-' + name
                    bridge = bridge_lib.BridgeDevice(bridge_name)
                    if bridge.exists():
                        try:
                            if bridge.owns_interface(props['int_tap_name']):
                                bridge.delif(props['int_tap_name'])
                            if bridge.owns_interface(props['ext_tap_name']):
                                bridge.delif(props['ext_tap_name'])
                            bridge.link.set_down()
                            bridge.delbr()
                        except Exception as exc:
                            LOG.debug(exc)
            else:
                LOG.error('Unknown port type %s during unbind' % 
                            props['bind_type'])
                return
            #Delete the interface from network data
            net_data = self.network_on_host(props['network_id'])
            net_data['interfaces'].discard(uuid)
            LOG.debug("Removed port(%s) from used interfaces of net_data(%s)" %
                        (uuid, str(net_data)))
            #Delete structures of unused network and free up resources
            if not net_data['interfaces']:
                LOG.debug("Deleting unused network resources for network: %s" 
                          % net_data)
                self.delete_network_on_host(props['network_id'])



LEADIN = '/networking-vpp'  # TODO: make configurable?
class EtcdListener(object):
    def __init__(self, host, etcd_client, vppf, physnets):
        self.host = host
        self.etcd_client = etcd_client
        self.vppf = vppf
        self.physnets = physnets
        self.CONNECT = 15 # etcd connect timeout
        self.HEARTBEAT = 20 # read timeout in seconds
        # We need certain directories to exist
        self.mkdir(LEADIN + '/state/%s/ports' % self.host)
        self.mkdir(LEADIN + '/nodes/%s/ports' % self.host)

    def mkdir(self, path):
        try:
            self.etcd_client.write(path, None, dir=True)
        except etcd.EtcdNotFile:
            # Thrown when the directory already exists, which is fine
            pass

    def repop_interfaces(self):
        pass

    # The vppf bits
    def unbind(self, id):
        self.vppf.unbind_interface_on_host(id)

    def bind(self, id, binding_type, mac_address, physnet, 
        network_type, segmentation_id, network_id):
        return self.vppf.bind_interface_on_host(
                     id,
                     binding_type,
                     mac_address,
                     network_type,
                     segmentation_id,
                     network_id,
                     physnet
                     )

    def _sync_state(self, port_key_space):
        """
        Sync the port state in etcd with that of vpp
        return the watch index for the next event
        """
        LOG.debug('Syncing VPP port state..')
        #TODO(najoy): Clean up existing (possibly) stale port states
        rv = self.etcd_client.read(port_key_space, recursive=True )
        for child in rv.children:
            m = re.match(port_key_space + '/([^/]+)$', child.key)
            if m:
                port = m.group(1)
                LOG.debug('Syncing vpp state by binding existing port:%s' % port)
                data = json.loads(child.value)
                props = self.bind(
                                  port,
                                  data['binding_type'],
                                  data['mac_address'],
                                  data['physnet'],
                                  data['network_type'],
                                  data['segmentation_id'],
                                  data['network_id']
                                  )
                if props:
                    self.etcd_client.write(LEADIN + '/state/%s/ports/%s'
                                        % (self.host, port), json.dumps(props))
        return self._recover_etcd_state(port_key_space)

    def _clear_state(self, key_space):
        """
        Clear all the keys in the key_space directory
        """
        LOG.debug("Clearing key space: %s" % key_space)
        try:
            rv =  self.etcd_client.read(key_space)
            for child in rv.children:
                self.etcd_client.delete(child.key)
        except etcd.EtcdNotFile:
            pass

    def _recover_etcd_state(self, key_space):
        """
        Recover the current state of the watching keyspace if we miss all the
        1000 events. This is done by reading the key_space and re-starting the 
        watch from etcd_index + 1
        """
        LOG.debug("Recovering etcd key space: %s" % key_space)
        rv = self.etcd_client.read(key_space)
        return rv.etcd_index + 1


    def process_ops(self):
        # TODO(ijw): needs to remember its last tick on reboot, or
        # reconfigure from start (which means that VPP needs it
        # storing, so it's lost on reboot of VPP)
        physnets = self.physnets.keys()
        for f in physnets:
            self.etcd_client.write(LEADIN + '/state/%s/physnets/%s' % (self.host, f), 1)
        #Set the port keyspace to watch
        port_key_space = LEADIN + "/nodes/%s/ports" % self.host
        state_key_space = LEADIN + "/state/%s/ports" % self.host
        self._clear_state(state_key_space)
        tick = self._sync_state(port_key_space)
        LOG.debug("Starting watch on ports key space from Index: %s" % tick)
        while True:
            # The key that indicates to people that we're alive
            # (not that they care)
            self.etcd_client.write(LEADIN + '/state/%s/alive' % self.host,
                                   1, ttl=3*self.HEARTBEAT)
            try:
                LOG.debug("ML2_VPP(%s): thread watching" % self.__class__.__name__)
                rv = self.etcd_client.read(port_key_space,
                                           recursive=True,
                                           waitIndex=tick,
                                           wait=True,
                                           timeout=Timeout(
                                                        connect=None,
                                                        read=None))
                LOG.debug('watch received %s on %s at tick %s with data %s' %
                           (rv.action, rv.key, rv.modifiedIndex, rv.value))
                tick = rv.modifiedIndex+1
                LOG.debug("ML2_VPP(%s): thread active" % self.__class__.__name__)
                # Matches a port key, gets host and uuid
                m = re.match(port_key_space + '/([^/]+)$', rv.key)
                if m:
                    port = m.group(1)
                    if rv.action == 'delete':
                        # Removing key == desire to unbind
                        self.unbind(port)
                        try:
                            self.etcd_client.delete(port_key_space + '/%s' % port)
                        except etcd.EtcdKeyNotFound:
                            # Gone is fine, if we didn't delete it it's no problem
                            pass
                    else:
                        # Create or update == bind
                        data = json.loads(rv.value)
                        props = self.bind(
                                  port,
                                  data['binding_type'],
                                  data['mac_address'],
                                  data['physnet'],
                                  data['network_type'],
                                  data['segmentation_id'],
                                  data['network_id']
                                  )
                        if props:
                            self.etcd_client.write(state_key_space + '/%s'
                                                   % port, json.dumps(props))
                else:
                    LOG.warn('Unexpected key change in etcd port feedback')

            except (etcd.EtcdWatchTimedOut, etcd.EtcdConnectionFailed):
                # This is normal
                pass
            except etcd.EtcdEventIndexCleared:
                LOG.debug("etcd event index cleared. recovering the etcd watch index")
                #Reset the watch Index as etcd only keeps a buffer of 1000 events
                tick = self._recover_etcd_state(port_key_space)
                LOG.debug("Etcd watch index recovered at %s" % tick)
            except etcd.EtcdException as e:
                LOG.debug('Received an etcd exception: %s' % type(e))
            except ReadTimeoutError:
                LOG.debug('Etcd read timed out')
            except etcd.EtcdError as e:
                LOG.debug('Agent received an etcd error: %s' % str(e))
            except Exception as e:
                LOG.debug('Agent received exception of type %s' % type(e))
                time.sleep(1) # TODO(ijw): prevents tight crash loop, but adds latency
                # Should be specific to etcd faults, should have sensible behaviour
                # Don't just kill the thread...


class VPPService(object):
    "Provides functionality to manage the VPP service"
    
    @classmethod
    def restart(cls):
        cmd = [ 'service', 'vpp', 'restart']
        return utils.execute(cmd, run_as_root=True)

def main():
    cfg.CONF(sys.argv[1:])
    logging.setup(cfg.CONF, 'VPPAgent')
    LOG.debug('Restarting VPP..')
    VPPService.restart()
    #TODO(najoy). check if VPP's state is actually up
    time.sleep(5)  #wait for VPP to become active
    # If the user and/or group are specified in config file, we will use
    # them as configured; otherwise we try to use defaults depending on
    # distribution. Currently only supporting ubuntu and redhat.
    qemu_user = cfg.CONF.ml2_vpp.qemu_user
    qemu_group = cfg.CONF.ml2_vpp.qemu_group
    default_user, default_group = get_qemu_default()
    if not qemu_user:
        qemu_user = default_user
    if not qemu_group:
        qemu_group = default_group

    physnet_list = cfg.CONF.ml2_vpp.physnets.replace(' ', '').split(',')
    physnets = {}
    for f in physnet_list:
        if f:
            try:
                (k, v) = f.split(':')
                physnets[k] = v
            except:
                LOG.error("Could not parse physnet to interface mapping "
                          "check the format in the config file: "
                          "physnets = physnet1:<interface1>,physnet2:<interface2>")
    vppf = VPPForwarder(physnets,
                        vxlan_src_addr=cfg.CONF.ml2_vpp.vxlan_src_addr,
                        vxlan_bcast_addr=cfg.CONF.ml2_vpp.vxlan_bcast_addr,
                        vxlan_vrf=cfg.CONF.ml2_vpp.vxlan_vrf,
                        qemu_user=qemu_user,
                        qemu_group=qemu_group)
    LOG.debug("setting etcd client to host:%s port:%s" % 
                       (cfg.CONF.ml2_vpp.etcd_host,
                        cfg.CONF.ml2_vpp.etcd_port,)
                       )
    etcd_client = etcd.Client(
                        host=cfg.CONF.ml2_vpp.etcd_host,
                        port=cfg.CONF.ml2_vpp.etcd_port,
                        allow_reconnect=True
                        )
    ops = EtcdListener(cfg.CONF.host, etcd_client, vppf, physnets)
    ops.process_ops()


if __name__ == '__main__':
    main()
