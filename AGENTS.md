# AGENTS.md — Infinix X6871 (MT6895) gen6 GKI bring-up

Running an **Android 16 / GKI 6.12** kernel on an Infinix X6871 (MT6895)
with the **stock gen5 (6.12.38) vendor modules**, unmodified. Treat those
modules as opaque prebuilts that must load as-is; related vendor source
trees are references, not substitutes for the exact device binaries.

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
- Keep `CONFIG_BLK_DEV_THROTTLING` off (invariant 8); the prebuilt UFS
  module wakes `request_queue::mq_freeze_wq` directly.
- For targeted `make O=out ...` checks, mirror `build.sh`'s toolchain
  `PATH` and `LIBCLANG_PATH`, including its build-tools and bindgen.
  Missing pahole can silently disable `CONFIG_DEBUG_INFO_BTF` and then
  `CONFIG_SCHED_CLASS_EXT`, changing scheduler layouts. Check both config
  values before trusting any offsets from a targeted build.

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
while read -r s; do
	case $s in ''|\#*|\[*) continue;; esac
	grep -qw "$s" out/System.map || echo "MISSING: $s"
	grep -qw "__ksymtab_$s" out/System.map || echo "NOT EXPORTED: $s"
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

# 6. usb4_link must sit in the KABI tail, never between tunnel_mode and
#    slot_id. The gen5 xhci-mtk module writes slot_id using the old offsets.
toolchain/build-tools/bin/pahole -C usb_device out/vmlinux |
	grep -E 'tunnel_mode|usb4_link|slot_id'

# 7. task_struct / sched_entity must keep the android16-5 offsets the
#    prebuilt scheduler and cpufreq_sugov_ext modules dereference.
toolchain/build-tools/bin/pahole -C task_struct out/vmlinux |
	grep -E '\b(prio|cpus_ptr|cgroups|android_vendor_data1|thread)\b'
# expect prio 148, cpus_ptr 1376, cgroups 2872, android_vendor_data1 3320,
# thread 3488 (0xda0)
toolchain/build-tools/bin/pahole -C task_struct out/vmlinux | grep 'size:'
# expect 5184
toolchain/build-tools/bin/pahole -C sched_entity out/vmlinux |
	grep -E '\b(cfs_rq|vprot)\b'
# expect cfs_rq 160, vprot 184
# kernel/fork.c asserts these at build time

# 8. request_queue::mq_freeze_wq must stay at 0x3b8 for the UFS timer.
toolchain/build-tools/bin/pahole -C request_queue out/vmlinux |
	grep mq_freeze_wq
# expect 952; block/blk-core.c asserts it at build time and
# CONFIG_BLK_DEV_THROTTLING must stay off

# 9. blocktag must load but never run its init
strings out/vmlinux | grep -c 'incompatible vendor module; init/exit suppressed'
# expect 1

# 10. vendor-visible structs carried across LTS rebases
P=toolchain/build-tools/bin/pahole
$P -C gpio_chip out/vmlinux |
	grep -E 'set_config|set_rv|size:'
# expect set_config 112, set_rv 616, size 632
$P -C net_device out/vmlinux |
	grep -E 'proto_down_reason|priv\[\]|size:'
# expect proto_down_reason 1300, priv 2688, size 2688
$P -C ethtool_ops out/vmlinux |
	grep -E 'create_rxfh_context|get_rxfh_fields|size:'
# expect create_rxfh_context 328, get_rxfh_fields 624, size 656
$P -C mbox_chan out/vmlinux |
	grep -E 'tx_status|active_req|size:'
# expect tx_status 12, active_req 56, size 248
$P -C Scsi_Host out/vmlinux | grep hostdata
# expect 2472 (0x9a8)
$P -C pernet_operations out/vmlinux |
	grep -E '\bid;|size;|size:'
# expect id 56, size 64, struct size 72
$P -C tty_struct out/vmlinux |
	grep -E 'disc_data|driver_data|size:'
