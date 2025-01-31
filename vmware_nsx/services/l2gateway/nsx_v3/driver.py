# Copyright 2015 VMware, Inc.
#
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

from networking_l2gw.db.l2gateway import l2gateway_db
from networking_l2gw.services.l2gateway.common import constants as l2gw_const
from networking_l2gw.services.l2gateway import exceptions as l2gw_exc
from oslo_config import cfg
from oslo_db import exception as db_exc
from oslo_log import log as logging
from oslo_utils import excutils
from oslo_utils import uuidutils

from neutron.api.v2 import attributes
from neutron.callbacks import events
from neutron.callbacks import registry
from neutron.callbacks import resources
from neutron import context
from neutron.extensions import providernet
from neutron import manager
from neutron.plugins.common import utils as n_utils
from neutron_lib import exceptions as n_exc

from vmware_nsx._i18n import _, _LE, _LI
from vmware_nsx.common import exceptions as nsx_exc
from vmware_nsx.common import nsx_constants
from vmware_nsx.common import utils as nsx_utils
from vmware_nsx.db import db as nsx_db
from vmware_nsx.nsxlib import v3 as nsxlib

LOG = logging.getLogger(__name__)


class NsxV3Driver(l2gateway_db.L2GatewayMixin):

    """Class to handle API calls for L2 gateway and NSXv3 backend."""
    gateway_resource = l2gw_const.GATEWAY_RESOURCE_NAME

    def __init__(self):
        # Create a  default L2 gateway if default_bridge_cluster_uuid is
        # provided in nsx.ini
        self._ensure_default_l2_gateway()
        self.subscribe_callback_notifications()
        LOG.debug("Initialization complete for NSXv3 driver for "
                  "L2 gateway service plugin.")

    @property
    def _core_plugin(self):
        return manager.NeutronManager.get_plugin()

    def subscribe_callback_notifications(self):
        registry.subscribe(self._prevent_l2gw_port_delete, resources.PORT,
                           events.BEFORE_DELETE)

    def _ensure_default_l2_gateway(self):
        """
        Create a default logical L2 gateway.

        Create a logical L2 gateway in the neutron database if the
        default_bridge_cluster_uuid config parameter is set and if it is
        not previously created. If not set, return.
        """
        def_l2gw_uuid = cfg.CONF.nsx_v3.default_bridge_cluster_uuid
        # Return if no default_bridge_cluster_uuid set in config
        if not def_l2gw_uuid:
            LOG.info(_LI("NSX: Default bridge cluster UUID not configured "
                         "in nsx.ini. No default L2 gateway created."))
            return
        admin_ctx = context.get_admin_context()
        # Optimistically create the default L2 gateway in neutron DB
        device = {'device_name': def_l2gw_uuid,
                  'interfaces': [{'name': 'default-bridge-cluster'}]}
        def_l2gw = {'name': 'default-l2gw',
                    'devices': [device]}
        l2gw_dict = {self.gateway_resource: def_l2gw}
        l2_gateway = self.create_l2_gateway(admin_ctx, l2gw_dict)
        # Verify that only one default L2 gateway is created
        def_l2gw_exists = False
        l2gateways = self._get_l2_gateways(admin_ctx)
        for l2gateway in l2gateways:
            # Since we ensure L2 gateway is created with only 1 device, we use
            # the first device in the list.
            if l2gateway['devices'][0]['device_name'] == def_l2gw_uuid:
                if def_l2gw_exists:
                    LOG.info(_LI("Default L2 gateway is already created."))
                    try:
                        # Try deleting this duplicate default L2 gateway
                        self.delete_l2_gateway(admin_ctx, l2gateway['id'])
                    except l2gw_exc.L2GatewayInUse:
                        # If the L2 gateway we are trying to delete is in
                        # use then we should delete the L2 gateway which
                        # we just created ensuring there is only one
                        # default L2 gateway in the database.
                        self.delete_l2_gateway(admin_ctx, l2_gateway['id'])
                else:
                    def_l2gw_exists = True
        return l2_gateway

    def _prevent_l2gw_port_delete(self, resource, event, trigger, **kwargs):
        context = kwargs.get('context')
        port_id = kwargs.get('port_id')
        port_check = kwargs.get('port_check')
        if port_check:
            self.prevent_l2gw_port_deletion(context, port_id)

    def _validate_device_list(self, devices):
        # In NSXv3, one L2 gateway is mapped to one bridge cluster.
        # So we expect only one device to be configured as part of
        # a L2 gateway resource. The name of the device must be the bridge
        # cluster's UUID.
        if len(devices) != 1:
            msg = _("Only a single device is supported for one L2 gateway")
            raise n_exc.InvalidInput(error_message=msg)
        if not uuidutils.is_uuid_like(devices[0]['device_name']):
            msg = _("Device name must be configured with a UUID")
            raise n_exc.InvalidInput(error_message=msg)

    def create_l2_gateway(self, context, l2_gateway):
        """Create a logical L2 gateway."""
        gw = l2_gateway[self.gateway_resource]
        devices = gw['devices']
        self._validate_device_list(devices)
        return super(NsxV3Driver, self).create_l2_gateway(context,
                                                          l2_gateway)

    def _validate_network(self, context, network_id):
        network = self._core_plugin.get_network(context, network_id)
        network_type = network.get(providernet.NETWORK_TYPE)
        # If network is a provider network, verify whether it is of type VXLAN
        if network_type and network_type != nsx_utils.NsxV3NetworkTypes.VXLAN:
            msg = (_("Unsupported network type %s for L2 gateway "
                     "connection. Only VXLAN network type supported") %
                   network_type)
            raise n_exc.InvalidInput(error_message=msg)

    def _validate_segment_id(self, seg_id):
        if not seg_id:
            raise l2gw_exc.L2GatewaySegmentationRequired
        return n_utils.is_valid_vlan_tag(seg_id)

    def create_l2_gateway_connection(self, context, l2_gateway_connection):
        """Create a L2 gateway connection."""
        #TODO(abhiraut): Move backend logic in a separate method
        gw_connection = l2_gateway_connection.get(l2gw_const.
                                                  CONNECTION_RESOURCE_NAME)
        network_id = gw_connection.get(l2gw_const.NETWORK_ID)
        self._validate_network(context, network_id)
        l2gw_connection = super(
            NsxV3Driver, self).create_l2_gateway_connection(
                context, l2_gateway_connection)
        l2gw_id = gw_connection.get(l2gw_const.L2GATEWAY_ID)
        devices = self._get_l2_gateway_devices(context, l2gw_id)
        # In NSXv3, there will be only one device configured per L2 gateway.
        # The name of the device shall carry the backend bridge cluster's UUID.
        device_name = devices[0].get('device_name')
        # The seg-id will be provided either during gateway create or gateway
        # connection create. l2gateway_db_mixin makes sure that it is
        # configured one way or the other.
        seg_id = gw_connection.get(l2gw_const.SEG_ID)
        if seg_id is None:
            seg_id = devices[0]['interfaces'][0].get('segmentation_id')
        self._validate_segment_id(seg_id)
        try:
            tags = nsx_utils.build_v3_tags_payload(
                gw_connection, resource_type='os-neutron-l2gw-id',
                project_name=context.tenant_name)
            bridge_endpoint = nsxlib.create_bridge_endpoint(
                device_name=device_name,
                seg_id=seg_id,
                tags=tags)
        except nsx_exc.ManagerError:
            LOG.exception(_LE("Unable to update NSX backend, rolling back "
                              "changes on neutron"))
            with excutils.save_and_reraise_exception():
                super(NsxV3Driver,
                      self).delete_l2_gateway_connection(context,
                                                         l2gw_connection['id'])
        # Create a logical port and connect it to the bridge endpoint.
        tenant_id = gw_connection['tenant_id']
        if context.is_admin and not tenant_id:
            tenant_id = context.tenant_id
        #TODO(abhiraut): Consider specifying the name of the port
        port_dict = {'port': {
                        'tenant_id': tenant_id,
                        'network_id': network_id,
                        'mac_address': attributes.ATTR_NOT_SPECIFIED,
                        'admin_state_up': True,
                        'fixed_ips': [],
                        'device_id': bridge_endpoint['id'],
                        'device_owner': nsx_constants.BRIDGE_ENDPOINT,
                        'name': '', }}
        try:
            #TODO(abhiraut): Consider adding UT for port check once UTs are
            #                refactored
            port = self._core_plugin.create_port(context, port_dict,
                                                 l2gw_port_check=True)
            # Deallocate IP address from the port.
            for fixed_ip in port.get('fixed_ips', []):
                self._core_plugin._delete_ip_allocation(context, network_id,
                                                        fixed_ip['subnet_id'],
                                                        fixed_ip['ip_address'])
            LOG.debug("IP addresses deallocated on port %s", port['id'])
        except (nsx_exc.ManagerError,
                n_exc.NeutronException):
            with excutils.save_and_reraise_exception():
                LOG.exception(_LE("Unable to create L2 gateway port, "
                                  "rolling back changes on neutron"))
                nsxlib.delete_bridge_endpoint(bridge_endpoint['id'])
                super(NsxV3Driver,
                      self).delete_l2_gateway_connection(context,
                                                         l2gw_connection['id'])
        try:
            # Update neutron's database with the mappings.
            nsx_db.add_l2gw_connection_mapping(
                session=context.session,
                connection_id=l2gw_connection['id'],
                bridge_endpoint_id=bridge_endpoint['id'],
                port_id=port['id'])
        except db_exc.DBError:
            with excutils.save_and_reraise_exception():
                LOG.exception(_LE("Unable to add L2 gateway connection "
                                  "mappings, rolling back changes on neutron"))
                nsxlib.delete_bridge_endpoint(bridge_endpoint['id'])
                super(NsxV3Driver,
                      self).delete_l2_gateway_connection(context,
                                                         l2gw_connection['id'])
        return l2gw_connection

    def delete_l2_gateway_connection(self, context, l2_gateway_connection):
        """Delete a L2 gateway connection."""
        conn_mapping = nsx_db.get_l2gw_connection_mapping(
            session=context.session,
            connection_id=l2_gateway_connection)
        bridge_endpoint_id = conn_mapping.get('bridge_endpoint_id')
        # Delete the logical port from the bridge endpoint.
        self._core_plugin.delete_port(context=context,
                                      port_id=conn_mapping.get('port_id'),
                                      l2gw_port_check=False)
        try:
            nsxlib.delete_bridge_endpoint(bridge_endpoint_id)
        except nsx_exc.ManagerError:
            LOG.exception(_LE("Unable to delete bridge endpoint %s on the "
                              "backend.") % bridge_endpoint_id)
        return (super(NsxV3Driver, self).
                delete_l2_gateway_connection(context,
                                             l2_gateway_connection))

    def prevent_l2gw_port_deletion(self, context, port_id):
        """Prevent core plugin from deleting L2 gateway port."""
        try:
            port = self._core_plugin.get_port(context, port_id)
        except n_exc.PortNotFound:
            return
        if port['device_owner'] == nsx_constants.BRIDGE_ENDPOINT:
            reason = _("has device owner %s") % port['device_owner']
            raise n_exc.ServicePortInUse(port_id=port_id, reason=reason)
