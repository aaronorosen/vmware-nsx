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
#

import mock
from oslo_config import cfg
from oslo_utils import uuidutils
from six import moves

from neutron import context
from neutron.plugins.common import constants as plugin_const
from neutron.tests.unit import testlib_api
from neutron_lib import exceptions as n_exc
from vmware_nsx.common import exceptions as nsx_exc
from vmware_nsx.common import nsxv_constants
from vmware_nsx.db import nsxv_db
from vmware_nsx.plugins.nsx_v.vshield.common import (
    constants as vcns_const)
from vmware_nsx.plugins.nsx_v.vshield import edge_utils
from vmware_nsx.tests import unit as vmware

_uuid = uuidutils.generate_uuid

#Four types of backup edge with different status
EDGE_AVAIL = 'available-'
EDGE_CREATING = 'creating-'
EDGE_ERROR1 = 'error1-'
EDGE_ERROR2 = 'error2-'
EDGE_DELETING = 'deleting-'


class EdgeUtilsTestCaseMixin(testlib_api.SqlTestCase):

    def setUp(self):
        super(EdgeUtilsTestCaseMixin, self).setUp()
        nsxv_manager_p = mock.patch(vmware.VCNS_DRIVER_NAME, autospec=True)
        self.nsxv_manager = nsxv_manager_p.start()
        task = mock.Mock()
        nsxv_manager_p.return_value = task
        self.nsxv_manager.callbacks = mock.Mock()
        self.nsxv_manager.vcns = mock.Mock()
        get_ver = mock.patch.object(self.nsxv_manager.vcns,
                                    'get_version').start()
        get_ver.return_value = '6.1.4'
        self.ctx = context.get_admin_context()
        self.addCleanup(nsxv_manager_p.stop)

    def _create_router(self, name='router1'):
        return {'name': name,
                'id': _uuid()}

    def _create_network(self, name='network'):
        return {'name': name,
                'id': _uuid()}

    def _create_subnet(self, name='subnet'):
        return {'name': name,
                'id': _uuid()}

    def _populate_vcns_router_binding(self, bindings):
        for binding in bindings:
            nsxv_db.init_edge_vnic_binding(self.ctx.session,
                                           binding['edge_id'])
            nsxv_db.add_nsxv_router_binding(
                self.ctx.session, binding['router_id'],
                binding['edge_id'], None, binding['status'],
                appliance_size=binding['appliance_size'],
                edge_type=binding['edge_type'])


class EdgeDHCPManagerTestCase(EdgeUtilsTestCaseMixin):

    def setUp(self):
        super(EdgeDHCPManagerTestCase, self).setUp()
        self.edge_manager = edge_utils.EdgeManager(self.nsxv_manager, None)
        self.check = mock.patch.object(self.edge_manager,
                                       'check_edge_active_at_backend').start()
        self.check.return_value = True

    def test_create_dhcp_edge_service(self):
        fake_edge_pool = [{'status': plugin_const.ACTIVE,
                           'edge_id': 'edge-1',
                           'router_id': 'backup-11111111-1111',
                           'appliance_size': 'compact',
                           'edge_type': 'service'},
                          {'status': plugin_const.PENDING_DELETE,
                           'edge_id': 'edge-2',
                           'router_id': 'dhcp-22222222-2222',
                           'appliance_size': 'compact',
                           'edge_type': 'service'},
                          {'status': plugin_const.PENDING_DELETE,
                           'edge_id': 'edge-3',
                           'router_id': 'backup-33333333-3333',
                           'appliance_size': 'compact',
                           'edge_type': 'service'}]
        self._populate_vcns_router_binding(fake_edge_pool)
        fake_network = self._create_network()
        fake_subnet = self._create_subnet(fake_network['id'])
        with mock.patch.object(self.edge_manager,
                               '_get_used_edges', return_value=([], [])):
            self.edge_manager.create_dhcp_edge_service(self.ctx,
                                                       fake_network['id'],
                                                       fake_subnet)
        resource_id = (vcns_const.DHCP_EDGE_PREFIX + fake_network['id'])[:36]
        self.nsxv_manager.update_edge.assert_called_once_with(
            resource_id, 'edge-1', mock.ANY, None,
            appliance_size=vcns_const.SERVICE_SIZE_MAPPING['dhcp'],
            dist=False)

    def test_get_random_available_edge(self):
        available_edge_ids = ['edge-1', 'edge-2']
        selected_edge_id = self.edge_manager._get_random_available_edge(
            available_edge_ids)
        self.assertIn(selected_edge_id, available_edge_ids)

    def test_get_random_available_edge_missing_edges_returns_none(self):
        available_edge_ids = ['edge-1', 'edge-2']
        # Always return inactive(False) while checking whether the edge
        # exists on the backend.
        with mock.patch.object(self.edge_manager,
                               'check_edge_active_at_backend',
                               return_value=False):
            selected_edge_id = self.edge_manager._get_random_available_edge(
                available_edge_ids)
            # If no active edges are found on the backend, return None so that
            # a new DHCP edge is created.
            self.assertIsNone(selected_edge_id)


