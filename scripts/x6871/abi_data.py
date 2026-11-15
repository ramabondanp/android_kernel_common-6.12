# SPDX-License-Identifier: GPL-2.0-only
"""Small, read-only ELF64/BTF readers for the X6871 offline verifier."""

from pathlib import Path
import struct


class DataError(ValueError):
    """Malformed or unsupported audit input."""


def region(data, offset, size):
    if offset < 0 or size < 0 or offset + size > len(data):
        raise DataError("truncated binary data")
    return data[offset:offset + size]


def cstring(data, offset):
    if not 0 <= offset < len(data):
        raise DataError("invalid string offset")
    end = data.find(b"\0", offset)
    if end < 0:
        raise DataError("unterminated string")
    return data[offset:end].decode("utf-8", errors="replace")


class ELF:
    """Little-endian AArch64 ELF64, including relocatable vendor modules."""

    def __init__(self, data):
        self.data = data
        header = region(data, 0, 64)
        if header[:7] != b"\x7fELF\x02\x01\x01":
            raise DataError("expected a little-endian ELF64 file")
        self.elf_type, machine = struct.unpack_from("<HH", header, 16)
        if machine != 183:
            raise DataError("expected an AArch64 ELF file")
        shoff = struct.unpack_from("<Q", header, 40)[0]
        entry_size, count, stridx = struct.unpack_from("<HHH", header, 58)
        if entry_size != 64:
            raise DataError("unsupported ELF section-header size")
        first = struct.unpack("<IIQQQQIIQQ", region(data, shoff, 64))
        count = count or first[5]
        stridx = first[6] if stridx == 0xffff else stridx
        region(data, shoff, count * entry_size)
        self.sections = []
        for i in range(count):
            values = struct.unpack_from("<IIQQQQIIQQ", data, shoff + i * entry_size)
            keys = ("name_offset", "type", "flags", "address", "offset", "size",
                    "link", "info", "alignment", "entry_size")
            self.sections.append(dict(zip(keys, values)))
        if not 0 <= stridx < count:
            raise DataError("invalid ELF section-name table")
        names = self.section_bytes(self.sections[stridx])
        for section in self.sections:
            section["name"] = cstring(names, section["name_offset"])

    @classmethod
    def load(cls, path):
        return cls(Path(path).read_bytes())

    def section_bytes(self, section):
        if section["type"] == 8:  # SHT_NOBITS
            raise DataError("section has no file contents")
        return region(self.data, section["offset"], section["size"])

    def section(self, name):
        for section in self.sections:
            if section["name"] == name:
                return self.section_bytes(section)
        raise DataError(f"ELF section {name!r} not found")

    def symbols(self):
        found = False
        for section in self.sections:
            if section["type"] != 2:  # SHT_SYMTAB
                continue
            found = True
            if section["entry_size"] != 24 or section["size"] % 24:
                raise DataError("invalid ELF symbol-table size")
            if section["link"] >= len(self.sections):
                raise DataError("invalid ELF symbol string table")
            names = self.section_bytes(self.sections[section["link"]])
            for values in struct.iter_unpack("<IBBHQQ", self.section_bytes(section)):
                name, info, _, index, value, size = values
                yield dict(name=cstring(names, name), binding=info >> 4,
                           index=index, value=value, size=size)
        if not found:
            raise DataError("ELF symbol table missing; cannot audit stripped module")

    def imports(self):
        return {s["name"] for s in self.symbols()
                if s["name"] and s["index"] == 0 and s["binding"] in (1, 2)}

    def symbol_data(self, name):
        candidates = [s for s in self.symbols() if s["name"] == name and s["index"]]
        if len(candidates) != 1:
            raise DataError(f"expected one defined symbol {name!r}")
        symbol = candidates[0]
        if symbol["index"] >= len(self.sections):
            raise DataError("symbol does not refer to a file-backed section")
        section = self.sections[symbol["index"]]
        offset = symbol["value"]
        if self.elf_type != 1:  # ET_REL values are section-relative
            offset -= section["address"]
        return region(self.section_bytes(section), offset, symbol["size"])


