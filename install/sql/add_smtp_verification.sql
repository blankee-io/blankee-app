-- =========================================================================
-- Migration: Verification state for the instance SMTP configuration
-- =========================================================================
-- Purpose: Saving mail settings now has to prove they work before email
--          notifications can be turned on. The save sends a time-based code
--          to the configured address, and that code must be entered back into
--          the form. Until it is, the per-user "Email Notifications" toggle
--          refuses to switch on - in the route, not only in the UI.
--
-- HOW VERIFICATION IS INVALIDATED, and why there is no invalidation code:
--   verified_fingerprint holds a hash of the transport that was proven to work
--   (server, port, username, from address, TLS). is_verified() recomputes that
--   hash from the live configuration on every call and compares the two. So
--   editing any of those fields makes the instance unverified as a matter of
--   arithmetic - there is no "clear the flag on change" path that could be
--   forgotten, or drift out of step with the columns it guards.
--
--   The password is deliberately NOT in the fingerprint. Hashing a credential
--   in order to store it beside the credential is a worse problem than the one
--   it solves, and a password change is already known at save time - so
--   save_smtp_config() clears verified_at directly in that one case.
--
-- verification_secret is a pyotp base32 secret, never a code. The code is
-- derived from the secret and the clock, so a leaked row cannot be replayed
-- once the time step has passed. pyotp is already a dependency - it backs the
-- existing account MFA.
--
-- Pattern: plain ALTER TABLE, matching the other add_* migrations here.
--          Re-running fails with "Duplicate column name", which is noise
--          rather than damage - check with:
--            SHOW COLUMNS FROM instance_settings LIKE 'verif%';
--
-- Safe to run on a live app: every column defaults to "not verified", and the
-- code that reads them treats a missing row the same way.
-- =========================================================================

ALTER TABLE `instance_settings`
    ADD COLUMN `verified_at` datetime DEFAULT NULL
        COMMENT 'When the configured address last confirmed a code',
    ADD COLUMN `verified_fingerprint` char(64) DEFAULT NULL
        COMMENT 'SHA-256 of the proven transport; deliberately excludes the password',
    ADD COLUMN `verification_secret` varchar(64) DEFAULT NULL
        COMMENT 'pyotp base32 secret for the pending code, never the code itself',
    ADD COLUMN `verification_sent_at` datetime DEFAULT NULL,
    ADD COLUMN `verification_attempts` int NOT NULL DEFAULT 0
        COMMENT 'Failed attempts against the current secret; caps brute force';
