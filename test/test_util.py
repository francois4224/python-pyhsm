# Copyright (c) 2011 Yubico AB
# See the file COPYING for licence statement.

import sys
import unittest
import pyhsm

import test_common


class TestUtil(test_common.YHSM_TestCase):
    def setUp(self):
        test_common.YHSM_TestCase.setUp(self)

    def test_hexdump(self):
        """Test hexdump function."""
        data1 = bytes([x for x in range(8)])
        self.assertEquals("0000   00 01 02 03 04 05 06 07\n", data1.hex())
        data2 = bytes([x for x in range(64)])
        self.assertEquals(248, len(data2.hex()))
        self.assertEquals("", b"".hex())

    def test_response_validation(self):
        """Test response validation functions."""
        self.assertRaises(
            pyhsm.exception.YHSM_Error,
            pyhsm.util.validate_cmd_response_str,
            "test",
            "abc",
            "def",
            hex_encode=True,
        )

        self.assertRaises(
            pyhsm.exception.YHSM_Error,
            pyhsm.util.validate_cmd_response_str,
            "test",
            "abc",
            "def",
            hex_encode=False,
        )

    def test_input_validate_str(self):
        """Test string input validation."""
        self.assertRaises(
            pyhsm.exception.YHSM_WrongInputType,
            pyhsm.util.input_validate_str,
            0,
            "foo",
            exact_len=5,
        )

        self.assertRaises(
            pyhsm.exception.YHSM_InputTooLong,
            pyhsm.util.input_validate_str,
            "1234",
            "foo",
            max_len=3,
        )
        self.assertEquals(
            "1234", pyhsm.util.input_validate_str("1234", "foo", max_len=4)
        )
        self.assertEquals(
            "1234", pyhsm.util.input_validate_str("1234", "foo", max_len=14)
        )

        self.assertRaises(
            pyhsm.exception.YHSM_WrongInputSize,
            pyhsm.util.input_validate_str,
            "1234",
            "foo",
            exact_len=5,
        )
        self.assertEquals(
            "1234", pyhsm.util.input_validate_str("1234", "foo", exact_len=4)
        )
