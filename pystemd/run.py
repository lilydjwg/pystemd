#!/usr/bin/env python3
#
# Copyright (c) 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the LICENSE file in
# the root directory of this source tree.
#

import fcntl
import os
import pty as ptylib
import select
import struct
import sys
import termios
import tty
import uuid

import pystemd
from pystemd.dbuslib import DBus, DBusAddress, DBusMachine
from pystemd.exceptions import PystemdRunError
from pystemd.systemd1 import Manager as SDManager, Unit
from pystemd.utils import x2char_star, x2cmdlist


EXIT_SUBSTATES = (b"exited", b"failed", b"dead")


class CExit:
    def __init__(self):
        self.pipe = []

    def __enter__(self):
        return self

    def __exit__(self, *excargs, **exckw):
        for call, args, kwargs in reversed(self.pipe):
            call(*args, **kwargs)

    def register(self, meth, *args, **kwargs):
        self.pipe.append((meth, args, kwargs))


def get_fno(obj):
    """
    Try to get the best fileno of a obj:
        * If the obj is a integer, it return that integer.
        * If the obj has a fileno method, it return that function call.
    """
    if obj is None:
        return None
    elif isinstance(obj, int):
        return obj
    elif hasattr(obj, "fileno") and callable(getattr(obj, "fileno")):
        return obj.fileno()

    raise TypeError("Expected None, int or fileobject with fileno method")


