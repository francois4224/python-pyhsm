#
# Copyright (c) 2011, 2012 Yubico AB
# See the file COPYING for licence statement.
#
"""
 Credential validation server utilizing YubiHSM.

 Modes of operation :

  OTP - YubiKey validation using internal DB in YubiHSM.
        The YubiHSM can take care of complete Yubico OTP
        validation - including storing seen counter values
        in an internal database.

        There is an --otp mode available that tries to be
        compatible with YK-VAL, and there is a --short-otp
        mode that gives responses looking like YK-KSM.

  HOTP - OATH-HOTP validation using secrets stored on host
         computer (in secure AEADs only decryptable inside
         YubiHSM). The HMAC-SHA1 of the OATH counter value
         is done inside the YubiHSM, so the OATH Key is
         never exposed outside the YubiHSM.

  TOTP - OATH-TOTP validation using secrets stored on host
         computer (in secure AEADs only decryptable inside
         YubiHSM). The HMAC-SHA1 of the OATH timecounter value
         is compared, so the OATH Key is never exposed
         outside the YubiHSM.

  PWHASH - Uses AEAD plaintext compare in the YubiHSM to see
           if a supplied password hash matches the password
           hash used in an earlier 'set' operation. These
           AEADs can be generated using
           `yhsm-password-auth.py --set ...'.

 All these modes must be explicitly enabled on the command
 line to be allowed (--otp, --hotp, --totp and --pwhash).

 Examples using OATH-HOTP :

   > GET /yhsm/validate?hotp=ubftcdcdckcf359152 HTTP/1.1
   ...
   < HTTP/1.0 200 OK
   < OK counter=0003

   same again (replay), differently formatted :

   > GET /yhsm/validate?uid=ubftcdcdckcf&hotp=359152 HTTP/1.1
   ...
   < HTTP/1.0 200 OK
   < ERR Could not validate OATH-HOTP OTP

 Examples using OATH-TOTP :

   > GET /yhsm/validate?totp=ubftcdcdckcf216781 HTTP/1.1
   ...
   < HTTP/1.0 200 OK
   < OK timecounter=2ed5376

   same again (but outside of time tolerance), differently formatted :

   > GET /yhsm/validate?uid=ubftcdcdckcf&totp=359152 HTTP/1.1
   ...
   < HTTP/1.0 200 OK
   < ERR Could not validate OATH-TOTP OTP


 Example PWHASH (AEAD and NONCE as returned by
                 `yhsm-password-auth.py --set ...') :

 > GET /yhsm/validate?pwhash=pbkdf2-of-password-here&aead=2b70...2257&nonce=010203040506&kh=8192 HTTP/1.1
 ...
 < HTTP/1.0 200 OK
 < OK pwhash validated

"""

import re
import os
import sys
import time
import hmac
import syslog
import serial
import socket
import base64
import hashlib
import sqlite3
import argparse
import urlparse
import BaseHTTPServer
import pyhsm
import pyhsm.yubikey
import pyhsm.oath_hotp
import pyhsm.oath_totp

default_device = "/dev/ttyACM0"
default_serve_url = "/yhsm/validate?"
default_db_file = "/var/yubico/yhsm-validation-server.db"
default_clients_file = "/var/yubico/yhsm-validation-server_client-id.conf"
default_hotp_window = 5
default_totp_interval = 30
default_totp_tolerance = 1
default_pid_file = None

ykotp_valid_input = re.compile("^[cbdefghijklnrtuv]{32,48}$")
hotp_valid_input = re.compile("^[cbdefghijklnrtuv0-9]{6,20}$")
totp_valid_input = re.compile("^[cbdefghijklnrtuv0-9]{6,20}$")

hsm = None
args = None
saved_key_handle = None

client_ids = {}


