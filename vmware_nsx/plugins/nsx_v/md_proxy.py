# Copyright 2014 VMware, Inc.
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

import eventlet
import hashlib
import hmac

import netaddr
from neutron.api.v2 import attributes as attr
from neutron import context as neutron_context
from neutron_lib import constants
from oslo_config import cfg
from oslo_log import log as logging

from vmware_nsx._i18n import _, _LE
from vmware_nsx.common import exceptions as nsxv_exc
from vmware_nsx.common import locking
from vmware_nsx.common import nsxv_constants
from vmware_nsx.common import utils
from vmware_nsx.db import nsxv_db
from vmware_nsx.plugins.nsx_v.vshield import (
    nsxv_loadbalancer as nsxv_lb)
from vmware_nsx.plugins.nsx_v.vshield.common import (
    constants as vcns_const)
from vmware_nsx.plugins.nsx_v.vshield import edge_utils
from vmware_nsx.services.lbaas.nsx_v import lbaas_common

METADATA_VSE_NAME = 'MdSrv'
METADATA_IP_ADDR = '169.254.169.254'
METADATA_TCP_PORT = 80
METADATA_HTTPS_PORT = 443
METADATA_HTTPS_VIP_PORT = 8775
INTERNAL_SUBNET = '169.254.128.0/17'
MAX_INIT_THREADS = 3

NET_WAIT_INTERVAL = 240
NET_CHECK_INTERVAL = 10
EDGE_WAIT_INTERVAL = 900
EDGE_CHECK_INTERVAL = 10

LOG = logging.getLogger(__name__)

DEFAULT_EDGE_FIREWALL_RULE = {
    'name': 'VSERule',
    'enabled': True,
    'action': 'allow',
    'source_vnic_groups': ['vse']}


def get_router_fw_rules():
    # build the allowed destination ports list
    int_ports = [METADATA_TCP_PORT,
                 METADATA_HTTPS_PORT,
                 METADATA_HTTPS_VIP_PORT]
    str_ports = [str(p) for p in int_ports]
    # the list of ports can be extended by configuration
    if cfg.CONF.nsxv.metadata_service_allowed_ports:
        str_ports = str_ports + cfg.CONF.nsxv.metadata_service_allowed_ports
    separator = ','
    dest_ports = separator.join(str_ports)

    fw_rules = [
        DEFAULT_EDGE_FIREWALL_RULE,
        {
            'name': 'MDServiceIP',
            'enabled': True,
            'action': 'allow',
            'destination_ip_address': [METADATA_IP_ADDR],
            'protocol': 'tcp',
            'destination_port': dest_ports
        },
        {
            'name': 'MDInterEdgeNet',
            'enabled': True,
            'action': 'deny',
            'destination_ip_address': [INTERNAL_SUBNET]
        }]

    return fw_rules


def get_db_internal_edge_ips(context):
    ip_list = []
    edge_list = nsxv_db.get_nsxv_internal_edges_by_purpose(
        context.session,
        vcns_const.InternalEdgePurposes.INTER_EDGE_PURPOSE)

    if edge_list:
        ip_list = [edge['ext_ip_address'] for edge in edge_list]
    return ip_list


