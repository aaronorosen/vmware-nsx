# Copyright 2013 VMware, Inc.
#
# All Rights Reserved.
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

import neutron.db.api as db
from neutron.plugins.common import constants as neutron_const

import decorator
from oslo_db import exception as db_exc
from oslo_log import log as logging
from oslo_utils import excutils
import six
from sqlalchemy import func
from sqlalchemy.orm import exc
from sqlalchemy.sql import expression as expr

from vmware_nsx._i18n import _, _LE, _LW
from vmware_nsx.common import exceptions as nsx_exc
from vmware_nsx.common import nsxv_constants
from vmware_nsx.db import nsxv_models
from vmware_nsx.extensions import dns_search_domain as ext_dns_search_domain
from vmware_nsx.plugins.nsx_v.vshield.common import constants

NsxvEdgeDhcpStaticBinding = nsxv_models.NsxvEdgeDhcpStaticBinding
LOG = logging.getLogger(__name__)


def _apply_filters_to_query(query, model, filters, like_filters=None):
    if filters:
        for key, value in six.iteritems(filters):
            column = getattr(model, key, None)
            if column:
                query = query.filter(column.in_(value))
    if like_filters:
        for key, search_term in six.iteritems(like_filters):
            column = getattr(model, key, None)
            if column:
                query = query.filter(column.like(search_term))
    return query


def add_nsxv_router_binding(session, router_id, vse_id, lswitch_id, status,
                            appliance_size=nsxv_constants.LARGE,
                            edge_type=nsxv_constants.SERVICE_EDGE):
    with session.begin(subtransactions=True):
        binding = nsxv_models.NsxvRouterBinding(
            router_id=router_id,
            edge_id=vse_id,
            lswitch_id=lswitch_id,
            status=status,
            appliance_size=appliance_size,
            edge_type=edge_type)
        session.add(binding)
    return binding


@decorator.decorator
def warn_on_binding_status_error(f, *args, **kwargs):
    result = f(*args, **kwargs)
    if result is None:
        return
    # we support functions that return a single entry or a list
    if type(result) == list:
        bindings = result
    else:
        bindings = [result]

    for binding in bindings:
        if binding and binding['status'] == neutron_const.ERROR:
            LOG.warning(_LW("Found NSXV router binding entry with status "
                            "%(status)s: router %(router)s, "
                            "edge %(edge)s, lswitch %(lswitch)s, "
                            "status description: %(desc)s "),
                        {'status': binding['status'],
                         'router': binding['router_id'],
                         'edge': binding['edge_id'],
                         'lswitch': binding['lswitch_id'],
                         'desc': binding['status_description']})
    return result


@warn_on_binding_status_error
def get_nsxv_router_binding(session, router_id):
    return session.query(nsxv_models.NsxvRouterBinding).filter_by(
        router_id=router_id).first()


@warn_on_binding_status_error
def get_nsxv_router_binding_by_edge(session, edge_id):
    return session.query(nsxv_models.NsxvRouterBinding).filter_by(
        edge_id=edge_id).first()


@warn_on_binding_status_error
def get_nsxv_router_bindings_by_edge(session, edge_id):
    return session.query(nsxv_models.NsxvRouterBinding).filter_by(
        edge_id=edge_id).all()


@warn_on_binding_status_error
def get_nsxv_router_bindings(session, filters=None,
                             like_filters=None):
    session = db.get_session()
    query = session.query(nsxv_models.NsxvRouterBinding)
    return _apply_filters_to_query(query, nsxv_models.NsxvRouterBinding,
                                   filters, like_filters).all()


def update_nsxv_router_binding(session, router_id, **kwargs):
    with session.begin(subtransactions=True):
        binding = (session.query(nsxv_models.NsxvRouterBinding).
                   filter_by(router_id=router_id).one())
        for key, value in six.iteritems(kwargs):
            binding[key] = value
    return binding


