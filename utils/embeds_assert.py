import string

from utils.embeds import (
    blacklist_emb,
    cancel_notify_emb,
    emb8,
    emb11,
    emb13,
    emb14,
    emb20,
    emb21,
    emb_conv_choice,
    emb_conv_upl,
    emb_dl,
    emb_il,
    emb_upl_savegame,
    embApplied,
    embc,
    embCDone1,
    embCDone2,
    embCDone3,
    embChannelError,
    embchErr,
    embchgtav,
    embchLoading,
    embchrdr2,
    embconvCompleted,
    embCRdone,
    embDdone,
    embdec,
    embdecFormat,
    embDecrypt1,
    embdecTimeout,
    embDone_G,
    embe,
    embenc_out,
    embencComplete,
    embencinst,
    embEncrypted1,
    embencupl,
    embErrconv,
    embErrdec,
    embExit,
    embffn,
    embFileLarge,
    embfn,
    embgame,
    embgames,
    embgddone,
    embgdout,
    embgdt,
    embgs,
    embhttp,
    embinit,
    embkstone1,
    embkstone2,
    embLoad,
    embLoading,
    embmidComplete,
    embmo,
    embnt,
    embnvBin,
    embnvSys,
    embPdone,
    embpingfail,
    embpingsuccess,
    embpn,
    embpng,
    embpng1,
    embpng2,
    embpngs,
    embqcCompleted,
    embRbdone,
    embRdone,
    embres,
    embresb,
    embresbs,
    embress,
    embrrdone,
    embrrp,
    embrrps,
    embSceSys,
    embsl,
    embTdone,
    embTimedOut,
    embTitleChange,
    embTitleChange1,
    embTitleErr,
    embTitleSuccess,
    embuplSuccess,
    embuplSuccess1,
    embUtimeout,
    embvalidpsn,
    embwlcom,
    finished_emb,
    gd_maintenance_emb,
    gd_upl_progress_emb,
    keyset_emb,
    loadkeyset_emb,
    loadSFO_emb,
    paramEmb,
    retry_emb,
    working_emb,
)