class YHSM_VALRequestHandler(BaseHTTPServer.BaseHTTPRequestHandler):
    """
    Handle HTTP GET requests according to configuration in global variable `args'.
    """

    def do_GET(self):
        """
        Process validation GET requests.

        All modes of validation (OTP, OATH and PWHASH) must be explicitly
        enabled in `args' to be allowed.
        """
        if self.path.startswith(args.serve_url):
            res = None
            log_res = None
            mode = None
            params = urlparse.parse_qs(self.path[len(args.serve_url) :])
            if "otp" in params:
                if args.mode_short_otp:
                    # YubiKey internal db OTP in KSM mode
                    mode = "YubiKey OTP (short)"
                    res = validate_yubikey_otp_short(self, params)
                elif args.mode_otp:
                    # YubiKey internal db OTP validation 2.0
                    mode = "YubiKey OTP"
                    res = validate_yubikey_otp(self, params)
                    # status = [x for x in res.split('\n') if x.startswith("status=")]
                    # if len(status) == 1:
                    #    res = status[0][7:]
                    log_res = "&".join(res.split("\n"))
                else:
                    res = "ERR 'otp/otp2' disabled"
            elif "hotp" in params:
                if args.mode_hotp:
                    mode = "OATH-HOTP"
                    res = validate_oath_hotp(self, params)
                else:
                    res = "ERR 'hotp' disabled"
            elif "totp" in params:
                if args.mode_totp:
                    mode = "OATH-TOTP"
                    res = validate_oath_totp(self, params)
                else:
                    res = "ERR 'totp' disabled"
            elif "pwhash" in params:
                if args.mode_pwhash:
                    mode = "Password hash"
                    res = validate_pwhash(self, params)
                else:
                    res = "ERR 'pwhash' disabled"

            if not log_res:
                log_res = res

            self.log_message("%s validation result: %s -> %s", mode, self.path, log_res)

            if res != None:
                self.send_response(200)
                self.send_header("Content-type", "text/plain")
                self.end_headers()
                self.wfile.write(res)
                self.wfile.write("\n")
            else:
                self.log_error(
                    "No validation result to '%s' (responding 403)" % (self.path)
                )
                self.send_response(403, "Forbidden")
                self.end_headers()
        else:
            self.log_error(
                "Bad URL '%s' - I'm serving '%s' (responding 403)"
                % (self.path, args.serve_url)
            )
            self.send_response(403, "Forbidden")
            self.end_headers()

    def log_error(self, fmt, *fmt_args):
        """Log to syslog."""
        msg = self.my_address_string() + " - - " + fmt % fmt_args
        my_log_message(args, syslog.LOG_ERR, msg)

    def log_message(self, fmt, *fmt_args):
        """Log to syslog."""
        msg = self.my_address_string() + " - - " + fmt % fmt_args
        my_log_message(args, syslog.LOG_INFO, msg)

    def my_address_string(self):
        """For logging client host without resolving."""
        return self.client_address[0]


class YHSM_VALServer(BaseHTTPServer.HTTPServer):
    """
    Wrapper class to properly initialize address_family for IPv6 addresses.
    """

    def __init__(self, server_address, req_handler):
        if ":" in server_address[0]:
            self.address_family = socket.AF_INET6
        BaseHTTPServer.HTTPServer.__init__(self, server_address, req_handler)


def validate_yubikey_otp_short(self, params):
    """
    Validate YubiKey OTP using YubiHSM internal database.
    """
    from_key = params["otp"][0]
    if not re.match(ykotp_valid_input, from_key):
        self.log_error("IN: %s, Invalid OTP" % (from_key))
        return "ERR Invalid OTP"
    try:
        res = pyhsm.yubikey.validate_otp(hsm, from_key)
        return "OK counter=%04x low=%04x high=%02x use=%02x" % (
            res.use_ctr,
            res.ts_low,
            res.ts_high,
            res.session_ctr,
        )
    except pyhsm.exception.YHSM_CommandFailed as e:
        return "ERR %s" % (pyhsm.defines.status2str(e.status))


