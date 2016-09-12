# Introduction

The VPP platform, provided by the FD.io project (https://FD.io) is a production quality, high performing virtual switch that runs on commodity hardware. The networking-vpp ML2 mechanism driver enables high performance L2 networking within OpenStack by binding neutron ports with the VPP virtual forwarder. This driver currently supports VLAN and Flat networking ml2 type drivers and performs port binding for virtual interfaces (VIFs) of type vhost-user and tap.

# Design principles

The networking-vpp ML2 driver software design follows the basic OpenStack cloud software design tenets. The main design goals being scalability, simplicity and availability. The key design principles are:

1) All communications within the system are performed asynchronously. 

2) The software components are designed to be horizontally scalable and the state information is maintained in a highly scalable and available distributed key value store (etcd). 

3) All modules are unit and system tested to validate proper functionality. 

# Major components

The software architecture of the ML2 driver consists of the following main components:

1) The networking-vpp ML2 driver, which implements the neutron ML2 mechanism driver APIs. This driver is loaded by the ML2 framework and runs on the controller node.

2) The VPP agent, which runs on each compute node and programs the VPP data plane.

3) An etcd (version >= 3.0.x) cluster for storing agent state and communication between the driver and agent. The etcd cluster members run on the controller node(s). For etcd authentication (optional), an username and password could be used at the driver and agent.

       Note: The communication between the ML2 driver and VPP agent uses the native REST based interface of etcd and does not use the RabbitMQ messaging used by OpenStack services

4) The VPP switch platform, a high performance packet-processing stack, running on each compute node in the userspace. 
  	
# The vhost-user VIF type

In order to fully leverage the high performance vector packet processing technology used by VPP (https://fd.io/technology), virtual machines are provisioned under QEMU/KVM using the vhost-user vif-type port binding. The networking-vpp agent sets up shared memory communication between the guests and VPP using a UNIX domain socket based mechanism, enabling Vring resources to be shared directly between the two user-space processes, skipping the QEMU and kernel processing overheads.

Prerequisites for vhost-user:
 	
1)	Ensure that hugepages are enabled in the kernel command line and intel_iommu is set to pt. For instance, to enable 2048 2M hugepages, add the below line to /etc/default/grub for GRUB_CMDLINE_LINUX. Then perform a sudo update-grub and reboot the system for the changes to take effect.

        intel_iommu=pt default_hugepagesz=2M hugepagesz=2M hugepages=2048

2)	Build and install the VPP Platform and its python API bindings

        Refer to: http://wiki.fd.io/view/VPP (Building and Installing a VPP Package)

3)	Install QEMU emulator patches with vhost-user support if necessary.  We have tested this driver on Ubuntu 16.04 LTS, which ships with the QEMU emulator version 2.5.0 that has the required vhost-user support. 

4)	Enable QEMU guest memory allocation with hugepages by setting the hw:mem_page_size extra specification in the flavor. To allocate 2048MB hugepage size for guests on flavor m1.small use:

        nova flavor-key m1.small set hw:mem_page_size=2048 
     
5) Edit the /etc/libvirt/qemu.conf and set the following to prevent any access denial issues for vhost-user

       security_driver = "none"
     
       user = "root"
     
       group = "libvirtd"
     
       cgroup_device_acl = [
       "/dev/null", "/dev/full", "/dev/zero",
       "/dev/random", "/dev/urandom",
       "/dev/ptmx", "/dev/kvm", "/dev/kqemu",
       "/dev/rtc", "/dev/hpet","/dev/net/tun",
       ]


# Port binding overview
	
When an instance is spawned by nova-compute, it calls neutron to bind the port.  This results in a bind_port call to the ML2 plugin driver.  If the binding is successful and the results are committed by the plugin,it calls the driver to update the port. The driver handles port update calls by implementing the corresponding ML2 plugin pre-commit and post-commit API calls.  The ML2 plugin framework makes the pre-commit call as part of a DB transaction and the post-commit call after the transaction has been committed.


