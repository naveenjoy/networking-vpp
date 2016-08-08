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
from flask import Flask
from flask_restful import Api
from flask_restful import reqparse
from flask_restful import Resource
import os
import sys
from threading import Thread
import time
import vpp
from networking_vpp import config_opts
from neutron.agent.linux import bridge_lib
from neutron.agent.linux import ip_lib
from neutron.common import constants as n_const
from oslo_config import cfg
import logging

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
    # TODO(ijw): this should be moved to the devstack code
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
                 physnets,  # physnet_name: interface-name (for vlan backed networks)
                 flat_network_if=None, #comma separated string of flat network interfaces 
                                       #for association with arbitrary physical network names
                 vxlan_src_addr=None,
                 vxlan_bcast_addr=None,
                 vxlan_vrf=None,
                 qemu_user=None,
                 qemu_group=None):
        self.vpp = vpp.VPPInterface(app.logger)
        # This is the list of flat network interfaces for providing FLAT networking
        self.flat_if = flat_network_if.split(',') if flat_network_if else []
        self.active_ifs = set() #set of used upstream interfaces for flat networking
        # This is the trunk interface for VLAN networking
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
        self.networks = {}      # (physnet, type, ID): datastruct
        self.interfaces = {}    # uuid: if idx
        self.nets= {}  #net_uuid : {data}

    def get_flat_interface(self, phys_net):
        """Return an available interface for flat networking """
        interface = self.physnets.get(phys_net, None) #If a physnet mapping is available use we use it
        if interface is None:
            for intf in self.flat_if:
                if intf not in self.active_ifs:
                    interface = intf
                    break
        app.logger.debug("Using interface:%s for flat networking" % intf)
        return interface

    def get_trunk_interface(self, phys_net):
        """ Return the trunk interface for VLAN networking corresponding to the physnet mapping """
        intf = self.physnets.get(phys_net, None)
        if intf:
            app.logger.debug("Using trunk interface:%s for VLAN networking" % intf)
        else:
            app.logger.error("Could not find a VLAN trunk interface" 
                             " for physical network:%s" % phys_net)
        return intf

    def get_vpp_ifidx(self, if_name):
        """Return VPP's interface index value for the network interface"""
        if self.vpp.get_interface(if_name):
            indx = self.vpp.get_interface(if_name).sw_if_index
            app.logger.debug("VPP interface:%s has index:%s" % (if_name, indx))
            return int(indx)
        else:
            app.logger.error("Error obtaining interface data from vpp "
                             "for interface:%s" % if_name)
            return None

    def get_vpp_intf(self, if_name):
        return self.vpp.get_interface(if_name)

    # This, here, is us creating a FLAT, VLAN or VxLAN backed network.
    # The network is created lazily when a port bind happens on the host
    def network_on_host(self, net_uuid, phys_net=None, net_type=None, seg_id=None, net_name=None):
        if net_uuid not in self.nets and net_type is not None:
            #if (net_type, seg_id) not in self.networks:
            # TODO(ijw): bridge domains have no distinguishing marks.
            # VPP needs to allow us to name or label them so that we
            # can find them when we restart
            if net_type == 'flat':
                intf = self.get_flat_interface(phys_net)
                if intf is None:
                    app.logger.error("Error: creating network as a flat network interface is not available")
                    return {}
                if_upstream = self.get_vpp_ifidx(intf)
                #if_upstream = self.flat_ifidx
                app.logger.debug('Adding upstream interface-indx:%s-%s to bridge for flat networking' % (intf, if_upstream))
                self.active_ifs.add(intf)
            elif net_type == 'vlan':
                # (najoy): If the vlan sub-interface exists, we just use it, else we create one
                intf = self.get_trunk_interface(phys_net)
                seg_id = int(seg_id)
                if intf is None:
                    app.logger.error("Error: creating network as a trunk network interface"
                                      " mapping could not be found for physnet:%s" % phys_net)
                    return {}
                trunk_ifidx = self.get_vpp_ifidx(intf)
                app.logger.debug("Activating VPP's Vlan trunk interface: %s - index:%d" % (intf, trunk_ifidx))
                self.vpp.ifup(trunk_ifidx)
                if not self.get_vpp_intf('%s.%s' % (intf, seg_id)):
                    if_upstream = self.vpp.create_vlan_subif(trunk_ifidx,
                                                             seg_id)
                else:
                    if_upstream = self.get_vpp_ifidx('%s.%s' % (intf, seg_id))
                app.logger.debug('Adding upstream trunk interface:%s.%s \
                to bridge for vlan networking' % (intf, seg_id))
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
                               'interfaces' : [], #List tuples of bound vpp ifaces [(id, props)]
                                  }
            app.logger.debug('Created network UUID:%s-%s' % (net_uuid, self.nets[net_uuid]))
        return self.nets.get(net_uuid, {})  

    def delete_network_on_host(self, net_uuid, net_type=None):
        net = self.network_on_host(net_uuid)
        if net:
            if net_type is None:
                net_type = net['network_type']
            elif net_type != net['network_type']:
                app.logger.error("Delete Network: Type mismatch "
                                 "Expecting network type:%s Got:%s" 
                                 % (net['network_type'], net_type)
                                 )
                return
            try:
                if net_type == 'flat':
                    self.active_ifs.discard(net['if_upstream'])
            except Exception:
                app.logger.error("Delete Network: network UUID:%s is unknown to agent" % net_uuid)
            bridge_domain = net.get('bridge_domain_id', None)
            if_upstream_idx = net.get('if_upstream_idx', None)
            if bridge_domain:
                self.vpp.delete_bridge_domain(bridge_domain)
            if if_upstream_idx:
                self.vpp.ifdown(if_upstream_idx)
        else:
            app.logger.error("Delete Network: network is unknown "
                             "to agent")

    def new_bridge_domain(self):
        x = self.next_bridge_id
        self.vpp.create_bridge_domain(x)
        self.next_bridge_id += 1
        return x
        
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
                app.logger.debug('External tap device %s found!'
                                 % device_name)
                app.logger.debug('Bridging tap interface %s on %s'
                                 % (device_name, bridge_name))
                if not bridge.owns_interface(device_name):
                    bridge.addif(device_name)
                else:
                    app.logger.debug('Interface: %s is already added '
                                     'to the bridge %s' %
                                     (device_name, bridge_name))
                found = True
                break
            else:
                time.sleep(2)
                wait_time -= 2
        if not found:
            app.logger.error('Failed waiting for external tap device:%s'
                             % device_name)

    def create_interface_on_host(self, if_type, uuid, mac):
        if uuid in self.interfaces:
            app.logger.debug('port %s repeat binding request - ignored' % uuid)
        else:
            app.logger.debug('binding port %s as type %s' %
                             (uuid, if_type))

            # TODO(ijw): naming not obviously consistent with
            # Neutron's naming
            name = uuid[0:11]
            bridge_name = 'br-' + name
            tap_name = 'tap' + name

            if if_type == 'maketap' or if_type == 'plugtap':
                if if_type == 'maketap':
                    iface = self.vpp.create_tap(tap_name, mac)
                    props = {'bind_type': 'maketap', 'name': tap_name}
                else:
                    int_tap_name = 'vpp' + name
                    props = {'bind_type': 'plugtap',
                             'bridge_name': bridge_name,
                             'ext_tap_name': tap_name,
                             'int_tap_name': int_tap_name}

                    app.logger.debug('Creating tap interface %s with mac %s'
                                     % (int_tap_name, mac))
                    iface = self.vpp.create_tap(int_tap_name, mac)
                    # TODO(ijw): someone somewhere ought to be sorting
                    # the MTUs out
                    br = self.ensure_bridge(bridge_name)
                    # This is the external TAP device that will be
                    # created by an agent, say the DHCP agent later in
                    # time
                    t = Thread(target=self.add_external_tap,
                               args=(tap_name, br, bridge_name,))
                    t.start()
                    # This is the device that we just created with VPP
                    if not br.owns_interface(int_tap_name):
                        br.addif(int_tap_name)
            elif if_type == 'vhostuser':
                path = get_vhostuser_name(uuid)
                iface = self.vpp.create_vhostuser(path, mac, self.qemu_user,
                                                  self.qemu_group)
                props = {'bind_type': 'vhostuser', 'path': uuid}
            else:
                raise Exception('unsupported interface type')
            self.interfaces[uuid] = (iface, props)
        return self.interfaces[uuid]

    def bind_interface_on_host(self, if_type, uuid, mac, net_type, seg_id, net_id, phys_net):
        #Bind if the network exists else create network and bind
        if self.network_on_host(net_id, phys_net, net_type, seg_id): 
            net_data = self.network_on_host(net_id)
            net_br_idx = net_data['bridge_domain_id']
            (iface, props) = self.create_interface_on_host(if_type, uuid, mac)
            net_data['interfaces'].append(self.interfaces[uuid])
            self.vpp.ifup(iface)
            self.vpp.add_to_bridge(net_br_idx, iface)
            app.logger.debug('Bound vpp interface with sw_indx:%s on bridge domain:%s' 
                % (iface, net_br_idx))
            return props
        else:
            app.logger.error('Error:Binding port:%s on network:%s failed' % (uuid, net_id))

    def unbind_interface_on_host(self, uuid, if_type, net_id):
        if uuid not in self.interfaces:
            app.logger.debug("unknown port %s unbinding request - ignored" % uuid)
        else:
            iface_idx, props = self.interfaces[uuid]
            if if_type != props['bind_type']:
                app.logger.error("Incorrect unbinding port type:%s request received" % if_type)
                app.logger.error("Expected type:%s, Received Type:%s" % (props['bind_type'], if_type))
                return 1
            app.logger.debug("unbinding port %s, recorded as type %s" % (uuid, props['bind_type']))
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
                            app.logger.debug(exc)
            else:
                app.logger.error('Unknown port type %s during unbind' % props['bind_type'])
            #Delete the interface from network data
            net_data = self.network_on_host(net_id)
            net_data['interfaces'].remove(self.interfaces[uuid])
            #Delete network if no interfaces
            if not net_data['interfaces']:
                self.delete_network_on_host(net_id)


