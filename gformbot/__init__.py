"""
gformbot — A general-purpose Google Form auto-filler CLI.
"""

__version__ = "0.3.1"


def main():
    """Keep version checks independent of optional UI and browser imports."""
    import sys

    if sys.argv[1:] == ["--version"]:
        print(f"gformbot, version {__version__}")
        return
    from .cli import main as invoke

    invoke()


__author__ = "yogeshkrishna"
