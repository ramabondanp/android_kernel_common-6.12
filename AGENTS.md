# AGENTS.md — Infinix X6871 (MT6895) gen6 GKI bring-up

Running an **Android 16 / GKI 6.12** kernel on an Infinix X6871 (MT6895)
with the **stock gen5 (6.12.38) vendor modules**, unmodified. Those modules
have no sources and must load as-is.

**Hard rule: never modify a `.ko`.** Every incompatibility is fixed in
kernel source (or, where unavoidable, in build plumbing) — no patching, no
binary editing, no replacing modules. User's words: "no edit ko, solution
should be by modify kernelsource".

This file describes invariants and how to check them, not the current state
of the tree. Anything that changes per commit or per boot (SHAs, commit
counts, image hashes, kernel sublevel) is deliberately not recorded here —
derive it (see "State, not facts" at the end).

## Build

```sh
./build.sh
```

Self-contained: on first run it downloads clang, rustc, AOSP build-tools and
bindgen into `./toolchain/`, then reuses them. Output is
`out/arch/arm64/boot/Image` (a raw arm64 `Image`, not a boot image).

Optional environment knobs: `TOOLCHAIN` (`kernel-llvm` = kernel.org LLVM,
default; `google-clang` = AOSP), `TOOLCHAIN_ROOT`, `OUT_DIR`, `JOBS`,
`LOCALVERSION`, `TZ` (default `Asia/Jakarta`), and the archive URLs.

Notes that are easy to get wrong:

- There is no kleaf/bazel here (`tools/bazel` is absent). The script drives
  plain kbuild; `--config=stamp` and `//common:kernel_aarch64_dist` do not
  apply.
- A host gcc is not required. `cargo` needs some `cc` to build bindgen, and
  the script supplies a wrapper around the downloaded clang when needed.
- Use the AOSP build-tools `pahole`, not the distro one. Distro pahole 1.25
  stops at 4096 per-CPU variables and silently emits an empty `.BTF`.
- bindgen must be >= 0.72.1 with libclang >= 22, or it emits opaque structs
  for forward-declared types and the Rust build fails.
- Keep `CONFIG_DEFAULT_HUNG_TASK_TIMEOUT` out of `gki_defconfig`. It is a
  diagnostic knob only.

## Verify before flashing

All checks are cheap and should be re-run after any rebase or toolchain
change. Exact expected values are the invariants in the next section.

```sh
# 1. struct request must stay 0x120 (288) for the gen5 storage stack
bpftool btf dump file out/vmlinux format raw | grep -m1 "STRUCT 'request'"

# 2. post_alloc_hook() must refcount the page itself. The four callers it
#    was moved to upstream (prep_new_page, split_free_pages, mark_allocated,
#    compaction_alloc) must not do it again; compaction.c must have none.
grep -n 'set_page_refcounted' mm/page_alloc.c mm/compaction.c

# 3. blk_plug_invalidate_ts() must test current->plug before storing
grep -A10 'static __always_inline void blk_plug_invalidate_ts' include/linux/blkdev.h

# 4. every compat symbol must be exported
while read -r s; do case $s in ''|\#*) continue;; esac
	grep -qw "$s" out/System.map || echo "MISSING: $s"
done < gki/aarch64/symbols/android-dlkm-compat

# 5. module-loading relaxations
grep -E '^CONFIG_MODULE_FORCE_LOAD=' out/.config
grep -n 'disagrees about version of symbol' kernel/module/version.c
grep -n 'also exported by the kernel' kernel/module/main.c
# the arm64 GKI target must not enforce the protected list (other targets may)
awk '/name = "kernel_aarch64",/{f=1} f&&/^\)/{exit} f' BUILD.bazel \
	| grep -q '^[[:space:]]*protected_module_names_list' && echo "ENFORCED (bad)" \
	|| echo "not enforced (good)"
# and both bypass strings must be present in the built image
strings out/vmlinux | grep -c '(ignoring)'
```

Byte-level backstop for (2), if the source check looks right but the device
still reports `BUG: Bad page state`:

