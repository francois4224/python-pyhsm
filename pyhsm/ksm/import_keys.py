#
"""
Tool to import YubiKey secrets to YubiHSM.

The default mode is to turn each YubiKey secret into an AEAD
(Authenticated Encryption with Associated Data) block that is stored
in a file on the host computer (one file per YubiKey). This enables
validation of virtually unlimited numbers of YubiKey's OTPs.

If --internal-db is used, the YubiKey secret will be stored inside
the YubiHSM, and complete validation (including counter management)
will be done inside the YubiHSM. The internal database is currently
limited to 1024 entries.

The input is supposed to be a comma-separated list of entries like this

  # ykksm 1
  123456,ftftftcccc,534543524554,fcacd309a20ce1809c2db257f0e8d6ea,000000000000,,,

  (seqno, public id, private uid, AES key, dunno,,,)

This is also the format of a database export from a traditional YK-KSM.
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
from pyhsm.soft_hsm import SoftYHSM
from pyhsm.tools.generate_keys import output_filename, shorten_aead

default_device = "/dev/ttyACM0"
default_dir = "/var/cache/yubikey-ksm/aeads"
default_public_id_chars = 12


def parse_args():
    """
    Parse the command line arguments
    """
    parser = argparse.ArgumentParser(
        description="Import existing secrets to YubiHSM eco system",
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
        metavar="DIR",
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
        "--public-id-chars",
        dest="public_id_chars",
        type=int,
        default=default_public_id_chars,
        required=False,
        help="Number of chars in generated public ids",
        metavar="NUM",
    )
    parser.add_argument(
        "--key-handles",
        dest="key_handles",
        nargs="+",
        required=True,
        help="Key handle(s) to encrypt the imported secrets with",
        metavar="HANDLE",
    )
    parser.add_argument(
        "--internal-db",
        dest="internal_db",
        action="store_true",
        default=False,
        help="Store key in YubiHSM internal database",
    )
    parser.add_argument(
        "--aes-key",
        dest="aes_key",
        required=False,
        default=None,
        help="AES key to use when generating AEADs (no YubiHSM)",
        metavar="HEXSTR",
    )
    parser.add_argument(
        "--random-nonce",
        dest="random_nonce",
        action="store_true",
        default=False,
        help="Let the HSM generate random nonce",
    )

    args = parser.parse_args()
    if args.internal_db:
        if len(args.key_handles) != 1:
            sys.stderr.write("--internal-db requires exactly one key handle.\n")
            sys.exit(1)
        if args.aes_key:
            sys.stderr.write("--internal-db incompatible with --aes-key.\n")
            sys.exit(1)
    return args


def args_fixup(args):
    if not args.internal_db and not os.path.isdir(args.output_dir):
        sys.stderr.write("Output directory '%s' does not exist.\n" % (args.output_dir))
        sys.exit(1)

    if args.aes_key:
        args.aes_key = args.aes_key.decode("hex")
    keyhandles_fixup(args)


def keyhandles_fixup(args):
    """
    Walk through the supplied key handles and normalize them, while keeping
    the input format too (as value in a dictionary). The input format is
    used in AEAD filename paths.
    """
    new_handles = {}
    for val in args.key_handles:
        n = pyhsm.util.key_handle_to_int(val)
        new_handles[n] = val

    args.key_handles = new_handles


def import_keys(hsm, args):
    """
    The main stdin iteration loop.
    """
    res = True

    # ykksm 1
    # 123456,ftftftcccc,534543524554,fcacd309a20ce1809c2db257f0e8d6ea,000000000000,,,

    for line in sys.stdin:
        if line[0] == "#":
            continue

        l = line.split(",")
        modhex_id = l[1]
        uid = l[2].decode("hex")
        key = l[3].decode("hex")

        if modhex_id and uid and key:
            public_id = pyhsm.yubikey.modhex_decode(modhex_id)
            padded_id = modhex_id.rjust(args.public_id_chars, "c")

        if int(public_id, 16) == 0:
            print("WARNING: Skipping import of key with public ID: %s" % (padded_id))
            print("This public ID is unsupported by the YubiHSM.\n")
            continue

        if args.verbose:
            print("  %s" % (padded_id))

        secret = pyhsm.aead_cmd.YHSM_YubiKeySecret(key, uid)
        hsm.load_secret(secret)

        for kh in args.key_handles.keys():
            if args.random_nonce:
                nonce = ""
            else:
                nonce = public_id.decode("hex")
            aead = hsm.generate_aead(nonce, kh)

            if args.internal_db:
                if not store_in_internal_db(args, hsm, modhex_id, public_id, kh, aead):
                    res = False
                continue

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

    if res:
        print("\nDone\n")
    else:
        print("\nDone (one or more entries rejected)")
    return res


def store_in_internal_db(args, hsm, modhex_id, public_id, kh, aead):
    """Store record (AEAD) in YubiHSM internal DB"""
    if args.verbose:
        print(
            "    %i bytes (%s) -> internal db..." % (len(aead.data), shorten_aead(aead))
        )
    try:
        hsm.db_store_yubikey(public_id.decode("hex"), kh, aead)
        if args.verbose:
            print("OK")
    except pyhsm.exception.YHSM_CommandFailed as e:
        if args.verbose:
            print("%s" % (pyhsm.defines.status2str(e.status)))
        else:
            print(
                "Storing ID %s FAILED: %s"
                % (modhex_id, pyhsm.defines.status2str(e.status))
            )
        return False
    return True


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

    if sys.stdin.readline() != "# ykksm 1\n":
        sys.stderr.write("Did not get '# ykksm 1' header as first line of input.\n")
        sys.exit(1)

    print("output dir		: %s" % (args.output_dir))
    print("key handles		: %s" % (args.key_handles))
    print("YHSM device		: %s" % (args.device))
    print("")

    if args.aes_key:
        keys = {kh: args.aes_key for kh in args.key_handles}
        hsm = SoftYHSM(keys, args.debug)
    elif os.path.isfile(args.device):
        hsm = SoftYHSM.from_file(args.device, debug=args.debug)
    else:
        hsm = pyhsm.YHSM(device=args.device, debug=args.debug)

    return not import_keys(hsm, args)


if __name__ == "__main__":
    sys.exit(main())