# expect disc_data 664, driver_data 672, size 760
$P -C rq out/vmlinux |
	grep -E '\b(curr|idle|stop)\b'
# expect curr 3344 (0xd10), idle 3360 (0xd20), stop 3368 (0xd28)
$P -C nf_hook_ops out/vmlinux |
	grep -E '\b(hook|dev|priv|pf|hooknum|priority)\b|size:'
# expect hook 0, dev 8, priv 16, pf 24, hooknum 28, priority 32, size 40
$P -C signal_struct out/vmlinux |
	grep -E 'oom_score_adj|oom_mm|cred_guard_mutex|exec_update_lock|cgroup_threadgroup_rwsem|size:'
# expect oom_score_adj 954, oom_score_adj_min 956, oom_mm 960,
# cred_guard_mutex 968, exec_update_lock 1016, cgroup_threadgroup_rwsem 1120,
# size 1184 (the 1120-byte vendor prefix plus the kernel-private semaphore)
$P -C usb_dynids out/vmlinux | grep -E '\blist;|size:'
# expect list 8, size 24
$P -C usb_driver out/vmlinux |
	grep -E '\b(dynids|driver|supports_autosuspend)\b|size:'
# expect dynids 96, driver 120, supports_autosuspend at byte 296 bit 1,
# size 336; the unused per-driver lock slot is NOT used for synchronization

# 11. page-table fixmap slots must stay inside the range registered by mkp.ko
grep '^#define NR_FIX_BTMAPS' arch/arm64/include/asm/fixmap.h
# expect (SZ_256K / PAGE_SIZE), without +1
toolchain/llvm-23.1.4-x86_64/bin/llvm-objdump -d --no-show-raw-insn \
	--disassemble-symbols=set_swapper_pgd out/vmlinux
# for the 4K GKI configuration, expect FIX_PGD index 0x3cd (973)
# and the store address base 0xffffffffff433000, not 0xffffffffff42c000

# 12. keep the layout-sensitive features and CFI enabled
grep -E '^CONFIG_(DEBUG_INFO_BTF|SCHED_CLASS_EXT|CFI_CLANG)=' out/.config
# expect all three =y; CONFIG_CFI_PERMISSIVE must not be enabled
if grep -q '^CONFIG_CFI_PERMISSIVE=y' out/.config; then
	echo "UNSAFE: permissive CFI"
fi
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

Each of these is backed by a crash or a confirmed module-access mismatch.
The commit that introduced the fix carries the analysis; do not drop a fix
without checking upstream and the device module's contract first. A boot
without a panic does not prove that a vendor field is read correctly.

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

**4. Blocktag symbols must resolve, and `blocktag.ko` must never run.**
`blocktag.ko` and friends need `mtk_btag_*`, `mmc_mtk_biolog_*` and
`set_mmc_perf_mode`. Provided as no-op GPL stubs in
`kernel/mtk_blocktag_stubs.c`, exported via
`gki/aarch64/symbols/android-dlkm-compat`, which also carries
`kernel_read_file_from_path` for older `system_dlkm` modules. The stubs
only cover calls through its exported hooks; the module's own
`block_bio_queue` tracepoint probe dereferences the vendor 6.12.38
`struct request` and oopses in `btag_trace_block_bio_queue+0x90` during
the first UFS scan. Rejecting the module is not an option: every client
lists `blocktag` in `depends=`, so modprobe would skip the storage stack.
It is on the built-in inert list in `kernel/module/main.c` instead: the
ELF loads and its exports stay registered (shadowed by the stubs), but
`init_module()`/`cleanup_module()` are never called and no tracepoints or
procfs entries are created.

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

