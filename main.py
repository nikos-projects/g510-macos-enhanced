"""Single entry point for the bundled app.

A packaged .app has one executable, so the same binary has to be able to run
the window, the background agent and the command line. Which one is decided by
the first argument.
"""
import sys


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "--daemon":
        import daemon
        sys.exit(daemon.main())
    if argv and argv[0] == "--cli":
        sys.argv = ["g510"] + argv[1:]
        import cli
        sys.exit(cli.main())
    import gui
    gui.main()


if __name__ == "__main__":
    main()