def run(
    cmd,
    address=None,
    service_type=None,
    name=None,
    user=None,
    user_mode=os.getuid() != 0,
    nice=None,
    runtime_max_sec=None,
    env=None,
    extra=None,
    cwd=None,
    machine=None,
    wait=False,
    remain_after_exit=False,
    collect=False,
    raise_on_fail=False,
    pty=None,
    pty_master=None,
    pty_path=None,
    stdin=None,
    stdout=None,
    stderr=None,
    _wait_polling=None,
    slice_=None,
    stop_cmd=None,
    stop_post_cmd=None,
    start_pre_cmd=None,
    start_post_cmd=None,
):
    """
    pystemd.run imitates systemd-run, but with a pythonic feel to it.

    Options:

        cmd: Array with the command to execute (absolute path only)
        stop_cmd: Array with the command to execute on stop (absolute path only)
        stop_post_cmd: Array with the command to execute after stop (absolute path only)
        start_pre_cmd: Array with the command to execute on pre start (absolute path only)
        start_post_cmd: Array with the command to execute on on post start (absolute path only)
        address: A custom dbus socket address
        service_type: Set the unit type, e.g. notify, oneshot. If you dont give a
            value, the unit type will be whatever systemd thinks is the default.
        name: Name of the unit. If not provided, it will be autogenerated.
        user: Username to execute the command, defaults to current user.
        user_mode: Equivalent to running `systemd-run --user`. Defaults to True
            if current user id not root (uid = 0).
        nice: Nice level to run the command.
        runtime_max_sec: Set seconds before sending a sigterm to the process, if
           the service does not die nicely, it will send a sigkill.
        env: A dict with environment variables.
        extra: If you know what you are doing, you can pass extra configuration
            settings to the start_transient_unit method.
        machine: Machine name to execute the command, by default we connect to
            the host's dbus.
        wait: Wait for command completion before returning control, defaults
            to False.
        remain_after_exit: If True, the transient unit will remain after cmd
            has finished, also if true, this methods will return
            pystemd.systemd1.Unit object. defaults to False and this method
            returns None and the unit will be gone as soon as is done.
        collect: Unload unit after it ran, even when failed.
        raise_on_fail: Will raise a PystemdRunError is cmd exit with non 0
            status code, it won't take affect unless you set wait=True,
            defaults to False.
        pty: Set this variable to True if you want a pty to be created. if you
            pass a `machine`, the pty will be created in the machine. Setting
            this value will ignore whatever you set in pty_master and pty_path.
        pty_master: It has only meaning if you pass a pty_path also, this file
            descriptor will be used to forward redirection to `stdin` and `stdout`
            if no `stdin` or `stdout` is present, then this value does nothing.
        pty_path: Setting this value will pass this pty_path to the created
            process and will connect the process stdin, stdout and stderr to this
            pty. by itself it only ensure that your process has a real pty that
            can have ioctl operation over it. if you also pass a `pty_master`,
            `stdin` and `stdout` the pty forwars is handle for you.
        stdin: Specify a file descriptor for stdin. By default this is `None`
            and your unit will not have a stdin. If you set pty = True, or set a
            `pty_master` then that pty will be read and forwarded to this file
            descriptor.
        stdout: Specify a file descriptor for stdout. By default this is `None`
            and your unit will not have a stdout. If you set pty = True, or set a
            `pty_master` then that pty will be read and forwarded to this file
            descriptor.
        stderr: Specify a file descriptor for stderr. By default this is `None`
            and your unit will not have a stderr.
        slice_: the slice under you want to run the unit.

    More info and examples in:
    https://github.com/facebookincubator/pystemd/blob/master/_docs/pystemd.run.md

    """

    def bus_factory():
        if address:
            return DBusAddress(x2char_star(address))
        elif machine:
            return DBusMachine(x2char_star(machine))
        else:
            return DBus(user_mode=user_mode)

    name = x2char_star(name or "pystemd{}.service".format(uuid.uuid4().hex))
    runtime_max_usec = (runtime_max_sec or 0) * 10**6 or runtime_max_sec

    stdin, stdout, stderr = get_fno(stdin), get_fno(stdout), get_fno(stderr)
    env = env or {}
    unit_properties = {}
    selectors = []

    extra = extra or {}
    start_cmd = x2cmdlist(cmd, False) + extra.pop(b"ExecStart", [])
    stop_cmd = x2cmdlist(stop_cmd, False) + extra.pop(b"ExecStop", [])
    stop_post_cmd = x2cmdlist(stop_post_cmd, False) + extra.pop(b"ExecStopPost", [])
    start_pre_cmd = x2cmdlist(start_pre_cmd, False) + extra.pop(b"ExecStartPre", [])
    start_post_cmd = x2cmdlist(start_post_cmd, False) + extra.pop(b"ExecStartPost", [])

    if user_mode:
        _wait_polling = _wait_polling or 0.5

    with CExit() as ctexit, bus_factory() as bus, SDManager(bus=bus) as manager:

        if pty:
            if machine:
                with pystemd.machine1.Machine(machine) as m:
                    pty_master, pty_path = m.Machine.OpenPTY()
            else:
                pty_master, pty_follower = ptylib.openpty()
                pty_path = os.ttyname(pty_follower).encode()
                ctexit.register(os.close, pty_master)

        if slice_:
            unit_properties[b"Slice"] = x2char_star(slice_)

        if pty_path:
            unit_properties.update(
                {
                    b"StandardInput": b"tty",
                    b"StandardOutput": b"tty",
                    b"StandardError": b"tty",
                    b"TTYPath": pty_path,
                }
            )

            if None not in (stdin, pty_master):
                # lets set raw mode for stdin so we can forward input without
                # waiting for a new line, but lets also make sure we return the
                # attributes as they where after this method is done
                stdin_attrs = tty.tcgetattr(stdin)
                tty.setraw(stdin)
                ctexit.register(tty.tcsetattr, stdin, tty.TCSAFLUSH, stdin_attrs)
                selectors.append(stdin)

            if None not in (stdout, pty_master):
                if os.getenv("TERM"):
                    env[b"TERM"] = env.get(b"TERM", os.getenv("TERM").encode())

                selectors.append(pty_master)
                # lets be a friend and set the size of the pty.
                winsize = fcntl.ioctl(
                    stdout, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0)
                )
                fcntl.ioctl(pty_master, termios.TIOCSWINSZ, winsize)
        else:
            unit_properties.update(
                {
                    b"StandardInputFileDescriptor": get_fno(stdin) if stdin else stdin,
                    b"StandardOutputFileDescriptor": get_fno(stdout)
                    if stdout
                    else stdout,
                    b"StandardErrorFileDescriptor": get_fno(stderr)
                    if stderr
                    else stderr,
                }
            )

        unit_properties.update(
            {
                b"Type": service_type,
                b"Description": b"pystemd: " + name,
                b"ExecStartPre": start_pre_cmd or None,
                b"ExecStart": start_cmd,
                b"ExecStartPost": start_post_cmd or None,
                b"ExecStop": stop_cmd or None,
                b"ExecStopPost": stop_post_cmd or None,
                b"RemainAfterExit": remain_after_exit,
                b"CollectMode": b"inactive-or-failed" if collect else None,
                b"WorkingDirectory": cwd,
                b"User": user,
                b"Nice": nice,
                b"RuntimeMaxUSec": runtime_max_usec,
                b"Environment": [
                    b"%s=%s" % (x2char_star(key), x2char_star(value))
                    for key, value in env.items()
                ]
                or None,
            }
        )

        unit_properties.update(extra)
        unit_properties = {k: v for k, v in unit_properties.items() if v is not None}

        unit = Unit(name, bus=bus, _autoload=True)
        if wait:
            mstr = (
                (
                    "type='signal',"
                    "sender='org.freedesktop.systemd1',"
                    "path='{}',"
                    "interface='org.freedesktop.DBus.Properties',"
                    "member='PropertiesChanged'"
                )
                .format(unit.path.decode())
                .encode()
            )

            monbus = bus_factory()
            monbus.open()
            ctexit.register(monbus.close)

            monitor = pystemd.DBus.Manager(bus=monbus, _autoload=True)
            monitor.Monitoring.BecomeMonitor([mstr], 0)

            monitor_fd = monbus.get_fd()
            selectors.append(monitor_fd)

        # start the process
        unit_start_job = manager.Manager.StartTransientUnit(
            name, b"fail", unit_properties
        )

        while wait:
            _in, _, _ = select.select(selectors, [], [], _wait_polling)

            if stdin in _in:
                data = os.read(stdin, 1024)
                os.write(pty_master, data)

            if pty_master in _in:

                try:
                    data = os.read(pty_master, 1024)
                except OSError:
                    selectors.remove(pty_master)
                else:
                    os.write(stdout, data)

            if monitor_fd in _in:
                m = monbus.process()
                if m.is_empty():
                    continue

                m.process_reply(False)
                if (
                    m.get_path() == unit.path
                    and m.body[0] == b"org.freedesktop.systemd1.Unit"
                ):
                    _, message_job_path = m.body[1].get(b"Job", (0, b"/"))

                    if (
                        message_job_path != unit_start_job
                        and m.body[1].get(b"SubState") in EXIT_SUBSTATES
                    ):
                        break

            if _wait_polling and not _in and unit.Service.MainPID == 0:
                # on usermode the subscribe to events does not work that well
                # this is a temporary hack. you can always not wait on usermode.
                break

        if raise_on_fail:
            if unit.Service.ExecMainStatus:
                raise PystemdRunError(
                    "cmd {} exited with status {}".format(
                        cmd, unit.Service.ExecMainStatus
                    )
                )

        unit.load()
        unit.bus_context = bus_factory
        return unit


# do pystemd.run callable.
run.__module__ = sys.modules[__name__]
sys.modules[__name__] = run
