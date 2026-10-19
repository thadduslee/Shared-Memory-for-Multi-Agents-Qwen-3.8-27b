"""Trusted exec wrapper: deny networking and cross-process escape syscalls.

Mounted outside the candidate workspace. Seccomp survives exec and applies to
all descendants. AF_UNIX socket pairs remain available for Python event-loop
wakeups; creating or connecting network/abstract-host sockets is forbidden.
"""
import ctypes
import errno
import os
import sys


def restrict():
    library = ctypes.CDLL('libseccomp.so.2', use_errno=True)
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    class Comparison(ctypes.Structure):
        _fields_ = [('arg',ctypes.c_uint),('op',ctypes.c_uint),
                    ('a',ctypes.c_uint64),('b',ctypes.c_uint64)]
    context = library.seccomp_init(0x7fff0000)  # ALLOW, narrowed below
    if not context:
        raise RuntimeError('seccomp initialization failed')
    deny = 0x00050000 | errno.EPERM
    try:
        for name in ('socketpair',):
            number = library.seccomp_syscall_resolve_name(name.encode())
            if number < 0 or library.seccomp_rule_add(context,deny,number,1,Comparison(0,1,1,0)):
                raise RuntimeError('network filter unavailable')
        for name in ('socket','connect','bind','listen','accept','accept4','sendto',
                     'ptrace','process_vm_readv','process_vm_writev','open_by_handle_at',
                     'name_to_handle_at','bpf','setns','mount','umount2','pivot_root'):
            number = library.seccomp_syscall_resolve_name(name.encode())
            if number >= 0 and library.seccomp_rule_add(context,deny,number,0):
                raise RuntimeError('isolation filter unavailable')
        if library.seccomp_load(context):
            raise RuntimeError('seccomp activation failed')
    finally:
        library.seccomp_release(context)


if __name__ == '__main__':
    try:
        restrict()
    except (OSError,RuntimeError):
        sys.stderr.write('trusted sandbox unavailable: seccomp activation failed\n')
        sys.exit(125)
    os.execv(sys.argv[1],sys.argv[1:])
