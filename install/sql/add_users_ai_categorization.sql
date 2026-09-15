-- Migration: the AI categorization switch, per user
-- Off by default: nothing about a person's transactions leaves the server
-- until they turn this on themselves. The switch is only the user's choice;
-- the feature is actually active when the choice, a verified key and a
-- linked bank account all hold (providers/claude_enrichment.py).
--
-- Also clears setup_step for anyone mid-wizard: the wizard gains two steps
-- (bank connection, AI) between MFA and categories, so a saved step number
-- from before would land on the wrong screen. Restarting at the welcome
-- screen is the same treatment the last renumbering used.
-- Created: 2026-09-13

ALTER TABLE `users`
  ADD COLUMN `ai_categorization` tinyint(1) NOT NULL DEFAULT 0
    COMMENT 'User opted in to sending transactions to their own Anthropic key'
    AFTER `bank_auto_import`;

UPDATE `users` SET `setup_step` = 0 WHERE `setup_step` > 1 AND `member_since` IS NULL;
