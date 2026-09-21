**What this changes, and why**

**How it was checked**

- [ ] `python3 -m unittest discover -s tests` passes
- [ ] `python3 scripts/leak_guard.py` passes
- [ ] a behaviour change has a test
- [ ] a detection or matching change has a measurement on recorded sessions,
      written into `docs/research/benchmarks.md`
- [ ] the README or `docs/` say what changed, if a user would notice
