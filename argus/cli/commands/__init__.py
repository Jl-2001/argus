"""One module per subcommand. Each exposes ``add_parser(subparsers)`` and
``run(args, repository, now) -> int``. Commands parse arguments, call
``argus.cli.queries`` for data, call ``argus.cli.formatting`` to render
it, and print -- no SQL, no staleness logic, and no business rules live
here.
"""
