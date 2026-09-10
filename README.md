# The Algorithmic Mirror

Module one of a local-first system for seeing and steering your own digital footprint.
This module does one thing: take your browser exhaust, embed it, cluster it, and show you
the shape.

## The hypothesis this repo tests

    My own browser exhaust, embedded and clustered, resolves into
    behavioral islands I actually recognize.

That sentence is the whole point. If running this produces clusters you recognize as
yourself, the larger system is worth building. If it produces mush, you learned that in a
weekend, before pouring days into a node graph, drift tracking, or a DNS layer.
Understanding first, product later.

## What it is not

It does not read the algorithm's parameters. No ad network exposes its internal weights, so
nothing here pretends to. This reflects *you*, from your own data on your own machine. The
"what do trackers think I am" overlay is a separate, later module built from a different
capture path (your inferred ad-topic lists), and even then it is an inference proxy of their
output, not a mirror of their internals. Naming it honestly keeps the tool from lying to you.

## Run it

Prove the machinery first, with synthetic data and no browser access:

    pip install -r requirements.txt
    python mirror.py --demo

That writes `mirror.html`: an interactive scatter with five planted themes and some noise.
If those five separate cleanly, the pipeline works and the only remaining unknown is your
real data.

Then point it at yourself:

    python mirror.py --browser firefox
    python mirror.py --browser chrome

Open `mirror.html`, hover any point to read the title or search query behind it, and ask the
one question: does this look like someone I recognize?

## What is deliberately correct in here

- Clustering runs on a mid-dimensional (15D) UMAP projection, not on the 2D picture.
  Clustering on a 2D projection manufactures groups that are only projection artifacts. The
  2D view is a second, separate projection, meant for your eye alone.
- Outliers come from HDBSCAN's own GLOSH score (the curiosity-clicks that belong to no
  island) rather than three overlapping anomaly detectors bolted together.
- The output HTML embeds its own plotting library. No CDN call, nothing leaves the machine.
  A privacy tool must not become a privacy hole.

## Where the real friction lives (honest TODOs)

- History DBs are locked while the browser is open; this copies the file first, which works
  but is a hack. Close the browser if a read fails.
- Search-query extraction is a naive `q=` param grab. Enough to reveal structure, not
  exhaustive.
- `min_cluster_size` is set to 15. Too many tiny islands means raise it; one giant blob means
  lower it. Tuning it against your own data *is* part of the understanding.

## The larger system this is module one of

Four interception points, all on classical hardware you already own and can see into: a DNS
sink, an egress inspector, the browser, and this local store. Build them one module at a
time, the way a smart-home hub accretes integrations. This module is the first brick, not the
cathedral.
