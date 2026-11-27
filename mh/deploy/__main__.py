import sys

USAGE = "python3 -m mh.deploy <migrate|install|push|tunnel> …（各自 --help；tunnel --print 只打印命令）"


def main(argv):
    cmd, rest = (argv[0] if argv else ""), argv[1:]
    if cmd == "migrate":
        from . import migrate
        migrate.main(rest)
    elif cmd == "install":
        from . import install
        install.main(rest)
    elif cmd == "push":
        from . import push
        push.main(rest)
    elif cmd == "tunnel":
        from . import tunnel
        tunnel.main(rest)
    else:
        raise SystemExit(USAGE)


if __name__ == "__main__":
    main(sys.argv[1:])
