"""Disabled legacy source. Shipped applications never generate sample records."""
from ..connections.sources import SourceConnection


class DemoExtension(SourceConnection):
    def __init__(self):
        super().__init__("demo", "not_configured")
