#
"""
Tool to generate YubiKey secret keys using YubiHSM.

After generation with this tool, you can (given that you know the AES
key for the key handle used in the HSM) generate a CSV file of the
unencrypted AEADs formatted for YubiKey personalization using the
YubiKey multi configuration utility (Windows) using the command
yhsm-decrypt-aead.

Example :

  1) Configure HSM with key handle 99 having key
      2000200020002000200020002000200020002000200020002000200020002000
  2) Generate 1000 AEADs for YubiKeys using something like this
     (XXXX can be a customer specific public_id prefix allocated by Yubico -
      0000-0009 (in modhex) are for tests)

      $ yhsm-generate-keys --key-handles 99 --start-public-id djXXXXcccccc \
            -O /var/cache/yubikey-ksm/aeads --count 1000
  3) Create CSV-file with
      $ yhsm-decrypt-aead --aes-key 2000...2000 --format yubikey-csv \
            /var/cache/yubikey-ksm/aeads
  4) Program YubiKeys using CSV file contents
  5) Start a KSM to decrypt OTPs from the YubiKeys
      $ yhsm-yubikey-ksm --key-handle 99
"""
#
# Copyright (c) 2011, 2012 Yubico AB
# See the file COPYING for licence statement.
#

import os
import sys
import argparse
import pyhsm
import pyhsm.yubikey

default_device = "/dev/ttyACM0"
default_dir = "/var/cache/yubikey-ksm/aeads"


def parse_args():
    """
    Parse the command line arguments
    """
    parser = argparse.ArgumentParser(
        description="Generate secrets for YubiKeys using YubiHSM",
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
        "-O",
        "--output-dir",
        "--aead-dir",
        dest="output_dir",
        default=default_dir,
        required=False,
        help="Output directory (AEAD base dir)",
    )
    parser.add_argument(
        "-c",
        "--count",
        dest="count",
        type=int,
        default=1,
        required=False,
        help="Number of secrets to generate",
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
        "--public-id-chars",
        dest="public_id_chars",
        type=int,
        default=12,
        required=False,
        help="Number of chars in generated public ids",
    )
    parser.add_argument(
        "--key-handles",
        dest="key_handles",
        nargs="+",
        required=True,
        help="Key handles to encrypt the generated secrets with",
    )
    parser.add_argument(
        "--start-public-id",
        dest="start_id",
        required=True,
        help="The first public id to generate AEAD for",
    )

    parser.add_argument(
        "--random-nonce",
        dest="random_nonce",
        required=False,
        action="store_true",
        default=False,
        help="Let the HSM generate nonce",
    )

    return parser.parse_args()


def args_fixup(args):
    if not os.path.isdir(args.output_dir):
        sys.stderr.write("Output directory '%s' does not exist.\n" % (args.output_dir))
        sys.exit(1)

    keyhandles_fixup(args)

    try:
        n = int(args.start_id)
    except ValueError:
        hexstr = pyhsm.yubikey.modhex_decode(args.start_id)
        n = int(hexstr, 16)

    if n <= 0:
        sys.stderr.write("Start ID must be greater than 0, was %d\n" % (n))
        sys.exit(1)

    args.start_id = n


def keyhandles_fixup(args):
    """
    Walk through the supplied key handles and normalize them, while keeping
    the input format too (as value in a dictionary). The input format is
    used in AEAD filename paths.
    """
    new_handles = {}
    for val in args.key_handles:
        for this in val.split(","):
            n = pyhsm.util.key_handle_to_int(this)
            new_handles[n] = this

    args.key_handles = new_handles


def gen_keys(hsm, args):
    """
    The main key generating loop.
    """

    if args.verbose:
        print("Generating %i keys :\n" % (args.count))
    else:
        print("Generating %i keys" % (args.count))

    for int_id in range(args.start_id, args.start_id + args.count):
        public_id = ("%x" % int_id).rjust(args.public_id_chars, "0")
        padded_id = pyhsm.yubikey.modhex_encode(public_id)

        if args.verbose:
            print("  %s" % (padded_id))

        num_bytes = len(pyhsm.aead_cmd.YHSM_YubiKeySecret("a" * 16, "b" * 6).pack())
        hsm.load_random(num_bytes)
        for kh in args.key_handles.keys():
            if args.random_nonce:
                nonce = ""
            else:
                nonce = public_id.decode("hex")
            aead = hsm.generate_aead(nonce, kh)

            filename = output_filename(args.output_dir, args.key_handles[kh], padded_id)

            if args.verbose:
                print(
                    "    %4s, %i bytes (%s) -> %s"
                    % (
                        args.key_handles[kh],
                        len(aead.data),
                        shorten_aead(aead),
                        filename,
                    )
                )
            aead.save(filename)

        if args.verbose:
            print("")

    print("\nDone\n")


def shorten_aead(aead):
    """Produce pretty-printable version of long AEAD."""
    head = aead.data[:4].encode("hex")
    tail = aead.data[-4:].encode("hex")
    return "%s...%s" % (head, tail)


def output_filename(output_dir, key_handle, public_id):
    """
    Return an output filename for a generated AEAD. Creates a hashed directory structure
    using the last three bytes of the public id to get equal usage.
    """
    parts = [output_dir, key_handle] + pyhsm.util.group(public_id, 2)
    path = os.path.join(*parts)

    if not os.path.isdir(path):
        os.makedirs(path)

    return os.path.join(path, public_id)


def main():
    args = parse_args()

    args_fixup(args)

    print("output dir		: %s" % (args.output_dir))
    print("keys to generate	: %s" % (args.count))
    print("key handles		: %s" % (args.key_handles))
    print("start public_id		: %s (0x%x)" % (args.start_id, args.start_id))
    print("YHSM device		: %s" % (args.device))
    print("")

    if os.path.isfile(args.device):
        hsm = pyhsm.soft_hsm.SoftYHSM.from_file(args.device)
    else:
        hsm = pyhsm.YHSM(device=args.device)

    gen_keys(hsm, args)
