"""
gformbot — A synthetic Google Forms response CLI.
"""

__version__ = "0.4.0"


def main():
    """Keep version checks independent of UI imports."""
    import sys

    if sys.argv[1:] == ["--version"]:
        print(f"gformbot, version {__version__}")
        return
    from .cli import main as invoke

    invoke()


__author__ = "yogeshkrishna"
