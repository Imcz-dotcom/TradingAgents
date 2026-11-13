"""Entry point for the execution-agent CLI.

Stage 1 ships only the shell; the ``run`` command lands in a later stage.
"""

import typer

app = typer.Typer(
    name="tradingagents-execution",
    help=(
        "Execution agent: run TradingAgents, get Telegram approval, and place "
        "orders on Moomoo (paper by default)."
    ),
    no_args_is_help=True,
)


@app.callback()
def _root() -> None:
    """Execution-agent CLI."""


if __name__ == "__main__":
    app()
