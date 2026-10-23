// SPDX-License-Identifier: GPL-2.0
/*
 * Android: no-op compatibility stubs for the MediaTek blocktag vendor module.
 *
 * The Infinix X6871 (MediaTek MT6895) vendor_boot ramdisk ships a prebuilt
 * blocktag.ko built against a MediaTek 6.12.38 kernel.  Its hook functions
 * dereference kernel structures (fields next to the scsi_cmnd PDU inside a
 * struct request, etc.) whose layout changed in this 6.12 GKI kernel, which
 * crashes the kernel as soon as ufs-mediatek-mod calls into it.
 *
 * There is no source for blocktag.ko, so instead of fixing the module, export
 * no-op replacements from vmlinux.  find_symbol() always searches the kernel
 * symbol tables before module exports, so the dependent vendor modules
 * (ufs-mediatek-mod, mtk-mmc-mod, cqhci, mtk-mmc-dbg, mtk_perf_common) bind to
 * these stubs and never reach the vendor code.  blocktag.ko itself still
 * loads; its duplicate exports are tolerated and shadowed, and its procfs/
 * tracepoint based logging keeps working.
 *
 * Return values mirror the "feature disabled" behaviour of the originals:
 *  - mtk_btag_ufs_init() returns NULL, which makes the ufs-mediatek-mod
 *    wrappers skip all further blocktag calls by themselves.
 *  - mmc_mtk_biolog_init() reports success without registering anything.
 *  - the remaining hooks are one-way notifications whose return values are
 *    ignored by their callers.
 */

#include <linux/module.h>

void *mtk_btag_ufs_init(void *priv, unsigned int hba_ver, unsigned int proto);
void mtk_btag_ufs_exit(void *ctx);
void mtk_btag_ufs_send_command(void *ctx, unsigned int tag,
			       unsigned int data, void *cmd);
void mtk_btag_ufs_transfer_req_compl(void *ctx, unsigned int tag, void *cmd);
void mmc_mtk_biolog_check(void *host, unsigned int tag);
void mmc_mtk_biolog_exit(void *host);
int mmc_mtk_biolog_init(void *host);
void mmc_mtk_biolog_send_command(unsigned int tag, void *mrq);
void mmc_mtk_biolog_transfer_req_compl(void *host, unsigned int tag,
				       void *data);
void set_mmc_perf_mode(void *host, unsigned int mode);
int mtk_btag_mictx_get_data(void *ctx, void *data);
int mtk_btag_mictx_register(void *ctx, const char *name, const void *ops,
			    void *priv);
void mtk_btag_mictx_unregister(void *ctx);

void *mtk_btag_ufs_init(void *priv, unsigned int hba_ver, unsigned int proto)
{
	return NULL;
}
EXPORT_SYMBOL_GPL(mtk_btag_ufs_init);

void mtk_btag_ufs_exit(void *ctx)
{
}
EXPORT_SYMBOL_GPL(mtk_btag_ufs_exit);

void mtk_btag_ufs_send_command(void *ctx, unsigned int tag,
			       unsigned int data, void *cmd)
{
}
EXPORT_SYMBOL_GPL(mtk_btag_ufs_send_command);

void mtk_btag_ufs_transfer_req_compl(void *ctx, unsigned int tag, void *cmd)
{
}
EXPORT_SYMBOL_GPL(mtk_btag_ufs_transfer_req_compl);

void mmc_mtk_biolog_check(void *host, unsigned int tag)
{
}
EXPORT_SYMBOL_GPL(mmc_mtk_biolog_check);

void mmc_mtk_biolog_exit(void *host)
{
}
EXPORT_SYMBOL_GPL(mmc_mtk_biolog_exit);

int mmc_mtk_biolog_init(void *host)
{
	return 0;
}
EXPORT_SYMBOL_GPL(mmc_mtk_biolog_init);

void mmc_mtk_biolog_send_command(unsigned int tag, void *mrq)
{
}
EXPORT_SYMBOL_GPL(mmc_mtk_biolog_send_command);

void mmc_mtk_biolog_transfer_req_compl(void *host, unsigned int tag,
				       void *data)
{
}
EXPORT_SYMBOL_GPL(mmc_mtk_biolog_transfer_req_compl);

void set_mmc_perf_mode(void *host, unsigned int mode)
{
}
EXPORT_SYMBOL_GPL(set_mmc_perf_mode);

int mtk_btag_mictx_get_data(void *ctx, void *data)
{
	return 0;
}
EXPORT_SYMBOL_GPL(mtk_btag_mictx_get_data);

int mtk_btag_mictx_register(void *ctx, const char *name, const void *ops,
			    void *priv)
{
	return 0;
}
EXPORT_SYMBOL_GPL(mtk_btag_mictx_register);

void mtk_btag_mictx_unregister(void *ctx)
{
}
EXPORT_SYMBOL_GPL(mtk_btag_mictx_unregister);
