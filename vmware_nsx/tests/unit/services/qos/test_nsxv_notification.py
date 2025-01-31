# Copyright 2016 VMware, Inc.
# All Rights Reserved
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
import mock

from oslo_config import cfg
from oslo_utils import uuidutils

from neutron import context
from neutron import manager
from neutron.objects.qos import policy as policy_object
from neutron.objects.qos import rule as rule_object
from neutron.services.qos import qos_plugin
from neutron.tests.unit.services.qos import base

from vmware_nsx.dvs import dvs
from vmware_nsx.dvs import dvs_utils
from vmware_nsx.services.qos.nsx_v import utils as qos_utils
from vmware_nsx.tests.unit.nsx_v import test_plugin

CORE_PLUGIN = "vmware_nsx.plugins.nsx_v.plugin.NsxVPluginV2"


class TestQosNsxVNotification(test_plugin.NsxVPluginV2TestCase,
                              base.BaseQosTestCase):

    @mock.patch.object(dvs_utils, 'dvs_create_session')
    @mock.patch.object(dvs.DvsManager, '_get_dvs_moref')
    def setUp(self, *mocks):
        # init the nsx-v plugin for testing with DVS
        self._init_dvs_config()
        super(TestQosNsxVNotification, self).setUp(plugin=CORE_PLUGIN,
                                                   ext_mgr=None)
        plugin_instance = manager.NeutronManager.get_plugin()
        self._core_plugin = plugin_instance

        # Setup the QoS plugin:
        # Add a dummy notification driver that calls our handler directly
        # (to skip the message queue)
        cfg.CONF.set_override(
            "notification_drivers",
            ['vmware_nsx.tests.unit.services.qos.fake_nsxv_notifier.'
             'DummyNsxVNotificationDriver'],
            "qos")
        self.qos_plugin = qos_plugin.QoSPlugin()
        mock.patch.object(qos_utils.NsxVQosRule,
                          '_get_qos_plugin',
                          return_value=self.qos_plugin).start()

        # Pre defined QoS data for the tests
        self.ctxt = context.Context('fake_user', 'fake_tenant')
        self.policy_data = {
            'policy': {'id': uuidutils.generate_uuid(),
                       'tenant_id': uuidutils.generate_uuid(),
                       'name': 'test-policy',
                       'description': 'Test policy description',
                       'shared': True}}

        self.rule_data = {
            'bandwidth_limit_rule': {'id': uuidutils.generate_uuid(),
                                     'max_kbps': 100,
                                     'max_burst_kbps': 150}}

        self.policy = policy_object.QosPolicy(
            self.ctxt, **self.policy_data['policy'])

        self.rule = rule_object.QosBandwidthLimitRule(
            self.ctxt, **self.rule_data['bandwidth_limit_rule'])

        self._net_data = {'network': {
            'name': 'test-qos',
            'tenant_id': 'fake_tenant',
            'qos_policy_id': self.policy.id,
            'port_security_enabled': False,
            'admin_state_up': False,
            'shared': False
        }}
        self._rules = [self.rule_data['bandwidth_limit_rule']]

        mock.patch('neutron.objects.db.api.create_object').start()
        mock.patch('neutron.objects.db.api.update_object').start()
        mock.patch('neutron.objects.db.api.delete_object').start()
        mock.patch('neutron.objects.db.api.get_object').start()
        mock.patch(
            'neutron.objects.qos.policy.QosPolicy.obj_load_attr').start()

    def _init_dvs_config(self):
        # Ensure that DVS is enabled
        # and enable the DVS features for nsxv qos support
        cfg.CONF.set_override('host_ip', 'fake_ip', group='dvs')
        cfg.CONF.set_override('host_username', 'fake_user', group='dvs')
        cfg.CONF.set_override('host_password', 'fake_password', group='dvs')
        cfg.CONF.set_override('dvs_name', 'fake_dvs', group='dvs')
        cfg.CONF.set_default('use_dvs_features', True, 'nsxv')

    def _create_net(self):
        return self._core_plugin.create_network(self.ctxt, self._net_data)

    @mock.patch.object(qos_utils, 'update_network_policy_binding')
    @mock.patch.object(dvs.DvsManager, 'update_port_groups_config')
    def test_create_network_with_policy_rule(self,
                                             dvs_update_mock,
                                             update_bindings_mock):
        """Test the DVS update when a QoS rule is attached to a network"""
        # Create a policy with a rule
        _policy = policy_object.QosPolicy(
            self.ctxt, **self.policy_data['policy'])
        setattr(_policy, "rules", [self.rule])

        with mock.patch('neutron.services.qos.qos_plugin.QoSPlugin.'
                        'get_policy_bandwidth_limit_rules',
                        return_value=self._rules) as get_rules_mock:
            # create the network to use this policy
            net = self._create_net()

            # make sure the network-policy binding was updated
            update_bindings_mock.assert_called_once_with(
                self.ctxt, net['id'], self.policy.id)
            # make sure the qos rule was found
            get_rules_mock.assert_called_with(self.ctxt, self.policy.id)
            # make sure the dvs was updated
            self.assertTrue(dvs_update_mock.called)

    def _test_rule_action_notification(self, action):
        with mock.patch.object(qos_utils, 'update_network_policy_binding'):
            with mock.patch.object(dvs.DvsManager,
                                   'update_port_groups_config') as dvs_mock:

                # Create a policy with a rule
                _policy = policy_object.QosPolicy(
                    self.ctxt, **self.policy_data['policy'])

                # set the rule in the policy data
                if action != 'create':
                    setattr(_policy, "rules", [self.rule])

                with mock.patch('neutron.services.qos.qos_plugin.QoSPlugin.'
                                'get_policy_bandwidth_limit_rules',
                                return_value=self._rules) as get_rules_mock:
                    with mock.patch('neutron.objects.qos.policy.'
                                    'QosPolicy.get_object',
                                    return_value=_policy):
                        # create the network to use this policy
                        self._create_net()

                        # create/update/delete the rule
                        if action == 'create':
                            self.qos_plugin.create_policy_bandwidth_limit_rule(
                                self.ctxt, self.policy.id, self.rule_data)
                        elif action == 'update':
                            self.qos_plugin.update_policy_bandwidth_limit_rule(
                                self.ctxt, self.rule.id,
                                self.policy.id, self.rule_data)
                        else:
                            self.qos_plugin.delete_policy_bandwidth_limit_rule(
                                self.ctxt, self.rule.id, self.policy.id)

                        # make sure the qos rule was found
                        self.assertTrue(get_rules_mock.called)
                        # make sure the dvs was updated
                        self.assertTrue(dvs_mock.called)

    def test_create_rule_notification(self):
        """Test the DVS update when a QoS rule, attached to a network,
        is created
        """
        self._test_rule_action_notification('create')

    def test_update_rule_notification(self):
        """Test the DVS update when a QoS rule, attached to a network,
        is modified
        """
        self._test_rule_action_notification('update')

    def test_delete_rule_notification(self):
        """Test the DVS update when a QoS rule, attached to a network,
        is deleted
        """
        self._test_rule_action_notification('delete')