def delete_nsxv_router_binding(session, router_id):
    with session.begin(subtransactions=True):
        binding = (session.query(nsxv_models.NsxvRouterBinding).
                   filter_by(router_id=router_id).first())
        if binding:
            session.delete(binding)


def get_edge_vnic_binding(session, edge_id, network_id):
    return session.query(nsxv_models.NsxvEdgeVnicBinding).filter_by(
        edge_id=edge_id, network_id=network_id).first()


def get_edge_vnic_bindings_by_edge(session, edge_id):
    query = session.query(nsxv_models.NsxvEdgeVnicBinding)
    return query.filter(
        nsxv_models.NsxvEdgeVnicBinding.edge_id == edge_id,
        nsxv_models.NsxvEdgeVnicBinding.network_id != expr.null()).all()


def get_edge_vnic_bindings_by_int_lswitch(session, lswitch_id):
    return session.query(nsxv_models.NsxvEdgeVnicBinding).filter_by(
        network_id=lswitch_id).all()


def create_edge_vnic_binding(session, edge_id, vnic_index,
                             network_id, tunnel_index=-1):
    with session.begin(subtransactions=True):
        binding = nsxv_models.NsxvEdgeVnicBinding(
            edge_id=edge_id,
            vnic_index=vnic_index,
            tunnel_index=tunnel_index,
            network_id=network_id)
        session.add(binding)
    return binding


def delete_edge_vnic_binding_by_network(session, edge_id, network_id):
    with session.begin(subtransactions=True):
        binding = (session.query(nsxv_models.NsxvEdgeVnicBinding).
                   filter_by(edge_id=edge_id, network_id=network_id).one())
        session.delete(binding)


def init_edge_vnic_binding(session, edge_id):
    """Init edge vnic binding to preallocated 10 available edge vnics."""

    with session.begin(subtransactions=True):
        for vnic_index in range(constants.MAX_VNIC_NUM)[1:]:
            start = (vnic_index - 1) * constants.MAX_TUNNEL_NUM
            stop = vnic_index * constants.MAX_TUNNEL_NUM
            for tunnel_index in range(start, stop):
                binding = nsxv_models.NsxvEdgeVnicBinding(
                    edge_id=edge_id,
                    vnic_index=vnic_index,
                    tunnel_index=tunnel_index + 1)
                session.add(binding)


def clean_edge_vnic_binding(session, edge_id):
    """Clean edge vnic binding."""

    with session.begin(subtransactions=True):
        (session.query(nsxv_models.NsxvEdgeVnicBinding).
         filter_by(edge_id=edge_id).delete())


def allocate_edge_vnic(session, edge_id, network_id):
    """Allocate an available edge vnic to network."""

    with session.begin(subtransactions=True):
        bindings = (session.query(nsxv_models.NsxvEdgeVnicBinding).
                    filter_by(edge_id=edge_id, network_id=None).all())
        for binding in bindings:
            if binding['tunnel_index'] % constants.MAX_TUNNEL_NUM == 1:
                binding['network_id'] = network_id
                session.add(binding)
                return binding
    msg = (_("Failed to allocate one available vnic on edge_id: "
             ":%(edge_id)s to network_id: %(network_id)s") %
           {'edge_id': edge_id, 'network_id': network_id})
    LOG.exception(msg)
    raise nsx_exc.NsxPluginException(err_msg=msg)


def allocate_edge_vnic_with_tunnel_index(session, edge_id, network_id):
    """Allocate an available edge vnic with tunnel index to network."""

    # TODO(berlin): temporary solution to let metadata and dhcp use
    # different vnics
    net_list = get_nsxv_internal_network(
        session, constants.InternalEdgePurposes.INTER_EDGE_PURPOSE)
    metadata_net_id = net_list[0]['network_id'] if net_list else None

    with session.begin(subtransactions=True):
        query = session.query(nsxv_models.NsxvEdgeVnicBinding)
        query = query.filter(
            nsxv_models.NsxvEdgeVnicBinding.edge_id == edge_id,
            nsxv_models.NsxvEdgeVnicBinding.network_id == expr.null())
        if metadata_net_id:
            vnic_binding = get_edge_vnic_binding(
                session, edge_id, metadata_net_id)
            if vnic_binding:
                vnic_index = vnic_binding.vnic_index
                query = query.filter(
                    nsxv_models.NsxvEdgeVnicBinding.vnic_index != vnic_index)

        binding = query.first()
        if not binding:
            msg = (_("Failed to allocate one available vnic on edge_id: "
                     ":%(edge_id)s to network_id: %(network_id)s") %
                   {'edge_id': edge_id, 'network_id': network_id})
            LOG.exception(msg)
            raise nsx_exc.NsxPluginException(err_msg=msg)
        binding['network_id'] = network_id
        session.add(binding)
        session.flush()
    return binding


