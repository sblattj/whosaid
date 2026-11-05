# Vendored code

Pure-Python readers for the Chromium IndexedDB store that the Teams desktop app keeps its
chat cache in, used by `lib/teams_cache.py`. Copied unmodified (only the modules that
module needs) so `pip install whosaid` works without git dependencies.

| Package | Upstream | Commit | License |
|---|---|---|---|
| `ccl_chromium_reader` (indexeddb, leveldb, blink/v8 deserializers) | https://github.com/cclgroupltd/ccl_chromium_reader | ad37443541dd91dac3ac1fb2eecaeb8693f36815 (0.3.19) | MIT, see `ccl_chromium_reader/LICENSE` |
| `ccl_simplesnappy` | https://github.com/cclgroupltd/ccl_simplesnappy | 3d085230baa8c46cf2090ebba29bf6e8eab31087 (0.4) | MIT, see `ccl_simplesnappy/LICENSE` |

The `ccl_chromium_reader` package `__init__.py` files are empty on purpose: upstream's imports the
profile-folder reader, which needs brotli and zstd. `ccl_simplesnappy/__init__.py` re-exports the
module, because `ccl_leveldb` calls `ccl_simplesnappy.decompress` on a top-level import.
`lib/teams_cache.py` puts `lib/vendor` on `sys.path` for both.