def validate_yubikey_otp(self, params):
    """
    Validate YubiKey OTP using YubiHSM internal database.
    """
    vres = {}
    from_key = params["otp"][0]
    if not re.match(ykotp_valid_input, from_key):
        self.log_error("IN: %s, Invalid OTP" % (from_key))
        vres["status"] = "BAD_OTP"
    else:
        vres["otp"] = from_key
    if not "nonce" in params:
        self.log_error("IN: %s, no nonce" % (from_key))
        vres["status"] = "MISSING_PARAMETER"
    else:
        nonce = params["nonce"][0]
        if len(nonce) < 16 or len(nonce) > 40:
            self.log_error("IN: %s, bad nonce : %s" % (from_key, nonce))
            vres["status"] = "MISSING_PARAMETER"
        else:
            vres["nonce"] = nonce
    if "sl" in params and not (params["sl"] == "100" or params["sl"] == "secure"):
        self.log_error("IN: %s, sync level unsupported" % (from_key))
        vres["status"] = "BACKEND_ERROR"
    (
        sig,
        client_key,
    ) = check_signature(params)
    if sig != True:
        self.log_error("IN: %s, signature validation error" % (from_key))
        if client_key == None:
            vres["status"] = "NO_SUCH_CLIENT"
            # To be compatible with YK-VAL, we will sign this response using a null key
            client_key = chr(0)
        else:
            vres["status"] = "BAD_SIGNATURE"
    if "status" not in vres:
        try:
            res = pyhsm.yubikey.validate_otp(hsm, from_key)
            vres.update(
                {
                    "status": "OK",
                    "sessioncounter": str(res.use_ctr),  # known confusion
                    "sessionuse": str(res.session_ctr),  # known confusion
                    "timestamp": str((res.ts_high << 16 | res.ts_low) / 8),
                }
            )
            if "sl" in params:
                vres["sl"] = "100"
                if "timestamp" in params:
                    vres["t"] = time.strftime("%FT%TZ0000", time.gmtime())
        except pyhsm.exception.YHSM_CommandFailed as e:
            if e.status == pyhsm.defines.YSM_ID_NOT_FOUND:
                vres["status"] = "BAD_OTP"
            elif e.status == pyhsm.defines.YSM_OTP_REPLAY:
                vres["status"] = "REPLAYED_OTP"
            elif e.status == pyhsm.defines.YSM_OTP_INVALID:
                vres["status"] = "BAD_OTP"
            else:
                vres["status"] = "BACKEND_ERROR"
            self.log_error(
                "IN: %s, validation result %s (replying %s)"
                % (from_key, pyhsm.defines.status2str(e.status), vres["status"])
            )

    return make_otp_response(vres, client_key)


def make_otp_response(vres, client_key):
    """
    Create validation response (signed, if a client key is supplied).
    """
    if client_key is not None:
        sig = make_signature(vres, client_key)
        vres["h"] = sig
    # produce "key=value" pairs from vres
    pairs = [x + "=" + "".join(vres[x]) for x in sorted(vres.keys())]
    return "\n".join(pairs)


def check_signature(params):
    """
    Verify the signature of the parameters in an OTP v2.0 verify request.

    Returns ValResultBool, Key
    """
    if "id" in params:
        try:
            id_int = int(params["id"][0])
        except:
            my_log_message(
                args,
                syslog.LOG_INFO,
                "Non-numerical client id (%s) in request." % (params["id"][0]),
            )
            return False, None
        key = client_ids.get(id_int)
        if key:
            if "h" in params:
                sig = params["h"][0]
                good_sig = make_signature(params, key)
                if sig == good_sig:
                    # my_log_message(args, syslog.LOG_DEBUG, "Good signature (client id '%i')" % id_int)
                    return True, key
                else:
                    my_log_message(
                        args,
                        syslog.LOG_INFO,
                        "Bad signature from client id '%i' (%s, expected %s)."
                        % (id_int, sig, good_sig),
                    )
            else:
                my_log_message(
                    args,
                    syslog.LOG_INFO,
                    "Client id (%i) but no HMAC in request." % (id_int),
                )
                return False, key
        else:
            my_log_message(args, syslog.LOG_INFO, "Unknown client id '%i'" % (id_int))
            return False, None
    return True, None