def allocate_specific_edge_vnic(session, edge_id, vnic_index,
                                tunnel_index, network_id):
    """Allocate an specific edge vnic to network."""

    with session.begin(subtransactions=True):
        binding = (session.query(nsxv_models.NsxvEdgeVnicBinding).
                   filter_by(edge_id=edge_id,
                             vnic_index=vnic_index,
                             tunnel_index=tunnel_index).one())
        binding['network_id'] = network_id
        session.add(binding)
    return binding


def get_dhcp_edge_network_binding(session, network_id):
    with session.begin(subtransactions=True):
        dhcp_router_edges = [binding['edge_id']
                             for binding in get_nsxv_router_bindings(session)
                             if binding['router_id'].startswith(
                                 constants.DHCP_EDGE_PREFIX)]
        bindings = (session.query(nsxv_models.NsxvEdgeVnicBinding).
                    filter_by(network_id=network_id))
        for binding in bindings:
            edge_id = binding['edge_id']
            if edge_id in dhcp_router_edges:
                return binding


def free_edge_vnic_by_network(session, edge_id, network_id):
    """Free an edge vnic."""

    with session.begin(subtransactions=True):
        binding = (session.query(nsxv_models.NsxvEdgeVnicBinding).
                   filter_by(edge_id=edge_id, network_id=network_id).one())
        binding['network_id'] = None
        session.add(binding)
    return binding


def create_edge_dhcp_static_binding(session, edge_id, mac_address, binding_id):
    with session.begin(subtransactions=True):
        binding = nsxv_models.NsxvEdgeDhcpStaticBinding(
            edge_id=edge_id,
            mac_address=mac_address,
            binding_id=binding_id)
        session.add(binding)
    return binding


def get_edge_dhcp_static_binding(session, edge_id, mac_address):
    return session.query(nsxv_models.NsxvEdgeDhcpStaticBinding).filter_by(
        edge_id=edge_id, mac_address=mac_address).first()


def get_dhcp_static_bindings_by_edge(session, edge_id):
    return session.query(nsxv_models.NsxvEdgeDhcpStaticBinding).filter_by(
        edge_id=edge_id).all()


def delete_edge_dhcp_static_binding(session, edge_id, mac_address):
    with session.begin(subtransactions=True):
        session.query(nsxv_models.NsxvEdgeDhcpStaticBinding).filter_by(
            edge_id=edge_id, mac_address=mac_address).delete()


def get_nsxv_dhcp_bindings_count_per_edge(session):
    return (
        session.query(
            NsxvEdgeDhcpStaticBinding.edge_id,
            func.count(NsxvEdgeDhcpStaticBinding.mac_address)).group_by(
            NsxvEdgeDhcpStaticBinding.edge_id).all())


def clean_edge_dhcp_static_bindings_by_edge(session, edge_id):
    with session.begin(subtransactions=True):
        session.query(nsxv_models.NsxvEdgeDhcpStaticBinding).filter_by(
            edge_id=edge_id).delete()


def create_nsxv_internal_network(session, network_purpose, network_id):
    with session.begin(subtransactions=True):
        try:
            network = nsxv_models.NsxvInternalNetworks(
                network_purpose=network_purpose,
                network_id=network_id)
            session.add(network)
        except db_exc.DBDuplicateEntry:
            with excutils.save_and_reraise_exception():
                LOG.exception(_LE("Duplicate internal network for purpose %s"),
                              network_purpose)


