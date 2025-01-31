#!/usr/bin/env python3

from __future__ import print_function
from bcc import BPF, USDT
import argparse
import ctypes as ct
import time
import os
import io
import ipaddress
import socket
from collections import defaultdict

# globals
SYSCALLS = ["socket", "socketpair", "bind", "listen", "accept", "accept4",
            "connect", "getsockname", "getpeername", "sendto", "recvfrom",
            "setsockopt", "getsockopt", "shutdown", "sendmsg", "sendmmsg",
            "recvmsg", "recvmmsg", "read", "write", "open", "openat", "creat",
            "close", "sendfile64"]

SYSCALL = 1
DISK = 3
NET = 4
PADDING = "  "
BLUE = '\033[95m'
UNDERLINE = '\033[4m'
ENDC = '\033[0m'

# C result class

class CallEvent(ct.Structure):
    _fields_ = [
        ("depth", ct.c_ulonglong),
        ("pid", ct.c_ulonglong),
        ("lat", ct.c_ulonglong),
        ("type", ct.c_ulonglong),
        ("fd_type", ct.c_ulonglong),
        ("fdw", ct.c_ulonglong),
        ("fdr", ct.c_ulonglong),
        ("fd_ret", ct.c_ulonglong),
        ("bytes_write", ct.c_ulonglong),
        ("bytes_read", ct.c_ulonglong),
        ("addr", ct.c_ulonglong),
        ("clazz", ct.c_char * 80),
        ("method", ct.c_char * 80),
        ("file", ct.c_char * 80),
        ]


###############################################################################
# TEMPLATES
###############################################################################

# program template
PROGRAM = """
#include <uapi/linux/ip.h>
#include <uapi/linux/in.h>

struct call_t {
    u64     depth;                  // first bit is direction (0 entry, 1 return)
    u64     pid;                    // (tgid << 32) + pid from bpf_get_current...
    u64     lat;                    // time latency
    u64     type;                   // syscall or php function
    u64     fd_type;                // disk or net filedescriptor
    u64     fdw;                    // filedescriptor write
    u64     fdr;                    // filedescriptor read
    u64     fd_ret;                 // returned filedescriptor
    u64     bytes_write;            // number of write bytes
    u64     bytes_read;             // number of read bytes
    u64     addr;                   // addr to connect
    char    clazz[80];              // class name
    char    method[80];             // method name
    char    file[80];               // php file name
};

#define SYS     1
#define FUNC    2
#define DISK    3
#define NET     4

BPF_PERF_OUTPUT(calls);
BPF_HASH(entry, u64, u64);
BPF_HASH(start, u64, u64);
BPF_HASH(start_func, u64, u64);
BPF_HASH(fd, u64, u64);
BPF_HASH(addr, u64, u64);
BPF_HASH(filedescriptors, u64, u64);
"""

# php probes template
PHP_TRACE_TEMPLATE = """
int {name}(struct pt_regs *ctx) {{
    u64 *depth, zero = 0, clazz = 0, method = 0, file = 0;
    struct call_t data = {{}};
    u64 pid = bpf_get_current_pid_tgid();

    {read_class}
    {read_method}
    {read_file}
    bpf_probe_read(&data.clazz, sizeof(data.clazz), (void *)clazz);
    bpf_probe_read(&data.method, sizeof(data.method), (void *)method);
    bpf_probe_read(&data.file, sizeof(data.file), (void *)file);
    u64 id = clazz + method + file;

    data.type = FUNC;
    data.pid = pid;
    depth = entry.lookup_or_init(&data.pid, &zero);
    data.depth = {depth};
    {update_func}

    if (!(data.depth & (1ULL << 63))) {{
        u64 time = bpf_ktime_get_ns();
        start_func.update(&id, &time);
    }} else {{
        u64 *start_ns = start_func.lookup(&id);
        if (!start_ns) {{
            calls.perf_submit(ctx, &data, sizeof(data));
            start_func.delete(&id);
            return 0;
        }}
        data.lat = bpf_ktime_get_ns() - *start_ns;
        start_func.delete(&method);
    }}

    calls.perf_submit(ctx, &data, sizeof(data));
    return 0;
}}
"""

