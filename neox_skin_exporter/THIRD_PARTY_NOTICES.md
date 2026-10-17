# Third-party components

## Arm ASTC Encoder

Bundled file: `bin/astcenc-sse2.exe`, version 5.7.0, Windows x64 SSE2 build.

Upstream: https://github.com/ARM-software/astc-encoder/tree/5.7.0

License: Apache License 2.0. The upstream license is included verbatim as `bin/ASTC_LICENSE.txt`. The encoder is invoked as a separate executable. No encoder source modifications are included here.

## User-supplied NeoX tools

`decoder.py` is an adapted, Blender-independent subset of the supplied `extract_cmesh_blender.py`. `metadata.py` is derived from the supplied `prepare_npk_model.py`.

These files were provided with the user's existing NeoX workflow. This package does not claim ownership of those components or grant any additional redistribution permission for them. Preserve their provenance and check the original permissions before publishing this personal-use package elsewhere.

## Runtime and content

Blender and its bundled NumPy provide the runtime; neither is bundled in this ZIP. The ZIP contains no game model, game texture, penguin model, or other third-party character artwork.