def get_nsxv_internal_network(session, network_purpose):
    with session.begin(subtransactions=True):
        return (session.query(nsxv_models.NsxvInternalNetworks).
                filter_by(network_purpose=network_purpose).all())


def update_nsxv_internal_network(session, network_purpose, network_id):
    with session.begin(subtransactions=True):
        nets = get_nsxv_internal_network(session, network_purpose)
        for net in nets:
            net['network_id'] = network_id


def delete_nsxv_internal_network(session, network_purpose):
    with session.begin(subtransactions=True):
        return (session.query(nsxv_models.NsxvInternalNetworks).
                filter_by(network_purpose=network_purpose).delete())


def create_nsxv_internal_edge(session, ext_ip_address, purpose, router_id):
    with session.begin(subtransactions=True):
        try:
            internal_edge = nsxv_models.NsxvInternalEdges(
                ext_ip_address=ext_ip_address,
                purpose=purpose,
                router_id=router_id)
            session.add(internal_edge)
        except db_exc.DBDuplicateEntry:
            with excutils.save_and_reraise_exception():
                LOG.exception(_LE("Duplicate internal Edge IP %s"),
                              ext_ip_address)


def get_nsxv_internal_edge(session, ext_ip_address):
    with session.begin(subtransactions=True):
        return (session.query(nsxv_models.NsxvInternalEdges).
                filter_by(ext_ip_address=ext_ip_address).all())


def update_nsxv_internal_edge(session, ext_ip_address, router_id):
    with session.begin(subtransactions=True):
        edges = get_nsxv_internal_edge(session, ext_ip_address)

        for edge in edges:
            edge['router_id'] = router_id


def get_nsxv_internal_edges_by_purpose(session, purpose):
    with session.begin(subtransactions=True):
        return (session.query(nsxv_models.NsxvInternalEdges).
                filter_by(purpose=purpose).all())


def delete_nsxv_internal_edge(session, ext_ip_address):
    with session.begin(subtransactions=True):
        return (session.query(nsxv_models.NsxvInternalEdges).
                filter_by(ext_ip_address=ext_ip_address).delete())


def add_neutron_nsx_section_mapping(session, neutron_id, section_id):
    with session.begin(subtransactions=True):
        mapping = nsxv_models.NsxvSecurityGroupSectionMapping(
            neutron_id=neutron_id, ip_section_id=section_id)
        session.add(mapping)
    return mapping


def add_neutron_nsx_rule_mapping(session, neutron_id, nsx_rule_id):
    with session.begin(subtransactions=True):
        mapping = nsxv_models.NsxvRuleMapping(neutron_id=neutron_id,
                                              nsx_rule_id=nsx_rule_id)
        session.add(mapping)
    return mapping


def add_neutron_nsx_port_vnic_mapping(session, neutron_id, nsx_id):
    with session.begin(subtransactions=True):
        mapping = nsxv_models.NsxvPortVnicMapping(
            neutron_id=neutron_id, nsx_id=nsx_id)
        session.add(mapping)
    return mapping


def get_nsx_section(session, neutron_id):
    try:
        mapping = (session.query(nsxv_models.NsxvSecurityGroupSectionMapping).
                   filter_by(neutron_id=neutron_id).
                   one())
        return mapping
    except exc.NoResultFound:
        LOG.debug("NSX identifiers for neutron security group %s not yet "
                  "stored in Neutron DB", neutron_id)


def get_nsx_rule_id(session, neutron_id):
    try:
        mapping = (session.query(nsxv_models.NsxvRuleMapping).
                   filter_by(neutron_id=neutron_id).
                   one())
        return mapping['nsx_rule_id']
    except exc.NoResultFound:
        LOG.debug("NSX identifiers for neutron rule %s not yet "
                  "stored in Neutron DB", neutron_id)


