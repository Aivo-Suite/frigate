# Visitor records

The browser worker saves one anonymous visitor per confirmed continuous track
(three matched detections). It stores a JPEG crop of the person, not a facial
embedding or full video. The first photo is preserved, with dimensions bounded
to 256 by 384 pixels. Stopping capture does not delete saved records.

`traffic_visitors` is separate from store login accounts. Its composite primary
key is `(tenant_id, visitor_id)`; `(tenant_id, camera_id, tracking_id)` is unique.
Fields include first_seen, last_seen, photo (JPEG bytes), photo_at, and a nullable
crm_external_id reserved for later integration. All timestamps are Unix seconds.
Join traffic_crossings on tenant_id, camera_id and tracking_id to find entries
and exits. CRM synchronization and a public visitor API are not enabled yet.

A visitor ID is not a verified person identity. Tracking loss, reconnection or
a later visit can create a new record. Do not label counts as unique customers.
Existing historical crossings are preserved and are not assigned invented photos.
New Edge crossing uploads also create visitor metadata, but Edge photo upload is
not yet implemented; photo capture in this release is from browser webcams.

The Visitantes screen scopes reads and inline photos to the authenticated tenant,
with local-date and camera filters and twelve records per page. Images are not
served through public Streamlit media URLs. No CRM calls or facial recognition
are performed. Stored photos have no automatic expiry in this initial release.
Database backups therefore contain visitor photos and must remain private.

Schema creation is additive and idempotent via init_traffic_db. No production
fixtures or historical data deletion are part of this migration. Test using
test_visitors with temporary databases and generated pixels, never store cameras.
