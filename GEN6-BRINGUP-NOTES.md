# Infinix X6871 (MT6895) — gen6 GKI bring-up with gen5 vendor modules

Device: Infinix X6871, MediaTek MT6895
Kernel tree: `android16-6.12-gen6` (android16-6 branch, 6.12.92)
Vendor modules: stock `6.12.38-android16-5` (gen5) binaries from the firmware
`vendor_boot` ramdisk — no sources, loaded as-is.

Result: boots to Android home screen without panic.

## Kernel-source changes (all uncommitted, patch: `gen6-kernel-changes.patch`)

### 1. Loader relaxations (allow old, unsigned/CRC-mismatched modules)
- `arch/arm64/configs/gki_defconfig`: `CONFIG_MODULE_FORCE_LOAD=y`
- `kernel/module/version.c`: `check_version()` ignores symbol CRC mismatch
  (`bad_version` → return 1, log "(ignoring)")
- `BUILD.bazel`:
  - `protected_module_names_list` disabled (MODULE_SIG_PROTECT off)
  - `gki/aarch64/symbols/android-dlkm-compat` added to the compat symbol lists
- `kernel/module/main.c`: `verify_exported_symbols()` tolerates a vendor module
  re-exporting a symbol the kernel already owns (kernel copy shadows it)

### 2. Blocktag / MMC biology compat stubs
- `kernel/mtk_blocktag_stubs.c` (new): 13 no-op GPL exports used by
  blocktag.ko's clients (`mtk_btag_ufs_*`, `mtk_btag_mictx_*`,
  `mmc_mtk_biolog_*`, `set_mmc_perf_mode`). Kernel ksymtabs are searched
  before module ksymtabs, so importers bind to the stubs.
- `kernel/Makefile`: build the stub object.
- `gki/aarch64/symbols/android-dlkm-compat` (new): stub names +
  `kernel_read_file_from_path` compat.

`mtk_btag_ufs_init()` returns NULL, which makes ufs-mediatek-mod's wrappers
short-circuit all further blocktag calls on their own.

### 3. `struct request` ABI padding (the RPMB crash fix)
- `include/linux/blk-mq.h`: restore the 8 bytes that upstream removed with
  `write_hint`/`ioprio`, keeping `sizeof(struct request) == 0x120` and all
  trailing field offsets identical to the android16-5 (6.12.38) layout.
- Reason: prebuilt MTK modules derive the request from a `scsi_cmnd` with
  `blk_mq_rq_from_pdu()` math using the 0x120 constant. With the 0x118
  layout, `ufs_rpmb_vh_compl_command()` read `rq->__sector` (= -1) instead of
  `rq->bio` and oopsed on the first RPMB read.

Note: `struct bio`, `scsi_cmnd`, `ufs_hba`, `request_queue` are byte-identical
between the two branches; `struct request` was the only mismatch.

### 4. `post_alloc_hook()` refcount contract (the bad-page fix)

Symptom: after the RPMB fix the device reached the home screen, but under
swap/readahead pressure it died with

```
BUG: Bad page state in process arch_disk_io_2  pfn:15e208
page: refcount:-1 mapcount:0 mapping:0000000000000000 index:0x22d2
 ___rmqueue_pcplist
 get_page_from_freelist
 __folio_alloc_noprof
 page_cache_ra_unbounded
 page_cache_ra_order
 page_cache_sync_ra
 filemap_get_pages
 filemap_read
 f2fs_file_read_iter
```

A factory reset did not help — it is an allocator contract mismatch, not data
corruption.

Cause: `priority_allocate.ko` (Transsion "VIP page pool") registers a kprobe on
`post_alloc_hook` and caches the resolved address in `post_alloc_hook_dup`. It
calls it directly to hand pages out of its private pool:

```
get_page_from_vip_pages_pool():
        page = vip_page_pool_alloc_pages(...);   /* _refcount == 0 */
        if (page_ref_count(page) == 0) {
                /* relies on post_alloc_hook() to refcount the page */
                post_alloc_hook_dup(page, order, gfp);
                ...
        }
```

Upstream 6.12 removed `set_page_refcounted()` from `post_alloc_hook()` and moved
it into each caller (`prep_new_page()`, `mark_allocated()`,
`compaction_alloc()`, `split_free_pages()`). The android16-5 (6.12.38) binaries
were built against the old contract, so the call now returns a page with
`_refcount == 0`. The pool then forces 1 onto it (`str w8, [x21, #52]`) and
pushes it onto an active pcp list while the page is already reachable, so the
page-cache allocator later pops it with a negative refcount.

Fix: put `set_page_refcounted()` back inside `post_alloc_hook()` and drop the
now-redundant caller-side calls. Net in-tree behaviour is unchanged; the
exported function again satisfies the 6.12.38 contract.

`priority_allocate.ko` is the only module in the firmware that references
`post_alloc_hook`.

The fix was validated on device: booted, reachable home screen, no hang and
no `BUG: Bad page state` through normal use and swap pressure. A temporary
`put_page`-on-zero-refcount detector (plus a 30 s hung-task timeout) used
during diagnosis was dropped once the fix was confirmed; it is not part of
this branch.

## Audited and found identical gen5 vs gen6

- All 690 gen5 / 870 gen6 vendor hooks: only one signature changed
  (`android_vh_f2fs_printk`, unused by the loaded modules); hooks only-gen6
  cannot be registered by gen5 binaries.
- Imported symbol prototypes for the Transsion memory modules: no changes.
- `struct page`, `zone`, `per_cpu_pages`, `pglist_data`, `free_area`,
  `page`, `folio`, `address_space`, `inode`, `file`, `mm_struct`,
  `vm_area_struct`: byte-identical.
- `split_page()`, `__free_pages()`, the pcp free path and the pcp alloc path:
  identical.

## Build

```sh
cd android16-6.12-gen6
tools/bazel run --config=stamp //common:kernel_aarch64_dist -- --destdir=$PWD/out/dist
```

Outputs in `out/dist/`:
- `boot-lz4.img` — Android boot image header v4, lz4 kernel, `ramdisk_size 0`
  (this is the image to flash)
- `Image.lz4`, `vmlinux`, `System.map`, `abi_symbollist*`

Working image also saved as `boot-lz4-6.12.92-gen6-fix2.img`
sha256 `717825aaad623812e9adb20d63f4bb92d597b8f4f389d331d061939d75844c87`.

Latest image (post_alloc_hook fix + put_page detector) saved as
`boot-lz4-6.12.92-gen6-fix3-postalloc.img`
sha256 `c9ae6a2ac69ac794c3f60f11f67f56d8259dd74827518cf6af08f800b5b735aa`.

## Expected log noise (harmless)

- `disagrees about version of symbol ... (ignoring)` — CRC bypass working.
- `exports symbol ... also exported by the kernel (ignoring)` — blocktag dupes.
- `proc_register` WARN from `tran_usb_extcon_probe` (vendor module creates a
  duplicate proc entry).
- `rust_binder.ko` fails to load (old module, not needed by the system).