```sh
llvm-objdump -d --no-show-raw-insn \
  --start-address=0x$(awk '$3=="post_alloc_hook"{print $1; exit}' out/System.map) \
  --stop-address=0x$(printf '%x' $((0x$(awk '$3=="post_alloc_hook"{print $1; exit}' out/System.map) + 0x200))) \
  out/vmlinux | grep -E 'str\s+(xzr|w8),\s+\[x19, #0x(28|34)\]'
# expect page->private = 0 then page->_refcount = 1, in that order
```

## Compat invariants

Each of these was found by a crash. The commit that introduced the fix
carries the full analysis; do not re-derive it, and do not drop a fix
without checking upstream first.

**1. `sizeof(struct request)` must be 0x120.**
Gen5 is `0x120`, mainline 6.12 is `0x118` (it dropped `write_hint`/`ioprio`).
`ufs-mediatek-mod` derives the request from a `scsi_cmnd` using
`blk_mq_rq_from_pdu()` arithmetic hard-coded to `0x120`, so with the smaller
struct it reads `rq->__sector` (== -1) as `rq->bio` and oopses on the first
RPMB read. Fixed with 8 bytes of padding in `include/linux/blk-mq.h` at the
original field position, which also restores every trailing field offset.

**2. `post_alloc_hook()` must set the page refcount itself.**
Mainline moved `set_page_refcounted()` out of `post_alloc_hook()` into its
four callers. `priority_allocate.ko` (Transsion "VIP page pool") kprobes
`post_alloc_hook`, caches the address, and calls it directly to hand out
pooled pages, relying on it to refcount. Under the new contract the page
stayed at `_refcount == 0`, the module forced 1 onto it and queued it on an
active pcp list, and the allocator later popped it negative:
`BUG: Bad page state ... refcount:-1 ... ___rmqueue_pcplist`. This caused
the intermittent hang that survived a factory reset. Fixed in
`mm/page_alloc.c` (plus dropping the redundant caller-side calls in
`mm/compaction.c`).

**3. Vendor modules must be allowed to load.**
Gen5 modules are signed with a different key and their symbol CRCs differ.
Four relaxations, all required: `CONFIG_MODULE_FORCE_LOAD` in
`gki_defconfig`; ignore CRC mismatch in `kernel/module/version.c`; tolerate
a module re-exporting a kernel-owned symbol in `kernel/module/main.c`; drop
`protected_module_names_list` for the arm64 GKI target in `BUILD.bazel`.

**4. Blocktag symbols must resolve.**
`blocktag.ko` and friends need `mtk_btag_*`, `mmc_mtk_biolog_*` and
`set_mmc_perf_mode`. Provided as no-op GPL stubs in
`kernel/mtk_blocktag_stubs.c`, exported via
`gki/aarch64/symbols/android-dlkm-compat`, which also carries
`kernel_read_file_from_path` for older `system_dlkm` modules.

**5. `blk_plug_invalidate_ts()` must be NULL-safe.**
An LTS-only regression (`1fbc2d576370`, upstream `fad156c2af22`) moved the
`PF_BLOCK_TS` invalidation into `finish_task_switch()` and dereferenced
`current->plug` unconditionally. The invariant it assumed is false:
`blk_finish_plug()` nulls the plug but only clears the flag when the
finished plug is the current one. Result, ~13 s into boot with the task
preempted inside `submit_bio()`:

```
Unable to handle kernel NULL pointer dereference at virtual address 0x20
pc : __schedule+0xe9c/0x1dc8
  ldr  x8, [x19, #0xa98]   ; task_struct::plug == NULL
  str  xzr, [x8, #0x20]    ; blk_plug::cur_ktime
```

Fixed in `include/linux/blkdev.h` by testing `current->plug` and still
clearing the flag. AOSP `android16-6.12` never took the offending commit and
keeps a NULL-safe form — a useful reference when a rebase carries the bug
again.

## Moving to a newer LTS / AOSP base

