#
# Copyright (C) 2012-2013 Yubico AB. All rights reserved.
#
"""
This is a tool to decrypt AEADs generated using a YubiHSM, provided that
you know the key_handle used as well as the AES key used.

This can be used together with yhsm-generate-keys to generate a number
of AEADs, and then decrypt them to program YubiKeys accordingly.
"""

import os
import re
import sys
import fcntl
import argparse
import traceback

import pyhsm

args = None
yknum = 0


def parse_args():
    """
    Parse the command line arguments
    """
    parser = argparse.ArgumentParser(
        description="Decrypt AEADs",
        add_help=True,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
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
        "--format",
        dest="format",
        default="raw",
        help="Select output format (aead, raw or yubikey-csv)",
    )
    parser.add_argument(
        "--output-dir",
        dest="output_dir",
        help="Output dir basename (for --format aead)",
        metavar="DIR",
    )
    parser.add_argument(
        "--print-filename",
        dest="print_filename",
        action="store_true",
        default=False,
        help="Prefix each row with the AEAD filename",
    )
    parser.add_argument(
        "--key-handle",
        dest="key_handle",
        help="Key handle used when generating the AEADs.",
        metavar="HANDLE",
    )
    parser.add_argument(
        "--key-handle-out",
        dest="key_handle_out",
        help="Key handle used when generating *new* AEADs (with --format aead).",
        metavar="HANDLE",
    )
    parser.add_argument(
        "--aes-key",
        dest="aes_key",
        required=True,
        help="AES key used when generating the AEADs.",
        metavar="HEXSTR",
    )
    parser.add_argument(
        "--aes-key-out",
        dest="aes_key_out",
        required=False,
        help="AES key used when generating *new* AEADs (with --format aead).",
        metavar="HEXSTR",
    )
    parser.add_argument(
        "--start-public-id",
        dest="start_id",
        required=False,
        default=None,
        help="The first public id to decrypt",
        metavar="INT-OR-MODHEX",
    )
    parser.add_argument(
        "--stop-public-id",
        dest="stop_id",
        required=False,
        default=None,
        help="The last public id to decrypt",
        metavar="INT-OR-MODHEX",
    )
    parser.add_argument(
        "--fail-fast",
        dest="fail_fast",
        action="store_true",
        default=False,
        help="Terminate on the first AEAD failure, rather than keep going.",
    )
    parser.add_argument(
        "paths",
        nargs="+",
        help="Files and/or directories to process.",
        metavar="FILE-OR-DIR",
    )
    args = parser.parse_args()
    # argument fixups
    args.format = args.format.lower()
    args.aes_key = args.aes_key.decode("hex")
    if args.key_handle:
        args.key_handle = pyhsm.util.key_handle_to_int(args.key_handle)
    if args.start_id is not None:
        try:
            n = int(args.start_id)
        except ValueError:
            hexstr = pyhsm.yubikey.modhex_decode(args.start_id)
            n = int(hexstr, 16)
        args.start_id = n
    if args.stop_id is not None:
        try:
            n = int(args.stop_id)
        except ValueError:
            hexstr = pyhsm.yubikey.modhex_decode(args.stop_id)
            n = int(hexstr, 16)
        args.stop_id = n
    # some checks
    if args.format == "aead":
        if not args.output_dir:
            sys.stderr.write(
                "error: --output-dir is required when using --format aead.\n"
            )
            return False
        if not os.path.isdir(args.output_dir):
            sys.stderr.write(
                "error: Output directory '%s' not found\n" % (args.output_dir)
            )
            return False
        if not args.aes_key_out:
            sys.stderr.write(
                "error: --aes-key-out is required when using --format aead.\n"
            )
            return False
        if not args.key_handle_out:
            sys.stderr.write(
                "error: --key-handle-out is required when using --format aead.\n"
            )
            return False
        # argument fixups
        args.aes_key_out = args.aes_key_out.decode("hex")
        args.key_handle_out_orig = (
            args.key_handle_out
        )  # save to use in AEAD output paths
        args.key_handle_out = pyhsm.util.key_handle_to_int(args.key_handle_out)
    return args


class MyState:
    """
    Class to keep track of failed files.
    """

    def __init__(self, args):
        self.args = args
        self.failed_files = []
        self.file_count = 0

    def log_failed(self, fn):
        self.failed_files.append(fn)
        self.file_count += 1

    def log_success(self, fn):
        self.file_count += 1

    def should_quit(self):
        return self.failed_files and self.args.fail_fast