def make_signature(params, hmac_key):
    """
    Calculate a HMAC-SHA-1 (using hmac_key) of all the params except "h=".

    Returns base64 encoded signature as string.
    """
    # produce a list of "key=value" for all entries in params except `h'
    pairs = [x + "=" + "".join(params[x]) for x in sorted(params.keys()) if x != "h"]
    sha = hmac.new(hmac_key, "&".join(pairs), hashlib.sha1)
    return base64.b64encode(sha.digest())


def validate_oath_hotp(self, params):
    """
    Validate OATH-HOTP code using YubiHSM HMAC-SHA1 hashing with token keys
    secured in AEAD's that we have stored in an SQLite3 database.
    """
    from_key = params["hotp"][0]
    if not re.match(hotp_valid_input, from_key):
        self.log_error("IN: %s, Invalid OATH-HOTP OTP" % (params))
        return "ERR Invalid OATH-HOTP OTP"
    (
        uid,
        otp,
    ) = get_oath_hotp_bits(params)
    if not uid or not otp:
        self.log_error("IN: %s, could not get UID/OTP ('%s'/'%s')" % (params, uid, otp))
        return "ERR Invalid OATH-HOTP input"
    if args.debug:
        print("OATH-HOTP uid %s, OTP %s" % (uid, otp))

    # Fetch counter value for `uid' from database
    try:
        db = ValOathDb(args.db_file)
        entry = db.get(uid)
    except Exception as e:
        self.log_error("IN: %s, database error : '%s'" % (params, e))
        return "ERR Internal error"

    # Check for correct OATH-HOTP OTP
    nonce = entry.data["nonce"].decode("hex")
    aead = entry.data["aead"].decode("hex")
    new_counter = pyhsm.oath_hotp.search_for_oath_code(
        hsm,
        entry.data["key_handle"],
        nonce,
        aead,
        entry.data["oath_c"],
        otp,
        args.look_ahead,
    )
    if args.debug:
        print(
            "OATH-HOTP %i..%i -> new C == %s"
            % (
                entry.data["oath_c"],
                entry.data["oath_c"] + args.look_ahead,
                new_counter,
            )
        )
    if type(new_counter) != int:
        # XXX increase 'throttling parameter' to make brute forcing harder/impossible
        return "ERR Could not validate OATH-HOTP OTP"
    try:
        # Must successfully store new_counter before we return OK
        if db.update_oath_hotp_c(entry, new_counter):
            return "OK counter=%04x" % (new_counter)
        else:
            return "ERR replayed OATH-HOTP"
    except Exception as e:
        self.log_error("IN: %s, database error updating counter : %s" % (params, e))
        return "ERR Internal error"


