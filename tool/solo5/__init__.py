"""The effect shim: one C event loop against the Solo5 API, for Solo5 and as a host process.

    emit.py      manifest -> node.c (the compiled step), node_tables.h, Solo5 manifest
    driver.c     the event loop; dumb effects only
    host.c       the Solo5 API over a file and a datagram socket (a host process)
    run.py       start a driver, play a scripted client against it, record what happened
    compare.py   a recorded run as a simulator trace, or the reason it is not one
    check.py     python3 -m tool check solo5

See README.md in this directory for what is established and what is not.
"""