def get_nsx_vnic_id(session, neutron_id):
    try:
        mapping = (session.query(nsxv_models.NsxvPortVnicMapping).
                   filter_by(neutron_id=neutron_id).
                   one())
        return mapping['nsx_id']
    except exc.NoResultFound:
        LOG.debug("NSX identifiers for neutron port %s not yet "
                  "stored in Neutron DB", neutron_id)


def get_network_bindings(session, network_id):
    session = session or db.get_session()
    return (session.query(nsxv_models.NsxvTzNetworkBinding).
            filter_by(network_id=network_id).
            all())


def get_network_bindings_by_vlanid_and_physical_net(session, vlan_id,
                                                    phy_uuid):
    session = session or db.get_session()
    return (session.query(nsxv_models.NsxvTzNetworkBinding).
            filter_by(vlan_id=vlan_id, phy_uuid=phy_uuid).
            all())


def delete_network_bindings(session, network_id):
    return (session.query(nsxv_models.NsxvTzNetworkBinding).
            filter_by(network_id=network_id).delete())


def add_network_binding(session, network_id, binding_type, phy_uuid, vlan_id):
    with session.begin(subtransactions=True):
        binding = nsxv_models.NsxvTzNetworkBinding(network_id, binding_type,
                                                   phy_uuid, vlan_id)
        session.add(binding)
    return binding


def get_network_bindings_by_vlanid(session, vlan_id):
    session = session or db.get_session()
    return (session.query(nsxv_models.NsxvTzNetworkBinding).
            filter_by(vlan_id=vlan_id).
            all())


#
# Edge Firewall binding methods
#
def add_nsxv_edge_firewallrule_binding(session, map_info):
    with session.begin(subtransactions=True):
        binding = nsxv_models.NsxvEdgeFirewallRuleBinding(
            rule_id=map_info['rule_id'],
            rule_vse_id=map_info['rule_vseid'],
            edge_id=map_info['edge_id'])
        session.add(binding)
    return binding


def delete_nsxv_edge_firewallrule_binding(session, id):
    with session.begin(subtransactions=True):
        if not (session.query(nsxv_models.NsxvEdgeFirewallRuleBinding).
                filter_by(rule_id=id).delete()):
            msg = _("Rule Resource binding with id:%s not found!") % id
            raise nsx_exc.NsxPluginException(err_msg=msg)


def get_nsxv_edge_firewallrule_binding(session, id, edge_id):
    with session.begin(subtransactions=True):
        return (session.query(nsxv_models.NsxvEdgeFirewallRuleBinding).
                filter_by(rule_id=id, edge_id=edge_id).first())


def get_nsxv_edge_firewallrule_binding_by_vseid(
        session, edge_id, rule_vseid):
    with session.begin(subtransactions=True):
        try:
            return (session.query(nsxv_models.NsxvEdgeFirewallRuleBinding).
                    filter_by(edge_id=edge_id, rule_vse_id=rule_vseid).one())
        except exc.NoResultFound:
            msg = _("Rule Resource binding not found!")
            raise nsx_exc.NsxPluginException(err_msg=msg)


def cleanup_nsxv_edge_firewallrule_binding(session, edge_id):
    with session.begin(subtransactions=True):
        session.query(
            nsxv_models.NsxvEdgeFirewallRuleBinding).filter_by(
                edge_id=edge_id).delete()


def map_spoofguard_policy_for_network(session, network_id, policy_id):
    with session.begin(subtransactions=True):
        mapping = nsxv_models.NsxvSpoofGuardPolicyNetworkMapping(
            network_id=network_id, policy_id=policy_id)
        session.add(mapping)
    return mapping


def get_spoofguard_policy_id(session, network_id):
    try:
        mapping = (session.query(
            nsxv_models.NsxvSpoofGuardPolicyNetworkMapping).
            filter_by(network_id=network_id).one())
        return mapping['policy_id']
    except exc.NoResultFound:
        LOG.debug("SpoofGuard Policy for network %s was not found",
                  network_id)