# Networking-vpp driver

The class VPPMechanismDriver implements the ML2 plugin framework methods for handling neutron port level operations such as bindings, updates and deletes. When a neutron port is updated, if the networking-vpp driver is responsible for binding the port, it tells the VPP agent running on the hypervisor that it has work to do i.e. to program the VPP data plane. Due to the distributed nature of the system, the driver first logs this requirement to a journal (ML2 pre-commit phase) and then it notifies a background thread to read the journal entries and create the port key/value pairs in etcd. For journalling, the ML2 driver uses a table in the existing neutron MySQL database.

The driver creates a port key in a directory setup for storing the port information for the node. The layout for storing port information in etcd is as below:

    Here the variable LEADIN=/networking-vpp
  
    Directory: LEADIN/nodes – subdirs are compute nodes

    LEADIN/nodes/X/ports  (port-space), where X=compute-node-name. 
    The entries are JSON strings containing all the information on each 
    bound port on the compute node. The deletion of an entry in this directory refers to an 
    unbind action.

The VPP agent running on the compute node watches the corresponding node directory within etcd recursively for work to do. When it receives a watch notification, it performs the action by programming the VPP data plane using the VPP_PAPI python interface and writes the return state of the port into etcd. 

The ML2 driver runs a thread that polls etcd for the return state of the port created in VPP. The return state information is stored in etcd using the below directory structure.

     LEADIN/state/nodes/X, where X=compute-node-name

     LEADIN/state/nodes/X/alive  - heartbeat back

     LEADIN/state/nodes/X/ports (state-space) - return port state

     LEADIN/state/nodes/X/physnets – physnets present on the hypervisor

A key in the state_space directory indicates that the port has been bound and is receiving traffic.
When the driver detects that the agent has successfully created the port, i.e. VPP has dropped a vhost-user socket where it can be found by QEMU, it sends a notification to nova compute to start the VM. 

    Pro-tip: Use etcdctl watch --recursive --forever / to see the two ends fiddling with the data,   which is (a) cool and (b) really useful for debugging

Sample JSON node entries in etcd port-space for a vhostuser port type created on a compute node named "server-2"

    {
    "action": "get" 
    "node": {
        "key": "/networking-vpp/nodes",
        "dir": true,
        "nodes": [
            {
                "key": "/networking-vpp/nodes/server-2",
                "dir": true,
                "nodes": [
                    {
                        "key": "/networking-vpp/nodes/server-2/ports",
                        "dir": true,
                        "nodes": [
                            {
                                "key": "/networking-vpp/nodes/server-2/ports/fd07aa8f-4572-4eba-af75-16c7c59e544c",
                                "value": "{\"network_id\": \"04016a26-b571-458d-9f2e-524aee598a37\", \"segmentation_id\": 2070, \"mtu\": 1500, \"binding_type\": \"vhostuser\", \"mac_address\": \"fa:16:3e:bd:5d:c8\", \"network_type\": \"vlan\", \"physnet\": \"physnet1\"}",
                                "modifiedIndex": 458419,
                                "createdIndex": 458419
                            },
                        ],
                        "modifiedIndex": 36,
                        "createdIndex": 36
                    }
                ],
                "modifiedIndex": 36,
                "createdIndex": 36
            }
        ],
        "modifiedIndex": 36,
        "createdIndex": 36
      }
    }


