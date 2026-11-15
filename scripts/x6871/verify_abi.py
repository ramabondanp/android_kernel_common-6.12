#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Verify the X6871 build offline; passing does not replace device testing."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from abi_data import BTF, DataError, ELF


LAYOUTS = {
    "request": (288, {}),
    "request_queue": (None, {"mq_freeze_wq": 952}),
    "usb_device": (None, {"slot_id": 1488, "usb4_link": 1560}),
    "task_struct": (5184, {"prio": 148, "cpus_ptr": 1376, "cgroups": 2872,
                           "android_vendor_data1": 3320, "thread": 3488}),
    "sched_entity": (320, {"cfs_rq": 160, "vprot": 184}),
    "gpio_chip": (632, {"set_config": 112, "irq": 288}),
    "net_device": (2688, {"proto_down_reason": 1300, "reg_state": 1344, "priv": 2688}),
    "ethtool_ops": (656, {"create_rxfh_context": 328}),
    "mbox_chan": (248, {"active_req": 56}),
    "Scsi_Host": (2472, {"hostdata": 2472}),
    "pernet_operations": (72, {"id": 56, "size": 64}),
    "tty_struct": (760, {"hangup_work": 632, "disc_data": 664, "driver_data": 672}),
    "rq": (None, {"curr": 3344, "idle": 3360, "stop": 3368}),
    "nf_hook_ops": (40, {"hook": 0, "dev": 8, "priv": 16, "pf": 24,
                         "hooknum": 28, "priority": 32}),
    "signal_struct": (1184, {"oom_score_adj": 954, "oom_score_adj_min": 956,
                             "oom_mm": 960, "cred_guard_mutex": 968,
                             "exec_update_lock": 1016,
                             "cgroup_threadgroup_rwsem": 1120}),
    "usb_dynids": (24, {"list": 8}),
    "usb_driver": (336, {"dynids": 96, "driver": 120, "__kabi_reserved1": 304}),
}
OPTIONAL_FIELDS = {
    "gpio_chip": {"set_rv": 616, "set_multiple_rv": 624},
    "ethtool_ops": {"get_rxfh_fields": 624, "set_rxfh_fields": 632},
    "mbox_chan": {"tx_status": 12},
}
AUDIT_TYPES = """
task_struct thread_struct sched_entity sched_ext_entity rq scx_rq
cfs_rq rt_rq dl_rq root_domain task_group sched_avg sched_info
mm_struct vm_area_struct page folio pglist_data zone
request request_queue gendisk block_device bio bio_vec blk_plug
blk_mq_hw_ctx blk_mq_tag_set scsi_cmnd scsi_device Scsi_Host
ufs_hba ufs_clk_info ufs_dev_info ufs_pa_layer_attr
file file_operations inode super_block address_space dentry
cred signal_struct sighand_struct task_cputime
binder_proc binder_thread binder_transaction binder_buffer binder_alloc
binder_node binder_ref binder_work binder_transaction_data
sk_buff skb_shared_info sock socket proto net_device net_device_ops
ethtool_ops nf_hook_ops net pernet_operations inet_sock tcp_sock
cfg80211_ops wireless_dev wiphy cfg80211_scan_request ieee80211_hw
device device_driver dev_pm_info dev_pm_ops platform_device
gpio_chip mbox_chan power_supply power_supply_desc power_supply_config
thermal_zone_device cpufreq_policy cpufreq_driver devfreq
dma_buf dma_buf_ops dma_buf_attachment dma_heap scatterlist sg_table
usb_device usb_interface usb_driver usb_dynids urb usb_bus usb_hcd
tty_struct tty_driver tty_operations tty_port
work_struct delayed_work timer_list irq_work notifier_block
tracepoint tracepoint_func module shmem_inode_info shmem_sb_info simple_xattrs
""".split()


def require(condition, message):
    if not condition:
        raise DataError(message)


def read_config(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        if line.startswith("CONFIG_"):
            name, value = line.split("=", 1)
            result[name] = value
        elif line.startswith("# CONFIG_") and line.endswith(" is not set"):
            result[line[2:-11]] = "n"
    return result


def run(command, cwd=None):
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True)
    if result.returncode:
        raise DataError(f"{' '.join(map(str, command))}: {result.stderr.strip()}")
    return result.stdout


