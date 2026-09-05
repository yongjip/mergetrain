# Bounded persistent-loop check

Separate mechanical follow-up, not an AI timing trial. Use a fresh isolated repo
and local bare destination `/private/tmp/mt-luna-daemon-loop-20260905/train.git`.
Same original patches, base, and baseline policy. Replay previously accepted
repair `30ce0f6a9f18f8bce4d87f0f70b8ba2259e42d4f` by fast-forwarding the owning moves
branch after the first partial deployment. No feature implementation or repair
time is included. This bounded full local daemon check authorizes auto jobs only
for that local destination and unchanged policy.

Run the actual foreground daemon continuously with its default 15-second
interval, call retry once, and observe whether it deploys the replacement without
another runner invocation or confirmation. Observe SQLite read-only with a
0.1-second polling interval; never edit its state. Bound the process to 60 seconds
and terminate the exact owned process in finally, then verify final deployed SHA
with strict all. Preserve event times, daemon output and exit status. This tests
automatic continuation and scheduling delay only, not general performance or
approval transfer across destinations. Setup and observation overhead are not
engine-only execution time.