######################################################################
class PortBind(Resource):
    bind_args = reqparse.RequestParser()
    bind_args.add_argument('mac_address', type=str, required=True)
    bind_args.add_argument('mtu', type=str, required=True)
    bind_args.add_argument('segmentation_id', type=int, required=True)
    bind_args.add_argument('network_type', type=str, required=True)
    bind_args.add_argument('host', type=str, required=True)
    bind_args.add_argument('binding_type', type=str, required=True)
    bind_args.add_argument('network_id', type=str, required=True)
    bind_args.add_argument('physnet', type=str, required=True)

    def __init(self, *args, **kwargs):
        super('PortBind', self).__init__(*args, **kwargs)

    def put(self, id):
        global vppf
        args = self.bind_args.parse_args()
        app.logger.debug("on host %s, binding %s %d to mac %s id %s as binding_type %s"
                         "on network %s"
                         % (args['host'],
                            args['network_type'],
                            args['segmentation_id'],
                            args['mac_address'], 
                            id,
                            args['binding_type'],
                            args['network_id'])
                         )
        binding_type = args['binding_type']
        if binding_type in [ 'vhostuser', 'plugtap']:
            app.logger.debug('Creating a port:%s with %s binding on host %s' % 
                              (id, bindng_type, args['host']))
            vppf.bind_interface_on_host(
                                    binding_type,
                                    id,
                                    args['mac_address'],
                                    args['network_type'],
                                    args['segmentation_id'],
                                    args['network_id'],
                                    args['physnet']
                                    )
        else:
            app.logger.error('Unsupported binding type :%s requested' % binding_type)