def validate_oath_totp(self, params):
    """
    Validate OATH-TOTP code using YubiHSM HMAC-SHA1 hashing with token keys
    secured in AEAD's that we have stored in an SQLite3 database.
    """
    from_key = params["totp"][0]
    if not re.match(totp_valid_input, from_key):
        self.log_error("IN: %s, Invalid OATH-TOTP OTP" % (params))
        return "ERR Invalid OATH-TOTP OTP"
    (
        uid,
        otp,
    ) = get_oath_totp_bits(params)
    if not uid or not otp:
        self.log_error("IN: %s, could not get UID/OTP ('%s'/'%s')" % (params, uid, otp))
        return "ERR Invalid OATH-TOTP input"
    if args.debug:
        print("OATH-TOTP uid %s, OTP %s" % (uid, otp))

    # Fetch counter value for `uid' from database
    try:
        db = ValOathDb(args.db_file)
        entry = db.get(uid)
    except Exception as e:
        self.log_error("IN: %s, database error : '%s'" % (params, e))
        return "ERR Internal error"

    # Check for correct OATH-TOTP OTP
    nonce = entry.data["nonce"].decode("hex")
    aead = entry.data["aead"].decode("hex")
    new_timecounter = pyhsm.oath_totp.search_for_oath_code(
        hsm, entry.data["key_handle"], nonce, aead, otp, args.interval, args.tolerance
    )

    if args.debug:
        print(
            "OATH-TOTP counter: %i, interval: %i -> new timecounter == %s"
            % (entry.data["oath_c"], args.interval, new_timecounter)
        )
    if type(new_timecounter) != int:
        return "ERR Could not validate OATH-TOTP OTP"
    try:
        # Must successfully store new_timecounter before we return OK
        # Can use existing hotp function since it would be identical
        if db.update_oath_hotp_c(entry, new_timecounter):
            return "OK timecounter=%04x" % (new_timecounter)
        else:
            return "ERR replayed OATH-TOTP"
    except Exception as e:
        self.log_error("IN: %s, database error updating counter : %s" % (params, e))
        return "ERR Internal error"


def validate_pwhash(_self, params):
    """
    Validate password hash using YubiHSM.
    """
    pwhash, nonce, aead, key_handle = get_pwhash_bits(params)
    d_aead = aead.decode("hex")
    plaintext_len = len(d_aead) - pyhsm.defines.YSM_AEAD_MAC_SIZE
    pw = pwhash.ljust(plaintext_len, chr(0x0))
    if hsm.validate_aead(nonce.decode("hex"), key_handle, d_aead, pw):
        return "OK pwhash validated"
    return "ERR Could not validate pwhash"


def get_pwhash_bits(params):
    """Extract bits for password hash validation from params."""
    if (
        not "pwhash" in params
        or not "nonce" in params
        or not "aead" in params
        or not "kh" in params
    ):
        raise Exception(
            "Missing required parameter in request (pwhash, nonce, aead or kh)"
        )
    pwhash = params["pwhash"][0]
    nonce = params["nonce"][0]
    aead = params["aead"][0]
    key_handle = pyhsm.util.key_handle_to_int(params["kh"][0])
    return pwhash, nonce, aead, key_handle


def get_oath_hotp_bits(params):
    """Extract the OATH-HOTP uid and OTP from params."""
    if "uid" in params:
        return params["uid"][0], int(params["hotp"][0])
    m = re.match("^([cbdefghijklnrtuv]*)([0-9]{6,8})", params["hotp"][0])
    (
        uid,
        otp,
    ) = m.groups()
    return (
        uid,
        int(otp),
    )


def get_oath_totp_bits(params):
    """Extract the OATH-TOTP uid and OTP from params."""
    if "uid" in params:
        return params["uid"][0], int(params["totp"][0])
    m = re.match("^([cbdefghijklnrtuv]*)([0-9]{6,8})", params["totp"][0])
    (
        uid,
        otp,
    ) = m.groups()
    return (
        uid,
        int(otp),
    )


class ValOathDb:
    """Provides access to database with AEAD's and other information for OATH tokens."""

    def __init__(self, filename):
        self.filename = filename
        self.conn = sqlite3.connect(self.filename)
        self.conn.row_factory = sqlite3.Row

    def get(self, key):
        """Fetch entry from database."""
        c = self.conn.cursor()
        for row in c.execute(
            "SELECT key, nonce, key_handle, aead, oath_C, oath_T FROM oath WHERE key = ?",
            (key,),
        ):
            return ValOathEntry(row)
        raise Exception(
            "OATH token for '%s' not found in database (%s)" % (key, self.filename)
        )

    def update_oath_hotp_c(self, entry, new_c):
        """
        Update the OATH-HOTP counter value for `entry' in the database.

        Use SQL statement to ensure we only ever increase the counter.
        """
        key = entry.data["key"]
        c = self.conn.cursor()
        c.execute(
            "UPDATE oath SET oath_c = ? WHERE key = ? AND ? > oath_c",
            (
                new_c,
                key,
                new_c,
            ),
        )
        self.conn.commit()
        return c.rowcount == 1


