# Copyright 2012 VMware, Inc
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

from neutron_lib import exceptions as n_exc

from vmware_nsx._i18n import _


class NsxPluginException(n_exc.NeutronException):
    message = _("An unexpected error occurred in the NSX Plugin: %(err_msg)s")


class InvalidVersion(NsxPluginException):
    message = _("Unable to fulfill request with version %(version)s.")


class InvalidConnection(NsxPluginException):
    message = _("Invalid NSX connection parameters: %(conn_params)s")


class InvalidClusterConfiguration(NsxPluginException):
    message = _("Invalid cluster values: %(invalid_attrs)s. Please ensure "
                "that these values are specified in the [DEFAULT] "
                "section of the NSX plugin ini file.")


class InvalidNovaZone(NsxPluginException):
    message = _("Unable to find cluster config entry "
                "for nova zone: %(nova_zone)s")


class NoMorePortsException(NsxPluginException):
    message = _("Unable to create port on network %(network)s. "
                "Maximum number of ports reached")


class NatRuleMismatch(NsxPluginException):
    message = _("While retrieving NAT rules, %(actual_rules)s were found "
                "whereas rules in the (%(min_rules)s,%(max_rules)s) interval "
                "were expected")


class InvalidAttachmentType(NsxPluginException):
    message = _("Invalid NSX attachment type '%(attachment_type)s'")


class MaintenanceInProgress(NsxPluginException):
    message = _("The networking backend is currently in maintenance mode and "
                "therefore unable to accept requests which modify its state. "
                "Please try later.")


class L2GatewayAlreadyInUse(n_exc.Conflict):
    message = _("Gateway Service %(gateway)s is already in use")


class InvalidTransportType(NsxPluginException):
    message = _("The transport type %(transport_type)s is not recognized "
                "by the backend")


class InvalidSecurityCertificate(NsxPluginException):
    message = _("An invalid security certificate was specified for the "
                "gateway device. Certificates must be enclosed between "
                "'-----BEGIN CERTIFICATE-----' and "
                "'-----END CERTIFICATE-----'")


class ServiceOverQuota(n_exc.Conflict):
    message = _("Quota exceeded for NSX resource %(overs)s: %(err_msg)s")


class ServiceClusterUnavailable(NsxPluginException):
    message = _("Service cluster: '%(cluster_id)s' is unavailable. Please, "
                "check NSX setup and/or configuration")


class PortConfigurationError(NsxPluginException):
    message = _("An error occurred while connecting LSN %(lsn_id)s "
                "and network %(net_id)s via port %(port_id)s")

    def __init__(self, **kwargs):
        super(PortConfigurationError, self).__init__(**kwargs)
        self.port_id = kwargs.get('port_id')


class LogicalRouterNotFound(n_exc.NotFound):
    message = _('Unable to find logical router for %(entity_id)s')


class LsnNotFound(n_exc.NotFound):
    message = _('Unable to find LSN for %(entity)s %(entity_id)s')


class LsnPortNotFound(n_exc.NotFound):
    message = (_('Unable to find port for LSN %(lsn_id)s '
                 'and %(entity)s %(entity_id)s'))


class LsnMigrationConflict(n_exc.Conflict):
    message = _("Unable to migrate network '%(net_id)s' to LSN: %(reason)s")


class LsnConfigurationConflict(NsxPluginException):
    message = _("Configuration conflict on Logical Service Node %(lsn_id)s")


class DvsNotFound(n_exc.NotFound):
    message = _('Unable to find DVS %(dvs)s')


class NoRouterAvailable(n_exc.ResourceExhausted):
    message = _("Unable to create the router. "
                "No tenant router is available for allocation.")


class ManagerError(NsxPluginException):
    message = _("Unexpected error from backend manager (%(manager)s) "
                "for %(operation)s %(details)s")

    def __init__(self, **kwargs):
        kwargs['details'] = (': %s' % kwargs['details']
                             if 'details' in kwargs
                             else '')
        super(ManagerError, self).__init__(**kwargs)
        self.msg = self.message % kwargs


class ResourceNotFound(ManagerError):
    message = _("Resource could not be found on backend (%(manager)s) for "
                "%(operation)s")


class StaleRevision(ManagerError):
    pass


class NsxL2GWConnectionMappingNotFound(n_exc.NotFound):
    message = _('Unable to find mapping for L2 gateway connection: %(conn)s')


class NsxL2GWDeviceNotFound(n_exc.NotFound):
    message = _('Unable to find logical L2 gateway device.')


class NsxL2GWInUse(n_exc.InUse):
    message = _("L2 Gateway '%(gateway_id)s' has been used")


class InvalidIPAddress(n_exc.InvalidInput):
    message = _("'%(ip_address)s' must be a /32 CIDR based IPv4 address")


class SecurityGroupMaximumCapacityReached(NsxPluginException):
    message = _("Security Group %(sg_id)s has reached its maximum capacity, "
                "no more ports can be associated with this security-group.")


class NsxResourceNotFound(n_exc.NotFound):
    message = _("%(res_name)s %(res_id)s not found on the backend.")
