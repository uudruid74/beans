#!/bin/sh
# beans — hand everything to the beansd supervisor (PID 1). It owns Olla, the
# bridges, and the downloads; SIGTERM propagates through it — no zombies.
exec python3 /opt/beansd.py "$@"