class ValOathEntry:
    """Class to hold a row of ValOathDb."""

    def __init__(self, row):
        if row:
            self.data = row


def parse_args():
    """
    Parse the command line arguments
    """
    parser = argparse.ArgumentParser(
        description="Validate secrets using YubiHSM",
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
        "-U",
        "--serve-url",
        dest="serve_url",
        default=default_serve_url,
        required=False,
        help="Base URL for validation web service",
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
        "--port",
        dest="listen_port",
        type=int,
        default=8003,
        required=False,
        help="Port to listen on",
        metavar="PORT",
    )
    parser.add_argument(
        "--addr",
        dest="listen_addr",
        default="127.0.0.1",
        required=False,
        help="Address to bind to",
        metavar="ADDR",
    )
    parser.add_argument(
        "--hmac-kh",
        dest="hmac_kh",
        required=False,
        default=0,
        help="Key handle to use for creating HMAC-SHA1 hashes",
        metavar="KEY_HANDLE",
    )
    parser.add_argument(
        "--short-otp",
        dest="mode_short_otp",
        action="store_true",
        default=False,
        help="Enable YubiKey OTP validation (KSM style response)",
    )
    parser.add_argument(
        "--otp",
        dest="mode_otp",
        action="store_true",
        default=False,
        help="Enable YubiKey OTP validation 2.0",
    )
    parser.add_argument(
        "--hotp",
        dest="mode_hotp",
        action="store_true",
        default=False,
        help="Enable OATH-HOTP validation",
    )
    parser.add_argument(
        "--totp",
        dest="mode_totp",
        action="store_true",
        default=False,
        help="Enable OATH-TOTP validation",
    )
    parser.add_argument(
        "--pwhash",
        dest="mode_pwhash",
        action="store_true",
        default=False,
        help="Enable password hash validation",
    )
    parser.add_argument(
        "--db-file",
        dest="db_file",
        default=default_db_file,
        required=False,
        help="DB file for storing AEAD's etc. for --pwhash and --hotp",
        metavar="FILENAME",
    )
    # XXX bad interaction with argparse.ArgumentDefaultsHelpFormatter here - we don't want to
    # use default=default_clients_file since we need to know if this option was specified explicitly
    # or not.
    parser.add_argument(
        "--clients-file",
        dest="clients_file",
        default=None,
        required=False,
        help="File with OTP validation clients shared secrets. for --otp. Default : %s"
        % (default_clients_file),
        metavar="FILENAME",
    )
    parser.add_argument(
        "--hotp-window",
        dest="look_ahead",
        type=int,
        required=False,
        default=default_hotp_window,
        help="Number of OATH-HOTP codes to search",
        metavar="NUM",
    )
    parser.add_argument(
        "--totp-interval",
        dest="interval",
        type=int,
        required=False,
        default=default_totp_interval,
        help="Timeframe in seconds for a valid OATH-TOTP code",
        metavar="NUM",
    )
    parser.add_argument(
        "--totp-tolerance",
        dest="tolerance",
        type=int,
        required=False,
        default=default_totp_tolerance,
        help="Tolerance in time-steps for a valid OATH-TOTP code",
        metavar="NUM",
    )
    parser.add_argument(
        "--pid-file",
        dest="pid_file",
        default=default_pid_file,
        required=False,
        help="PID file",
        metavar="FILENAME",
    )

    return parser.parse_args()


