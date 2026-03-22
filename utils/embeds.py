"""Shim module replacing Discord embeds for web use. Provides no-op objects."""


class _EmbedShim:
    """Minimal shim to satisfy import references to Discord embeds."""

    description = ""

    def copy(self):
        return _EmbedShim()

    def set_footer(self, **kwargs):
        pass


# All named embed imports used across the codebase
embuplSuccess = _EmbedShim()
embfn = _EmbedShim()
embFileLarge = _EmbedShim()
embnvSys = _EmbedShim()
embpn = _EmbedShim()
embnvBin = _EmbedShim()
emb_il = _EmbedShim()
embChannelError = _EmbedShim()
retry_emb = _EmbedShim()
blacklist_emb = _EmbedShim()
gd_maintenance_emb = _EmbedShim()
embe = _EmbedShim()
embErrconv = _EmbedShim()
embdecTimeout = _EmbedShim()
embdecFormat = _EmbedShim()
embErrdec = _EmbedShim()
embDone_G = _EmbedShim()
embchErr = _EmbedShim()
embchgtav = _EmbedShim()
embchrdr2 = _EmbedShim()
