# Mergetrain express mark

The owner selected the express direction on 2026-10-27 and approved its use
for MCP, the plugin, and the public websites. Two incoming bands share a train
nose. The source is a static vector drawing, with no generated raster texture.

- `express.svg`: canonical transparent blue symbol, `#005DE8`.
- `wordmark.svg`: approved name composition, with lettering stored as paths.
- `python scripts/export_brand.py`: generate public, plugin and packaged icons.
  PNG export requires `rsvg-convert` from librsvg; it is not a runtime dependency.

Use the transparent blue mark on light surfaces and the white variant on dark
surfaces. Product and sharing cards use `#F7F7F5`. The generator preserves the
selected shape, proportions and window cutout. It also provides a transparent
theme-aware favicon. No font is needed to render the distributed assets.

The MCP server embeds the packaged 128px PNG variants in its identity metadata.
The Registry references their public HTTPS copies. The Codex plugin carries
512px PNG variants for its composer and directory listing. The Oddfini website
consumes the public card and icon exports; record their hashes in its asset manifest.

Website and plugin assets can ship from the repository independently of PyPI.
Publish the website assets before publishing Registry metadata that references
them. Installed MCP servers receive the embedded icons with the next package
release and a server restart. This branding change alone does not require an
immediate patch release under the release cadence policy.