def args_fixup():
    """
    Various cleanups/initializations based on result of parse_args().
    """
    global saved_key_handle
    saved_key_handle = args.hmac_kh

    args.key_handle = pyhsm.util.key_handle_to_int(args.hmac_kh)

    if not (
        args.mode_otp
        or args.mode_short_otp
        or args.mode_totp
        or args.mode_hotp
        or args.mode_pwhash
    ):
        my_log_message(args, syslog.LOG_ERR, "No validation mode enabled")
        sys.exit(1)

    global client_ids
    if args.clients_file != None:
        if not args.mode_otp:
            my_log_message(
                args, syslog.LOG_ERR, "Clients file should only be used with --otp."
            )
            sys.exit(1)
        client_ids = load_clients_file(args.clients_file)
        if not client_ids:
            my_log_message(
                args,
                syslog.LOG_ERR,
                'Failed loading clients file "%s"' % (args.clients_file),
            )
            sys.exit(1)
    else:
        # we accept failure to load this file when the default is used
        loaded_client_ids = load_clients_file(default_clients_file)
        if loaded_client_ids:
            args.clients_file = default_clients_file
            client_ids = loaded_client_ids


def load_clients_file(filename):
    """
    Load a list of base64 encoded shared secrets for numerical client ids.

    Returns a dict.

    Format of file is expected to be

        # This is a comment. Blank lines are OK.

        123,c2hhcmVkIHNlY3JldA==
        456,MTIzNDU2Nzg5MDEyMw==
    """
    res = {}
    content = []
    try:
        fhandle = file(filename)
        content = fhandle.readlines()
        fhandle.close()
    except IOError:
        return None
    linenum = 0
    for line in content:
        linenum += 1
        while line.endswith("\r") or line.endswith("\n"):
            line = line[:-1]
        if re.match("(^\s*#|^\s*$)", line):
            # skip comments and empty lines
            continue
        parts = [x.strip() for x in line.split(",")]
        try:
            if len(parts) != 2:
                raise Exception()
            id_num = int(parts[0])
            key = base64.b64decode(parts[1])
            res[id_num] = key
        except:
            my_log_message(
                args,
                syslog.LOG_ERR,
                'Bad data on line %i of clients file "%s" : "%s"'
                % (linenum, filename, line),
            )
            return None
    return res


def write_pid_file(fn):
    """Create a file with our PID."""
    if not fn:
        return None
    if fn == "" or fn == "''":
        # work around argument passings in init-scripts
        return None
    f = open(fn, "w")
    f.write("%s\n" % (os.getpid()))
    f.close()


def run():
    """
    Start the BaseHTTPServer and serve requests forever.
    """
    server_address = (args.listen_addr, args.listen_port)
    httpd = YHSM_VALServer(server_address, YHSM_VALRequestHandler)
    my_log_message(
        args,
        syslog.LOG_INFO,
        "Serving requests to 'http://%s:%s%s' (YubiHSM: '%s')"
        % (args.listen_addr, args.listen_port, args.serve_url, args.device),
    )
    httpd.serve_forever()


def my_log_message(my_args, prio, msg):
    """
    Log msg to syslog, and possibly also output to stderr.
    """
    syslog.syslog(prio, msg)
    if my_args.debug or my_args.verbose or prio == syslog.LOG_ERR:
        sys.stderr.write("%s\n" % (msg))


def main():
    """
    The main function that will be executed when running this as a stand alone script.
    """
    my_name = os.path.basename(sys.argv[0])
    if not my_name:
        my_name = "yhsm-validation-server"
    syslog.openlog(my_name, syslog.LOG_PID, syslog.LOG_LOCAL0)

    global args
    args = parse_args()
    args_fixup()

    global hsm
    try:
        hsm = pyhsm.YHSM(device=args.device, debug=args.debug)
    except serial.SerialException as e:
        my_log_message(
            args,
            syslog.LOG_ERR,
            'Failed opening YubiHSM device "%s" : %s' % (args.device, e),
        )
        return 1

    write_pid_file(args.pid_file)

    try:
        run()
    except KeyboardInterrupt:
        print("")
        print("Shutting down")
        print("")


if __name__ == "__main__":
    sys.exit(main())
