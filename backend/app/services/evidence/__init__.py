"""Evidence ingest: type check, metadata, stripped copy, text. No LLM calls here (that's WS3)."""

import pillow_heif

# Lets Pillow open HEIC wherever evidence is read (ingest, and HEIC-to-JPG in the package).
pillow_heif.register_heif_opener()
