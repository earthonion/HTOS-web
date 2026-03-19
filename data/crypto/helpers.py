from __future__ import annotations

from types import SimpleNamespace

from data.crypto.exceptions import CryptoError
from utils.constants import (
    BL3_TITLEID,
    DI1_TITLEID,
    DI2_TITLEID,
    DIGIMON_TITLEID,
    DL1_TITLEID,
    DL2_TITLEID,
    DSR_TITLEID,
    GTAV_TITLEID,
    LA_NOIRE_TITLEID,
    LOH_TRAILS_CS4_TITLEID,
    LOH_TRAILS_DAYBREAK_TITLEID,
    LOH_TRAILS_ZERO_AZURE,
    MGSV_GZ_TITLEID,
    MGSV_TPP_TITLEID,
    MHWI_TITLEID,
    NDOG_COL_TITLEID,
    NDOG_TITLEID,
    NDOG_TLOU2_TITLEID,
    NIOH2_TITLEID,
    NMS_TITLEID,
    RCUBE_TITLEID,
    RDR2_TITLEID,
    RE2R_TITLEID,
    RE3R_TITLEID,
    RE4R_TITLEID,
    RE7_TITLEID,
    RE_VILLAGE_TITLEID,
    RERES_TITLEID,
    REV2_TITLEID,
    RGG_TITLEID,
    SDEW_TITLEID,
    SMT5_TITLEID,
    TERRARIA_TITLEID,
    WONDERLANDS_TITLEID,
    XENO2_TITLEID,
)


async def extra_decrypt(
    d_ctx,
    Crypto: SimpleNamespace,
    title_id: str,
    destination_directory: str,
    savepairname: str,
    choice: bool | None = None,
) -> None:
    """Apply second-layer decryption. Web version always passes choice=True and d_ctx=None."""

    assert choice is not None, "Web mode requires explicit choice parameter"

    try:
        if title_id in GTAV_TITLEID:
            if choice:
                await Crypto.Rstar.check_dec_ps(
                    destination_directory, Crypto.Rstar.GTAV_PS_HEADER_OFFSET
                )

        elif title_id in RDR2_TITLEID:
            if choice:
                await Crypto.Rstar.check_dec_ps(
                    destination_directory, Crypto.Rstar.RDR2_PS_HEADER_OFFSET
                )

        elif title_id in XENO2_TITLEID:
            if choice:
                await Crypto.Xeno2.check_dec_ps(destination_directory)

        elif title_id in BL3_TITLEID:
            if choice:
                await Crypto.BL3.check_dec_ps(destination_directory)

        elif title_id in WONDERLANDS_TITLEID:
            if choice:
                await Crypto.BL3.check_dec_ps(destination_directory, True)

        elif title_id in NDOG_TITLEID:
            if choice:
                await Crypto.Ndog.check_dec_ps(destination_directory, Crypto.Ndog.START_OFFSET)

        elif title_id in NDOG_COL_TITLEID:
            if choice:
                await Crypto.Ndog.check_dec_ps(destination_directory, Crypto.Ndog.START_OFFSET_COL)

        elif title_id in NDOG_TLOU2_TITLEID:
            if choice:
                await Crypto.Ndog.check_dec_ps(
                    destination_directory, Crypto.Ndog.START_OFFSET_TLOU2
                )

        elif title_id in MGSV_TPP_TITLEID or title_id in MGSV_GZ_TITLEID:
            if choice:
                await Crypto.MGSV.check_dec_ps(destination_directory, title_id)

        elif title_id in REV2_TITLEID:
            if choice:
                await Crypto.Rev2.check_dec_ps(destination_directory)

        elif title_id in DL1_TITLEID or title_id in DL2_TITLEID or title_id in DI1_TITLEID:
            if choice:
                await Crypto.DL.check_dec_ps(destination_directory)

        elif title_id in RGG_TITLEID:
            if choice:
                await Crypto.RGG.check_dec_ps(destination_directory)

        elif title_id in DI2_TITLEID:
            if choice:
                await Crypto.DI2.check_dec_ps(destination_directory)

        elif title_id in NMS_TITLEID:
            if choice:
                await Crypto.NMS.check_dec_ps(destination_directory)

        elif title_id in TERRARIA_TITLEID:
            if choice:
                await Crypto.TERRARIA.check_dec_ps(destination_directory)

        elif title_id in SMT5_TITLEID:
            if choice:
                await Crypto.SMT5.check_dec_ps(destination_directory)

        elif title_id in RCUBE_TITLEID:
            if choice:
                await Crypto.RCube.check_dec_ps(destination_directory)

        elif title_id in DSR_TITLEID:
            if choice:
                await Crypto.DSR.check_dec_ps(destination_directory)

        elif title_id in RE4R_TITLEID:
            if choice:
                await Crypto.RE4R.check_dec_ps(destination_directory)

        elif title_id in RE2R_TITLEID:
            if choice:
                await Crypto.RE4R.check_dec_ps(destination_directory, True)

        elif title_id in SDEW_TITLEID:
            if choice:
                await Crypto.Sdew.check_dec_ps(destination_directory)

        elif title_id in NIOH2_TITLEID:
            if choice:
                await Crypto.Nioh2.check_dec_ps(destination_directory)

        elif title_id in MHWI_TITLEID:
            if choice:
                await Crypto.Mhwi.check_dec_ps(destination_directory)

        elif title_id in LA_NOIRE_TITLEID:
            if choice:
                await Crypto.LaNoire.check_dec_ps(destination_directory, savepairname)

        elif title_id in LOH_TRAILS_CS4_TITLEID:
            if choice:
                await Crypto.LoHTrails.check_dec_ps(destination_directory)

    except (OSError, ValueError, IndexError):
        raise CryptoError("Invalid save!")