Cherry-pick the local fixes onto the new base rather than merging. A merge
conflicts in Rust binder, f2fs and `abi.stg` (the 2026-09 base has
`transactions_hashed` and `blkzoned` f2fs logic LTS never took; LTS adds a
f2fs "pinned area" feature that base lacks). Cherry-picking avoids all of it
and has applied cleanly across large LTS jumps.

The AOSP base is not a configured remote by default; add it once:

```sh
git remote add aosp https://android.googlesource.com/kernel/common
git fetch aosp android16-6.12-lts
```

Then:

```sh
git log --oneline $(git merge-base HEAD aosp/android16-6.12-lts)..HEAD  # what to carry
git checkout -b tmp aosp/android16-6.12-lts
git cherry-pick -x <commits>
# re-point the branch, rebuild, and re-run "Verify before flashing"
```

`git merge-base` fails if the base has moved away from the local history, so
fetch before relying on it.

After a rebase, re-check each invariant above instead of assuming it still
holds. Two independent things can change: AOSP may have fixed one upstream
(then dropping ours is correct), or a new base may reintroduce one.

Branch names carry no kernel version on purpose. `x6871-6.12-lts` tracks the
rolling `android16-6.12-lts` base, so never bake a sublevel into the name.

## On device

`build.sh` emits a raw arm64 `Image` (`out/arch/arm64/boot/Image`), not a
flashable boot image. To flash it you must wrap it in an Android boot image v4
yourself (lz4 kernel, `ramdisk_size 0`); this tree has no `mkbootimg`, though
the AOSP build-tools do ship `lz4` and `avbtool`.

The resulting image goes to the `boot` partition and carries no ramdisk — the
`vendor_boot` ramdisk supplies the modules.

Map symptoms to invariants — the first three are the historical crashes:

| Symptom | Look at |
|---|---|
| `BUG: Bad page state`, first frame `___rmqueue_pcplist` | invariant 2; confirm `priority_allocate.ko` is loaded |
| NULL deref in `ufs_rpmb_vh_compl_command` | invariant 1 |
| `INFO: task ... blocked for more than N seconds` | invariant 2; capture the blocked task's stack, compare with `___rmqueue_pcplist` / `page_cache_ra_unbounded` |
| a module fails to load | vermagic, CRC warnings, invariant 3, symbol present in `android-dlkm-compat` |

A good run: boots to the home screen, `/data` encryption works, no bad-page
report, and no blocked-task warning under swap/readahead pressure. The
original repro needed a few minutes of ordinary app use, not a special
trigger, so a short boot is not proof.

Capture logs from `/sys/fs/pstore/console-ramoops*` or the MTK AEE dump.

## Expected log noise

- `disagrees about version of symbol ... (ignoring)` — CRC bypass working.
- `exports symbol ... also exported by the kernel (ignoring)` — blocktag dupes.
- `proc_register` WARN from `tran_usb_extcon_probe` — duplicate proc entry.
- `rust_binder.ko` fails to load — old module, not needed.

## Conventions

- Git identity: `Rama Bondan Prakoso <ramanarubp@gmail.com>`.
- Kernel-style commit messages, `Signed-off-by:` trailer, one logical change
  per commit. Reference the observed crash, not just the fix.
- `--force-with-lease` is fine on `x6871-*` branches; they are rewritten when
  a patch is dropped or the base moves.
- Keep `build.sh` and the toolchain cache out of the repo history unless
  deliberately adding the script.

## State, not facts

Never record these in this file; derive them:

| Want | Command |
|---|---|
| current base and sublevel | `git log --oneline -1` |
| local fixes vs base | `git log --oneline $(git merge-base HEAD aosp/android16-6.12-lts)..HEAD` |
| branches and their state | `git branch -vv` |
| a built image's identity | `sha256sum out/arch/arm64/boot/Image`, or the `build.sh` summary |
| the exact version string | `cat out/include/generated/utsrelease.h` (needs a build) |
| what happened in a boot | `dmesg` / pstore, not this file |
