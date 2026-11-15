#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Synthetic fixtures/negative controls; no kernel build or vendor files needed."""

import contextlib
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

from abi_data import BTF, DataError, ELF
from verify_abi import (
    Reporter, check_config, check_layout, check_usb_bits, compare_imports,
    compare_stock, function_body, read_config,
)


class BTFFixture:
    def __init__(self):
        self.strings = bytearray(b"\0")
        self.offsets = {"": 0}
        self.types = []

    def string(self, name):
        if name not in self.offsets:
            self.offsets[name] = len(self.strings)
            self.strings.extend(name.encode() + b"\0")
        return self.offsets[name]

    def add(self, kind, name="", size=0, payload=b"", count=0, bitfields=False):
        info = (kind << 24) | count | (int(bitfields) << 31)
        self.types.append(struct.pack("<III", self.string(name), info, size) + payload)
        return len(self.types)

    def integer(self, name="int", size=4):
        return self.add(1, name, size, struct.pack("<I", (1 << 24) | (size * 8)))

    def aggregate(self, name, size, fields, kind=4, bitfields=False):
        payload = b"".join(
            struct.pack("<III", self.string(n), t, offset)
            for n, t, offset in fields
        )
        return self.add(kind, name, size, payload, len(fields), bitfields)

    def function(self, name, return_type, args):
        payload = b"".join(struct.pack("<II", 0, tid) for tid in args)
        proto = self.add(13, size=return_type, payload=payload, count=len(args))
        return self.add(12, name, size=proto)

    def data(self):
        types = b"".join(self.types)
        return struct.pack("<HBBIIIII", 0xeb9f, 1, 0, 24, 0, len(types),
                           len(types), len(self.strings)) + types + self.strings

    def btf(self):
        return BTF(self.data())


