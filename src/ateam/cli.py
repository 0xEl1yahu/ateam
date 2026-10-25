"""The ateam command-line interface."""

from __future__ import annotations

import click

from ateam import __version__


@click.group()
@click.version_option(__version__, prog_name="ateam")
def main() -> None:
    """ATeam: durable intent, role-separated workers, human approval gates."""


if __name__ == "__main__":
    main()