# syscall tracepoint template
SYS_TRACE_TEMPLATE = """
TRACEPOINT_PROBE(syscalls, sys_enter_{syscall_name}) {{
    struct bpf_pidns_info ns = {{}};
    if(bpf_get_ns_current_pid_tgid(DEV, INO, &ns, sizeof(struct bpf_pidns_info)))
	    return 0;
    u64 pid = ((u64)ns.pid << 32);
    if ({pid_condition}) {{
        return 0;
    }}
    pid = bpf_get_current_pid_tgid();
    u64 time = bpf_ktime_get_ns();
    start.update(&pid, &time);
    {syscall_enter_logic}
    return 0;
}}

TRACEPOINT_PROBE(syscalls, sys_exit_{syscall_name}) {{
    struct bpf_pidns_info ns = {{}};
    if(bpf_get_ns_current_pid_tgid(DEV, INO, &ns, sizeof(struct bpf_pidns_info)))
	    return 0;
    u64 pid = ((u64)ns.pid << 32);
    if ({pid_condition}) {{
        return 0;
    }}

    u64 *depth, zero = 0, clazz = 0, method = 0;
    pid = bpf_get_current_pid_tgid();

    struct call_t data = {{}};
    data.type = SYS;
    data.pid = pid;
    depth = entry.lookup_or_init(&data.pid, &zero);
    data.depth = *depth;
    char method_str[80] = "{syscall_name}";
    bpf_probe_read(&data.method, sizeof(data.method), method_str);

    u64 *start_ns = start.lookup(&pid);
    if (!start_ns) {{
        calls.perf_submit(args, &data, sizeof(data));
        return 0;
    }}

    data.lat = bpf_ktime_get_ns() - *start_ns;
    {syscall_exit_logic}
    calls.perf_submit(args, &data, sizeof(data));
    return 0;
}}
"""


class SyscallEvents:
    e = defaultdict(list)

    def event(self, syscalls, enter, exit):
        for syscall in syscalls:
            self.e[syscall].append((enter, exit))

    def plugin(self, syscall):
        return ("".join(a[0] for a in self.e[syscall]),
                "".join(a[1] for a in self.e[syscall]))

    def pid_condition(self, pids):
        return " && ".join("pid >> 32 != %s" % str(pid) for pid in pids)

    def syscall(self, pids, syscall):
        enter, exit = self.plugin(syscall)
        return SYS_TRACE_TEMPLATE.format(syscall_name=syscall,
                                         pid_condition=self.pid_condition(pids),
                                         syscall_enter_logic=enter,
                                         syscall_exit_logic=exit
                                         )

    def generate(self, pids, syscalls=None):
        if syscalls is None:
            global SYSCALLS
            syscalls = SYSCALLS
        return "".join(self.syscall(pids, syscall) for syscall in syscalls)


def print_event(pid, lat, message, depth):
    return ("%-6d %-10s %-40s" %
            (pid, str(lat), (PADDING * (depth - 1)) + message))


def syscall_message(event):
    message = io.StringIO()
    message.write("sys.")
    message.write(BLUE)
    message.write(event.method.decode("utf-8", "replace"))
    message.write(ENDC)
    if event.fdw > 0:
        message.write(" write on fd: %s" % event.fdw)
    if event.fdr > 0:
        message.write(" read fd: %s" % event.fdr)
    if event.fd_ret > 0:
        message.write(" return fd: %s" % event.fd_ret)

    if event.addr > 0:
        addr = str(ipaddress.ip_address(event.addr))
        rev = addr.split('.')[::-1]
        addr = '.'.join(rev)
        message.write(" connect to: %s" % addr)
        try:
            host = socket.gethostbyaddr(addr)
            message.write(" -> %s" % host[0])
        except socket.herror or socket.gaierror:
            pass
    return message.getvalue()


class Process:
    total_lat = 0
    total_net_time = 0
    total_disk_time = 0
    net_write_volume = 0
    disk_write_volume = 0
    net_read_volume = 0
    disk_read_volume = 0

    def __init__(self):
        self.data_buffer = io.StringIO()

    def reset(self):
        self.total_lat = 0
        self.total_net_lat = 0
        self.total_disk_lat = 0
        self.net_write_volume = 0
        self.disk_write_volume = 0
        self.net_read_volume = 0
        self.disk_read_volume = 0

    def add_in_buffer(self, data):
        self.data_buffer.write(data + '\n')

    def get_buffer(self):
        ret = self.data_buffer.getvalue()
        self.data_buffer.close()
        self.data_buffer = io.StringIO()
        return ret


