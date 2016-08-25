#!/usr/bin/env bash
### Install etcd if required and start the cluster
set -o xtrace
###Cluster settings
#The IP address on which where etcd will listen
host_ip=${HOST_IP:-127.0.0.1}
home=${HOME:-/home/demo}
log_file=/opt/stack/logs/etcd.log  
etcd_version="v3.0.4"
etcd="etcd-${etcd_version}-linux-amd64"
etcd_package="${etcd}.tar.gz"
download_location="https://github.com/coreos/etcd/releases/download"


#Comma separated list of all cluster members in the format: member_name=advertised-peer-urls"
export ETCD_INITIAL_CLUSTER="etcd0=http://${host_ip}:2380"
################################################################################
export ETCD_INITIAL_CLUSTER_STATE=new
export ETCD_INITIAL_CLUSTER_TOKEN="etcd-cluster-1"
export ETCD_INITIAL_ADVERTISE_PEER_URLS="http://${host_ip}:2380"
export ETCD_DATA_DIR="${home}/etcd"
export ETCD_LISTEN_PEER_URLS="http://${host_ip}:2380"
export ETCD_LISTEN_CLIENT_URLS="http://${host_ip}:2379"
export ETCD_ADVERTISE_CLIENT_URLS="http://${host_ip}:2379"
export ETCD_NAME="etcd0"


echo "Getting ${etcd}"
if [ ! -d ${etcd} ];then
   curl --proxy $https_proxy -L ${download_location}/${etcd_version}/${etcd_package} -o ${etcd_package}
   echo "Uncompressing package..${etcd_package}" 
   tar xvzf ${etcd_package}
fi
cd ${etcd}
if ./etcd --version; then
   echo "etcd version:${etcd_version} installed successfully"
   echo "starting etcd on host:${host_ip}"
   ./etcd >> ${log_file} 2>&1 &
   [ $? != 0 ] && echo "Install etcd failed" && exit 1
   echo "Etcd started ..etcl_listen_client_url=$ETCD_LISTEN_CLIENT_URLS"

else
   echo "Error installing etcd version:${etcd_version}"
fi