def check_layout(btf, name, size, fields):
    layout = btf.layout(name)
    require(layout is not None, f"BTF structure {name} missing")
    if size is not None:
        require(layout["size"] == size,
                f"{name}: size {layout['size']}, expected {size}")
    for field, byte in fields.items():
        actual = layout["fields"].get(field)
        require(actual is not None, f"{name}.{field}: field missing")
        require(actual["offset"] == byte * 8,
                f"{name}.{field}: bit offset {actual['offset']}, expected {byte * 8}")
    for field, byte in OPTIONAL_FIELDS.get(name, {}).items():
        if field in layout["fields"]:
            require(layout["fields"][field]["offset"] == byte * 8,
                    f"{name}.{field}: optional API moved outside its KABI slot/hole")
    return layout


def check_usb_bits(btf):
    layout = btf.layout("usb_driver")
    require(layout is not None, "usb_driver layout missing")
    fields = layout["fields"]
    for name, bit in [("supports_autosuspend", 1), ("disable_hub_initiated_lpm", 2)]:
        require(fields.get(name, {}).get("offset") == 296 * 8 + bit,
                f"usb_driver.{name}: PM flag moved")
        require(fields[name]["bits"] == 1, f"usb_driver.{name}: expected one bit")


def check_config(config):
    for name in ["ARM64", "ARM64_4K_PAGES", "DEBUG_INFO_BTF",
                 "SCHED_CLASS_EXT", "MODULE_FORCE_LOAD", "CFI_CLANG"]:
        require(config.get("CONFIG_" + name) == "y", f"CONFIG_{name} must be y")
    for name in ["BLK_DEV_THROTTLING", "CFI_PERMISSIVE"]:
        require(config.get("CONFIG_" + name, "n") == "n",
                f"CONFIG_{name} must stay disabled")


def check_exports(repo, symbols, image):
    names = [
        line.strip() for line in
        (repo / "gki/aarch64/symbols/android-dlkm-compat").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "["))
    ]
    require(bool(names), "compatibility export list is empty")
    for name in names:
        require(name in symbols and "__ksymtab_" + name in symbols,
                f"compat symbol {name} missing or not exported")
    for text in [b"disagrees about version of symbol %s (ignoring)",
                 b"also exported by the kernel (ignoring)",
                 b"incompatible vendor module; init/exit suppressed"]:
        require(text in image, f"linked module-loader diagnostic missing: {text!r}")
    return len(names)


def function_body(source, name):
    # These narrow source-contract checks complement, not replace, the linked
    # layout checks. Fail explicitly if a refactor moves/renames a function.
    source = re.sub(r"/\*.*?\*/|//[^\n]*", "", source, flags=re.S)
    match = re.search(r"\b" + re.escape(name) + r"\s*\([^;{}]*\)\s*\{", source)
    require(match is not None, f"function {name} not found")
    depth = 1
    for end in range(match.end(), len(source)):
        depth += (source[end] == "{") - (source[end] == "}")
        if not depth:
            return source[match.end():end]
    raise DataError(f"unclosed function {name}")


def check_sources(repo):
    build = (repo / "BUILD.bazel").read_text()
    target = re.search(r'name\s*=\s*"kernel_aarch64"\s*,(.*?)(?=^\))',
                       build, flags=re.S | re.M)
    require(target is not None, "kernel_aarch64 build target missing")
    require(not re.search(r"^\s*protected_module_names_list\s*=",
                          target[1], flags=re.M),
            "protected module-name enforcement returned for kernel_aarch64")
    for relative in ["drivers/usb/core/driver.c", "drivers/usb/serial/bus.c",
                     "drivers/usb/serial/usb-serial.c"]:
        text = (repo / relative).read_text()
        require("usb_dynids_lock" in text, f"{relative}: global dynid mutex missing")
        require(not re.search(r"dynids(?:\.|->)lock|__abi_compat_lock", text),
                f"{relative}: obsolete per-driver lock is in use")
    for relative in ["net/netfilter/nf_nat_core.c", "net/ipv4/netfilter/iptable_nat.c",
                     "net/ipv6/netfilter/ip6table_nat.c"]:
        text = (repo / relative).read_text()
        require(text.count("kfree_rcu_mightsleep(") == 2,
                f"{relative}: expected two headless-RCU array release sites")
        require(not re.search(r"kfree_rcu\((?:nat_ops|ops),\s*rcu\)", text),
                f"{relative}: embedded-head NAT array freeing returned")
    alloc = (repo / "mm/page_alloc.c").read_text()
    hook = function_body(alloc, "post_alloc_hook")
    require(hook.count("set_page_refcounted(page)") == 1,
            "post_alloc_hook no longer initializes the page refcount")
    for name in ["prep_new_page", "split_free_pages"]:
        require("set_page_refcounted(" not in function_body(alloc, name),
                f"{name} duplicates the post_alloc_hook refcount initialization")
    require("set_page_refcounted(" not in (repo / "mm/compaction.c").read_text(),
            "compaction duplicates the refcount initialization")
    block = (repo / "include/linux/blkdev.h").read_text()
    require(re.search(r"if\s*\(current->plug\)",
                      function_body(block, "blk_plug_invalidate_ts")),
            "blk_plug_invalidate_ts lost the NULL check")


