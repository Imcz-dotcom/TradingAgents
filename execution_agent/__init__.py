"""Execution agent: turn TradingAgents decisions into approved Moomoo orders.

The pipeline is ``decision -> Telegram approval -> broker execution``, with a
human in the loop at the approval step. The Moomoo and Telegram SDKs are
imported lazily inside their modules so this package imports cleanly without
them installed (see broker.py and notify.py).
"""

__version__ = "0.1.0"