def process_file(path, fn, args, state):
    """
    The main function for reading a file and decrypting it.
    """
    full_fn = os.path.join(path, fn)

    if not re.match("^[cbdefghijklnrtuv]+$", fn):
        if args.debug:
            sys.stderr.write("warning: Ignoring non-modhex file '%s'\n" % (full_fn))
        return True

    if (args.start_id is not None) or (args.stop_id is not None):
        this = int(pyhsm.yubikey.modhex_decode(fn), 16)
        if (args.start_id is not None) and this < args.start_id:
            if args.debug:
                sys.stderr.write(
                    "warning: Skipping public id %s (%i) < %i\n"
                    % (fn, this, args.start_id)
                )
            return True
        if (args.stop_id is not None) and this > args.stop_id:
            if args.debug:
                sys.stderr.write(
                    "warning: Skipping public id %s (%i) > %i\n"
                    % (fn, this, args.stop_id)
                )
            return True

    if args.debug:
        sys.stderr.write("Loading AEAD : %s\n" % full_fn)

    aead = pyhsm.aead_cmd.YHSM_GeneratedAEAD(None, None, "")
    aead.load(full_fn)

    if not aead.nonce:
        # AEAD file version 0, need to fill in nonce etc.
        if args.key_handle is None:
            sys.stderr.write(
                "error: AEAD in file %s does not include key_handle, and none provided.\n"
                % (full_fn)
            )
            state.log_failed(full_fn)
            return False
        aead.key_handle = args.key_handle
        aead.nonce = pyhsm.yubikey.modhex_decode(fn).decode("hex")

    if args.debug:
        sys.stderr.write("%s\n" % aead)
        sys.stderr.write(
            "AEAD len %i : %s\n" % (len(aead.data), aead.data.encode("hex"))
        )
    pt = pyhsm.soft_hsm.aesCCM(
        args.aes_key, aead.key_handle, aead.nonce, aead.data, decrypt=True
    )

    if args.print_filename:
        print("%s " % (full_fn)),

    if args.format == "raw":
        print(pt.encode("hex"))
    elif args.format == "aead":
        # encrypt secrets with new key
        ct = pyhsm.soft_hsm.aesCCM(
            args.aes_key_out, args.key_handle_out, aead.nonce, pt, decrypt=False
        )
        aead_out = pyhsm.aead_cmd.YHSM_GeneratedAEAD(
            aead.nonce, args.key_handle_out, ct
        )
        filename = aead_filename(args.output_dir, args.key_handle_out_orig, fn)
        aead_out.save(filename)
        if args.print_filename:
            print("")
    elif args.format == "yubikey-csv":
        key = pt[: pyhsm.defines.KEY_SIZE]
        uid = pt[pyhsm.defines.KEY_SIZE :]
        access_code = "00" * 6
        timestamp = ""
        global yknum
        yknum += 1
        print(
            "%i,%s,%s,%s,%s,%s,,,,,"
            % (
                yknum,
                fn,
                uid.encode("hex"),
                key.encode("hex"),
                access_code,
                timestamp,
            )
        )

    state.log_success(full_fn)

    return True


def aead_filename(aead_dir, key_handle, public_id):
    """
    Return the filename of the AEAD for this public_id,
    and create any missing directorys.
    """
    parts = [aead_dir, key_handle] + pyhsm.util.group(public_id, 2)
    path = os.path.join(*parts)

    if not os.path.isdir(path):
        os.makedirs(path)

    return os.path.join(path, public_id)


def safe_process_files(path, files, args, state):
    """
    Process a number of files in a directory. Catches any exception from the
    processing and checks if we should fail directly or keep going.
    """
    for fn in files:
        full_fn = os.path.join(path, fn)
        try:
            if not process_file(path, fn, args, state):
                return False
        except Exception as e:
            sys.stderr.write(
                "error: %s\n%s\n" % (os.path.join(path, fn), traceback.format_exc())
            )
            state.log_failed(full_fn)
        if state.should_quit():
            return False
    return True


def walk_dir(path, args, state):
    """
    Check all files in `path' to see if there is any requests that
    we should send out on the bus.
    """
    if args.debug:
        sys.stderr.write("Walking %s\n" % path)

    for root, _dirs, files in os.walk(path):
        if not safe_process_files(root, files, args, state):
            return False
        if state.should_quit():
            return False
    return True


def main():
    """Main function when running as a program."""
    global args
    args = parse_args()

    if not args:
        return 1

    state = MyState(args)

    for path in args.paths:
        if os.path.isdir(path):
            walk_dir(path, args, state)
        else:
            safe_process_files(
                os.path.dirname(path), [os.path.basename(path)], args, state
            )
        if state.should_quit():
            break
    if state.failed_files:
        sys.stderr.write(
            "error: %i/%i AEADs failed\n" % (len(state.failed_files), state.file_count)
        )
        return 1
    if args.debug:
        sys.stderr.write("Successfully processed %i AEADs\n" % (state.file_count))
