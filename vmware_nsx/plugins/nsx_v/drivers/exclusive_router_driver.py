# Copyright 2014 VMware, Inc
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

from oslo_log import log as logging

from neutron.api.v2 import attributes as attr
from neutron.plugins.common import constants

from vmware_nsx._i18n import _
from vmware_nsx.common import exceptions as nsxv_exc
from vmware_nsx.common import locking
from vmware_nsx.db import nsxv_db
from vmware_nsx.plugins.nsx_v.drivers import (
    abstract_router_driver as router_driver)
from vmware_nsx.plugins.nsx_v import plugin as nsx_v
from vmware_nsx.plugins.nsx_v.vshield import edge_utils

LOG = logging.getLogger(__name__)


class RouterExclusiveDriver(router_driver.RouterBaseDriver):

    def get_type(self):
        return "exclusive"

    def create_router(self, context, lrouter, appliance_size=None,
                      allow_metadata=True):
        self.edge_manager.create_lrouter(
            context, lrouter, dist=False, appliance_size=appliance_size)
        if allow_metadata:
            self.plugin.metadata_proxy_handler.configure_router_edge(
                lrouter['id'])

    def update_router(self, context, router_id, router):
        r = router['router']
        is_routes_update = True if 'routes' in r else False

        gw_info = self.plugin._extract_external_gw(context, router,
                                                   is_extract=True)
        super(nsx_v.NsxVPluginV2, self.plugin).update_router(
            context, router_id, router)
        if gw_info != attr.ATTR_NOT_SPECIFIED:
            self._update_router_gw_info(context, router_id, gw_info,
                                        is_routes_update)
        elif is_routes_update:
            # here is used to handle routes which tenant updates.
            router_db = self.plugin._get_router(context, router_id)
            nexthop = self.plugin._get_external_attachment_info(
                context, router_db)[2]
            self.plugin._update_subnets_and_dnat_firewall(context, router_db)
            self.update_routes(context, router_id, nexthop)
        if 'admin_state_up' in r:
            self.plugin._update_router_admin_state(
                context, router_id, self.get_type(), r['admin_state_up'])
        return self.plugin.get_router(context, router_id)

    def detach_router(self, context, router_id, router):
        LOG.debug("Detach exclusive router id %s", router_id)
        self.edge_manager.unbind_router_on_edge(context, router_id)
        metadata_proxy_handler = self.plugin.metadata_proxy_handler
        if metadata_proxy_handler:
            metadata_proxy_handler.cleanup_router_edge(router_id)

    def _build_router_data_from_db(self, router_db, router):
        if 'status' not in router['router']:
            router['router']['status'] = router_db.status
        if 'name' not in router['router']:
            router['router']['name'] = router_db.name
        if 'admin_state_up' not in router['router']:
            router['router']['admin_state_up'] = router_db.admin_state_up
        if 'tenant_id' not in router['router']:
            router['router']['tenant_id'] = router_db.tenant_id
        if 'id' not in router['router']:
            router['router']['id'] = router_db.id

    def attach_router(self, context, router_id, router, appliance_size=None):
        router_db = self.plugin._get_router(context, router_id)

        # Add DB attributes to the router data structure
        # before creating it as an exclusive router
        self._build_router_data_from_db(router_db, router)

        self.create_router(context,
                           router['router'],
                           allow_metadata=False,
                           appliance_size=appliance_size)

        edge_id = edge_utils.get_router_edge_id(context, router_id)
        LOG.debug("Exclusive router %s attached to edge %s",
                  router_id, edge_id)

        # add all internal interfaces of the router on edge
        intf_net_ids = (
            self.plugin._get_internal_network_ids_by_router(context,
                                                            router_id))
        for network_id in intf_net_ids:
            address_groups = self.plugin._get_address_groups(
                context, router_id, network_id)
            edge_utils.update_internal_interface(
                self.nsx_v, context, router_id, network_id,
                address_groups, router_db.admin_state_up)

        # Update external interface (which also update nat rules, routes, etc)
        external_net_id = self._get_external_network_id_by_router(context,
                                                                  router_id)
        gw_info = None
        if (external_net_id):
            gw_info = {'network_id': external_net_id}
        self._update_router_gw_info(
            context, router_id, gw_info, force_update=True)

    def delete_router(self, context, router_id):
        self.edge_manager.delete_lrouter(context, router_id, dist=False)
        if self.plugin.metadata_proxy_handler:
            self.plugin.metadata_proxy_handler.cleanup_router_edge(
                router_id)

    def update_routes(self, context, router_id, nexthop):
        with locking.LockManager.get_lock(
                self._get_router_edge_id(context, router_id)):
            self.plugin._update_routes(context, router_id, nexthop)

    def _update_router_gw_info(self, context, router_id, info,
                               is_routes_update=False, force_update=False):
        router = self.plugin._get_router(context, router_id)
        org_ext_net_id = router.gw_port_id and router.gw_port.network_id
        org_enable_snat = router.enable_snat
        orgaddr, orgmask, orgnexthop = (
            self.plugin._get_external_attachment_info(
                context, router))

        super(nsx_v.NsxVPluginV2, self.plugin)._update_router_gw_info(
            context, router_id, info, router=router)

        new_ext_net_id = router.gw_port_id and router.gw_port.network_id
        new_enable_snat = router.enable_snat
        newaddr, newmask, newnexthop = (
            self.plugin._get_external_attachment_info(
                context, router))

        edge_id = self._get_router_edge_id(context, router_id)
        with locking.LockManager.get_lock(edge_id):
            if ((new_ext_net_id != org_ext_net_id or force_update)
                and orgnexthop):
                # network changed, so need to remove default gateway before
                # vnic can be configured
                LOG.debug("Delete default gateway %s", orgnexthop)
                edge_utils.clear_gateway(self.nsx_v, context, router_id)

            # Update external vnic if addr or mask is changed
            if orgaddr != newaddr or orgmask != newmask or force_update:
                edge_utils.update_external_interface(
                    self.nsx_v, context, router_id,
                    new_ext_net_id, newaddr, newmask)

            # Update SNAT rules if ext net changed
            # or ext net not changed but snat is changed.
            if (new_ext_net_id != org_ext_net_id or
                (new_ext_net_id == org_ext_net_id and
                 new_enable_snat != org_enable_snat) or
                force_update):
                self.plugin._update_nat_rules(context, router)

            if (new_ext_net_id != org_ext_net_id or
                new_enable_snat != org_enable_snat or
                is_routes_update or force_update):
                self.plugin._update_subnets_and_dnat_firewall(context, router)

            # Update static routes in all.
            self.plugin._update_routes(context, router_id, newnexthop)

    def add_router_interface(self, context, router_id, interface_info):
        self.plugin._check_intf_number_of_router(context, router_id)
        info = super(nsx_v.NsxVPluginV2, self.plugin).add_router_interface(
            context, router_id, interface_info)

        router_db = self.plugin._get_router(context, router_id)
        subnet = self.plugin.get_subnet(context, info['subnet_id'])
        network_id = subnet['network_id']
        address_groups = self.plugin._get_address_groups(
            context, router_id, network_id)
        with locking.LockManager.get_lock(
                self._get_router_edge_id(context, router_id)):
            edge_utils.update_internal_interface(
                self.nsx_v, context, router_id, network_id, address_groups,
                router_db['admin_state_up'])
            # Update edge's firewall rules to accept subnets flows.
            self.plugin._update_subnets_and_dnat_firewall(context, router_db)

            if router_db.gw_port and router_db.enable_snat:
                # Update Nat rules on external edge vnic
                self.plugin._update_nat_rules(context, router_db)
        return info

    def remove_router_interface(self, context, router_id, interface_info):

        # If a loadbalancer is attached to this Edge appliance, we cannot
        # detach the subnet from the exclusive router.
        subnet = interface_info.get('subnet_id')
        if not subnet and interface_info.get('port_id'):
            port = self.plugin.get_port(context, interface_info['port_id'])
            port_subnets = [
                fixed_ip['subnet_id'] for fixed_ip in port.get(
                    'fixed_ips', [])]
            subnet = port_subnets[0]

        if subnet and self._check_lb_on_subnet(context, subnet):
            error = _('Cannot delete router interface while loadbalancers are '
                      'provisioned on attached subnet')
            raise nsxv_exc.NsxPluginException(err_msg=error)

        info = super(nsx_v.NsxVPluginV2, self.plugin).remove_router_interface(
            context, router_id, interface_info)
        router_db = self.plugin._get_router(context, router_id)
        subnet = self.plugin.get_subnet(context, info['subnet_id'])
        network_id = subnet['network_id']

        with locking.LockManager.get_lock(
                self._get_router_edge_id(context, router_id)):
            if router_db.gw_port and router_db.enable_snat:
                # First update nat rules
                self.plugin._update_nat_rules(context, router_db)
            ports = self.plugin._get_router_interface_ports_by_network(
                context, router_id, network_id)
            self.plugin._update_subnets_and_dnat_firewall(context, router_db)
            # No subnet on the network connects to the edge vnic
            if not ports:
                edge_utils.delete_interface(self.nsx_v, context,
                                            router_id, network_id,
                                            dist=False)
            else:
                address_groups = self.plugin._get_address_groups(
                    context, router_id, network_id)
                edge_utils.update_internal_interface(self.nsx_v, context,
                                                     router_id, network_id,
                                                     address_groups)
        return info

    def _check_lb_on_subnet(self, context, subnet_id):
        # Check lbaas
        dev_owner_v1 = 'neutron:' + constants.LOADBALANCER
        dev_owner_v2 = 'neutron:' + constants.LOADBALANCERV2
        filters = {'device_owner': [dev_owner_v1, dev_owner_v2],
                   'fixed_ips': {'subnet_id': [subnet_id]}}
        ports = super(nsx_v.NsxVPluginV2, self.plugin).get_ports(
            context, filters=filters)
        return (len(ports) >= 1)

    def _update_edge_router(self, context, router_id):
        router = self.plugin._get_router(context.elevated(), router_id)
        with locking.LockManager.get_lock(
                self._get_router_edge_id(context, router_id)):
            self.plugin._update_external_interface(context, router)
            self.plugin._update_nat_rules(context, router)
            self.plugin._update_subnets_and_dnat_firewall(context, router)

    def _get_router_edge_id(self, context, router_id):
        binding = nsxv_db.get_nsxv_router_binding(context.session, router_id)
        return binding['edge_id']
