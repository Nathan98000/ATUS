"""Page-cache residency (mincore) of the emulation DB's data files, optionally
dropping them first (posix_fadvise DONTNEED). Runs inside a throwaway helper
container with the atus-emu-pgdata volume mounted READ-ONLY at /pgdata:
  docker run --rm -v atus-emu-pgdata:/pgdata:ro -v <scratch>:/s:ro atus-audit-api:proto \
      python /s/pgcache.py [--drop]
Affects only the kernel page cache for these files; never writes them."""
import ctypes
import ctypes.util
import os
import sys

libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
libc.mmap.restype = ctypes.c_void_p
libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long]
libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
libc.mincore.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p]
PAGE = os.sysconf("SC_PAGE_SIZE")
drop = "--drop" in sys.argv


def resident(path):
    size = os.path.getsize(path)
    if size == 0:
        return 0, 0
    fd = os.open(path, os.O_RDONLY)
    try:
        if drop:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        addr = libc.mmap(None, size, 1, 1, fd, 0)  # PROT_READ, MAP_SHARED
        if addr in (None, ctypes.c_void_p(-1).value):
            return size, -1
        npages = (size + PAGE - 1) // PAGE
        vec = (ctypes.c_ubyte * npages)()
        rc = libc.mincore(addr, size, vec)
        libc.munmap(addr, size)
        if rc != 0:
            return size, -1
        return size, sum(v & 1 for v in vec) * PAGE
    finally:
        os.close(fd)


tot_size = tot_res = 0
for root, _, files in os.walk("/pgdata/base"):
    for f in files:
        s, r = resident(os.path.join(root, f))
        tot_size += s
        tot_res += max(r, 0)
print(f"drop={drop} files_bytes={tot_size} resident_bytes={tot_res} "
      f"resident_MB={tot_res/1048576:.1f} of {tot_size/1048576:.1f} MB ({100*tot_res/max(tot_size,1):.1f}%)")
