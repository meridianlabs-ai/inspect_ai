"""Control-channel API version (see design/control-channel.md "Version skew").

The `inspect ctl` CLI talks to live eval processes that embed whatever
inspect version they were launched with, so a newer CLI pointed at an
older process is the expected first-contact scenario for any new knob.
An older server's PATCH handlers silently ignore unknown query params;
the version integer lets the CLI gate a requested knob *before* sending
the mutation instead of sniffing response shapes after a partial apply.
"""

# Version of the control-channel HTTP API. The server stamps it on every
# ``GET /tasks`` row (as ``api_version``) — the read the CLI already performs
# for selector resolution, so the gate costs no extra round trip. A row
# without the field means version 0 (a server that predates version
# reporting).
#
# Convention: a PR that adds anything the CLI must gate on (a new retunable
# knob, an endpoint an existing command starts depending on) bumps this
# constant AND gives the feature's ``_KNOB_SINCE`` entry (in
# ``inspect_ai._cli.ctl``) the same new value — the two land atomically in
# the same PR. Purely additive response fields the CLI already null-guards
# don't need a bump. Two in-flight PRs bumping to the same value conflict
# here, which is the point — the second PR notices the first.
CONTROL_API_VERSION: int = 1