def get_nsxv_spoofguard_policy_network_mappings(session, filters=None,
                                                like_filters=None):
    session = db.get_session()
    query = session.query(nsxv_models.NsxvSpoofGuardPolicyNetworkMapping)
    return _apply_filters_to_query(
               query, nsxv_models.NsxvSpoofGuardPolicyNetworkMapping,
               filters, like_filters).all()


def add_vdr_dhcp_binding(session, vdr_router_id, dhcp_edge_id):
    with session.begin(subtransactions=True):
        binding = nsxv_models.NsxvVdrDhcpBinding(vdr_router_id=vdr_router_id,
                                                 dhcp_edge_id=dhcp_edge_id)
        session.add(binding)
    return binding


def get_vdr_dhcp_bindings(session):
    try:
        bindings = session.query(nsxv_models.NsxvVdrDhcpBinding).all()
        return bindings
    except exc.NoResultFound:
        return None


def get_vdr_dhcp_binding_by_vdr(session, vdr_router_id):
    try:
        binding = session.query(
            nsxv_models.NsxvVdrDhcpBinding).filter_by(
            vdr_router_id=vdr_router_id).one()
        return binding
    except exc.NoResultFound:
        return None


def delete_vdr_dhcp_binding(session, vdr_router_id):
    return (session.query(nsxv_models.NsxvVdrDhcpBinding).
            filter_by(vdr_router_id=vdr_router_id).delete())


def add_nsxv_lbaas_loadbalancer_binding(
        session, loadbalancer_id, edge_id, edge_fw_rule_id, vip_address):
    with session.begin(subtransactions=True):
        binding = nsxv_models.NsxvLbaasLoadbalancerBinding(
            loadbalancer_id=loadbalancer_id,
            edge_id=edge_id,
            edge_fw_rule_id=edge_fw_rule_id,
            vip_address=vip_address)
        session.add(binding)
    return binding


def get_nsxv_lbaas_loadbalancer_binding(session, loadbalancer_id):
    try:
        return session.query(
            nsxv_models.NsxvLbaasLoadbalancerBinding).filter_by(
            loadbalancer_id=loadbalancer_id).one()
    except exc.NoResultFound:
        return


def get_nsxv_lbaas_loadbalancer_binding_by_edge(session, edge_id):
    return session.query(
        nsxv_models.NsxvLbaasLoadbalancerBinding).filter_by(
        edge_id=edge_id).all()


def del_nsxv_lbaas_loadbalancer_binding(session, loadbalancer_id):
    return (session.query(nsxv_models.NsxvLbaasLoadbalancerBinding).
            filter_by(loadbalancer_id=loadbalancer_id).delete())


def add_nsxv_lbaas_listener_binding(session, loadbalancer_id, listener_id,
                                    app_profile_id, vse_id):
    with session.begin(subtransactions=True):
        binding = nsxv_models.NsxvLbaasListenerBinding(
            loadbalancer_id=loadbalancer_id,
            listener_id=listener_id,
            app_profile_id=app_profile_id,
            vse_id=vse_id)
        session.add(binding)
    return binding


def get_nsxv_lbaas_listener_binding(session, loadbalancer_id, listener_id):
    try:
        return session.query(
            nsxv_models.NsxvLbaasListenerBinding).filter_by(
            loadbalancer_id=loadbalancer_id, listener_id=listener_id).one()
    except exc.NoResultFound:
        return


def del_nsxv_lbaas_listener_binding(session, loadbalancer_id, listener_id):
    return (session.query(nsxv_models.NsxvLbaasListenerBinding).
            filter_by(loadbalancer_id=loadbalancer_id,
                      listener_id=listener_id).delete())


def add_nsxv_lbaas_pool_binding(session, loadbalancer_id, listener_id,
                                pool_id, edge_pool_id):
    with session.begin(subtransactions=True):
        binding = nsxv_models.NsxvLbaasPoolBinding(
            loadbalancer_id=loadbalancer_id,
            listener_id=listener_id,
            pool_id=pool_id,
            edge_pool_id=edge_pool_id)
        session.add(binding)
    return binding


