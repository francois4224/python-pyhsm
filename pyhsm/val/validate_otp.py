#
# Tool to validate a YubiKey OTP using the YubiHSM internal database.
#
# This requires that you have imported the secret AES key of the YubiKey
# into the database with `../yubikey-ksm/yhsm-import-keys --internal-db'
# or otherwise.
#
# Copyright (c) 2011 Yubico AB
# See the file COPYING for licence statement.
#

import os
import re
import sys
import struct
import argparse
import pyhsm
import pyhsm.yubikey

default_device = "/dev/ttyACM0"


def parse_args():
    """
    Parse the command line arguments
    """
    global default_device

    parser = argparse.ArgumentParser(
        description="Validate YubiKey OTP's using YubiHSM", add_help=True
    )
    parser.add_argument(
        "-D",
        "--device",
        dest="device",
        default=default_device,
        required=False,
        help="YubiHSM device (default : %s)." % default_device,
    )
    parser.add_argument(
        "-v",
        "--verbose",
        dest="verbose",
        action="store_true",
        default=False,
        help="Enable verbose operation.",
    )
    parser.add_argument(
        "--debug",
        dest="debug",
        action="store_true",
        default=False,
        help="Enable debug operation.",
    )
    group = parser.add_argument_group("Modes", "What you want to validate")
    mode_group = group.add_mutually_exclusive_group(required=True)
    mode_group.add_argument("--otp", dest="otp", help="The output from your YubiKey.")
    mode_group.add_argument(
        "--oath", dest="oath", help="The output from your OATH-HOTP token."
    )

    args = parser.parse_args()
    return args


def validate_otp(hsm, args):
    """
    Validate an OTP.
    """
    try:
        res = pyhsm.yubikey.validate_otp(hsm, args.otp)
        if args.verbose:
            print(
                "OK counter=%04x low=%04x high=%02x use=%02x"
                % (res.use_ctr, res.ts_low, res.ts_high, res.session_ctr)
            )
        return 0
    except pyhsm.exception.YHSM_CommandFailed as e:
        if args.verbose:
            print("%s" % (pyhsm.defines.status2str(e.status)))
        # figure out numerical response code
        for r in [
            pyhsm.defines.YSM_OTP_INVALID,
            pyhsm.defines.YSM_OTP_REPLAY,
            pyhsm.defines.YSM_ID_NOT_FOUND,
        ]:
            if e.status == r:
                return r - pyhsm.defines.YSM_RESPONSE
        # not found
        return 0xFF


def validate_oath(hsm, args):
    """
    Validate an OATH OTP.
    """
    print("ERROR: Not implemented, try 'yhsm-validation-server'.")
    return 0


def main():
    args = parse_args()

    if args.debug:
        print("YHSM device		: %s" % (args.device))
        print("")

    hsm = pyhsm.YHSM(device=args.device, debug=args.debug)

    status = 1
    if args.otp:
        status = validate_otp(hsm, args)
    elif args.oath:
        status = validate_oath(hsm, args)

    return status


if __name__ == "__main__":
    sys.exit(main())