def elf_fixture(btf=b"", executable=False):
    names = b"\0.shstrtab\0.strtab\0.symtab\0.data\0.BTF\0"
    sections = [b"", names, b"\0driver\0external\0", b"", b"\x00" * 296 + b"\x06" +
                b"\x00" * 39, btf]
    address = 0x1000 if executable else 0
    sections[3] = b"\0" * 24 + struct.pack(
        "<IBBHQQ", 1, 0x11, 0, 4, address, 336,
    ) + struct.pack("<IBBHQQ", 8, 0x10, 0, 0, 0, 0)
    shoff = 64
    offset = shoff + len(sections) * 64
    headers = [b"\0" * 64]
    for index, (name, data) in enumerate(zip(
        [b"", b".shstrtab", b".strtab", b".symtab", b".data", b".BTF"], sections,
    )):
        if not index:
            continue
        section_type = 2 if index == 3 else 3 if index in (1, 2) else 1
        headers.append(struct.pack(
            "<IIQQQQIIQQ", names.index(name), section_type, 0,
            address if index == 4 else 0, offset, len(data),
            2 if index == 3 else 0, 0, 1, 24 if index == 3 else 0,
        ))
        offset += len(data)
    header = bytearray(64)
    header[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<HHI", header, 16, 2 if executable else 1, 183, 1)
    struct.pack_into("<Q", header, 40, shoff)
    struct.pack_into("<HHHH", header, 52, 64, 0, 0, 64)
    struct.pack_into("<HH", header, 60, len(sections), 1)
    return bytes(header) + b"".join(headers) + b"".join(sections)


class DataTests(unittest.TestCase):
    def test_btf_fields_arrays_and_anonymous_union(self):
        f = BTFFixture()
        integer = f.integer()
        array = f.add(3, payload=struct.pack("<III", integer, integer, 2))
        union = f.aggregate("", 8, [("x", integer, 0), ("items", array, 0)], kind=5)
        f.aggregate("outer", 16, [("", union, 64)])
        layout = f.btf().layout("outer")
        self.assertEqual(layout["fields"]["x"]["offset"], 64)
        self.assertIn("[2]", layout["fields"]["items"]["type"])

    def test_bitfields(self):
        f = BTFFixture()
        integer = f.integer()
        f.aggregate("bits", 4, [("flag", integer, (1 << 24) | 17)], bitfields=True)
        field = f.btf().layout("bits")["fields"]["flag"]
        self.assertEqual((field["offset"], field["bits"]), (17, 1))

    def test_partial_duplicate_is_not_preferred(self):
        f = BTFFixture()
        integer = f.integer()
        f.aggregate("record", 0, [])
        f.aggregate("record", 4, [("x", integer, 0)])
        self.assertEqual(f.btf().layout("record")["size"], 4)

    def test_conflicting_complete_duplicates_fail(self):
        f = BTFFixture()
        integer = f.integer()
        f.aggregate("record", 8, [("x", integer, 0)])
        f.aggregate("record", 8, [("x", integer, 32)])
        with self.assertRaisesRegex(DataError, "ambiguous"):
            f.btf().layout("record")

    def test_qualifier_and_typedef_resolution(self):
        f = BTFFixture()
        integer = f.integer()
        const = f.add(10, size=integer)
        alias = f.add(8, "alias", const)
        pointer = f.add(2, size=alias)
        f.function("callback", integer, [pointer])
        signature = next(iter(f.btf().function_signatures("callback")))
        self.assertIn("*", signature)
        self.assertNotIn("alias", signature)

    def test_bad_btf_header_and_truncation(self):
        f = BTFFixture()
        f.integer()
        for data in [b"", b"\0" * 24, f.data()[:-1]]:
            with self.subTest(data=data[:8]), self.assertRaises(DataError):
                BTF(data)

    def test_unsupported_kind_fails(self):
        f = BTFFixture()
        f.add(31)
        with self.assertRaisesRegex(DataError, "kind 31"):
            f.btf()

    def test_recursive_type_fails_instead_of_matching_placeholder(self):
        f = BTFFixture()
        f.add(8, "cycle", 1)
        with self.assertRaisesRegex(DataError, "recursive"):
            f.btf().signature(1)

    def test_elf_symbols_and_data_rel_and_exec(self):
        for executable in [False, True]:
            with self.subTest(executable=executable):
                elf = ELF(elf_fixture(executable=executable))
                self.assertEqual(elf.imports(), {"external"})
                self.assertEqual(len(elf.symbol_data("driver")), 336)
                self.assertEqual(elf.symbol_data("driver")[296], 6)
                with self.assertRaises(DataError):
                    elf.symbol_data("missing")

    def test_btf_loaded_from_elf_and_raw(self):
        f = BTFFixture()
        f.integer()
        with tempfile.TemporaryDirectory() as directory:
            for name, contents in [("raw.btf", f.data()), ("vmlinux", elf_fixture(f.data()))]:
                path = Path(directory) / name
                path.write_bytes(contents)
                self.assertEqual(len(BTF.load(path).types), 2)

    def test_invalid_elf_fails(self):
        for data in [b"", b"\0" * 64, elf_fixture()[:100]]:
            with self.subTest(size=len(data)), self.assertRaises(DataError):
                ELF(data)


class CheckTests(unittest.TestCase):
    @staticmethod
    def layout_fixture(offset=0, size=4, field="member"):
        f = BTFFixture()
        integer = f.integer()
        f.aggregate("record", size, [(field, integer, offset)])
        return f.btf()

    def test_size_offset_and_missing_field_negative_controls(self):
        btf = self.layout_fixture()
        check_layout(btf, "record", 4, {"member": 0})
        for name, size, fields in [
            ("record", 8, {}), ("record", 4, {"member": 1}),
            ("record", 4, {"missing": 0}), ("absent", 4, {}),
        ]:
            with self.subTest(name=name, fields=fields), self.assertRaises(DataError):
                check_layout(btf, name, size, fields)

    def test_removed_optional_api_is_allowed_but_wrong_slot_fails(self):
        f = BTFFixture()
        integer = f.integer()
        f.aggregate("gpio_chip", 632, [("set_config", integer, 112 * 8)])
        check_layout(f.btf(), "gpio_chip", 632, {"set_config": 112})
        f = BTFFixture()
        integer = f.integer()
        f.aggregate("gpio_chip", 632, [("set_config", integer, 112 * 8),
                                       ("set_rv", integer, 128 * 8)])
        with self.assertRaisesRegex(DataError, "optional API"):
            check_layout(f.btf(), "gpio_chip", 632, {"set_config": 112})

    def test_usb_pm_bit_positions(self):
        for bit_delta in [0, -64]:
            f = BTFFixture()
            integer = f.integer()
            f.aggregate("usb_driver", 336, [
                ("supports_autosuspend", integer, (1 << 24) | (296 * 8 + 1 + bit_delta)),
                ("disable_hub_initiated_lpm", integer, (1 << 24) | (296 * 8 + 2 + bit_delta)),
            ], bitfields=True)
            if bit_delta:
                with self.assertRaisesRegex(DataError, "flag moved"):
                    check_usb_bits(f.btf())
            else:
                check_usb_bits(f.btf())

    def test_required_config_cannot_silently_disable_btf(self):
        config = {"CONFIG_" + key: "y" for key in [
            "ARM64", "ARM64_4K_PAGES", "DEBUG_INFO_BTF", "SCHED_CLASS_EXT",
            "MODULE_FORCE_LOAD", "CFI_CLANG",
        ]}
        check_config(config)
        for name in ["CONFIG_DEBUG_INFO_BTF", "CONFIG_CFI_CLANG"]:
            with self.subTest(name=name), self.assertRaises(DataError):
                check_config(dict(config, **{name: "n"}))
        with self.assertRaises(DataError):
            check_config(dict(config, CONFIG_BLK_DEV_THROTTLING="y"))

    def test_config_parser(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".config"
            path.write_text('CONFIG_A=y\n# CONFIG_B is not set\nCONFIG_C="text"\n')
            self.assertEqual(read_config(path), {"CONFIG_A": "y", "CONFIG_B": "n",
                                                 "CONFIG_C": '"text"'})

    def test_source_function_body_handles_nested_blocks(self):
        source = "/* f() {} */ void f(int x) { if (x) { x++; } }\nvoid g() {}"
        self.assertIn("if (x)", function_body(source, "f"))
        with self.assertRaises(DataError):
            function_body(source, "absent")

    def test_signature_mismatch_and_coverage_are_distinct(self):
        old, new = BTFFixture(), BTFFixture()
        old_int, new_int = old.integer(size=4), new.integer(size=8)
        old.function("api", old_int, [old_int])
        new.function("api", new_int, [new_int])
        result = compare_imports({"api", "assembly_entry"}, old.btf(), new.btf())
        self.assertIn("api", result["changed"])
        self.assertIn("assembly_entry", result["uncovered"])
        self.assertFalse(result["matching"])

    def test_stock_regression_and_restoration(self):
        def record(offset):
            f = BTFFixture()
            integer = f.integer()
            f.aggregate("task_struct", 8, [("member", integer, offset)])
            return f.btf()
        stock, shifted = record(0), record(32)
        regression = compare_stock(stock, shifted, stock)
        self.assertIn("member", regression["regressions"]["task_struct"])
        restored = compare_stock(stock, stock, shifted)
        self.assertEqual(restored["improvements"], {"task_struct": ["member"]})

    def test_failed_check_returns_nonzero_and_writes_json(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            report = Reporter()
            report.check("negative", lambda: check_layout(self.layout_fixture(), "record", 8, {}))
            path = Path(directory) / "report.json"
            self.assertEqual(report.finish(path), 1)
            self.assertEqual(json.loads(path.read_text())["result"], "FAIL")

    def test_optimized_python_still_rejects_bad_input(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-O", str(Path(__file__).with_name("verify_abi.py")),
                 "--out", directory], text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 1, result.stderr)

    def test_invalid_optional_input_combination_is_cli_error(self):
        result = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("verify_abi.py")),
             "--module-hashes", "missing.json"], text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("require --modules-dir", result.stderr)

    def test_report_cannot_overwrite_module_or_input_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vendor.ko"
            path.write_bytes(b"must not change")
            result = subprocess.run(
                [sys.executable, str(Path(__file__).with_name("verify_abi.py")),
                 "--json-report", str(path)], text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(path.read_bytes(), b"must not change")
            path = Path(directory) / "hashes.json"
            path.write_text("{}")
            result = subprocess.run(
                [sys.executable, str(Path(__file__).with_name("verify_abi.py")),
                 "--modules-dir", directory, "--module-hashes", str(path),
                 "--json-report", str(path)], text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("must not overwrite", result.stderr)
            self.assertEqual(path.read_text(), "{}")


if __name__ == "__main__":
    unittest.main()
