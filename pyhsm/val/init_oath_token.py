#
# Tool to add an OATH token to the yhsm-validation-server database.
#
# Copyright (c) 2011 Yubico AB
# See the file COPYING for licence statement.
#

import sys
import struct
import sqlite3
import argparse
import pyhsm
import pyhsm.oath_hotp
from hashlib import sha1

default_device = "/dev/ttyACM0"
default_db_file = "/var/yubico/yhsm-validation-server.db"


def parse_args():
    """
    Parse the command line arguments
    """
    global default_device

    parser = argparse.ArgumentParser(
        description="Initialize OATH token for use with yhsm-validation-server",
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
        "--force",
        dest="force",
        action="store_true",
        default=False,
        help="Overwrite any present entry",
    )
    parser.add_argument(
        "--key-handle",
        dest="key_handle",
        required=True,
        help="Key handle to create AEAD",
        metavar="HANDLE",
    )
    parser.add_argument(
        "--uid",
        dest="uid",
        required=True,
        help="User ID",
        metavar="STR",
    )
    parser.add_argument(
        "--oath-c",
        dest="oath_c",
        required=False,
        default=0,
        help="Initial OATH counter value",
        metavar="INT",
    )
    parser.add_argument(
        "--test-oath-window",
        dest="look_ahead",
        required=False,
        default=10,
        help="Number of codes to search with --test-code",
        metavar="INT",
    )
    parser.add_argument(
        "--test-code",
        dest="test_code",
        type=int,
        required=False,
        help="Optional OTP from token for verification",
        metavar="INT",
    )
    parser.add_argument(
        "--oath-k",
        dest="oath_k",
        required=False,
        help="The secret key of the token, hex encoded",
        metavar="HEXSTR",
    )
    parser.add_argument(
        "--db-file",
        dest="db_file",
        default=default_db_file,
        required=False,
        help="DB file for storing AEAD's for --pwhash and --oath in the yhsm-validation-server",
        metavar="FN",
    )

    args = parser.parse_args()
    return args


def args_fixup(args):
    keyhandles_fixup(args)


def keyhandles_fixup(args):
    args.key_handle = pyhsm.util.key_handle_to_int(args.key_handle)


def generate_aead(hsm, args):
    """Protect the oath-k in an AEAD."""
    key = get_oath_k(args)
    # Enabled flags 00010000 = YSM_HMAC_SHA1_GENERATE
    flags = struct.pack("< I", 0x10000)
    hsm.load_secret(key + flags)
    nonce = hsm.get_nonce().nonce
    aead = hsm.generate_aead(nonce, args.key_handle)
    if args.debug:
        print("AEAD: %s (%s)" % (aead.data.encode("hex"), aead))
    return nonce, aead


def validate_oath_c(hsm, args, nonce, aead):
    if args.test_code:
        if args.verbose:
            print(
                "Trying to validate the OATH counter value in the range %i..%i."
                % (args.oath_c, args.oath_c + args.look_ahead)
            )
        counter = pyhsm.oath_hotp.search_for_oath_code(
            hsm,
            args.key_handle,
            nonce,
            aead,
            args.oath_c,
            args.test_code,
            args.look_ahead,
        )
        if type(counter) != int:
            sys.stderr.write(
                "Failed validating OTP %s (in range %i..%i) using supplied key.\n"
                % (args.test_code, args.oath_c, args.oath_c + args.look_ahead)
            )
            sys.exit(1)
        if args.verbose:
            print("OATH C==%i validated with code %s" % (counter - 1, args.test_code))
        return counter
    return args.oath_c


def get_oath_k(args):
    """Get the OATH K value (secret key), either from args or by prompting."""
    if args.oath_k:
        decoded = args.oath_k.decode("hex")
    else:
        t = raw_input("Enter OATH key (hex encoded) : ")
        decoded = t.decode("hex")

    if len(decoded) > 20:
        decoded = sha1(decoded).digest()
    decoded = decoded.ljust(20, "\0")
    return decoded


class ValOathDb:
    """Provides access to database with AEAD's and other information."""

    def __init__(self, filename):
        self.filename = filename
        self.conn = sqlite3.connect(self.filename)

        self.create_table()

    def create_table(self):
        c = self.conn.cursor()
        c.execute(
            "CREATE TABLE IF NOT EXISTS oath "
            "(key TEXT PRIMARY KEY, nonce TEXT, key_handle INTEGER, aead TEXT, oath_C INTEGER, oath_T INTEGER)"
        )

    def add(self, entry):
        """Add entry to database."""
        c = self.conn.cursor()
        c.execute(
            "INSERT INTO oath (key, aead, nonce, key_handle, oath_C, oath_T) VALUES (?, ?, ?, ?, ?, ?)",
            (
                entry.data["key"],
                entry.data["aead"],
                entry.data["nonce"],
                entry.data["key_handle"],
                entry.data["oath_C"],
                entry.data["oath_T"],
            ),
        )
        self.conn.commit()
        return c.rowcount == 1

    def delete(self, entry):
        """Delete entry from database."""
        c = self.conn.cursor()
        c.execute("DELETE FROM oath WHERE key = ?", (entry.data["key"],))


class ValOathEntry:
    """Class to hold a row of ValOathDb."""

    def __init__(self, row):
        if row:
            self.data = row


def store_oath_entry(args, nonce, aead, oath_c):
    """Store the AEAD in the database."""
    data = {
        "key": args.uid,
        "aead": aead.data.encode("hex"),
        "nonce": nonce.encode("hex"),
        "key_handle": args.key_handle,
        "oath_C": oath_c,
        "oath_T": None,
    }
    entry = ValOathEntry(data)
    db = ValOathDb(args.db_file)
    try:
        if args.force:
            db.delete(entry)
        db.add(entry)
    except sqlite3.IntegrityError as e:
        sys.stderr.write("ERROR: %s\n" % (e))
        return False
    return True


def main():
    args = parse_args()

    args_fixup(args)

    print("Key handle		: %s" % (args.key_handle))
    print("YHSM device		: %s" % (args.device))
    print("")

    hsm = pyhsm.YHSM(device=args.device, debug=args.debug)

    nonce, aead = generate_aead(hsm, args)
    oath_c = validate_oath_c(hsm, args, nonce, aead)
    if not store_oath_entry(args, nonce, aead, oath_c):
        return 1


if __name__ == "__main__":
    sys.exit(main())
