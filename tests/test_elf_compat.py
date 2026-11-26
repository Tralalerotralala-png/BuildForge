import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from elf_compat import compatible_page_sizes, page_size_flags, required_page_size


def elf(align=4096, *, machine=183, offset=0, vaddr=0, filesz=120, memsz=120, count=1, kind=1):
    header = bytearray(64)
    header[:7] = b'\x7fELF\x02\x01\x01'
    struct.pack_into('<HHI', header, 16, 3, machine, 1)
    struct.pack_into('<Q', header, 32, 64)
    struct.pack_into('<HHH', header, 52, 64, 56, count)
    return header + struct.pack('<IIQQQQQQ', kind, 5, offset, vaddr, 0, filesz, memsz, align)


class ElfCompatibilityTests(unittest.TestCase):
    def test_fixture_alignments(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / 'libfixture.so'
            for alignment, expected in [(4096, [4096]), (16384, [4096, 16384]), (1, [])]:
                p.write_bytes(elf(alignment))
                self.assertEqual(compatible_page_sizes(p, 183), expected)

    def test_malformed_elf_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / 'libfixture.so'
            for payload in [elf()[:64], elf(count=2), elf(kind=2), elf(machine=62),
                            elf(align=6000), elf(vaddr=1), elf(filesz=121, memsz=121),
                            elf(filesz=120, memsz=119)]:
                p.write_bytes(payload)
                with self.assertRaises(ValueError):
                    compatible_page_sizes(p, 183)

    def test_explicit_page_policy(self):
        self.assertEqual(required_page_size({}), 4096)
        self.assertEqual(page_size_flags({}), [])
        self.assertEqual(page_size_flags({'link_page_size': 16384}), ['-Wl,-z,max-page-size=16384'])
        for value in (True, '16384', 8192, 0):
            with self.assertRaises(ValueError):
                required_page_size({'link_page_size': value})

    @unittest.skipUnless(shutil.which('clang') and shutil.which('ld.lld'), 'Clang/LLD required')
    def test_real_freestanding_android_elf(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            src, lib = root / 'fixture.c', root / 'libfixture.so'
            src.write_text('int fixture(void) { return 42; }\n')
            for arch, machine in [('aarch64', 183), ('x86_64', 62)]:
                for page in (4096, 16384):
                    subprocess.run(['clang', '--target=' + arch + '-linux-android21', '-fuse-ld=lld',
                                    '-shared', '-nostdlib', '-fPIC', '-Wl,--no-undefined',
                                    '-Wl,-z,max-page-size=' + str(page), str(src), '-o', str(lib)],
                                   check=True, capture_output=True, timeout=30)
                    self.assertEqual(compatible_page_sizes(lib, machine),
                                     [4096] if page == 4096 else [4096, 16384])


if __name__ == '__main__':
    unittest.main()