class BTF:
    """BTF v1 kinds 1..19; no dependency on pahole, bpftool or libbpf."""

    def __init__(self, data):
        header = struct.unpack("<HBBIIIII", region(data, 0, 24))
        magic, version, flags, length, toff, tlen, soff, slen = header
        if magic != 0xeb9f or version != 1 or flags or length < 24:
            raise DataError("expected little-endian BTF v1")
        self.strings = region(data, length + soff, slen)
        if not self.strings or self.strings[0]:
            raise DataError("invalid BTF string table")
        types = region(data, length + toff, tlen)
        self.types = [None]
        self.structs = {}
        self.functions = {}
        extra = {1: 4, 2: 0, 3: 12, 4: 12, 5: 12, 6: 8, 7: 0,
                 8: 0, 9: 0, 10: 0, 11: 0, 12: 0, 13: 8, 14: 4,
                 15: 12, 16: 0, 17: 4, 18: 0, 19: 12}
        position = 0
        while position < len(types):
            noff, info, size = struct.unpack("<III", region(types, position, 12))
            kind, count, bitfields = (info >> 24) & 31, info & 0xffff, info >> 31
            if kind not in extra:
                raise DataError(f"unsupported BTF kind {kind}")
            nbytes = extra[kind] * (count if kind in (4, 5, 6, 13, 15, 19) else 1)
            payload = region(types, position + 12, nbytes)
            name = cstring(self.strings, noff)
            item = dict(name=name, kind=kind, size=size)
            if kind in (4, 5):
                item["members"] = [
                    dict(name=cstring(self.strings, n), type=t,
                         offset=o & 0xffffff if bitfields else o,
                         bits=o >> 24 if bitfields else 0)
                    for n, t, o in struct.iter_unpack("<III", payload)
                ]
                if name:
                    self.structs.setdefault(name, []).append(len(self.types))
            elif kind == 1:
                item["encoding"] = struct.unpack("<I", payload)[0]
            elif kind == 3:
                item["array"] = struct.unpack("<III", payload)
            elif kind == 13:
                item["args"] = [t for _, t in struct.iter_unpack("<II", payload)]
            elif kind == 12:
                self.functions.setdefault(name, []).append(len(self.types))
            self.types.append(item)
            position += 12 + nbytes

    @classmethod
    def load(cls, path):
        data = Path(path).read_bytes()
        if data.startswith(b"\x7fELF"):
            data = ELF(data).section(".BTF")
        return cls(data)

    def item(self, tid):
        if not 0 < tid < len(self.types):
            raise DataError(f"invalid BTF type ID {tid}")
        return self.types[tid]

    def signature(self, tid, seen=()):
        if not tid:
            return "void"
        if tid in seen or len(seen) > 64:
            raise DataError("recursive or excessively deep BTF signature")
        item = self.item(tid)
        kind, name = item["kind"], item["name"]
        seen = (*seen, tid)
        if kind in (8, 9, 10, 11, 18):
            return self.signature(item["size"], seen)
        if kind == 2:
            return self.signature(item["size"], seen) + "*"
        if kind in (4, 5, 7):
            return ("union " if kind == 5 else "struct ") + name
        if kind in (6, 19):
            return f"enum {name}/{item['size']}"
        if kind == 3:
            element, _, count = item["array"]
            return f"{self.signature(element, seen)}[{count}]"
        if kind == 12:
            return self.signature(item["size"], seen)
        if kind == 13:
            args = ", ".join(self.signature(t, seen) for t in item["args"])
            return f"{self.signature(item['size'], seen)} ({args})"
        if kind == 1:
            return f"{name}/{item['size']}/{item['encoding']}"
        return name or f"kind{kind}/{item['size']}"

    def fields(self, tid, base=0, seen=()):
        if tid in seen:
            raise DataError("recursive anonymous BTF member")
        result = {}
        for member in self.item(tid).get("members", []):
            offset = base + member["offset"]
            if member["name"]:
                result[member["name"]] = dict(
                    offset=offset, bits=member["bits"],
                    type=self.signature(member["type"]))
            elif self.item(member["type"])["kind"] in (4, 5):
                result.update(self.fields(member["type"], offset, (*seen, tid)))
        return result

    def layout(self, name):
        candidates = [
            dict(size=self.item(tid)["size"], fields=self.fields(tid))
            for tid in self.structs.get(name, [])
        ]
        if not candidates:
            return None
        # DWARF/BTF may contain forward/partial duplicates. Prefer the most
        # complete layout, but never silently choose among equally complete
        # conflicting definitions.
        count = max(len(layout["fields"]) for layout in candidates)
        complete = [layout for layout in candidates if len(layout["fields"]) == count]
        if any(layout != complete[0] for layout in complete[1:]):
            raise DataError(f"ambiguous complete BTF layouts for {name}")
        return complete[0]

    def function_signatures(self, name):
        return {self.signature(tid) for tid in self.functions.get(name, [])}
