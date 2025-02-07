#
# Copyright (c) 2011 Yubico AB
# See the file COPYING for licence statement.
#
"""
Utility to unlock the key store of a YubiHSM,
using the 'HSM password'/'master key'.
"""

import sys
import pyhsm
import argparse
import getpass

default_device = "/dev/ttyACM0"


def parse_args():
    """
    Parse the command line arguments
    """
    parser = argparse.ArgumentParser(
        description="Unlock key store of YubiHSM",
        add_help=True,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-D",
        "--device",
        dest="device",
        default=default_device,
        required=False,
        help="YubiHSM device",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        dest="verbose",
        action="store_true",
        default=False,
        help="Enable verbose operation",
    )
    parser.add_argument(
        "--debug",
        dest="debug",
        action="store_true",
        default=False,
        help="Enable debug operation",
    )
    parser.add_argument(
        "--no-otp",
        dest="no_otp",
        action="store_true",
        default=False,
        help="Don't ask for OTP, even if YubiHSM supports it",
    )
    parser.add_argument(
        "--stdin",
        dest="stdin",
        action="store_true",
        default=False,
        help="Read data from stdin instead of prompting",
    )

    args = parser.parse_args()

    return args


def get_password(hsm, args):
    """Get password of correct length for this YubiHSM version."""
    expected_len = 32
    name = "HSM password"
    if hsm.version.have_key_store_decrypt():
        expected_len = 64
        name = "master key"

    if args.stdin:
        password = sys.stdin.readline()
        while password and password[-1] == "\n":
            password = password[:-1]
    else:
        if args.debug:
            password = raw_input(
                "Enter %s (press enter to skip) (will be echoed) : " % (name)
            )
        else:
            password = getpass.getpass("Enter %s (press enter to skip) : " % (name))

    if len(password) <= expected_len:
        password = password.decode("hex")
        if not password:
            return None
        return password
    else:
        sys.stderr.write(
            "ERROR: Invalid HSM password (expected max %i chars, got %i)\n"
            % (expected_len, len(password))
        )
        return 1


def get_otp(hsm, args):
    """Get OTP from YubiKey."""
    if args.no_otp:
        return None
    if hsm.version.have_unlock():
        if args.stdin:
            otp = sys.stdin.readline()
            while otp and otp[-1] == "\n":
                otp = otp[:-1]
        else:
            otp = raw_input("Enter admin YubiKey OTP (press enter to skip) : ")
        if len(otp) == 44:
            # YubiHSM admin OTP's always have a public_id length of 6 bytes
            return otp
        if otp:
            sys.stderr.write("ERROR: Invalid YubiKey OTP\n")
    return None


def main():
    """
    What will be executed when running as a stand alone program.
    """
    args = parse_args()

    try:
        hsm = pyhsm.base.YHSM(device=args.device, debug=args.debug)

        if args.debug or args.verbose:
            print("Device  : %s" % (args.device))
            print("Version : %s" % (hsm.info()))
            print("")

        password = get_password(hsm, args)
        otp = get_otp(hsm, args)
        if not password and not otp:
            print("\nAborted\n")
            return 1
        else:
            if args.debug or args.verbose:
                print("")
            if hsm.unlock(password=password, otp=otp):
                if args.debug or args.verbose:
                    print("OK\n")
    except pyhsm.exception.YHSM_Error as e:
        sys.stderr.write("ERROR: %s\n" % (e.reason))
        if e.reason == "YubiHSM did not respond to command YSM_SYSTEM_INFO_QUERY":
            sys.stderr.write(
                "Please check whether your YubiHSM is really at "
                + args.device
                + ", you can specify an alternate device using the option -D"
            )
        return 1

    return 0