class PortUnbind(Resource):
    bind_args = reqparse.RequestParser()
    bind_args.add_argument('host', type=str, required=True)
    bind_args.add_argument('binding_type', type=str, required=True)
    bind_args.add_argument('network_id', type=str, required=True)

    def __init(self, *args, **kwargs):
        super('PortUnbind', self).__init__(*args, **kwargs)

    def put(self, id):
        global vppf
        args = self.bind_args.parse_args()
        app.logger.debug('on host %s, unbinding port:%s with binding_type:%s '
                         'on network:%s'
                         % (args['host'],
                            id,
                            args['binding_type']),
                            args['network_id']
                         )
        vppf.unbind_interface_on_host(id, args['binding_type'], args['network_id'])



# Basic Flask RESTful app setup with logging
app = Flask('vpp-agent')
app.debug = True
ch = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(message)s')
ch.setFormatter(formatter)
app.logger.addHandler(ch)
app.logger.debug('Debug logging enabled')


def main():
    cfg.CONF(sys.argv[1:])

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

    global vppf
    physnet_list = cfg.CONF.ml2_vpp.physnets.replace(' ', '').split(',')
    app.logger.debug('Physnet list: %s' % str(physnet_list))
    physnets = {}
    for f in physnet_list:
        if f:
            try:
                (k, v) = f.split(':')
                physnets[k] = v
            except:
                app.logger.error("Could not parse physnet to interface mapping "
                                 "check the format in the config file: "
                                 "physnets = physnet1:<interface1>,physnet2:<interface2>")

    vppf = VPPForwarder(physnets,
                        flat_network_if=cfg.CONF.ml2_vpp.flat_network_if,
                        vxlan_src_addr=cfg.CONF.ml2_vpp.vxlan_src_addr,
                        vxlan_bcast_addr=cfg.CONF.ml2_vpp.vxlan_bcast_addr,
                        vxlan_vrf=cfg.CONF.ml2_vpp.vxlan_vrf,
                        qemu_user=qemu_user,
                        qemu_group=qemu_group)

    api = Api(app)
    api.add_resource(PortBind, '/ports/<id>/bind')
    api.add_resource(PortUnbind, '/ports/<id>/unbind')
    api.add_resource(Network, '/networks/<id>')
    app.logger.debug("Starting VPP agent on host address: 0.0.0.0 and port 2704")
    app.run(host='0.0.0.0', port=2704)

if __name__ == '__main__':
    main()
