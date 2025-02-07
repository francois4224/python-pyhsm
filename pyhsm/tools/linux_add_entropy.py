#
# Copyright (c) 2011 Yubico AB
# See the file COPYING for licence statement.
#
"""
Get random data from TRNG on YubiHSM and insert it into host
entropy pool. Probably only works on Linux since the ioctl()
request value RNDADDENTROPY seems Linux specific.
"""

import os
import sys
import fcntl
import struct
import argparse
import pyhsm

default_device = "/dev/ttyACM0"
default_iterations = 100
default_entropy_ratio = 2  # number of bits of entropy per byte of random data

RNDADDENTROPY = 1074287107  # from /usr/include/linux/random.h


def parse_args():
    """
    Parse the command line arguments
    """
    parser = argparse.ArgumentParser(
        description="Add random data from YubiHSM to Linux entropy",
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
        "-r",
        "--ratio",
        dest="ratio",
        type=int,
        default=default_entropy_ratio,
        help="Bits per byte of data read to use as entropy",
    )
    parser.add_argument(
        "-c",
        "--count",
        dest="iterations",
        type=int,
        default=default_iterations,
        help="Number of iterations to run",
    )

    args = parser.parse_args()

    return args


def get_entropy(hsm, iterations, entropy_ratio):
    """
    Read entropy from YubiHSM and feed it to Linux as entropy using ioctl() syscall.
    """
    fd = os.open("/dev/random", os.O_WRONLY)
    # struct rand_pool_info {
    #    int     entropy_count;
    #    int     buf_size;
    #    __u32   buf[0];
    # };
    fmt = "ii%is" % (pyhsm.defines.YSM_MAX_PKT_SIZE - 1)
    for _ in xrange(iterations):
        rnd = hsm.random(pyhsm.defines.YSM_MAX_PKT_SIZE - 1)
        this = struct.pack(fmt, entropy_ratio * len(rnd), len(rnd), rnd)
        fcntl.ioctl(fd, RNDADDENTROPY, this)
    os.close(fd)


def main():
    """
    What will be executed when running as a stand alone program.
    """
    args = parse_args()

    try:
        s = pyhsm.base.YHSM(device=args.device, debug=args.debug)
        get_entropy(s, args.iterations, args.ratio)
        return 0
    except pyhsm.exception.YHSM_Error as e:
        sys.stderr.write("ERROR: %s" % (e.reason))
        return 1