def objdump_path(repo, explicit):
    if explicit:
        found = shutil.which(explicit)
        require(found is not None, f"objdump executable not found: {explicit}")
        return found
    root = Path(os.environ.get("TOOLCHAIN_ROOT", repo / "toolchain"))
    candidates = sorted(root.glob("*/bin/llvm-objdump"))
    if len(candidates) == 1:
        return str(candidates[0])
    found = shutil.which("llvm-objdump")
    require(found is not None, "set --objdump when LLVM is absent or ambiguous")
    return found


def check_fixmap(objdump, vmlinux):
    text = run([objdump, "-d", "--no-show-raw-insn",
                "--disassemble-symbols=set_swapper_pgd", str(vmlinux)])
    require(text.count("#0x3cd") == 2 and "#0xff43, lsl #16" in text
            and "#-0xd000" in text and "#0x3d4" not in text,
            "set_swapper_pgd does not encode FIX_PGD=973 / 0xffffffffff433000; "
            "inspect disassembly before changing this compiler-sensitive check")
    return text


def check_identity(repo, out, image, elf_data):
    text = (out / "include/generated/utsrelease.h").read_text()
    match = re.search(r'#define UTS_RELEASE "([^"]+)"', text)
    require(match is not None, "generated UTS_RELEASE missing")
    release = match[1]
    require("-dirty" not in release, "built release is marked dirty")
    sha = re.search(r"-g([0-9a-f]{7,40})$", release)
    require(sha is not None, "built release has no Git identity")
    commit = run(["git", "rev-parse", "--verify", sha[1] + "^{commit}"], repo).strip()
    # Documentation/verifier-only changes do not require another kernel build.
    # All tracked kernel/build changes since the embedded commit still fail.
    changed = run(["git", "diff", "--name-only", commit, "--", ".",
                   ":(exclude)AGENTS.md", ":(exclude)scripts/x6871/**"], repo).strip()
    require(not changed, "kernel/build sources differ from the built commit:\n" + changed)
    require(release.encode() in image and release.encode() in elf_data,
            "Image/vmlinux/generated release disagree")
    require(image[0x38:0x3c] == b"ARM\x64", "Image is not a raw arm64 kernel")
    return dict(release=release, built_commit=commit,
                head=run(["git", "rev-parse", "HEAD"], repo).strip(),
                image_bytes=len(image), sha256=hashlib.sha256(image).hexdigest())


def compare_stock(stock, current, baseline=None):
    types, regressions, improvements = {}, {}, {}
    for name in AUDIT_TYPES:
        expected, actual = stock.layout(name), current.layout(name)
        if expected is None or actual is None:
            types[name] = dict(stock=expected is not None, current=actual is not None)
            continue
        differences = {
            field: dict(stock=value, current=actual["fields"].get(field))
            for field, value in expected["fields"].items()
            if actual["fields"].get(field) != value
        }
        types[name] = dict(stock_size=expected["size"], current_size=actual["size"],
                           differences=differences)
        previous = baseline.layout(name) if baseline else None
        if previous is None:
            continue
        for field, value in expected["fields"].items():
            if field.startswith(("__kabi_reserved", "__android_kabi_reserved")):
                continue
            old, new = previous["fields"].get(field), actual["fields"].get(field)
            if old == value and new != value:
                regressions.setdefault(name, {})[field] = dict(stock=value, current=new)
            elif old != value and new == value:
                improvements.setdefault(name, []).append(field)
    return dict(types=types, regressions=regressions, improvements=improvements)


def compare_imports(imports, stock, current):
    matching, changed, uncovered = [], {}, {}
    for name in sorted(imports):
        old, new = stock.function_signatures(name), current.function_signatures(name)
        if not old or not new:
            uncovered[name] = dict(stock_btf=bool(old), current_btf=bool(new))
        elif len(old) != 1 or len(new) != 1:
            uncovered[name] = dict(reason="ambiguous named BTF function",
                                   stock=sorted(old), current=sorted(new))
        elif old == new:
            matching.append(name)
        else:
            changed[name] = dict(stock=sorted(old), current=sorted(new))
    return dict(matching=matching, changed=changed, uncovered=uncovered)