**6. `usb4_link` must live in the KABI reserve.**
The LTS merge carries `7d24fae116cf` ("UPSTREAM: usb: acpi: fix device link
removal"), which stores the USB4 host-interface device link in
`struct usb_device` between `tunnel_mode` and `slot_id`. That shifts
`slot_id`, the LPM parameter blocks and every later field by eight bytes.
The gen5 modules are built against the pre-fix layout: `xhci-mtk-hcd-v2.ko`
assigns `udev->slot_id` (1 for the first device), which lands on
`usb4_link`, and `usb_disconnect()` then feeds that integer to
`device_link_del()`:

```
Unable to handle kernel NULL pointer dereference at 0x3c5
pc : device_link_del+0x20/0xcc
  usb_disconnect+0x1dc
  hub_event
```

Seen on unplugging a USB-OTG drive. Google originally reverted the upstream
commit for exactly this ABI break; the merge re-introduced it. Keep the
pointer in `ANDROID_KABI_USE(3, ...)` at the end of the struct. Never insert
it before `slot_id`.

**7. `struct task_struct` must keep the android16-5 vendor layout.**
`blocked_on_l` (`bbb8ae42fd12`) and `is_blocked` (`5265740a12e3`) were
added after the vendor modules were built and sat before `prio`/`cgroups`:

- `is_blocked` at 0x94 made `cpufreq_sugov_ext` read a normal task as RT
  (the `p->prio > 99` check) and dereference NULL `min`/`max`:

```
pc : mtk_effective_cpu_util_total+0x170/0x300 [cpufreq_sugov_ext]
```

- `blocked_on_l` shifted `cgroups` to 0xb48, so `scheduler.ko` read
  `mems_allowed` (value 1) as `p->cgroups` during `sched_pause_init()`:

```
pc : mtk_find_energy_efficient_cpu+0x17c/0x29c4 [scheduler]
```

Both fields are kernel-internal, so they live below `thread` in
`include/linux/sched.h`; `__abi_compat_task_pad[0x1fc]` keeps
`sizeof(struct task_struct) == 0x1440`, so `android_task_vendor_data()`
(p + 1) matches the offset the vendor build uses for its dynamic vendor
area. Keeping them below `thread` also preserves the vendor `thread`
offset: the vendor kernel's `cpu_switch_to` loads `thread.cpu_context`
with `mov x10, #0xda0`, so `offsetof(struct task_struct, thread)` must
stay `0xda0`. `kernel/fork.c` asserts the size, `thread` and `prio`
(0x94), `max_allowed_capacity` (0x550), `nr_cpus_allowed` (0x558),
`cpus_ptr` (0x560), `cgroups` (0xb38) and `android_vendor_data1`
(0xcf8) — keep them passing when the base moves.

The same applies to `struct sched_entity`: `vprot` (`aedb824ee77d`)
must sit after `runnable_weight`, not after `vlag`. With the newer order
`se.cfs_rq` moved to 0xa8 (`p+0x168`), while the vendor `task_h_load()`
reads 0xa0 (`p+0x160`) — which is `se.parent`, NULL for a top-level
task:

```
Unable to handle kernel NULL pointer dereference at virtual address 0x138
pc : task_h_load+0x8/0xac [scheduler]
```

`kernel/fork.c` asserts `se`, `se.cfs_rq` and `sizeof(struct sched_entity)`
too.

**8. Keep `CONFIG_BLK_DEV_THROTTLING` off.**
The vendor build disables it. Enabling it inserts `struct throtl_data *td`
into `struct request_queue` and moves `mq_freeze_wq` from `0x3b8` to
`0x3c0`. `ufs_mediatek_mod`'s `rq_timer_fn` wakes that waitqueue directly:

```
add x0, x8, #0x3b8
bl  __wake_up
```

With the shifted field, `x0` points into `request_queue::callback_head`
and the wakeup path oopses (seen after screen-off/resume):

```
Unable to handle kernel paging request at virtual address fffffffffffffff8
pc : __wake_up+0x60/0xd8
rq_timer_fn+0x40/0x68 [ufs_mediatek_mod]
```

`gki_defconfig` keeps it off and `block/blk-core.c` asserts the offset.

**9. Vendor-visible structs must keep the android16-5 layout.**
LTS/AOSP changes can move fields in structs that prebuilt modules embed,
allocate, or access through inline helpers. Prefer preserving the new
behavior while using an existing KABI reserve/hole or keeping new internal
state after the vendor prefix. Where that is not practical, a GKI-side
revert is allowed. The modules themselves are never touched.

- `struct gpio_chip`: `set_rv`/`set_multiple_rv` (`7efab0c75e17`) moved
  `set_config` and everything after by 16 bytes and grew the struct from 632
  to 648.  `struct mtk_pinctrl` embeds the chip at `+0x20` with `soc`/`eint`
  right after it, so `gpiochip_add_data()` overwrote the module's `eint`:

```
pc : mtk_eint_irq_request_resources+0x44/0x124 [pinctrl_mtk_v2]
  ldr x8, [x20, #0x48]  ; corrupted gpio_xlate
  ldr x8, [x8]
  ldur w16, [x8, #-4]   ; KCFI hash read from a bogus address
```

  The callbacks live in reserve slots 1/2 (offsets 0x268/0x270); size is
  0x278.  `drivers/gpio/gpiolib.c` asserts it.

- `struct net_device`: `watchdog_lock`/`watchdog_ref_held`
  (`6b2330facc90`) moved `proto_down_reason` and every later field by eight
  bytes.  They live in reserve slots 1/2; `proto_down_reason` is 1300,
  `reg_state` 1344, `priv[]` 2688 and the size 2688.  `net/core/dev.c`
  asserts it.

- `struct ethtool_ops`: `get_rxfh_fields`/`set_rxfh_fields` moved
  `create_rxfh_context` and every later callback by 16 bytes, so a prebuilt
  driver's ops table would be interpreted with the wrong slots.  They live
  in reserve slots 1/2; `create_rxfh_context` is 328 and the size 656.
  `net/ethtool/ioctl.c` asserts it.

- `struct mbox_chan`: `tx_status` (`85a7ead4eec9`) moved `active_req` and
  later fields by eight bytes; controller modules allocate their own channel
  arrays.  `tx_status` sits in the four-byte hole after `txdone_method`
  (offset 12), so `active_req` stays at 56 and the size at 248.
  `drivers/mailbox/mailbox.c` asserts it.

- `struct Scsi_Host`: the `eh_work` backport (`866efe8ae8b8`) moved
  `hostdata[]` from `0x9a8` to `0x9c8`.  `ufs-mediatek-mod` gets its
  `ufs_hba` from `shost_priv()`, so the commit was reverted on this branch;
  `drivers/scsi/hosts.c` asserts `hostdata` at `0x9a8`.

- `struct mm_struct`: `futex_unique_id` moved `hiwater_rss` and later fields
  by eight bytes.  The field now occupies KABI reserve slot 2 (after
  `dmabuf_info`), restoring the vendor offsets.

- `struct pernet_operations`: the `exit_rtnl` hook (`424ca826f00a`, only used
  by WireGuard `707607db2193`) moved `id` from 56 to 64 and `size` from 64
  to 72.  The prebuilt GKI `ppp_generic.ko` in `system_dlkm` passes its own
  ops table, so `register_pernet_operations()` saw a bogus `id` and oopsed:

```
WARNING: at net/core/net_namespace.c:1341 register_pernet_operations+0x28c
Unable to handle kernel NULL pointer dereference at 0x18
pc : register_pernet_operations+0x70/0x2cc
init_module+0x28/0xf84 [ppp_generic]
```

  Both commits were reverted; `id` is 56, `size` 64, sizeof 72, asserted in
  `net/core/net_namespace.c`.

- `struct tty_struct`: `break_wait` (`3bc82999fb85`) was inserted before
  `hangup_work`, moving `disc_data` and `driver_data` by 24 bytes.  Prebuilt
  line disciplines (`ppp_generic`, `slcan`) keep their state in those
  fields.  The commit was reverted; `hangup_work` is 632, `disc_data` 664,
  `driver_data` 672 and sizeof 760, asserted in `drivers/tty/tty_io.c`.

- `struct rq`: `ffe63be29f0d` moved `ops_qseq` from `struct scx_rq` to
  `struct sched_ext_entity`, shrinking `scx_rq` from 264 to 256 bytes and
  moving `rq->curr` from `0xd10` to `0xd08`.  The prebuilt android16-5
  `scheduler.ko` reads `cpu_rq(cpu)->curr` at `0xd10`; it landed on the
  padding before `rq->idle`, passed NULL to `is_task_latency_sensitive()`
  and faulted:

```
Unable to handle kernel paging request at 0x1440
pc : is_task_latency_sensitive+0x38/0x9c [scheduler]
  mtk_rt_energy_aware_wake_cpu+0x400/0xacc
  mtk_select_task_rq_rt+0x238/0x484 [scheduler]
```

  An eight-byte `__abi_pad` at the old `ops_qseq` position in `struct
  scx_rq` restores `curr` 0xd10, `idle` 0xd20 and `stop` 0xd28; they are
  asserted in `kernel/sched/core.c`.

- `struct nf_hook_ops`: `050f98e56782` ("netfilter: nat: use kfree_rcu to
  release ops", upstream `6eda0d771f94`) prepended a 16-byte `rcu_head`,
  shifting every field and growing the array stride from 40 to 56.
  `hiber.ko` passes two old-layout hooks: IPv4/IPv6, hook 1, priority 226.
  The kernel reads an invalid protocol family and rejects registration.
  The module has already registered signal/binder tracepoint callbacks;
  its init error path returns failure without removing them. After the
  freed text is reused by another module, SIGKILL hits a stale callback:

```
hiber_netfilter_init: register netfilter hooks failed!
HiberK hiber_core_init: netfilter init failed!
CFI failure at __traceiter_android_vh_do_send_sig_info
  (target: init_module+0x4b8 [tran_perf_util]; expected type: 0x8574e6d7)
```

  `tran_perf_util` is the new occupant of the freed allocation, not the
  callback owner. The real `hiber::send_signal_handler` has the correct
  CFI type `0x8574e6d7`; do not disable CFI or change the signal hook.
  Restore the 40-byte ops layout and retain the upstream RCU lifetime fix
  using `kfree_rcu_mightsleep()` at all six NAT ops-array release sites.
  These paths already require sleepable context (mutexes/GFP_KERNEL).
  `net/netfilter/core.c` asserts the size and field offsets.

- `struct signal_struct`: `a0c8dd4495d4` inserted a 64-byte
  `cgroup_threadgroup_rwsem` before the OOM fields. The prebuilt
  `tran_perf_util.ko` and `tran_mm_collect.ko` read `oom_score_adj` at
  `0x3ba` (954); they instead read semaphore bytes while the real field
  was at 1018. This can silently corrupt process classification and
  diagnostics without causing a panic.

  Keep the per-thread-group locking, but move the semaphore after
  `ANDROID_OEM_DATA(1)`. The entire original 1120-byte signal prefix is
  preserved; the semaphore starts at 1120 and the kernel allocation
  remains 1184 bytes. Init/fork and cgroup locking still use the same
  named field. `kernel/fork.c` asserts the OOM and lock offsets.

- `struct usb_dynids` / `struct usb_driver`: `c0d083b7b496` removed the
  per-driver dynamic-ID lock when switching to `usb_dynids_lock`. That
  shrank the nested type from 24 to 16 bytes and moved `usb_driver::driver`
  from 120 to 112 and the flags from byte 296 to 288. The prebuilt
  `r8152.ko` has a 336-byte driver object with flags `0x06` at 296; reading
  at 288 sees zero and loses its autosuspend/LPM settings.

  Keep an unused `spinlock_t __abi_compat_lock` before the list to restore
  the old layout and 336-byte driver size. All kernel dynamic-ID operations
  must continue using the new global mutex, including USB serial: never
  switch back to the compatibility slot for locking. This preserves the
  newer locking/UAF fixes. `drivers/usb/core/driver.c` asserts the layout.

These are kernel/GKI-side changes. Prebuilt GKI modules from `system_dlkm`
follow the same rule as vendor modules: never patch the `.ko`; revert or
rework the kernel side.

`struct page` (flags 0, `_refcount` 52, `private` 40) did not change; the
folio overlay changes are above the page header used by
`priority_allocate.ko`.

**10. Keep the android16-5 page-table fixmap slot addresses.**
`15875de7f53a` ("arm64: fixmap: Allow 256K early_ioremap() at any offset",
upstream `21fc7ec93f8b`) adds one page to each of seven boot-time mapping
slots. On the 4K GKI configuration this moves `FIX_PGD` from index 973
(`0xffffffffff433000`) to 980 (`0xffffffffff42c000`).

This is also a vendor ABI: the stock `mkp.ko` calls
`mkp_setup_essential_hvc_call(PHYS_OFFSET, FIXADDR_TOP,
__fix_to_virt(__end_of_fixed_addresses - 1))`. Its compiled-in lower bound
is `0xffffffffff433000`, passed to the protection hypervisor through HVC
`0x82000060`. MKP protects kernel rodata, including `swapper_pg_dir`, and
the moved `FIX_PGD` alias is outside the registered fixmap range.

The failure signature is a new top-level module mapping remaining zero
after `__pmd_alloc()` returns success, followed by a fault in
`__vmap_pages_range_noflush` at `__va(0)`. Crossing a 1GB boundary in the
module window triggers a fresh PGD population; different KASLR layouts can
hide the problem by reusing an already-populated entry.

Restore the old `NR_FIX_BTMAPS`, not the module or hypervisor policy.
This deliberately retains the old early-ioremap limit: an unaligned full
256K request needs 65 pages and is rejected by its existing size check.
Do not add the extra pages back without preserving the runtime page-table
slot addresses. Device testing is still required after such a change;
matching disassembly verifies the layout, not the hypervisor's behavior.

## Auditing another suspected ABI mismatch

1. Identify the exact module and kernel build from the log. Use its ELF
   symbols, relocations, section sizes and disassembly to establish the
   offsets, array stride, function prototype or constant it actually uses.
   Read-only analysis of a `.ko` is allowed; altering it is not.
2. Compare with the linked `out/vmlinux`, using BTF or DWARF rather than
   only the current headers. Extracting stock BTF from a decompressed
   firmware Image is useful, but stock Image and module build revisions
   can differ. A stock-BTF difference alone does not prove a module bug.
3. Follow nested structures, bitfields, flexible arrays and embedded
   objects. Matching a total size or an outer field's offset is not enough.
   Function-prototype equality also does not establish layout or semantics.
4. If CFI reports a target in an unexpected module, trace earlier init
   failures and callback registration/cleanup. Freed module text can be
   reused by an innocent module, as in the `hiber` failure above. Keep CFI
   enabled; do not bypass it to conceal a dangling callback.
5. Fix only an established kernel-side mismatch. Preserve synchronization
   and lifetime fixes where possible. Add layout assertions and a check
   that fails against the pre-fix headers, then verify the linked result.

Temporary audit scripts and downloaded binaries are not repo prerequisites.
Preserve the reasoning, expected offsets and reproducible checks in commits
or this file; do not rely on a session's `/tmp/opencode` files surviving.

### Remaining audit watchpoints, not proven runtime failures

These do not justify speculative reverts or vendor-module changes:

- `thread_struct`: `90521e7d69df` replaces inline kernel FPSIMD state with
  a pointer. In the compared gen5/current layouts, size is 1696 versus
  1168 and later PAC/MTE/vendor fields move by 528 bytes. Matching the
  task's `thread` offset and total task size does not restore this tail.
  Establish a real module access before changing it.
- TCP/INET: `72b5ce3396d8` relocates `ipv6_fl_list` into `inet_sock`;
  `e4141ea18a73` adds the connection keepalive timer. Check consumers of
  the moved fields, not just struct sizes. In-tree TCP algorithms compile
  against the same headers as the kernel.
- Thermal: `CONFIG_THERMAL_DEBUGFS` adds a pointer before
  `thermal_zone_device::trips[]`; the compared offsets were 1440 without
  it and 1448 with it. Verify a prebuilt driver's direct trip-array access
  before altering the configuration or layout.
- Runtime PM: `dev_pm_info::smart_suspend` shifts the `must_resume` and
  `may_skip_resume` bits without changing the structure size. Byte-level
  offset checks alone miss this.
- Wireless: when using stock cfg80211/mac80211 modules with
  `CONFIG_CFG80211` disabled in the Image, unused newer header types do
  not by themselves prove a mismatch. Do not silently mix rebuilt wireless
  modules with the stock stack.

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

Then select and verify the intended base. A supplied commit need not equal
the advertised branch tip, and a successful fetch is not proof otherwise:

```sh
git show --no-patch --oneline <base>
git log --oneline <old-base>..HEAD  # review the local patches to carry
git checkout -b <new-branch> <base>
git cherry-pick -x <commits>
git merge-base --is-ancestor <base> HEAD
# rebuild and re-run "Verify before flashing"
```

If the old base is unknown, `git merge-base` with a fetched AOSP ref can
help identify it, but inspect the result instead of assuming it represents
the requested new base.

After a rebase, re-check each invariant above instead of assuming it still
holds. Two independent things can change: AOSP may have fixed one upstream
(then dropping ours is correct), or a new base may reintroduce one.

Avoid baking a changing kernel sublevel into a branch name. Preserve a
user-requested branch name; a base-SHA suffix can distinguish rebase/test
branches without claiming they track the current AOSP tip.

## On device

`build.sh` emits a raw arm64 `Image` (`out/arch/arm64/boot/Image`), not a
flashable boot image. To flash it you must wrap it in an Android boot image v4
yourself (lz4 kernel, `ramdisk_size 0`); this tree has no `mkbootimg`, though
the AOSP build-tools do ship `lz4` and `avbtool`.

The resulting image goes to the `boot` partition and carries no ramdisk — the
`vendor_boot` ramdisk supplies the modules.

Map symptoms to invariants:

| Symptom | Look at |
|---|---|
| `BUG: Bad page state`, first frame `___rmqueue_pcplist` | invariant 2; confirm `priority_allocate.ko` is loaded |
| NULL deref in `ufs_rpmb_vh_compl_command` | invariant 1 |
| `INFO: task ... blocked for more than N seconds` | invariant 2; capture the blocked task's stack, compare with `___rmqueue_pcplist` / `page_cache_ra_unbounded` |
| a module fails to load | vermagic, CRC warnings, invariant 3, symbol present in `android-dlkm-compat` |
| NULL deref in `device_link_del` from `usb_disconnect` on USB unplug | invariant 6; confirm `xhci-mtk-hcd-v2.ko` is loaded |
| oops at address `0x9` in `mtk_find_energy_efficient_cpu+0x17c` during module load | invariant 7; `cgroups` moved off 0xb38 |
| `task_h_load+0x8` or `mtk_effective_cpu_util_total+0x170` oops | invariant 7; `sched_entity`/`prio` offsets |
| `__wake_up+0x60` from `rq_timer_fn` after screen-off/resume | invariant 8; `mq_freeze_wq` moved off 0x3b8 |
| `mtk_eint_irq_request_resources+0x44` from `__setup_irq` | invariant 9; `gpio_chip` grew past the module's embedded chip |
| UFS probe failure or garbage `ufs_hba`, wrong `shost_priv()` | invariant 9; `Scsi_Host::hostdata` moved |
| prebuilt Wi-Fi driver corrupts fields or ethtool calls the wrong op | invariant 9; `net_device` / `ethtool_ops` layout |
| mailbox controller probe crash or channel corruption | invariant 9; `mbox_chan::active_req` moved |
| WARN at `register_pernet_operations` then oops at `0x18` from `ppp_generic` | invariant 9; `pernet_operations::id`/`size` moved |
| line-discipline corruption or crash when a TTY is attached | invariant 9; `tty_struct::disc_data` moved |
| oops at `0x1440` in `is_task_latency_sensitive` from `mtk_rt_energy_aware_wake_cpu` | invariant 9; `rq->curr` moved off 0xd10 |
| `hiber` netfilter init failure followed by signal-hook CFI failure into another module | invariant 9; `nf_hook_ops` layout/array stride changed, leaving hiber's callbacks dangling |
| vendor performance/memory reports classify OOM scores incorrectly without a panic | invariant 9; `signal_struct::oom_score_adj` must stay at 954 |
| prebuilt USB driver's autosuspend/LPM flags appear unset | invariant 9; `usb_dynids` size and `usb_driver` flags at byte 296 |
| `__vmap_pages_range_noflush` faults at `__va(0)` while allocating module memory | invariant 10; `FIX_PGD` moved outside the stock MKP fixmap range |

A good run: boots to the home screen, `/data` encryption works, no bad-page
report, no blocked-task warning under swap/readahead pressure, and a
screen-off/idle period resumes without a wakeup oops. The original repro
needed a few minutes of ordinary app use, not a special trigger, so a short
boot is not proof.

Capture logs from `/sys/fs/pstore/console-ramoops*` or the MTK AEE dump.

## Delivery and publishing

- Build with `./build.sh` and upload the raw `Image` to gofile. Label it
  clearly as a raw kernel, not a flashable boot image; provide its SHA256
  and identify the corresponding commit/build.
- A successful build or offline ABI check is not device confirmation.
  Test boot and normal use, screen-off/resume, Wi-Fi/network traffic and
  USB-OTG attach/detach; investigate any new pstore rather than assuming
  it is the previous crash.
- Push a kernel branch only after the user confirms device testing is
  good. Verify that the remote branch resolves to the intended commit.
  Do not stage toolchains, downloaded modules or generated images.

## Expected log noise

- `disagrees about version of symbol ... (ignoring)` — CRC bypass working.
- `exports symbol ... also exported by the kernel (ignoring)` — blocktag dupes.
- `blocktag: incompatible vendor module; init/exit suppressed` — inert load working.
- `btag host init failed, 0` — blocktag stub returned NULL; UFS skips its hooks.
- `[Size check ok] boot_arg=512, mtk_task=488` / `no share_buck err_code=-22` —
  vendor scheduler init.
- `proc_register` WARN from `tran_usb_extcon_probe` — duplicate proc entry.
- `rust_binder.ko` fails to load — old module, not needed.

## Conventions

- Git identity: `Rama Bondan Prakoso <ramanarubp@gmail.com>`.
- Kernel-style commit messages, `Signed-off-by:` trailer, one logical change
  per commit. Reference the observed crash, not just the fix.
- `--force-with-lease` is fine on `x6871-*` branches; they are rewritten when
  a patch is dropped or the base moves.
- A GKI-side commit that breaks the vendor ABI may be reverted (the
  `Scsi_Host` `eh_work` revert is the example).  Vendor modules are never
  modified or replaced.
- Keep `build.sh` and the toolchain cache out of the repo history unless
  deliberately adding the script.

## State, not facts

Never record these in this file; derive them:

| Want | Command |
|---|---|
| branch tip | `git log --oneline -1` |
| selected base and its sublevel | inspect the chosen base commit and `git show <base>:Makefile` |
| local fixes vs selected base | `git log --oneline <base>..HEAD` |
| branches and their state | `git branch -vv` |
| a built image's identity | `sha256sum out/arch/arm64/boot/Image`, or the `build.sh` summary |
| the exact version string | `cat out/include/generated/utsrelease.h` (needs a build) |
| what happened in a boot | `dmesg` / pstore, not this file |