class EdgeUtilsTestCase(EdgeUtilsTestCaseMixin):

    def setUp(self):
        super(EdgeUtilsTestCase, self).setUp()
        self.edge_manager = edge_utils.EdgeManager(self.nsxv_manager, None)

        # Args for vcns interface configuration
        self.internal_ip = '10.0.0.1'
        self.uplink_ip = '192.168.111.30'
        self.subnet_mask = '255.255.255.0'
        self.pref_len = '24'
        self.edge_id = 'dummy'
        self.orig_vnics = ({},
                           {'vnics': [
                                {'addressGroups':
                                    {'addressGroups': [
                                        {'subnetMask': self.subnet_mask,
                                         'subnetPrefixLength': self.pref_len,
                                         'primaryAddress': self.uplink_ip}]},
                                 'type': 'uplink',
                                 'index': 1},
                                {'addressGroups':
                                    {'addressGroups': [
                                        {'subnetMask': self.subnet_mask,
                                         'subnetPrefixLength': self.pref_len,
                                         'primaryAddress': self.internal_ip}]},
                                 'type': 'internal',
                                 'index': 2}]}
                           )

        # Args for vcns vdr interface configuration
        self.vdr_ip = '10.0.0.1'
        self.vnic = 1
        self.orig_vdr = ({},
                         {'index': 2,
                          'addressGroups': {'addressGroups':
                                [{'subnetMask': self.subnet_mask,
                                  'subnetPrefixLength': self.pref_len,
                                  'primaryAddress': self.vdr_ip}]},
                          'type': 'internal'})

    def test_create_lrouter(self):
        lrouter = self._create_router()
        self.nsxv_manager.deploy_edge.reset_mock()
        edge_utils.create_lrouter(self.nsxv_manager, self.ctx, lrouter,
                                  lswitch=None, dist=False)
        self.nsxv_manager.deploy_edge.assert_called_once_with(
            lrouter['id'], (lrouter['name'] + '-' + lrouter['id']),
            internal_network=None, dist=False,
            jobdata={'router_id': lrouter['id'],
                     'lrouter': lrouter,
                     'lswitch': None,
                     'context': self.ctx},
            appliance_size=vcns_const.SERVICE_SIZE_MAPPING['router'])

    def _test_update_intereface_primary_addr(self, old_ip, new_ip, isUplink):
        fixed_vnic = {'addressGroups':
                      {'addressGroups': [
                              {'subnetMask': self.subnet_mask,
                               'subnetPrefixLength': self.pref_len,
                               'primaryAddress': new_ip}] if new_ip else []},
                      'type': 'uplink' if isUplink else 'internal',
                      'index': 1 if isUplink else 2}

        with mock.patch.object(self.nsxv_manager.vcns,
            'get_interfaces', return_value=self.orig_vnics):
            self.edge_manager.update_interface_addr(
                self.ctx, self.edge_id, old_ip, new_ip,
                self.subnet_mask, is_uplink=isUplink)
            self.nsxv_manager.vcns.update_interface.assert_called_once_with(
                self.edge_id, fixed_vnic)

    def test_update_interface_addr_intrernal(self):
        self._test_update_intereface_primary_addr(
            self.internal_ip, '10.0.0.2', False)

    def test_remove_interface_primary_addr_intrernal(self):
        self._test_update_intereface_primary_addr(
            self.internal_ip, None, False)

    def test_update_interface_addr_uplink(self):
        self._test_update_intereface_primary_addr(
            self.uplink_ip, '192.168.111.31', True)

    def test_remove_interface_primary_addr_uplink(self):
        self._test_update_intereface_primary_addr(
            self.uplink_ip, None, True)

    def _test_update_intereface_secondary_addr(self, old_ip, new_ip):
        addr_group = {'subnetMask': self.subnet_mask,
                      'subnetPrefixLength': self.pref_len,
                      'primaryAddress': self.uplink_ip,
                      'secondaryAddresses': {'type': 'secondary_addresses',
                                             'ipAddress': [new_ip]}}
        fixed_vnic = {'addressGroups': {'addressGroups': [addr_group]},
                      'type': 'uplink',
                      'index': 1}

        with mock.patch.object(self.nsxv_manager.vcns,
            'get_interfaces', return_value=self.orig_vnics):
            self.edge_manager.update_interface_addr(
                self.ctx, self.edge_id, old_ip, new_ip,
                self.subnet_mask, is_uplink=True)
            self.nsxv_manager.vcns.update_interface.assert_called_once_with(
                self.edge_id, fixed_vnic)

    def test_add_secondary_interface_addr(self):
        self._test_update_intereface_secondary_addr(
            None, '192.168.111.31')

    def test_update_interface_addr_fail(self):
        # Old ip is not configured on the interface, so we should fail
        old_ip = '192.168.111.32'
        new_ip = '192.168.111.31'

        with mock.patch.object(self.nsxv_manager.vcns,
            'get_interfaces', return_value=self.orig_vnics):
            self.assertRaises(
                nsx_exc.NsxPluginException,
                self.edge_manager.update_interface_addr,
                self.ctx, self.edge_id, old_ip, new_ip,
                self.subnet_mask, is_uplink=True)

    def _test_update_vdr_intereface_primary_addr(self, old_ip,
                                                 new_ip):
        fixed_vnic = {'addressGroups':
                      {'addressGroups': [
                              {'subnetMask': self.subnet_mask,
                               'subnetPrefixLength': self.pref_len,
                               'primaryAddress': new_ip}] if new_ip else []},
                      'type': 'internal',
                      'index': 2}

        with mock.patch.object(self.nsxv_manager.vcns,
            'get_vdr_internal_interface', return_value=self.orig_vdr):
            with mock.patch.object(self.nsxv_manager.vcns,
                'update_vdr_internal_interface') as vcns_update:
                self.edge_manager.update_vdr_interface_addr(
                    self.ctx, self.edge_id, self.vnic, old_ip, new_ip,
                    self.subnet_mask)
                vcns_update.assert_called_once_with(self.edge_id,
                                                    self.vnic,
                                                    {'interface': fixed_vnic})

    def test_update_vdr_interface_addr_intrernal(self):
        self._test_update_vdr_intereface_primary_addr(
            self.vdr_ip, '20.0.0.2')

    def test_remove_vdr_interface_primary_addr_intrernal(self):
        self._test_update_vdr_intereface_primary_addr(
            self.vdr_ip, None)

    def test_update_vdr_interface_addr_fail(self):
        # Old ip is not configured on the vdr interface, so we should fail
        old_ip = '192.168.111.32'
        new_ip = '192.168.111.31'

        with mock.patch.object(self.nsxv_manager.vcns,
            'get_vdr_internal_interface', return_value=self.orig_vdr):
            self.assertRaises(
                nsx_exc.NsxPluginException,
                self.edge_manager.update_vdr_interface_addr,
                self.ctx, self.edge_id, self.vnic, old_ip, new_ip,
                self.subnet_mask)


