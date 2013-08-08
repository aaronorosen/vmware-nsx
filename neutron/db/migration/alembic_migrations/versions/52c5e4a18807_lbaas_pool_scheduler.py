# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright 2013 OpenStack Foundation
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

"""LBaaS Pool scheduler

Revision ID: 52c5e4a18807
Revises: 2032abe8edac
Create Date: 2013-06-14 03:23:47.815865

"""

# revision identifiers, used by Alembic.
revision = '52c5e4a18807'
down_revision = '2032abe8edac'

from alembic import op
import sqlalchemy as sa


def upgrade(active_plugin=None, options=None):
    ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        'poolloadbalanceragentbindings',
        sa.Column('pool_id', sa.String(length=36), nullable=False),
        sa.Column('agent_id', sa.String(length=36),
                  nullable=False),
        sa.ForeignKeyConstraint(['agent_id'], ['agents.id'],
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['pool_id'], ['pools.id'],
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('pool_id')
    )
    ### end Alembic commands ###


def downgrade(active_plugin=None, options=None):
    ### commands auto generated by Alembic - please adjust! ###
    op.drop_table('poolloadbalanceragentbindings')
    ### end Alembic commands ###