def get_nsxv_lbaas_pool_binding(session, loadbalancer_id, listener_id,
                                pool_id):
    try:
        return session.query(
            nsxv_models.NsxvLbaasPoolBinding).filter_by(
            loadbalancer_id=loadbalancer_id,
            listener_id=listener_id,
            pool_id=pool_id).one()
    except exc.NoResultFound:
        return


def del_nsxv_lbaas_pool_binding(session, loadbalancer_id, listener_id,
                                pool_id):
    return (session.query(nsxv_models.NsxvLbaasPoolBinding).
            filter_by(loadbalancer_id=loadbalancer_id,
                      listener_id=listener_id,
                      pool_id=pool_id).delete())


def add_nsxv_lbaas_monitor_binding(session, loadbalancer_id, listener_id,
                                   pool_id, hm_id, edge_id, edge_mon_id):
    with session.begin(subtransactions=True):
        binding = nsxv_models.NsxvLbaasMonitorBinding(
            loadbalancer_id=loadbalancer_id,
            listener_id=listener_id,
            pool_id=pool_id,
            hm_id=hm_id,
            edge_id=edge_id,
            edge_mon_id=edge_mon_id)
        session.add(binding)
    return binding


def get_nsxv_lbaas_monitor_binding(session, loadbalancer_id, listener_id,
                                   pool_id, hm_id, edge_id):
    try:
        return session.query(
            nsxv_models.NsxvLbaasMonitorBinding).filter_by(
            loadbalancer_id=loadbalancer_id,
            listener_id=listener_id,
            pool_id=pool_id,
            hm_id=hm_id,
            edge_id=edge_id).one()
    except exc.NoResultFound:
        return


def del_nsxv_lbaas_monitor_binding(session, loadbalancer_id, listener_id,
                                   pool_id, hm_id, edge_id):
    return (session.query(nsxv_models.NsxvLbaasMonitorBinding).
            filter_by(loadbalancer_id=loadbalancer_id,
                      listener_id=listener_id,
                      pool_id=pool_id,
                      hm_id=hm_id,
                      edge_id=edge_id).delete())


def add_nsxv_lbaas_certificate_binding(session, cert_id, edge_id,
                                       edge_cert_id):
    with session.begin(subtransactions=True):
        binding = nsxv_models.NsxvLbaasCertificateBinding(
            cert_id=cert_id,
            edge_id=edge_id,
            edge_cert_id=edge_cert_id)
        session.add(binding)
    return binding


def get_nsxv_lbaas_certificate_binding(session, cert_id, edge_id):
    try:
        return session.query(
            nsxv_models.NsxvLbaasCertificateBinding).filter_by(
            cert_id=cert_id,
            edge_id=edge_id).one()
    except exc.NoResultFound:
        return


def del_nsxv_lbaas_certificate_binding(session, cert_id, edge_id):
    return (session.query(nsxv_models.NsxvLbaasCertificateBinding).
            filter_by(cert_id=cert_id,
                      edge_id=edge_id).delete())


def add_nsxv_subnet_ext_attributes(session, subnet_id, dns_search_domain):
    with session.begin(subtransactions=True):
        binding = nsxv_models.NsxvSubnetExtAttributes(
            subnet_id=subnet_id,
            dns_search_domain=dns_search_domain)
        session.add(binding)
    return binding


def get_nsxv_subnet_ext_attributes(session, subnet_id):
    try:
        return session.query(
            nsxv_models.NsxvSubnetExtAttributes).filter_by(
            subnet_id=subnet_id).one()
    except exc.NoResultFound:
        return


def update_nsxv_subnet_ext_attributes(session, subnet_id, dns_search_domain):
    with session.begin(subtransactions=True):
        binding = (session.query(nsxv_models.NsxvSubnetExtAttributes).
                   filter_by(subnet_id=subnet_id).one())
        binding[ext_dns_search_domain.DNS_SEARCH_DOMAIN] = dns_search_domain
    return binding