# Networking-vpp agent
The VPP agent runs on the compute nodes and programs the VPP control plane as per the neutron networking model determined by the ML2. It uses the vpp_papi python package, which comes bundled with the VPP platform code to communicate with VPP.  When the agent is restarted, it resets VPP to a clean state, fetches any existing port data from etcd, and programs the VPP state. Then it watches the etcd port-space for ports to be bound and unbound.  When the agent receives a port_bind (etcd_action=create) or port_unbind (etcd_action=delete) event from etcd, it performs the required action on VPP and updates the etcd state space.  The agent also writes the physical networks that are present on the compute node to etcd. Using this data, the driver determines whether a port can be bound to a certain named physical network.   

    Note: Etcd only keeps track of the last 1000 events. So if the agent misses all the events, it gets an "index is outdated" response from etcd. At this time, the agent recovers the current state of the watching keyspace by re-starting the watch from etcd_index + 1


# 16.09 release features

The following neutron features are supported in the 16.09 release of the networking-vpp driver.

  1)	Vlan networking

  2)	Flat networking

  3)	Neutron DHCP service (q-dhcp)

  4)	Neutron L3 routers (q-router)

  5)	External network connectivity and floating IPs

  6)    DB journalling

  7)    Etcd based driver-agent communication
        (Only a single machine etcd cluster is supported at this time)

  8)    State recovery upon driver and agent restart
  

# Supported HA models

1) VPP agent Restart - Supported

2) ML2 driver Restart - Supported

3) VPP restart - Not supported

       Note: When VPP alone is restarted on a compute node, a manual agent restart is also required on that node to re-sync the port state from etcd.


# Devstack Settings

Use the devstack mitaka release with the below settings to stack the networking-vpp driver.
The ETCD_HOST is the IP address (i.e. the advertise-client-url IP) of the etcd cluster member. The ETCD_PORT is the etcd client port.

    Enable appropriate services

    disable_service n-net

    enable_service q-svc

    disable_service q-agt # we're not using OVS or LB

    enable_service q-dhcp

    enable_service q-l3

    enable_service q-meta

    enable_plugin networking-vpp  < URL of the networking-vpp repo >

    Q_PLUGIN=ml2

    Q_ML2_PLUGIN_MECHANISM_DRIVERS=vpp

    Q_ML2_PLUGIN_TYPE_DRIVERS=vlan,flat

    Q_ML2_TENANT_NETWORK_TYPE=vlan,flat

    ML2_VLAN_RANGES=physnet1:100:200

    MECH_VPP_PHYSNETLIST=physnet1:TenGigabitEthernetb/0/0

    ETCD_HOST=${ETCD_HOST}

    ETCD_PORT=${ETCD_PORT}
    
    
# VPP startup.conf
  
  VPP installs a startup config file in the /etc/vpp directory named startup.conf. For example, the below sample configuration starts up VPP with the following settings: 
  
  a) unix { nodaemon } - Implies do not fork or background the vpp process. This is typical when invoking VPP applications from a process monitor.
  
  b)  unix { log /tmp/vpp.log } - Logs the startup configuration and all subsequent CLI commands in /tmp/vpp.log
  
  c) unix { cli-listen localhost:5002 } - Binds the CLI to listen at address localhost on TCP port 5002
  
  d) unix { full-coredump } - Asks the Linux kernel to dump all memory-mapped address regions
  
  e) api-trace { on } - Enables API trace capture from the beginning of time, and arranges for a post-mortem dump of the API trace if the application terminates abnormally
  
  f) dpdk { dev 0000:03:00.0 } - Asks VPP to white-list [or drive] a specific PCI device at 0000:03:00.0. The PCI-dev is a string of the form [domain:]bus:devid.func. This is the same format used in the linux sysfs tree (i.e. /sys/bus/pci/devices) for PCI device directory names.
  
  g) dpdk {socket-mem 512,512} - Allocates 512MB of memory from hugepages on CPU sockets 
  
  h) cpu { workers 2 } - Allocates 2 CPU cores to VPP
  
     unix {
         nodaemon
         log /tmp/vpp.log
         cli-listen localhost:5002
         full-coredump
          }
     api-trace {
               on
               }
     dpdk {
        dev 0000:03:00.0
        socket-mem 512,512
          }  
     cpu {
       workers 2
         }