class EdgeManagerTestCase(EdgeUtilsTestCaseMixin):

    def setUp(self):
        super(EdgeManagerTestCase, self).setUp()
        cfg.CONF.set_override('backup_edge_pool', [], 'nsxv')
        self.edge_manager = edge_utils.EdgeManager(self.nsxv_manager, None)
        self.check = mock.patch.object(self.edge_manager,
                                       'check_edge_active_at_backend').start()
        self.check.side_effect = self.check_edge_active_at_backend
        self.default_edge_pool_dicts = {
            nsxv_constants.SERVICE_EDGE: {
                nsxv_constants.LARGE: {'minimum_pooled_edges': 1,
                                       'maximum_pooled_edges': 3},
                nsxv_constants.COMPACT: {'minimum_pooled_edges': 1,
                                         'maximum_pooled_edges': 3}},
            nsxv_constants.VDR_EDGE: {}}
        self.vdr_edge_pool_dicts = {
            nsxv_constants.SERVICE_EDGE: {},
            nsxv_constants.VDR_EDGE: {
                nsxv_constants.LARGE: {'minimum_pooled_edges': 1,
                                       'maximum_pooled_edges': 3}}}

    def check_edge_active_at_backend(self, edge_id):
        # workaround to let edge_id None pass since we wrapped router binding
        # db update op.
        if edge_id is None:
            edge_id = ""
        return not (edge_id.startswith(EDGE_ERROR1) or
                    edge_id.startswith(EDGE_ERROR2))

    def test_backup_edge_pool_with_default(self):
        cfg.CONF.set_override('backup_edge_pool',
                              ['service:large:1:3', 'service:compact:1:3'],
                              'nsxv')
        edge_pool_dicts = edge_utils.parse_backup_edge_pool_opt()
        self.assertEqual(self.default_edge_pool_dicts, edge_pool_dicts)

    def test_backup_edge_pool_with_empty_conf(self):
        cfg.CONF.set_override('backup_edge_pool', [], 'nsxv')
        edge_pool_dicts = edge_utils.parse_backup_edge_pool_opt()
        expect_edge_pool_dicts = {
            nsxv_constants.SERVICE_EDGE: {},
            nsxv_constants.VDR_EDGE: {}}
        self.assertEqual(expect_edge_pool_dicts, edge_pool_dicts)

    def test_backup_edge_pool_with_vdr_conf(self):
        cfg.CONF.set_override('backup_edge_pool', ['vdr::1:3'], 'nsxv')
        edge_pool_dicts = edge_utils.parse_backup_edge_pool_opt()
        expect_edge_pool_dicts = self.vdr_edge_pool_dicts
        self.assertEqual(expect_edge_pool_dicts, edge_pool_dicts)

    def test_backup_edge_pool_with_duplicate_conf(self):
        cfg.CONF.set_override('backup_edge_pool',
                              ['service:large:1:3', 'service::3:4'],
                              'nsxv')
        self.assertRaises(n_exc.Invalid, edge_utils.parse_backup_edge_pool_opt)

    def _create_available_router_bindings(
        self, num, size=nsxv_constants.LARGE,
        edge_type=nsxv_constants.SERVICE_EDGE):
        id_prefix = EDGE_AVAIL + size + '-' + edge_type
        return [{'status': plugin_const.ACTIVE,
                 'edge_id': id_prefix + '-edge-' + str(i),
                 'router_id': (vcns_const.BACKUP_ROUTER_PREFIX +
                               id_prefix + str(i)),
                 'appliance_size': size,
                 'edge_type': edge_type}
                for i in moves.range(num)]

    def _create_creating_router_bindings(
        self, num, size=nsxv_constants.LARGE,
        edge_type=nsxv_constants.SERVICE_EDGE):
        id_prefix = EDGE_CREATING + size + '-' + edge_type
        return [{'status': plugin_const.PENDING_CREATE,
                 'edge_id': id_prefix + '-edge-' + str(i),
                 'router_id': (vcns_const.BACKUP_ROUTER_PREFIX +
                               id_prefix + str(i)),
                 'appliance_size': size,
                 'edge_type': edge_type}
                for i in moves.range(num)]

    def _create_error_router_bindings(
        self, num, status=plugin_const.ERROR,
        size=nsxv_constants.LARGE,
        edge_type=nsxv_constants.SERVICE_EDGE):
        id_prefix = EDGE_ERROR1 + size + '-' + edge_type
        return [{'status': status,
                 'edge_id': id_prefix + '-edge-' + str(i),
                 'router_id': (vcns_const.BACKUP_ROUTER_PREFIX +
                               id_prefix + str(i)),
                 'appliance_size': size,
                 'edge_type': edge_type}
                for i in moves.range(num)]

    def _create_error_router_bindings_at_backend(
        self, num, status=plugin_const.ACTIVE,
        size=nsxv_constants.LARGE,
        edge_type=nsxv_constants.SERVICE_EDGE):
        id_prefix = EDGE_ERROR2 + size + '-' + edge_type
        return [{'status': status,
                 'edge_id': id_prefix + '-edge-' + str(i),
                 'router_id': (vcns_const.BACKUP_ROUTER_PREFIX +
                               id_prefix + str(i)),
                 'appliance_size': size,
                 'edge_type': edge_type}
                for i in moves.range(num)]

    def _create_deleting_router_bindings(
        self, num, size=nsxv_constants.LARGE,
        edge_type=nsxv_constants.SERVICE_EDGE):
        id_prefix = EDGE_DELETING + size + '-' + edge_type
        return [{'status': plugin_const.PENDING_DELETE,
                 'edge_id': id_prefix + '-edge-' + str(i),
                 'router_id': (vcns_const.BACKUP_ROUTER_PREFIX +
                               id_prefix + str(i)),
                 'appliance_size': size,
                 'edge_type': edge_type}
                for i in moves.range(num)]

    def _create_edge_pools(self, avail, creating, error,
                           error_at_backend, deleting,
                           size=nsxv_constants.LARGE,
                           edge_type=nsxv_constants.SERVICE_EDGE):
        """Create a backup edge pool with different status of edges.

        Backup edges would be edges with  avail, creating and error_at_backend,
        while available edges would only be edges with avail status.
        """
        return (
            self._create_error_router_bindings(
                error, size=size, edge_type=edge_type) +
            self._create_deleting_router_bindings(
                deleting, size=size, edge_type=edge_type) +
            self._create_error_router_bindings_at_backend(
                error_at_backend, size=size, edge_type=edge_type) +
            self._create_creating_router_bindings(
                creating, size=size, edge_type=edge_type) +
            self._create_available_router_bindings(
                avail, size=size, edge_type=edge_type))

    def _create_backup_router_bindings(
        self, avail, creating, error, error_at_backend, deleting,
        error_status=plugin_const.PENDING_DELETE,
        error_at_backend_status=plugin_const.PENDING_DELETE,
        size=nsxv_constants.LARGE,
        edge_type=nsxv_constants.SERVICE_EDGE):
        return (
            self._create_error_router_bindings(
                error, status=error_status, size=size, edge_type=edge_type) +
            self._create_error_router_bindings_at_backend(
                error_at_backend, status=error_at_backend_status,
                size=size, edge_type=edge_type) +
            self._create_creating_router_bindings(
                creating, size=size, edge_type=edge_type) +
            self._create_available_router_bindings(
                avail, size=size, edge_type=edge_type) +
            self._create_deleting_router_bindings(
                deleting, size=size, edge_type=edge_type))

    def _verify_router_bindings(self, exp_bindings, act_db_bindings):
        exp_dict = dict(zip([binding['router_id']
                             for binding in exp_bindings], exp_bindings))
        act_bindings = [{'router_id': binding['router_id'],
                         'edge_id': binding['edge_id'],
                         'status': binding['status'],
                         'appliance_size': binding['appliance_size'],
                         'edge_type': binding['edge_type']}
                        for binding in act_db_bindings]
        act_dict = dict(zip([binding['router_id']
                             for binding in act_bindings], act_bindings))
        self.assertEqual(exp_dict, act_dict)

    def test_get_backup_edge_bindings(self):
        """Test get backup edges filtering out deleting and error edges."""
        pool_edges = (self._create_edge_pools(1, 2, 3, 4, 5) +
                      self._create_edge_pools(
                          1, 2, 3, 4, 5, size=nsxv_constants.COMPACT))
        self._populate_vcns_router_binding(pool_edges)
        expect_backup_bindings = self._create_backup_router_bindings(
            1, 2, 0, 4, 0,
            error_at_backend_status=plugin_const.ACTIVE,
            size=nsxv_constants.LARGE)
        backup_bindings = self.edge_manager._get_backup_edge_bindings(self.ctx)
        self._verify_router_bindings(expect_backup_bindings, backup_bindings)

    def test_get_available_router_bindings(self):
        appliance_size = nsxv_constants.LARGE
        edge_type = nsxv_constants.SERVICE_EDGE
        pool_edges = (self._create_edge_pools(1, 2, 3, 0, 5) +
                      self._create_edge_pools(
                          1, 2, 3, 0, 5, size=nsxv_constants.COMPACT))
        self._populate_vcns_router_binding(pool_edges)
        expect_backup_bindings = self._create_backup_router_bindings(
            1, 2, 3, 0, 5, error_status=plugin_const.ERROR)
        binding = self.edge_manager._get_available_router_binding(
            self.ctx, appliance_size=appliance_size, edge_type=edge_type)
        router_bindings = [
            binding_db
            for binding_db in nsxv_db.get_nsxv_router_bindings(
                self.ctx.session)
            if (binding_db['appliance_size'] == appliance_size and
                binding_db['edge_type'] == edge_type)]
        self._verify_router_bindings(expect_backup_bindings, router_bindings)
        edge_id = (EDGE_AVAIL + appliance_size + '-' +
                   edge_type + '-edge-' + str(0))
        self.assertEqual(edge_id, binding['edge_id'])

    def test_check_backup_edge_pool_with_max(self):
        appliance_size = nsxv_constants.LARGE
        edge_type = nsxv_constants.SERVICE_EDGE
        pool_edges = (self._create_edge_pools(1, 2, 3, 4, 5) +
                      self._create_edge_pools(
                          1, 2, 3, 4, 5, size=nsxv_constants.COMPACT))
        self._populate_vcns_router_binding(pool_edges)
        expect_pool_bindings = self._create_backup_router_bindings(
            1, 2, 3, 4, 5,
            error_status=plugin_const.ERROR,
            error_at_backend_status=plugin_const.PENDING_DELETE)
        self.edge_manager._check_backup_edge_pool(
            0, 3,
            appliance_size=appliance_size, edge_type=edge_type)
        router_bindings = [
            binding
            for binding in nsxv_db.get_nsxv_router_bindings(self.ctx.session)
            if (binding['appliance_size'] == appliance_size and
                binding['edge_type'] == edge_type)]
        self._verify_router_bindings(expect_pool_bindings, router_bindings)

    def test_check_backup_edge_pool_with_min(self):
        appliance_size = nsxv_constants.LARGE
        edge_type = nsxv_constants.SERVICE_EDGE
        pool_edges = (self._create_edge_pools(1, 2, 3, 0, 5) +
                      self._create_edge_pools(
                          1, 2, 3, 4, 5, size=nsxv_constants.COMPACT))
        self._populate_vcns_router_binding(pool_edges)

        edge_utils.eventlet = mock.Mock()
        edge_utils.eventlet.spawn_n.return_value = None

        self.edge_manager._check_backup_edge_pool(
            5, 10, appliance_size=appliance_size, edge_type=edge_type)
        router_bindings = [
            binding
            for binding in nsxv_db.get_nsxv_router_bindings(self.ctx.session)
            if binding['edge_id'] is None
            and binding['status'] == plugin_const.PENDING_CREATE]

        binding_ids = [bind.router_id for bind in router_bindings]
        self.assertEqual(2, len(router_bindings))
        edge_utils.eventlet.spawn_n.assert_called_with(
            mock.ANY, mock.ANY, binding_ids, appliance_size, edge_type)

    def test_check_backup_edge_pools_with_empty_conf(self):
        pool_edges = (self._create_edge_pools(1, 2, 3, 4, 5) +
                      self._create_edge_pools(
                          1, 2, 3, 4, 5, size=nsxv_constants.COMPACT) +
                      self._create_edge_pools(
                          1, 2, 3, 4, 5, edge_type=nsxv_constants.VDR_EDGE))
        self._populate_vcns_router_binding(pool_edges)
        self.edge_manager._check_backup_edge_pools()
        router_bindings = nsxv_db.get_nsxv_router_bindings(self.ctx.session)
        for binding in router_bindings:
            self.assertEqual(plugin_const.PENDING_DELETE, binding['status'])

    def test_check_backup_edge_pools_with_default(self):
        self.edge_manager.edge_pool_dicts = self.default_edge_pool_dicts
        pool_edges = (self._create_edge_pools(1, 2, 3, 4, 5) +
                      self._create_edge_pools(
                          1, 2, 3, 4, 5, size=nsxv_constants.COMPACT) +
                      self._create_edge_pools(
                          1, 2, 3, 4, 5, edge_type=nsxv_constants.VDR_EDGE))
        self._populate_vcns_router_binding(pool_edges)
        self.edge_manager._check_backup_edge_pools()
        router_bindings = nsxv_db.get_nsxv_router_bindings(self.ctx.session)

        expect_large_bindings = self._create_backup_router_bindings(
            1, 2, 3, 4, 5,
            error_status=plugin_const.PENDING_DELETE,
            error_at_backend_status=plugin_const.PENDING_DELETE)
        large_bindings = [
            binding
            for binding in router_bindings
            if (binding['appliance_size'] == nsxv_constants.LARGE and
                binding['edge_type'] == nsxv_constants.SERVICE_EDGE)]
        self._verify_router_bindings(expect_large_bindings, large_bindings)

        expect_compact_bindings = self._create_backup_router_bindings(
            1, 2, 3, 4, 5,
            error_status=plugin_const.PENDING_DELETE,
            error_at_backend_status=plugin_const.PENDING_DELETE,
            size=nsxv_constants.COMPACT)
        compact_bindings = [
            binding
            for binding in router_bindings
            if (binding['appliance_size'] == nsxv_constants.COMPACT and
                binding['edge_type'] == nsxv_constants.SERVICE_EDGE)]
        self._verify_router_bindings(expect_compact_bindings, compact_bindings)

        vdr_bindings = [
            binding
            for binding in router_bindings
            if (binding['appliance_size'] == nsxv_constants.LARGE and
                binding['edge_type'] == nsxv_constants.VDR_EDGE)]
        for binding in vdr_bindings:
            self.assertEqual(plugin_const.PENDING_DELETE, binding['status'])

    def test_check_backup_edge_pools_with_vdr(self):
        self.edge_manager.edge_pool_dicts = self.vdr_edge_pool_dicts
        pool_edges = (self._create_edge_pools(1, 2, 3, 4, 5) +
                      self._create_edge_pools(
                          1, 2, 3, 4, 5, size=nsxv_constants.COMPACT) +
                      self._create_edge_pools(
                          1, 2, 3, 4, 5, edge_type=nsxv_constants.VDR_EDGE))
        self._populate_vcns_router_binding(pool_edges)
        self.edge_manager._check_backup_edge_pools()
        router_bindings = nsxv_db.get_nsxv_router_bindings(self.ctx.session)
        expect_vdr_bindings = self._create_backup_router_bindings(
            1, 2, 3, 4, 5,
            error_status=plugin_const.PENDING_DELETE,
            error_at_backend_status=plugin_const.PENDING_DELETE,
            edge_type=nsxv_constants.VDR_EDGE)
        vdr_bindings = [
            binding
            for binding in router_bindings
            if (binding['appliance_size'] == nsxv_constants.LARGE and
                binding['edge_type'] == nsxv_constants.VDR_EDGE)]
        self._verify_router_bindings(expect_vdr_bindings, vdr_bindings)
        service_bindings = [
            binding
            for binding in router_bindings
            if binding['edge_type'] == nsxv_constants.SERVICE_EDGE]
        for binding in service_bindings:
            self.assertEqual(plugin_const.PENDING_DELETE, binding['status'])

    def test_allocate_edge_appliance_with_empty(self):
        self.edge_manager._clean_all_error_edge_bindings = mock.Mock()
        self.edge_manager._allocate_edge_appliance(
            self.ctx, 'fake_id', 'fake_name')
        assert not self.edge_manager._clean_all_error_edge_bindings.called

    def test_allocate_large_edge_appliance_with_default(self):
        self.edge_manager.edge_pool_dicts = self.default_edge_pool_dicts
        pool_edges = (self._create_edge_pools(1, 2, 3, 4, 5) +
                      self._create_edge_pools(
                          1, 2, 3, 4, 5, size=nsxv_constants.COMPACT) +
                      self._create_edge_pools(
                          1, 2, 3, 4, 5, edge_type=nsxv_constants.VDR_EDGE))
        self._populate_vcns_router_binding(pool_edges)
        self.edge_manager._allocate_edge_appliance(
            self.ctx, 'fake_id', 'fake_name')
        edge_id = (EDGE_AVAIL + nsxv_constants.LARGE + '-' +
                   nsxv_constants.SERVICE_EDGE + '-edge-' + str(0))
        self.nsxv_manager.update_edge.assert_has_calls(
            [mock.call('fake_id', edge_id, 'fake_name', None,
                       appliance_size=nsxv_constants.LARGE, dist=False)])

    def test_allocate_compact_edge_appliance_with_default(self):
        self.edge_manager.edge_pool_dicts = self.default_edge_pool_dicts
        pool_edges = (self._create_edge_pools(1, 2, 3, 4, 5) +
                      self._create_edge_pools(
                          1, 2, 3, 4, 5, size=nsxv_constants.COMPACT) +
                      self._create_edge_pools(
                          1, 2, 3, 4, 5, edge_type=nsxv_constants.VDR_EDGE))
        self._populate_vcns_router_binding(pool_edges)
        self.edge_manager._allocate_edge_appliance(
            self.ctx, 'fake_id', 'fake_name',
            appliance_size=nsxv_constants.COMPACT)
        edge_id = (EDGE_AVAIL + nsxv_constants.COMPACT + '-' +
                   nsxv_constants.SERVICE_EDGE + '-edge-' + str(0))
        self.nsxv_manager.update_edge.assert_has_calls(
            [mock.call('fake_id', edge_id, 'fake_name', None,
                       appliance_size=nsxv_constants.COMPACT, dist=False)])

    def test_allocate_large_edge_appliance_with_vdr(self):
        self.edge_manager.edge_pool_dicts = self.vdr_edge_pool_dicts
        pool_edges = (self._create_edge_pools(1, 2, 3, 4, 5) +
                      self._create_edge_pools(
                          1, 2, 3, 4, 5, size=nsxv_constants.COMPACT) +
                      self._create_edge_pools(
                          1, 2, 3, 4, 5, edge_type=nsxv_constants.VDR_EDGE))
        self._populate_vcns_router_binding(pool_edges)
        self.edge_manager._allocate_edge_appliance(
            self.ctx, 'fake_id', 'fake_name', dist=True)
        edge_id = (EDGE_AVAIL + nsxv_constants.LARGE + '-' +
                   nsxv_constants.VDR_EDGE + '-edge-' + str(0))
        self.nsxv_manager.update_edge.assert_has_calls(
            [mock.call('fake_id', edge_id, 'fake_name', None,
                       appliance_size=nsxv_constants.LARGE, dist=True)])

    def test_free_edge_appliance_with_empty(self):
        self.edge_manager._clean_all_error_edge_bindings = mock.Mock()
        self.edge_manager._allocate_edge_appliance(
            self.ctx, 'fake_id', 'fake_name')
        self.edge_manager._free_edge_appliance(
            self.ctx, 'fake_id')
        assert not self.edge_manager._clean_all_error_edge_bindings.called

    def test_free_edge_appliance_with_default(self):
        self.edge_manager.edge_pool_dicts = self.default_edge_pool_dicts
        self.edge_manager._allocate_edge_appliance(
            self.ctx, 'fake_id', 'fake_name')
        self.edge_manager._free_edge_appliance(
            self.ctx, 'fake_id')
        assert not self.nsxv_manager.delete_edge.called
        self.nsxv_manager.update_edge.assert_has_calls(
            [mock.call(mock.ANY, mock.ANY, mock.ANY, None,
                       appliance_size=nsxv_constants.LARGE, dist=False)])

    def test_free_edge_appliance_with_default_with_full(self):
        self.edge_pool_dicts = {
            nsxv_constants.SERVICE_EDGE: {
                nsxv_constants.LARGE: {'minimum_pooled_edges': 1,
                                       'maximum_pooled_edges': 1},
                nsxv_constants.COMPACT: {'minimum_pooled_edges': 1,
                                         'maximum_pooled_edges': 3}},
            nsxv_constants.VDR_EDGE: {}}
        self.edge_manager._allocate_edge_appliance(
            self.ctx, 'fake_id', 'fake_name')
        self.edge_manager._free_edge_appliance(
            self.ctx, 'fake_id')
        assert self.nsxv_manager.delete_edge.called
        assert not self.nsxv_manager.update_edge.called
