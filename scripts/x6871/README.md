# X6871 offline ABI verification

Run the existing verifier after building; do not recreate a session-specific
script. It is read-only for the kernel sources, Image, vmlinux and modules.
Only the JSON report and an explicitly requested new hash manifest are written.
Nothing is downloaded, rebuilt, flashed, or uploaded.

## Normal use

```sh
./build.sh
python3 scripts/x6871/verify_abi.py
```

Requires Python 3.8+, Git and `llvm-objdump`. ELF and BTF are read with Python's
standard library: no `pahole`, `bpftool`, third-party Python packages, or
temporary-session files are needed.

The repository is located relative to the script, not the working directory.
Output defaults to `OUT_DIR` from the environment or `<repo>/out`. LLVM is
located in the downloaded toolchain cache (`TOOLCHAIN_ROOT` is supported) or
on `PATH`. With multiple cached toolchains, select the appropriate executable:

```sh
python3 scripts/x6871/verify_abi.py \
  --out /path/to/build-output \
  --objdump /path/to/toolchain/bin/llvm-objdump \
  --json-report /path/to/report.json
```

The default report is `out/x6871-abi-report.json` (under the selected output
directory). It contains PASS/FAIL/SKIP entries, build identity and Image SHA256.
Report/manifest output paths must end in `.json` and cannot overwrite an audit
input or each other.
Exit status is **0** for passed requested checks, **1** for a failed check, and
**2** for invalid command-line arguments. Checks still run under `python3 -O`.
Optional checks without inputs are explicitly SKIP, never described as passed.

### Mandatory local checks

- Required BTF/sched_ext, arm64 4K, module-force-load and CFI configuration;
  block throttling and permissive CFI must remain disabled.
- Linked BTF sizes and field/bit offsets for the known scheduler, storage,
  GPIO, network, mailbox, signal, TTY, netfilter and USB contracts.
- Removed optional APIs are allowed; if their fields are present, their KABI
  reserve/hole placement is checked.
- Compatibility symbols and exports, module-loader diagnostic strings,
  protected-module policy, allocator refcount contract, plug NULL check,
  USB global dynamic-ID locking and six headless NAT RCU release sites.
- Disassembly of `set_swapper_pgd`: the MKP-compatible fixmap index/address.
  This check is compiler-sensitive. If code generation changes, inspect the
  instructions rather than weakening the check until it passes.
- Raw arm64 Image magic, Image/vmlinux/generated release consistency and
  tracked kernel/build-source differences from the Image's embedded Git
  commit. Documentation and `scripts/x6871/`-only edits do not require a
  kernel rebuild. The report distinguishes the built commit from current HEAD.

## Optional stock/module/rebase audit

Keep firmware artifacts **outside the repository**. Do not commit `.ko`, stock
Images, BTF blobs, generated Images or toolchain files. Supply their locations:

```sh
python3 scripts/x6871/verify_abi.py \
  --stock-btf /path/to/stock.btf \
  --baseline-btf /path/to/previous-build.btf \
  --baseline-config /path/to/previous-build.config \
  --modules-dir /path/to/unmodified-modules \
  --module-hashes /path/to/trusted-module-hashes.json
```

`--stock-btf` and `--baseline-btf` accept either raw BTF or an AArch64 ELF
vmlinux containing `.BTF`. A compressed stock boot Image is **not** accepted;
extract its BTF separately using the firmware's documented format.

- Stock BTF: report differences for the audit's named structures and verify
  every original `signal_struct` and `usb_driver` field.
- Stock plus baseline BTF: fail if a non-reserve stock field that matched in
  the previous build no longer matches. Also report restored stock fields.
  `--baseline-btf` requires `--stock-btf`.
- Baseline config: report effective changes; intentional tuning differences
  are not automatically rejected.
- Modules plus stock BTF: compare covered imported function signatures.
  Missing/ambiguous BTF coverage is listed separately from actual mismatches.
- Modules plus trusted hash manifest: compare SHA256 and the exact relative
  `.ko` file set, including additions/removals. Modules must be uncompressed.
- If `r8152.ko` is at the module-directory root, locate its `rtl8152_driver`
  ELF symbol and verify the linked kernel layout reads both original PM bits.
  No hardcoded module section offset is used.

### Record a trusted module set once

On a verified firmware extraction, create an external manifest:

```sh
python3 scripts/x6871/verify_abi.py \
  --modules-dir /path/to/trusted-stock-modules \
  --record-module-hashes /path/to/new-module-hashes.json
```

The manifest is a JSON object mapping relative filenames to SHA256 strings.
Creation fails if that path already exists. Recording a manifest establishes
a comparison baseline; it does **not** authenticate the extraction. Record
firmware provenance separately and keep the manifest unchanged for later runs.
Without `--module-hashes`, current hashes are reported but integrity is SKIP.

## Tests

```sh
python3 -m unittest discover -s scripts/x6871 -p 'test_*.py' -v
```

Tests use small synthetic ELF/BTF fixtures; no toolchains, kernel output,
firmware or real vendor modules are needed.

## Limits

This is a targeted regression verifier, **not complete ABI certification**.
It does not establish every embedded structure, inline helper, constant,
callback's CFI hash, or behavioral/lifetime contract. Stock Image and vendor
modules may have different build revisions. BTF differences alone do not
justify speculative fixes; prove actual module access when investigating.

Build first and do not mutate generated outputs between build and verification.
The checks cannot establish complete build reproducibility or authenticate all
artifacts. A successful report never replaces device boot, memory-pressure,
idle/resume, network, charging and USB testing. It does not authorize pushing.