class Callback:
    process_dict = defaultdict(Process)

    def __init__(self, args):
        self.args = args

    def __call__(self, cpu, data, size):
        event = ct.cast(data, ct.POINTER(CallEvent)).contents
        depth = event.depth & (~(1 << 63))
        if depth == 0:
            return
        process = self.process_dict[str(event.pid)]
        if event.type == SYSCALL:
            process.total_lat += event.lat

            if event.fd_type == NET:
                process.total_net_time += event.lat

                if event.bytes_write > 0:
                    process.net_write_volume += event.bytes_write
                elif event.bytes_read > 0:
                    process.net_read_volume += event.bytes_read

            elif event.fd_type == DISK:
                process.total_disk_time += event.lat

                if event.bytes_write > 0:
                    process.disk_write_volume += event.bytes_write
                elif event.bytes_read > 0:
                    process.disk_read_volume += event.bytes_read

            if not self.args.syscalls:
                return

            process.add_in_buffer(print_event(
                event.pid >> 32,
                event.lat,
                syscall_message(event),
                depth
            ))

        else:
            # Return function case
            if event.depth & (1 << 63):
                direction = "<- "

                if SYSCALLS:
                    if process.total_lat > 0:
                        process.add_in_buffer(print_event(
                                event.pid >> 32,
                                process.total_lat,
                                BLUE + "traced syscalls total latence" + ENDC,
                                depth
                        ))

                    if process.total_net_time > 0:
                        process.add_in_buffer(
                            print_event(
                                event.pid >> 32, process.total_net_time, BLUE + (
                                    "sys time spent on the network |-> %d bytes written, %d bytes read" %
                                    (process.net_write_volume, process.net_read_volume)) + ENDC, depth))

                    if process.total_disk_time > 0:
                        process.add_in_buffer(
                            print_event(
                                event.pid >> 32, process.total_disk_time, BLUE + (
                                    "sys time spent on the disk |-> %d bytes written, %d bytes read" %
                                    (process.disk_write_volume, process.disk_read_volume)) + ENDC, depth))

                    # reset counters
                    process.reset()
            # Entry function case
            else:
                direction = "-> "

            process.add_in_buffer(
                print_event(
                    event.pid >> 32,
                    str(
                        event.lat) if event.lat > 0 else "-",
                    "".join(
                        (direction,
                         event.clazz.decode(
                             'utf-8',
                             'replace'),
                            ".",
                            event.method.decode(
                             'utf-8',
                             'replace'),
                            " ",
                            UNDERLINE,
                            "from ",
                            event.file.decode(
                            'utf-8',
                            'replace'),
                            ENDC)),
                    depth))
            # Quit the program on the last main return
            print(process.get_buffer())
            if event.depth & (
                    1 << 63) and event.method.decode(
                    'utf-8',
                    'replace') == "main" and depth == 1:
                print(process.get_buffer())
                del self.process_dict[str(event.pid)]
                if not self.process_dict:
                    exit()


class PHPEvents:
    usdt_tab = []
    txt = []
    probes = []

    def probe(self, pids, probe_name, func_name, read_class, read_method,
              read_file, is_return=False):
        "Generate the c for php probes"
        depth = "*depth + 1" if not is_return else "*depth | (1ULL << 63)"
        update = "++(*depth);" if not is_return else "if (*depth) --(*depth);"
        values = {
            'name': func_name,
            'read_class': read_class,
            'read_method': read_method,
            'read_file': read_file,
            'depth': depth,
            'update_func': update
        }
        self.txt.append(PHP_TRACE_TEMPLATE.format(**values))
        self.probes.append((probe_name, func_name))

    def generate(self, pids):
        for probe_name, func_name in self.probes:
            for pid in pids:
                usdt = USDT(pid=pid)
                usdt.enable_probe_or_bail(probe_name, func_name)
                self.usdt_tab.append(usdt)
        return "".join(self.txt)


