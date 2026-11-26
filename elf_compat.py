"""Bounded static Android ELF64 load-segment checks; not device QA."""
import struct


def compatible_page_sizes(path, machine):
    """Return supported 4/16 KiB load alignment; reject malformed ELF files."""
    with path.open('rb') as f:
        f.seek(0, 2)
        extent = f.tell()
        f.seek(0)
        header = f.read(64)
        if (len(header) != 64 or header[:7] != b'\x7fELF\x02\x01\x01'
                or struct.unpack_from('<HHI', header, 16) != (3, machine, 1)
                or struct.unpack_from('<H', header, 52)[0] != 64):
            raise ValueError('Expected target little-endian ELF64 ET_DYN.')
        offset = struct.unpack_from('<Q', header, 32)[0]
        size, count = struct.unpack_from('<HH', header, 54)
        if (size != 56 or not 0 < count < 65535 or offset < 64
                or offset + size * count > extent):
            raise ValueError('Malformed ELF program header table.')
        f.seek(offset)
        pages = {4096, 16384}
        loads = 0
        for _ in range(count):
            entry = f.read(size)
            if len(entry) != size:
                raise ValueError('Truncated ELF program header.')
            kind, _, start, vaddr, _, filesz, memsz, align = struct.unpack('<IIQQQQQQ', entry)
            if kind != 1:  # PT_LOAD
                continue
            loads += 1
            if (filesz > memsz or start + filesz > extent
                    or vaddr + memsz > 1 << 64
                    or (align > 1 and (align & (align - 1) or start % align != vaddr % align))):
                raise ValueError('Invalid ELF PT_LOAD extent or alignment.')
            pages = {p for p in pages if align >= p and align % p == 0
                     and start % p == vaddr % p}
        if not loads:
            raise ValueError('ELF has no PT_LOAD segments.')
        return sorted(pages)


def required_page_size(config):
    page = config.get('link_page_size', 4096)
    if type(page) is not int or page not in (4096, 16384):
        raise ValueError('Link page size must be 4096 or 16384 bytes.')
    return page


def page_size_flags(config):
    # Leave the toolchain default untouched for the legacy 4 KiB target.
    return ['-Wl,-z,max-page-size=16384'] if required_page_size(config) == 16384 else []
