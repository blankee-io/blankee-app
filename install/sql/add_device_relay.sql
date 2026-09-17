-- Migration: push through the relay
--
-- A self-hosted server cannot push to the app by itself - Apple only accepts
-- pushes from the key of the account that signed the app - so it asks the
-- relay (relay/README.md) instead. Two things the phone tells the server at
-- registration make that possible:
--
--   relay_secret      the secret the phone also gave the relay; the server
--                     presents it with every push, and the relay checks it.
--                     Nobody with only a device token can push to the phone.
--   apns_environment  'sandbox' for a build from Xcode or TestFlight,
--                     'production' for the App Store one. Apple issues a
--                     different token for each, and a push sent to the wrong
--                     environment is refused as a dead device.
--
-- Created: 2026-09-17

ALTER TABLE `device_tokens`
  ADD COLUMN `relay_secret` varchar(255) DEFAULT NULL,
  ADD COLUMN `apns_environment` varchar(20) NOT NULL DEFAULT 'production';