class NsxVMetadataProxyHandler:

    def __init__(self, nsxv_plugin):
        self.nsxv_plugin = nsxv_plugin
        self.context = neutron_context.get_admin_context()

        # Init cannot run concurrently on multiple nodes
        with locking.LockManager.get_lock('nsx-metadata-init'):
            self.internal_net, self.internal_subnet = (
                self._get_internal_network_and_subnet())

            self.proxy_edge_ips = self._get_proxy_edges()

    def _create_metadata_internal_network(self, cidr):
        # Neutron requires a network to have some tenant_id
        tenant_id = nsxv_constants.INTERNAL_TENANT_ID

        net_data = {'network': {'name': 'inter-edge-net',
                                'admin_state_up': True,
                                'port_security_enabled': False,
                                'shared': False,
                                'tenant_id': tenant_id}}
        net = self.nsxv_plugin.create_network(self.context, net_data)

        subnet_data = {'subnet':
                       {'cidr': cidr,
                        'name': 'inter-edge-subnet',
                        'gateway_ip': attr.ATTR_NOT_SPECIFIED,
                        'allocation_pools': attr.ATTR_NOT_SPECIFIED,
                        'ip_version': 4,
                        'dns_nameservers': attr.ATTR_NOT_SPECIFIED,
                        'host_routes': attr.ATTR_NOT_SPECIFIED,
                        'enable_dhcp': False,
                        'network_id': net['id'],
                        'tenant_id': tenant_id}}

        subnet = self.nsxv_plugin.create_subnet(
            self.context,
            subnet_data)

        return net['id'], subnet['id']

    def _get_internal_network_and_subnet(self):
        internal_net = None
        internal_subnet = None

        # Try to find internal net, internal subnet. If not found, create new
        net_list = nsxv_db.get_nsxv_internal_network(
            self.context.session,
            vcns_const.InternalEdgePurposes.INTER_EDGE_PURPOSE)

        if net_list:
            internal_net = net_list[0]['network_id']

        if internal_net:
            internal_subnet = self.nsxv_plugin.get_subnets(
                self.context,
                fields=['id'],
                filters={'network_id': [internal_net]})[0]['id']

        if internal_net is None or internal_subnet is None:
            if cfg.CONF.nsxv.metadata_initializer:
                # Couldn't find net, subnet - create new
                try:
                    internal_net, internal_subnet = (
                        self._create_metadata_internal_network(
                            INTERNAL_SUBNET))
                except Exception as e:
                    nsxv_db.delete_nsxv_internal_network(
                        self.context.session,
                        vcns_const.InternalEdgePurposes.INTER_EDGE_PURPOSE)

                    # if network is created, clean up
                    if internal_net:
                        self.nsxv_plugin.delete_network(self.context,
                                                        internal_net)

                    LOG.exception(_LE("Exception %s while creating internal "
                                      "network for metadata service"), e)
                    return

                # Update the new network_id in DB
                nsxv_db.create_nsxv_internal_network(
                    self.context.session,
                    nsxv_constants.INTER_EDGE_PURPOSE,
                    internal_net)
            else:
                error = _('Metadata initialization is incomplete on '
                          'initializer node')
                raise nsxv_exc.NsxPluginException(err_msg=error)

        return internal_net, internal_subnet

    def _get_edge_internal_ip(self, rtr_id):
            filters = {
                'network_id': [self.internal_net],
                'device_id': [rtr_id]}
            ports = self.nsxv_plugin.get_ports(self.context, filters=filters)
            return ports[0]['fixed_ips'][0]['ip_address']

    def _get_edge_rtr_id_by_ext_ip(self, edge_ip):
        rtr_list = nsxv_db.get_nsxv_internal_edge(
            self.context.session, edge_ip)
        if rtr_list:
            return rtr_list[0]['router_id']

    def _get_edge_id_by_rtr_id(self, rtr_id, context=None):
        if not context:
            context = self.context
        binding = nsxv_db.get_nsxv_router_binding(
            context.session,
            rtr_id)

        if binding:
            return binding['edge_id']

    def _get_proxy_edges(self):
        proxy_edge_ips = []

        db_edge_ips = get_db_internal_edge_ips(self.context)
        if len(db_edge_ips) > len(cfg.CONF.nsxv.mgt_net_proxy_ips):
            error = _('Number of configured metadata proxy IPs is smaller '
                      'than number of Edges which are already provisioned')
            raise nsxv_exc.NsxPluginException(err_msg=error)

        pool = eventlet.GreenPool(min(MAX_INIT_THREADS,
                                      len(cfg.CONF.nsxv.mgt_net_proxy_ips)))

        # Edge IPs that exist in both lists have to be validated that their
        # Edge appliance settings are valid
        for edge_inner_ip in pool.imap(
                self._setup_proxy_edge_route_and_connectivity,
                list(set(db_edge_ips) & set(cfg.CONF.nsxv.mgt_net_proxy_ips))):
            proxy_edge_ips.append(edge_inner_ip)

        # Edges that exist only in the CFG list, should be paired with Edges
        # that exist only in the DB list. The existing Edge from the list will
        # be reconfigured to match the new config
        edge_to_convert_ips = (
            list(set(db_edge_ips) - set(cfg.CONF.nsxv.mgt_net_proxy_ips)))
        edge_ip_to_set = (
            list(set(cfg.CONF.nsxv.mgt_net_proxy_ips) - set(db_edge_ips)))

        if edge_to_convert_ips:
            if cfg.CONF.nsxv.metadata_initializer:
                for edge_inner_ip in pool.imap(
                        self._setup_proxy_edge_external_interface_ip,
                        zip(edge_to_convert_ips, edge_ip_to_set)):
                    proxy_edge_ips.append(edge_inner_ip)
            else:
                error = _('Metadata initialization is incomplete on '
                          'initializer node')
                raise nsxv_exc.NsxPluginException(err_msg=error)

        # Edges that exist in the CFG list but do not have a matching DB
        # element will be created.
        remaining_cfg_ips = edge_ip_to_set[len(edge_to_convert_ips):]
        if remaining_cfg_ips:
            if cfg.CONF.nsxv.metadata_initializer:
                for edge_inner_ip in pool.imap(
                        self._setup_new_proxy_edge, remaining_cfg_ips):
                    proxy_edge_ips.append(edge_inner_ip)

                pool.waitall()
            else:
                error = _('Metadata initialization is incomplete on '
                          'initializer node')
                raise nsxv_exc.NsxPluginException(err_msg=error)

        return proxy_edge_ips

    def _setup_proxy_edge_route_and_connectivity(self, rtr_ext_ip,
                                                 rtr_id=None, edge_id=None):
        if not rtr_id:
            rtr_id = self._get_edge_rtr_id_by_ext_ip(rtr_ext_ip)
        if not edge_id:
            edge_id = self._get_edge_id_by_rtr_id(rtr_id)

        # Read and validate DGW. If different, replace with new value
        h, routes = self.nsxv_plugin.nsx_v.vcns.get_routes(edge_id)
        dgw = routes.get('defaultRoute', {}).get('gatewayAddress')

        if dgw != cfg.CONF.nsxv.mgt_net_default_gateway:
            if cfg.CONF.nsxv.metadata_initializer:
                self.nsxv_plugin._update_routes(
                    self.context, rtr_id,
                    cfg.CONF.nsxv.mgt_net_default_gateway)
            else:
                error = _('Metadata initialization is incomplete on '
                          'initializer node')
                raise nsxv_exc.NsxPluginException(err_msg=error)

        # Read and validate connectivity
        h, if_data = self.nsxv_plugin.nsx_v.get_interface(
            edge_id, vcns_const.EXTERNAL_VNIC_INDEX)
        cur_ip = if_data.get('addressGroups', {}
                             ).get('addressGroups', {}
                                   )[0].get('primaryAddress')
        cur_pgroup = if_data['portgroupId']
        if (if_data and cur_pgroup != cfg.CONF.nsxv.mgt_net_moid
                or cur_ip != rtr_ext_ip):
            if cfg.CONF.nsxv.metadata_initializer:
                self.nsxv_plugin.nsx_v.update_interface(
                    rtr_id,
                    edge_id,
                    vcns_const.EXTERNAL_VNIC_INDEX,
                    cfg.CONF.nsxv.mgt_net_moid,
                    address=rtr_ext_ip,
                    netmask=cfg.CONF.nsxv.mgt_net_proxy_netmask,
                    secondary=[])
            else:
                error = _('Metadata initialization is incomplete on '
                          'initializer node')
                raise nsxv_exc.NsxPluginException(err_msg=error)

        # Read and validate LB pool member configuration
        # When the Nova IP address is changed in the ini file, we should apply
        # this change to the LB pool
        lb_obj = nsxv_lb.NsxvLoadbalancer.get_loadbalancer(
            self.nsxv_plugin.nsx_v.vcns, edge_id)

        vs = lb_obj.virtual_servers.get(METADATA_VSE_NAME)
        if vs:
            md_members = {member.payload['ipAddress']: member.payload['name']
                          for member in vs.default_pool.members.values()}

            if len(cfg.CONF.nsxv.nova_metadata_ips) == len(md_members):
                m_ips = md_members.keys()
                m_to_convert = (list(set(m_ips) -
                                     set(cfg.CONF.nsxv.nova_metadata_ips)))
                m_ip_to_set = (list(set(cfg.CONF.nsxv.nova_metadata_ips)
                                    - set(m_ips)))

                for m_ip in m_to_convert:
                    m_name = md_members[m_ip]
                    vs.default_pool.members[m_name].payload['ipAddress'] = (
                        m_ip_to_set.pop())
            else:
                error = _('Number of metadata members should not change')
                raise nsxv_exc.NsxPluginException(err_msg=error)

            lb_obj.submit_to_backend(self.nsxv_plugin.nsx_v.vcns, edge_id)

        edge_ip = self._get_edge_internal_ip(rtr_id)

        if edge_ip:
            return edge_ip

    def _setup_proxy_edge_external_interface_ip(self, rtr_ext_ips):
        rtr_old_ext_ip, rtr_new_ext_ip = rtr_ext_ips

        rtr_id = self._get_edge_rtr_id_by_ext_ip(rtr_old_ext_ip)
        edge_id = self._get_edge_id_by_rtr_id(rtr_id)

        # Replace DB entry as we cannot update the table PK
        nsxv_db.delete_nsxv_internal_edge(self.context.session, rtr_old_ext_ip)

        edge_ip = self._setup_proxy_edge_route_and_connectivity(
            rtr_new_ext_ip, rtr_id, edge_id)

        nsxv_db.create_nsxv_internal_edge(
            self.context.session, rtr_new_ext_ip,
            vcns_const.InternalEdgePurposes.INTER_EDGE_PURPOSE, rtr_id)

        if edge_ip:
            return edge_ip

    def _setup_new_proxy_edge(self, rtr_ext_ip):
        rtr_id = None
        try:
            router_data = {
                'router': {
                    'name': 'metadata_proxy_router',
                    'admin_state_up': True,
                    'router_type': 'exclusive',
                    'tenant_id': None}}

            rtr = self.nsxv_plugin.create_router(
                self.context,
                router_data,
                allow_metadata=False)

            rtr_id = rtr['id']
            edge_id = self._get_edge_id_by_rtr_id(rtr_id)

            self.nsxv_plugin.nsx_v.update_interface(
                rtr['id'],
                edge_id,
                vcns_const.EXTERNAL_VNIC_INDEX,
                cfg.CONF.nsxv.mgt_net_moid,
                address=rtr_ext_ip,
                netmask=cfg.CONF.nsxv.mgt_net_proxy_netmask,
                secondary=[])

            port_data = {
                'port': {
                    'network_id': self.internal_net,
                    'name': None,
                    'admin_state_up': True,
                    'device_id': rtr_id,
                    'device_owner': constants.DEVICE_OWNER_ROUTER_INTF,
                    'fixed_ips': attr.ATTR_NOT_SPECIFIED,
                    'mac_address': attr.ATTR_NOT_SPECIFIED,
                    'port_security_enabled': False,
                    'tenant_id': None}}

            port = self.nsxv_plugin.create_port(self.context, port_data)

            address_groups = self._get_address_groups(
                self.context, self.internal_net, rtr_id, is_proxy=True)

            edge_ip = port['fixed_ips'][0]['ip_address']
            edge_utils.update_internal_interface(
                self.nsxv_plugin.nsx_v, self.context, rtr_id,
                self.internal_net, address_groups)

            self._setup_metadata_lb(rtr_id,
                                    port['fixed_ips'][0]['ip_address'],
                                    cfg.CONF.nsxv.nova_metadata_port,
                                    cfg.CONF.nsxv.nova_metadata_port,
                                    cfg.CONF.nsxv.nova_metadata_ips,
                                    proxy_lb=True)

            firewall_rules = [
                DEFAULT_EDGE_FIREWALL_RULE,
                {
                    'action': 'allow',
                    'enabled': True,
                    'source_ip_address': [INTERNAL_SUBNET]}]

            edge_utils.update_firewall(
                self.nsxv_plugin.nsx_v,
                self.context,
                rtr_id,
                {'firewall_rule_list': firewall_rules},
                allow_external=False)

            if cfg.CONF.nsxv.mgt_net_default_gateway:
                self.nsxv_plugin._update_routes(
                    self.context, rtr_id,
                    cfg.CONF.nsxv.mgt_net_default_gateway)

            nsxv_db.create_nsxv_internal_edge(
                self.context.session, rtr_ext_ip,
                vcns_const.InternalEdgePurposes.INTER_EDGE_PURPOSE, rtr_id)

            return edge_ip

        except Exception as e:
            LOG.exception(_LE("Exception %s while creating internal edge "
                              "for metadata service"), e)

            ports = self.nsxv_plugin.get_ports(
                self.context, filters={'device_id': [rtr_id]})

            for port in ports:
                self.nsxv_plugin.delete_port(self.context, port['id'],
                                             l3_port_check=True,
                                             nw_gw_port_check=True)

            nsxv_db.delete_nsxv_internal_edge(
                self.context.session,
                rtr_ext_ip)

            if rtr_id:
                self.nsxv_plugin.delete_router(self.context, rtr_id)

    def _get_address_groups(self, context, network_id, device_id, is_proxy):

        filters = {'network_id': [network_id],
                   'device_id': [device_id]}
        ports = self.nsxv_plugin.get_ports(context, filters=filters)

        subnets = self.nsxv_plugin.get_subnets(context, filters=filters)

        address_groups = []
        for subnet in subnets:
            address_group = {}
            net = netaddr.IPNetwork(subnet['cidr'])
            address_group['subnetMask'] = str(net.netmask)
            address_group['subnetPrefixLength'] = str(net.prefixlen)
            for port in ports:
                fixed_ips = port['fixed_ips']
                for fip in fixed_ips:
                    s_id = fip['subnet_id']
                    ip_addr = fip['ip_address']
                    if s_id == subnet['id'] and netaddr.valid_ipv4(ip_addr):
                        address_group['primaryAddress'] = ip_addr
                        break

            # For Edge appliances which aren't the metadata proxy Edge
            #  we add the metadata IP address
            if not is_proxy and network_id == self.internal_net:
                address_group['secondaryAddresses'] = {
                    'type': 'secondary_addresses',
                    'ipAddress': [METADATA_IP_ADDR]}

            address_groups.append(address_group)
        return address_groups

    def _create_ssl_cert(self, edge_id=None):
        # Create a self signed certificate in the backend if both Cert details
        # and private key are not supplied in nsx.ini
        if (not cfg.CONF.nsxv.metadata_nova_client_cert and
            not cfg.CONF.nsxv.metadata_nova_client_priv_key):
            h = self.nsxv_plugin.nsx_v.vcns.create_csr(edge_id)[0]
            # Extract the CSR ID from header
            csr_id = lbaas_common.extract_resource_id(h['location'])
            # Create a self signed certificate
            cert = self.nsxv_plugin.nsx_v.vcns.create_csr_cert(csr_id)[1]
            cert_id = cert['objectId']
        else:
            # Raise an error if either the Cert path or the private key is not
            # configured
            error = None
            if not cfg.CONF.nsxv.metadata_nova_client_cert:
                error = _('Metadata certificate path not configured')
            elif not cfg.CONF.nsxv.metadata_nova_client_priv_key:
                error = _('Metadata client private key not configured')
            if error:
                raise nsxv_exc.NsxPluginException(err_msg=error)
            pem_encoding = utils.read_file(
                cfg.CONF.nsxv.metadata_nova_client_cert)
            priv_key = utils.read_file(
                cfg.CONF.nsxv.metadata_nova_client_priv_key)
            request = {
                'pemEncoding': pem_encoding,
                'privateKey': priv_key}
            cert = self.nsxv_plugin.nsx_v.vcns.upload_edge_certificate(
                edge_id, request)[1]
            cert_id = cert.get('certificates')[0]['objectId']
        return cert_id

    def _setup_metadata_lb(self, rtr_id, vip, v_port, s_port, member_ips,
                           proxy_lb=False, context=None):

        if context is None:
            context = self.context

        edge_id = self._get_edge_id_by_rtr_id(rtr_id, context=context)
        LOG.debug('Setting up Edge device %s', edge_id)

        lb_obj = nsxv_lb.NsxvLoadbalancer()

        protocol = 'HTTP'
        ssl_pass_through = False
        cert_id = None
        # Set protocol to HTTPS with default port of 443 if metadata_insecure
        # is set to False.
        if not cfg.CONF.nsxv.metadata_insecure:
            protocol = 'HTTPS'
            if proxy_lb:
                v_port = METADATA_HTTPS_VIP_PORT
            else:
                v_port = METADATA_HTTPS_PORT
                # Create the certificate on the backend
                cert_id = self._create_ssl_cert(edge_id)
            ssl_pass_through = proxy_lb
        mon_type = protocol if proxy_lb else 'tcp'
        # Create virtual server
        virt_srvr = nsxv_lb.NsxvLBVirtualServer(
            name=METADATA_VSE_NAME,
            ip_address=vip,
            protocol=protocol,
            port=v_port)

        # For router Edge, we add X-LB-Proxy-ID header
        if not proxy_lb:
            md_app_rule = nsxv_lb.NsxvLBAppRule(
                'insert-mdp',
                'reqadd X-Metadata-Provider:' + edge_id)
            virt_srvr.add_app_rule(md_app_rule)

            # When shared proxy is configured, insert authentication string
            if cfg.CONF.nsxv.metadata_shared_secret:
                signature = hmac.new(
                    cfg.CONF.nsxv.metadata_shared_secret,
                    edge_id,
                    hashlib.sha256).hexdigest()
                sign_app_rule = nsxv_lb.NsxvLBAppRule(
                    'insert-auth',
                    'reqadd X-Metadata-Provider-Signature:' + signature)
                virt_srvr.add_app_rule(sign_app_rule)

        # Create app profile
        #  XFF is inserted in router LBs
        app_profile = nsxv_lb.NsxvLBAppProfile(
            name='MDSrvProxy',
            template=protocol,
            server_ssl_enabled=not cfg.CONF.nsxv.metadata_insecure,
            ssl_pass_through=ssl_pass_through,
            insert_xff=not proxy_lb,
            client_ssl_cert=cert_id)

        virt_srvr.set_app_profile(app_profile)

        # Create pool, members and monitor
        pool = nsxv_lb.NsxvLBPool(
            name='MDSrvPool')

        monitor = nsxv_lb.NsxvLBMonitor(name='MDSrvMon',
                                        mon_type=mon_type.lower())
        pool.add_monitor(monitor)

        i = 0
        for member_ip in member_ips:
            i += 1
            member = nsxv_lb.NsxvLBPoolMember(
                name='Member-%d' % i,
                ip_address=member_ip,
                port=s_port,
                monitor_port=s_port)
            pool.add_member(member)

        virt_srvr.set_default_pool(pool)
        lb_obj.add_virtual_server(virt_srvr)

        lb_obj.submit_to_backend(
            self.nsxv_plugin.nsx_v.vcns,
            edge_id, async=False)

    def configure_router_edge(self, rtr_id, context=None):
        # Connect router interface to inter-edge network
        port_data = {
            'port': {
                'network_id': self.internal_net,
                'name': None,
                'admin_state_up': True,
                'device_id': rtr_id,
                'device_owner': constants.DEVICE_OWNER_ROUTER_GW,
                'fixed_ips': attr.ATTR_NOT_SPECIFIED,
                'mac_address': attr.ATTR_NOT_SPECIFIED,
                'port_security_enabled': False,
                'tenant_id': None}}

        self.nsxv_plugin.create_port(self.context, port_data)

        address_groups = self._get_address_groups(
            self.context,
            self.internal_net,
            rtr_id,
            is_proxy=False)

        if context is None:
            context = self.context

        edge_utils.update_internal_interface(
            self.nsxv_plugin.nsx_v,
            context,
            rtr_id,
            self.internal_net,
            address_groups=address_groups)

        self._setup_metadata_lb(rtr_id,
                                METADATA_IP_ADDR,
                                METADATA_TCP_PORT,
                                cfg.CONF.nsxv.nova_metadata_port,
                                self.proxy_edge_ips,
                                proxy_lb=False,
                                context=context)

    def cleanup_router_edge(self, rtr_id):
        filters = {
            'network_id': [self.internal_net],
            'device_id': [rtr_id]}
        ports = self.nsxv_plugin.get_ports(self.context, filters=filters)

        if ports:
            self.nsxv_plugin.delete_port(
                self.context, ports[0]['id'],
                l3_port_check=False)