def c_program(pids):
    "Generate the C program"
    program = io.StringIO()
    program.write(PROGRAM)
    php = PHPEvents()

    php.probe(pids,
              "function__entry",
              "php_entry",
              "bpf_usdt_readarg(4, ctx, &clazz);",
              "bpf_usdt_readarg(1, ctx, &method);",
              "bpf_usdt_readarg(2, ctx, &file);",
              is_return=False)
    php.probe(pids,
              "function__return",
              "php_return",
              "bpf_usdt_readarg(4, ctx, &clazz);",
              "bpf_usdt_readarg(1, ctx, &method);",
              "bpf_usdt_readarg(2, ctx, &file);",
              is_return=True)

    program.write(php.generate(pids))
    # trace syscalls

    s = SyscallEvents()
    # intercept when an open filedescriptor is read. get the fd for printing
    # and get the type for sort the latence in NET or DISK
    s.event(("read",), """
                u64 fdarg = args->fd;
                fd.update(&pid, &fdarg);

                """,
            """
                data.bytes_read = args->ret;
                u64 *fdarg = fd.lookup(&pid);
                if (fdarg) {
                    data.fdr = *fdarg;
                    fd.delete(&pid);
                    u64 *fdt = filedescriptors.lookup(fdarg);
                    if (fdt) {
                        data.fd_type = *fdt;
                    }
                }

                """)

    # intercept when write on an open filedescriptor. get the fd for printing
    # and get the type for sort the latence in NET or DISK
    s.event(("write", "sendto", "sendmsg"), """
                u64 fdarg = args->fd;
                fd.update(&pid, &fdarg);

                """,
            """
                data.bytes_write = args->ret;
                u64 *fdarg = fd.lookup(&pid);
                if (fdarg) {
                    data.fdw = *fdarg;
                    fd.delete(&pid);
                    u64 *fdt = filedescriptors.lookup(fdarg);
                    if (fdt) {
                        data.fd_type = *fdt;
                    }
                }

                """)

    # store in a map the filedescriptors when open or socket open it.
    # and store the type: NET or DISK
    s.event(("open", "openat", "creat"), "", """
                u64 ret = args->ret;
                u64 flag = DISK;
                filedescriptors.update(&ret, &flag);
                data.fd_ret = ret;

                """)

    s.event(("socket",), "", """
                u64 ret = args->ret;
                u64 flag = NET;
                filedescriptors.update(&ret, &flag);
                data.fd_ret = ret;

                """)

    # decorator for trace the address in the connect arg
    s.event(("connect",), """
                struct sockaddr_in *useraddr = ((struct sockaddr_in *)(args->uservaddr));
                u64 a = useraddr->sin_addr.s_addr;
                addr.update(&pid, &a);
                u64 fdarg = args->fd;
                fd.update(&pid, &fdarg);

                """,
            """
                u64 *a = addr.lookup(&pid);
                if (a) {
                    data.addr = *a;
                    addr.delete(&pid);
                }
                u64 *fdarg = fd.lookup(&pid);
                if (fdarg) {
                    data.fdw = *fdarg;
                    fd.delete(&pid);
                }

                """)

    s.event(("bind",), """
                struct sockaddr_in *useraddr = ((struct sockaddr_in *)(args->umyaddr));
                u64 a = useraddr->sin_addr.s_addr;
                addr.update(&pid, &a);
                u64 fdarg = args->fd;
                fd.update(&pid, &fdarg);

                """,
            """
                u64 *a = addr.lookup(&pid);
                if (a) {
                    data.addr = *a;
                    addr.delete(&pid);
                }
                u64 *fdarg = fd.lookup(&pid);
                if (fdarg) {
                    data.fdw = *fdarg;
                    fd.delete(&pid);
                }

                """)

    program.write(s.generate(pids))
    return program.getvalue(), php.usdt_tab


def main():
    # cli arguments
    parser = argparse.ArgumentParser(
        description="php_tool",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "pid",
        type=int,
        nargs="+",
        help="process id to attach to")
    parser.add_argument(
        "--debug", action="store_true",
        help="debug mode: print the generated BPF program")
    parser.add_argument(
        "--check", action="store_true",
        help="print the generated BPF program and quit")
    parser.add_argument(
        "-S", "--syscalls", action="store_true",
        help="print the syscalls details inside each function")
    args = parser.parse_args()

    program, usdt_tab = c_program(args.pid)

    devinfo = os.stat("/proc/self/ns/pid")
    for r in (("DEV", str(devinfo.st_dev)), ("INO", str(devinfo.st_ino))):
      program = program.replace(*r)
    # debug options
    if args.check or args.debug:
        print(program)
        if args.check:
            exit()

    # inject the C program generated in eBPF
    bpf = BPF(text=program, usdt_contexts=usdt_tab)

    print("php super tool, pid = %s... Ctrl-C to quit." % (args.pid))
    print("%-6s %-10s %s" % ("PID", "LAT", "METHOD"))

    # don't forget the page_cnt option for increase the ring buffer size
    bpf["calls"].open_perf_buffer(Callback(args), page_cnt=8192)
    while True:
        try:
            bpf.perf_buffer_poll()
        except KeyboardInterrupt:
            exit()


if __name__ == "__main__":
    main()
