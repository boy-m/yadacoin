import unittest
import pyrx
import binascii
import os
import sys
parent_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir, os.pardir, os.pardir))
sys.path.insert(0, parent_dir)
parent_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir, os.pardir))
sys.path.insert(0, parent_dir)
parent_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir))
sys.path.insert(0, parent_dir)

from tornado.testing import gen_test

from test_setup import BaseTestCase


class TestConsensus(BaseTestCase):

    @gen_test
    async def test_scenerio_1(self):
        """ One block ahead, no fork
        """