_MAP_PLACEHOLDER = {
    embUtimeout: set(),
    embgdt: set(),
    embhttp: set(),
    embEncrypted1: set(),
    embDecrypt1: set(),
    emb14: set(),
    emb20: set(),
    emb21: set(),
    embpng: set(),
    emb8: {("description", frozenset({"msg"}))},
    embnt: set(),
    embvalidpsn: set(),
    embinit: set(),
    embTitleChange: set(),
    embTitleErr: set(),
    embTimedOut: set(),
    embDone_G: set(),
    emb_upl_savegame: set(),
    loadSFO_emb: set(),
    finished_emb: set(),
    loadkeyset_emb: set(),
    working_emb: set(),
    retry_emb: set(),
    blacklist_emb: set(),
    embChannelError: set(),
    cancel_notify_emb: set(),
    gd_upl_progress_emb: set(),
    gd_maintenance_emb: set(),
    embpng1: {
        ("description", frozenset({"savename", "j", "savecount", "i", "batches"}))
    },
    embpng2: {
        ("description", frozenset({"savename", "j", "savecount", "i", "batches"}))
    },
    embpngs: {
        ("description", frozenset({"savename", "j", "savecount", "i", "batches"}))
    },
    embPdone: {("description", frozenset({"printed", "id", "i", "batches"}))},
    embTitleChange1: {
        ("description", frozenset({"savename", "j", "savecount", "i", "batches"}))
    },
    embTitleSuccess: {
        ("description", frozenset({"savename", "j", "savecount", "i", "batches"}))
    },
    embTdone: {("description", frozenset({"printed", "id", "i", "batches"}))},
    emb_conv_upl: {("title", frozenset({"game"}))},
    emb_conv_choice: {
        ("title", frozenset({"basename"})),
        ("description", frozenset({"j", "count_entry", "i", "batches"})),
    },
    embCDone1: set(),
    embCDone2: set(),
    embCDone3: {
        (
            "description",
            frozenset({"result", "basename", "j", "count_entry", "i", "batches"}),
        )
    },
    embconvCompleted: {("description", frozenset({"finished_files", "i", "batches"}))},
    embSceSys: {("title", frozenset({"savename"}))},
    embgs: {
        ("title", frozenset({"savename"})),
        ("description", frozenset({"splitvalue"})),
    },
    embsl: {("title", frozenset({"displaysave"}))},
    embc: {("description", frozenset({"savename"}))},
    embCRdone: {("description", frozenset({"savename", "id"}))},
    emb11: {("description", frozenset({"savename", "j", "savecount", "i", "batches"}))},
    emb_dl: {
        ("description", frozenset({"savename", "j", "savecount", "i", "batches"}))
    },
    emb13: {("description", frozenset({"savename", "j", "savecount", "i", "batches"}))},
    embDdone: {("description", frozenset({"printed", "i", "batches"}))},
    embmo: {("description", frozenset({"savename", "j", "savecount", "i", "batches"}))},
    embmidComplete: {
        (
            "description",
            frozenset(
                {"dec_print", "savename", "id", "j", "savecount", "i", "batches"}
            ),
        )
    },
    embencComplete: {("description", frozenset({"printed", "id", "i", "batches"}))},
    keyset_emb: {("description", frozenset({"keyset", "fw"}))},
    embpingsuccess: {
        (
            "title",
            frozenset(
                {
                    "ftp_result",
                    "socket_result",
                    "instances_len",
                    "maximum_instances",
                    "latency",
                }
            ),
        )
    },
    embpingfail: {
        (
            "title",
            frozenset(
                {
                    "ftp_result",
                    "socket_result",
                    "instances_len",
                    "maximum_instances",
                    "latency",
                }
            ),
        )
    },
    embExit: set(),
    embresb: {("description", frozenset({"savename", "i", "savecount"}))},
    embresbs: {("description", frozenset({"savename", "id", "i", "savecount"}))},
    embRdone: {("description", frozenset({"printed", "id"}))},
    embLoading: {
        ("description", frozenset({"basename", "j", "count_entry", "i", "batches"}))
    },
    embApplied: {
        ("description", frozenset({"basename", "j", "count_entry", "i", "batches"}))
    },
    embqcCompleted: {("description", frozenset({"finished_files", "i", "batches"}))},
    embchLoading: {("description", frozenset({"game"}))},
    embkstone1: {("description", frozenset({"savename"}))},
    embkstone2: {("description", frozenset({"target_titleid"}))},
    embrrp: {
        ("description", frozenset({"savename", "j", "savecount", "i", "batches"}))
    },
    embrrps: {
        (
            "description",
            frozenset(
                {"savename", "id", "target_titleid", "j", "savecount", "i", "batches"}
            ),
        )
    },
    embrrdone: {
        ("description", frozenset({"printed", "id", "target_titleid", "i", "batches"}))
    },
    embres: {
        ("description", frozenset({"savename", "j", "savecount", "i", "batches"}))
    },
    embress: {
        ("description", frozenset({"savename", "id", "j", "savecount", "i", "batches"}))
    },
    embRbdone: {("description", frozenset({"printed", "id", "i", "batches"}))},
    embLoad: {("description", frozenset({"filename"}))},
    embdec: {("description", frozenset({"filename"}))},
    paramEmb: set(),
    embchErr: {("description", frozenset({"error"}))},
    embErrconv: {("description", frozenset({"error"}))},
    embErrdec: {("description", frozenset({"error"}))},
    embchgtav: {
        (
            "description",
            frozenset({"platform", "franklin_cash", "michael_cash", "trevor_cash"}),
        )
    },
    embchrdr2: {("description", frozenset({"platform", "money"}))},
    embfn: {("description", frozenset({"filename", "len", "max"}))},
    embFileLarge: {("description", frozenset({"filename", "max"}))},
    embnvSys: {("description", frozenset({"filename"}))},
    embpn: {("description", frozenset({"filename", "len", "max"}))},
    embnvBin: {("description", frozenset({"filename", "size"}))},
    embffn: {("description", frozenset({"path", "max"}))},
    embgddone: {("description", frozenset({"filename", "i", "filecount"}))},
    embuplSuccess: {("description", frozenset({"filename", "i", "filecount"}))},
    embe: {("description", frozenset({"error"}))},
    embuplSuccess1: {("description", frozenset({"filename"}))},
    embencupl: {
        ("title", frozenset({"savename"})),
        ("description", frozenset({"filename"})),
    },
    embenc_out: {("title", frozenset({"savename"}))},
    embencinst: {
        ("title", frozenset({"savename"})),
        ("description", frozenset({"splitvalue"})),
    },
    embgdout: {("description", frozenset({"url", "extra_msg"}))},
    embgames: set(),
    embgame: set(),
    emb_il: {("description", frozenset({"error"}))},
    embdecTimeout: set(),
    embdecFormat: {("title", frozenset({"savename"}))},
    embwlcom: {("description", frozenset({"user"}))},
}

for emb, v in _MAP_PLACEHOLDER.items():
    # assume title and description are the only fields that we care about

    if v == set():
        fmts = {"title": emb.title, "description": emb.description}
        for field, fmt in fmts.items():
            if not fmt:
                continue
            actual_fields = {
                fname for _, fname, _, _ in string.Formatter().parse(fmt) if fname
            }
            assert len(actual_fields) == 0, (
                f"There are extra placeholders {actual_fields} defined in the {field} of the embed with\n"
                f"title: {emb.title}\n"
                f"description: {emb.description}"
            )
        continue

    prev_field = None
    for field, ph_set in v:
        dummy_values = {ph: 0.0 for ph in ph_set}
        try:
            if field == "title":
                fmt = emb.title
            else:
                fmt = emb.description
            fmt.format(**dummy_values)
            actual_fields = {
                fname for _, fname, _, _ in string.Formatter().parse(fmt) if fname
            }
            assert len(dummy_values.keys()) == len(actual_fields)
        except KeyError as e:
            raise AssertionError(
                f"Cannot find placeholder {e} in the {field} of the embed with\n"
                f"title: {emb.title}\n"
                f"description: {emb.description}"
            )
        prev_field = field

    if len(v) == 1:
        if prev_field == "title":
            field = "description"
            fmt = emb.description
        else:
            field = "title"
            fmt = emb.title
        if fmt:
            actual_fields = {
                fname for _, fname, _, _ in string.Formatter().parse(fmt) if fname
            }
            assert len(actual_fields) == 0, (
                f"There are extra placeholders {actual_fields} defined in the {field} of the embed with\n"
                f"title: {emb.title}\n"
                f"description: {emb.description}"
            )
