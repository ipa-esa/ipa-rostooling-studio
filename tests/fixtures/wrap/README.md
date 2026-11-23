# fixtures/wrap — "wrap in subsystem" emission

`wrapped.json` is the project the /ros-studio editor produces when a selection of nodes is
wrapped into a new subsystem (right-click a multi-selection → "Wrap in subsystem"). It was
captured from the editor itself, not hand-written, and is byte-identical to what
`genProjectJson()` returned for this case.

The case is deliberately the one that used to break, silently:

* `camera` and `filter` are wrapped into a new `camera_stack`; `calib` stays outside.
* `calib.info → camera.info` therefore **crosses the new boundary** — its endpoint is written
  by the outer file but has to resolve against an interface the *subsystem* file declares.
* `camera.image` and `filter.image` share an interface **name**, so each file would derive a
  different disambiguated label for them if the labels were not pinned.

Both of those are why `wrapNodesInSubsystem` pins an explicit, subsystem-wide-unique `label`
and sets `exposed` on the extracted copy, rather than letting each file derive its own:
a `connections:` endpoint is a label STRING, and two files deriving independently do not agree.

Without that, `generate` writes two files that are each fine on their own and broken together;
the language server reports the connection (`rosmodel_lint.py --server` shows it as `RM100`):

    ERROR  demo_sys.rossystem:10  Couldn't resolve reference to RosInterface 'info'.

`check_wrap` in `tests/studio_roundtrip.py` regenerates from this project and asserts that
every connection endpoint in every emitted file resolves against a label some emitted file
actually declares — which is the invariant, not just this instance of breaking it.
