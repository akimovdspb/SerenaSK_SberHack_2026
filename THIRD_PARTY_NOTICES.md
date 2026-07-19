# Third-party notices

## Ouroboros

Communication Factory runs the unmodified Ouroboros source at tag `v6.61.4`, commit
`a00d51dd414f794d830cacf7da760061e442fa88`.

- Upstream: <https://github.com/razzant/ouroboros>
- Declared license: MIT (`pyproject.toml` and README metadata)
- Declared author: Anton Razzhigaev

The exact tag does not contain the `LICENSE` file linked from its README. This repository
therefore records the upstream license declaration and attribution without claiming that a
missing license text was present in the source archive. The runtime source is fetched from
the exact commit and verified against the SHA-256 recorded in `ouroboros/ouroboros.lock`.

Python and JavaScript dependency notices are generated from the committed lockfiles during
the release license-inventory gate.
