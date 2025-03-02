#!/usr/bin/env python3

# CORTX Python common library.
# Copyright (c) 2020 Seagate Technology LLC and/or its Affiliates
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
# For any questions about this software or licensing,
# please email opensource@seagate.com or cortx-questions@seagate.com.

import unittest
from cortx.utils.validator.v_service import ServiceV
from cortx.utils.validator.error import VError

class TestServiceValidator(unittest.TestCase):
	"""Test service related validations."""
	services = ['sshd']
	host = 'localhost'

	def test_service_running(self):
		"""Check if services are running."""

		ServiceV().validate('isrunning', self.services)

	def test_remote_service_running(self):
		"""Check if services are running."""

		ServiceV().validate('isrunning', self.services, self.host)

	def test_neg_service_running(self):
		"""Check if ned services are running."""
		neg_service = ['rabbitmq-server']
		self.assertRaises(VError, ServiceV().validate, 'isrunning', neg_service)


if __name__ == '__main__':
    unittest.main()
