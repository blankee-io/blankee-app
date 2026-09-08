-- Which sibling apps this instance offers its users.
--
-- One row per app, and only the switch: the app's name, icon and where it
-- lives are in apps_registry.py, because those are facts about the code rather
-- than about this deployment. An app with no row here is off - that is what a
-- new app looks like the moment its code lands, and it stays that way until an
-- administrator turns it on.
--
-- Blankee itself is deliberately not in here. It is the application serving
-- this table, so a switch that could turn it off is a switch that could lock
-- everyone out of the thing holding the switch.
CREATE TABLE IF NOT EXISTS `instance_apps` (
  `app_id` varchar(32) NOT NULL,
  `enabled` tinyint(1) NOT NULL DEFAULT '0',
  `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`app_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