class Reporter:
    def __init__(self):
        self.data = {"checks": [], "artifacts": {}, "limitations": [
            "Offline checks are not proof of complete vendor ABI or runtime correctness.",
            "Stock Image BTF and module build revisions may differ.",
            "Named function signatures do not validate nested layouts, semantics or CFI hashes.",
        ]}

    def check(self, label, function):
        try:
            value = function()
        except (DataError, OSError, ValueError, KeyError, IndexError) as error:
            self.data["checks"].append(dict(name=label, status="FAIL", detail=str(error)))
            print(f"FAIL: {label}: {error}", flush=True)
            return None
        self.data["checks"].append(dict(name=label, status="PASS"))
        print(f"PASS: {label}", flush=True)
        return value

    def skip(self, label, reason):
        self.data["checks"].append(dict(name=label, status="SKIP", detail=reason))
        print(f"SKIP: {label}: {reason}", flush=True)

    def finish(self, path):
        failed = any(c["status"] == "FAIL" for c in self.data["checks"])
        self.data["result"] = "FAIL" if failed else "PASS"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Replace the report atomically instead of following an existing
        # output symlink or modifying another artifact through a hard link.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", dir=path.parent, prefix="." + path.name + ".", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(self.data, stream, indent=2, sort_keys=True)
                stream.write("\n")
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        print(f"Report: {path}")
        print("OFFLINE CHECKS " + self.data["result"] +
              "; device boot/resume/network/USB testing remains required.")
        return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--out", type=Path,
                        help="build directory (default: OUT_DIR environment or repo/out)")
    parser.add_argument("--objdump", help="explicit llvm-objdump executable")
    parser.add_argument("--stock-btf", type=Path, help="stock raw BTF or AArch64 vmlinux")
    parser.add_argument("--baseline-btf", type=Path,
                        help="previous build BTF/vmlinux; requires --stock-btf")
    parser.add_argument("--baseline-config", type=Path, help="report effective config changes")
    parser.add_argument("--modules-dir", type=Path, help="read-only tree of unmodified .ko files")
    parser.add_argument("--module-hashes", type=Path, help="trusted JSON filename:SHA256 manifest")
    parser.add_argument("--record-module-hashes", type=Path,
                        help="create a new manifest from trusted input modules; never overwrites")
    parser.add_argument("--json-report", type=Path, help="default: out/x6871-abi-report.json")
    args = parser.parse_args(argv)
    if args.baseline_btf and not args.stock_btf:
        parser.error("--baseline-btf requires --stock-btf")
    if (args.module_hashes or args.record_module_hashes) and not args.modules_dir:
        parser.error("module hash options require --modules-dir")
    repo = args.repo.resolve()
    out = (args.out or Path(os.environ.get("OUT_DIR", repo / "out"))).resolve()
    report = Reporter()
    report_path = args.json_report or out / "x6871-abi-report.json"
    inputs = [out / "vmlinux", out / "arch/arm64/boot/Image", out / ".config",
              args.stock_btf, args.baseline_btf, args.baseline_config, args.module_hashes]
    input_paths = {path.resolve() for path in inputs if path is not None}
    outputs = [path for path in (report_path, args.record_module_hashes) if path is not None]
    if any(path.suffix != ".json" for path in outputs):
        parser.error("report and manifest outputs must use the .json extension")
    if len({path.resolve() for path in outputs}) != len(outputs):
        parser.error("report and new manifest must use different paths")
    if any(path.resolve() in input_paths for path in outputs):
        parser.error("an output path must not overwrite an audit input")
    try:
        elf = ELF.load(out / "vmlinux")
        current = BTF(elf.section(".BTF"))
        image = (out / "arch/arm64/boot/Image").read_bytes()
        config = read_config(out / ".config")
    except (DataError, OSError, ValueError) as error:
        report.check("build inputs", lambda: require(False, str(error)))
        return report.finish(report_path)
    report.data["artifacts"]["build"] = report.check(
        "build/Image identity", lambda: check_identity(repo, out, image, elf.data))
    report.check("required configuration", lambda: check_config(config))
    for name, (size, fields) in LAYOUTS.items():
        report.check(name, lambda n=name, s=size, f=fields: check_layout(current, n, s, f))
    report.check("USB PM bitfield offsets", lambda: check_usb_bits(current))
    symbols = report.check("System.map", lambda: {
        line.split()[-1] for line in (out / "System.map").read_text().splitlines()
        if line.strip()
    })
    if symbols is not None:
        report.check("compat exports and module loader",
                     lambda: check_exports(repo, symbols, elf.data))
    report.check("allocator, plug, USB and NAT source contracts", lambda: check_sources(repo))
    report.data["artifacts"]["fixmap_disassembly"] = report.check(
        "MKP page-table fixmap", lambda: check_fixmap(
            objdump_path(repo, args.objdump), out / "vmlinux"))
    if args.baseline_config:
        previous = report.check("baseline config input", lambda: read_config(args.baseline_config))
        if previous is not None:
            report.data["artifacts"]["config_changes"] = {
                name: dict(before=previous.get(name, "n"), current=config.get(name, "n"))
                for name in previous.keys() | config.keys()
                if previous.get(name, "n") != config.get(name, "n")
            }
    stock = report.check("stock BTF input", lambda: BTF.load(args.stock_btf)) \
        if args.stock_btf else None
    baseline = report.check("baseline BTF input", lambda: BTF.load(args.baseline_btf)) \
        if args.baseline_btf else None
    if stock is not None:
        audit = report.check("stock layout comparison",
                             lambda: compare_stock(stock, current, baseline))
        report.data["artifacts"]["stock_layouts"] = audit
        for name in ["signal_struct", "usb_driver"]:
            def check_prefix(n=name):
                before, after = stock.layout(n), current.layout(n)
                require(before is not None and after is not None, f"{n}: layout unavailable")
                require(all(after["fields"].get(f) == v for f, v in before["fields"].items()),
                        f"{n}: stock prefix differs")
            report.check(f"complete stock {name} prefix", check_prefix)
        if baseline is not None and audit is not None:
            report.check("previously matching stock fields", lambda: require(
                not audit["regressions"], json.dumps(audit["regressions"])))
        elif not args.baseline_btf:
            report.skip("cross-base regression detection", "--baseline-btf not supplied")
    elif not args.stock_btf:
        report.skip("stock layout and cross-base comparison", "--stock-btf not supplied")
    if args.modules_dir:
        def read_modules():
            root = args.modules_dir.resolve()
            paths = sorted(root.rglob("*.ko"))
            require(bool(paths), "no uncompressed .ko files found")
            hashes, imports = {}, set()
            for path in paths:
                data = path.read_bytes()
                hashes[str(path.relative_to(root))] = hashlib.sha256(data).hexdigest()
                imports.update(ELF(data).imports())
            return hashes, imports
        modules = report.check("module ELF inputs", read_modules)
        if modules is not None:
            hashes, imports = modules
            report.data["artifacts"]["module_hashes"] = hashes
            if args.module_hashes:
                def integrity():
                    expected = json.loads(args.module_hashes.read_text())
                    require(expected == hashes, "module set or SHA256 differs from manifest")
                report.check("stock module integrity", integrity)
            else:
                report.skip("stock module integrity", "no trusted --module-hashes manifest")
            if args.record_module_hashes:
                def record():
                    with args.record_module_hashes.open("x") as stream:
                        json.dump(hashes, stream, indent=2, sort_keys=True)
                        stream.write("\n")
                report.check("record trusted module hashes", record)
            if stock is not None:
                signatures = report.check("module signature comparison",
                                          lambda: compare_imports(imports, stock, current))
                report.data["artifacts"]["imported_functions"] = signatures
                if signatures is not None:
                    report.check("covered imported prototypes", lambda: require(
                        not signatures["changed"], json.dumps(signatures["changed"])))
                    print(f"Coverage: {len(signatures['matching'])} matching prototypes; "
                          f"{len(signatures['uncovered'])} imports not covered.")
            else:
                report.skip("imported prototypes", "stock BTF unavailable")
            r8152 = args.modules_dir / "r8152.ko"
            if r8152.is_file():
                def pm_flags():
                    driver = ELF.load(r8152).symbol_data("rtl8152_driver")
                    layout = current.layout("usb_driver")
                    require(layout is not None, "usb_driver layout missing")
                    fields = layout["fields"]
                    for name in ["supports_autosuspend", "disable_hub_initiated_lpm"]:
                        bit = fields[name]["offset"]
                        require(bit // 8 < len(driver) and
                                driver[bit // 8] & (1 << (bit % 8)),
                                f"r8152 {name}: linked layout does not read an enabled flag")
                report.check("r8152 embedded-driver PM flags", pm_flags)
            else:
                report.skip("r8152 PM flags", "r8152.ko not at modules-dir root")
    else:
        report.skip("module integrity, imported prototypes and r8152", "--modules-dir not supplied")
    return report.finish(report_path)


if __name__ == "__main__":
    sys.exit(main())
