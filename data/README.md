# Local data

Generated reference catalogs, vendor interface inventories, device identities,
firmware manifests and test results belong here locally and are not distributed.

The server starts without a reference database. Device parameter definitions come
from the connected device's online catalog. Optional offline reference tools need
a catalog generated from your own supported vendor software with
`scripts/build_catalog.py`; missing reference data is reported explicitly.
