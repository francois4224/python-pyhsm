#
# Copyright (C) 2013 Yubico AB. All rights reserved.
#
"""
This is a daemon to allow multiple users of a YubiHSM without requiring
permission to use the device. The daemon listens on a TCP port on localhost
and allows multiple connections to share a YubiHSM. Access the YubiHSM via
the daemon by specifying a device string following the yhsm://<host>:<port>
syntax:

hsm = YHSM('yhsm://localhost:5348')

Note that the daemon and clients need to share the same version number to be
compatible.
"""

import sys
import socket
import json
import threading
import argparse
import pyhsm.stick
import daemon
import os


CMD_WRITE = 0
CMD_READ = 1
CMD_FLUSH = 2
CMD_DRAIN = 3
CMD_LOCK = 4
CMD_UNLOCK = 5

COMMANDS = {
    CMD_WRITE: "write",
    CMD_READ: "read",
    CMD_FLUSH: "flush",
    CMD_DRAIN: "drain",
}

context = daemon.DaemonContext()


def pack_data(data):
    if isinstance(data, basestring):
        return data.encode("base64")
    return data


def unpack_data(data):
    if isinstance(data, basestring):
        return data.decode("base64")
    return data


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


class YHSM_Stick_Server:
    def __init__(self, device, addr):
        self.device = device
        self._stick = None
        self.pidfile = None

        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.socket.bind(addr)
        self.lock = threading.Lock()

    def serve(self):
        write_pid_file(self.pidfile)

        self.socket.listen(20)

        try:
            while True:
                cs, address = self.socket.accept()
                thread = threading.Thread(target=self.client_handler, args=(cs,))
                thread.start()
        except Exception as e:
            print(e)
            sys.exit(1)

    def invoke(self, cmd, *args):
        try:
            if not self._stick:
                self._stick = pyhsm.stick.YHSM_Stick(self.device)
            res = getattr(self._stick, COMMANDS[cmd])(*args)
        except Exception as e:
            res = e
            print(e)
            self._stick = None
        return res

    def client_handler(self, socket):
        socket_file = socket.makefile("wb")
        has_lock = False

        try:
            while True:
                data = json.loads(socket_file.readline())
                cmd = data[0]
                args = map(unpack_data, data[1:])
                if cmd == CMD_LOCK:
                    if not has_lock:
                        self.lock.acquire()
                        has_lock = True
                elif has_lock:
                    if cmd == CMD_UNLOCK:
                        self.lock.release()
                        has_lock = False
                    else:
                        res = self.invoke(cmd, *args)
                        json.dump(pack_data(res), socket_file)
                        socket_file.write("\n")
                        socket_file.flush()
                else:
                    err = "Command run without holding lock!"
                    print(err)
                    json.dump({"error": err}, socket_file)
                    socket_file.write("\n")
                    socket_file.flush()
                    break
        except Exception:
            # Client disconnected, ignore.
            pass
        finally:
            if has_lock:
                self.lock.release()
            socket_file.close()
            socket.close()


def main():
    parser = argparse.ArgumentParser(
        description="YubiHSM server daemon",
        add_help=True,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-D", "--device", nargs="?", default="/dev/ttyACM0", help="device name"
    )
    parser.add_argument(
        "-d", "--daemon", default=False, action="store_true", help="run as daemon"
    )
    parser.add_argument(
        "-I",
        "--interface",
        nargs="?",
        default="localhost",
        help="network interface to bind to",
    )
    parser.add_argument(
        "-P", "--port", nargs="?", type=int, default=5348, help="TCP port to bind to"
    )
    parser.add_argument(
        "--pid-file",
        dest="pid_file",
        default=None,
        required=False,
        help="PID file",
        metavar="FILENAME",
    )

    args = parser.parse_args()

    print(
        "Starting YubiHSM daemon for device: %s, listening on: %s:%d"
        % (args.device, args.interface, args.port)
    )

    server = YHSM_Stick_Server(args.device, (args.interface, args.port))
    print("You can connect to the server using the following device string:")
    print("yhsm://127.0.0.1:%d" % args.port)

    server.pidfile = args.pid_file
    context.files_preserve = [server.socket]
    if args.daemon:
        with context:
            server.serve()
    else:
        server.serve()