async def extra_import(
    Crypto: SimpleNamespace, title_id: str, filepath: str, savepairname: str
) -> None:
    try:
        if title_id in GTAV_TITLEID:
            await Crypto.Rstar.check_enc_ps(filepath, Crypto.Rstar.GTAV_PS_HEADER_OFFSET)

        elif title_id in RDR2_TITLEID:
            await Crypto.Rstar.check_enc_ps(filepath, Crypto.Rstar.RDR2_PS_HEADER_OFFSET)

        elif title_id in XENO2_TITLEID:
            await Crypto.Xeno2.check_enc_ps(filepath)

        elif title_id in BL3_TITLEID:
            await Crypto.BL3.check_enc_ps(filepath)

        elif title_id in WONDERLANDS_TITLEID:
            await Crypto.BL3.check_enc_ps(filepath, True)

        elif title_id in NDOG_TITLEID:
            await Crypto.Ndog.check_enc_ps(filepath, Crypto.Ndog.START_OFFSET)

        elif title_id in NDOG_COL_TITLEID:
            await Crypto.Ndog.check_enc_ps(filepath, Crypto.Ndog.START_OFFSET_COL)

        elif title_id in NDOG_TLOU2_TITLEID:
            await Crypto.Ndog.check_enc_ps(filepath, Crypto.Ndog.START_OFFSET_TLOU2)

        elif title_id in MGSV_TPP_TITLEID or title_id in MGSV_GZ_TITLEID:
            await Crypto.MGSV.check_enc_ps(filepath, title_id)

        elif title_id in REV2_TITLEID:
            await Crypto.Rev2.check_enc_ps(filepath)

        elif (
            title_id in RE7_TITLEID
            or title_id in RERES_TITLEID
            or title_id in RE3R_TITLEID
            or title_id in RE_VILLAGE_TITLEID
        ):
            await Crypto.RE7.check_enc_ps(filepath)

        elif title_id in DL1_TITLEID:
            await Crypto.DL.check_enc_ps(filepath, "DL1")

        elif title_id in DL2_TITLEID:
            await Crypto.DL.check_enc_ps(filepath, "DL2")

        elif title_id in RGG_TITLEID:
            await Crypto.RGG.check_enc_ps(filepath)

        elif title_id in DI1_TITLEID:
            await Crypto.DL.check_enc_ps(filepath, "DI1")

        elif title_id in DI2_TITLEID:
            await Crypto.DI2.check_enc_ps(filepath)

        elif title_id in NMS_TITLEID:
            await Crypto.NMS.check_enc_ps(filepath)

        elif title_id in TERRARIA_TITLEID:
            await Crypto.TERRARIA.check_enc_ps(filepath)

        elif title_id in SMT5_TITLEID:
            await Crypto.SMT5.check_enc_ps(filepath)

        elif title_id in RCUBE_TITLEID:
            await Crypto.RCube.check_enc_ps(filepath)

        elif title_id in DSR_TITLEID:
            await Crypto.DSR.check_enc_ps(filepath)

        elif title_id in RE4R_TITLEID:
            await Crypto.RE4R.check_enc_ps(filepath)

        elif title_id in RE2R_TITLEID:
            await Crypto.RE4R.check_enc_ps(filepath, True)

        elif title_id in DIGIMON_TITLEID:
            await Crypto.Digimon.check_enc_ps(filepath)

        elif title_id in SDEW_TITLEID:
            await Crypto.Sdew.check_enc_ps(filepath)

        elif title_id in NIOH2_TITLEID:
            await Crypto.Nioh2.check_enc_ps(filepath)

        elif title_id in MHWI_TITLEID:
            await Crypto.Mhwi.check_enc_ps(filepath)

        elif title_id in LA_NOIRE_TITLEID:
            await Crypto.LaNoire.check_enc_ps(filepath, savepairname)

        elif (
            title_id in LOH_TRAILS_CS4_TITLEID
            or title_id in LOH_TRAILS_DAYBREAK_TITLEID
            or title_id in LOH_TRAILS_ZERO_AZURE
        ):
            await Crypto.LoHTrails.check_enc_ps(filepath, title_id)
    except (OSError, ValueError, IndexError):
        raise CryptoError("Invalid save!")
