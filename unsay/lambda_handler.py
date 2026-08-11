"""AWS Lambda entry point for the demo app.

Lambda is the cheapest way to keep a demo reachable for the four weeks of the
judging window. There is no idle charge, so a page nobody is looking at costs
nothing, and demo-scale traffic sits inside cents. An always-on container would
cost roughly $15-30/month for the same thing.

Two Lambda-specific concerns are handled here:

1. Connection pooling. A Lambda execution environment is reused between
   invocations but frozen in between, so a large pool would hold connections
   open against the cluster's limit while doing nothing. The pool is kept small
   and created lazily.

2. Cold starts. The first request after a quiet period pays module import plus
   a fresh connection. That is a few seconds, once, and acceptable for a demo.
"""

from __future__ import annotations

import os

# CockroachDB Cloud requires TLS. Keep the pool tiny: Basic clusters cap
# concurrent connections and a frozen Lambda holding many is wasteful.
os.environ.setdefault("UNSAY_POOL_MIN", "0")
os.environ.setdefault("UNSAY_POOL_MAX", "2")

from mangum import Mangum  # noqa: E402

from unsay.api import app  # noqa: E402

handler = Mangum(app, lifespan="off")
