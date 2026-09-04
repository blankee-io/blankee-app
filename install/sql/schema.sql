-- =========================================================================
-- Blankee baseline schema
-- =========================================================================
-- Structure only, no data. Applied by install/migrate.py to an empty database;
-- the migrations listed there run on top of it.
--
-- Regenerated from a working deployment rather than hand-maintained, because
-- the hand-maintained version had drifted far enough that a database built from
-- it could not run the application - provision_user() failed on a missing
-- is_system column, and c_expense_category_groups was absent entirely.
--
-- Deliberately excluded:
--   *_backup / quiltt_purge_backup_*  migration artifacts, not part of a new install
--   schema_migrations                 created by install/migrate.py
--   stored routines                   the only one carries DEFINER=root, which the
--                                     application's own database user cannot create,
--                                     and nothing in the codebase calls it
--
-- ENCRYPTION='Y' appears on most tables. install/migrate.py strips it unless
-- MYSQL_TABLE_ENCRYPTION=1, because it needs a keyring component that a default
-- MySQL 8 does not have.
--
-- To regenerate: mysqldump --no-data --no-tablespaces --skip-add-drop-table
-- =========================================================================

/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `bundle_items` (
  `id` int NOT NULL AUTO_INCREMENT,
  `bundle_id` int NOT NULL,
  `account` varchar(255) NOT NULL,
  `credit_account_id` int DEFAULT NULL,
  `name` varchar(255) NOT NULL,
  `value` decimal(15,2) DEFAULT NULL,
  `date` date DEFAULT NULL,
  `description` varchar(255) DEFAULT NULL,
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `bundle_id` (`bundle_id`),
  KEY `bundle_items_account_fk` (`credit_account_id`),
  CONSTRAINT `bundle_items_ibfk_1` FOREIGN KEY (`bundle_id`) REFERENCES `bundles` (`id`) ON DELETE CASCADE,
  CONSTRAINT `bundle_items_account_fk` FOREIGN KEY (`credit_account_id`) REFERENCES `credit_accounts` (`id`) ON DELETE SET NULL
) ENGINE=InnoDB AUTO_INCREMENT=198 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `bundles` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `expense_category_id` int DEFAULT NULL,
  `name` varchar(255) NOT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `active` tinyint(1) NOT NULL DEFAULT '0',
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `user_id` (`user_id`),
  KEY `bundles_ibfk_2` (`expense_category_id`),
  CONSTRAINT `bundles_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE,
  CONSTRAINT `bundles_ibfk_2` FOREIGN KEY (`expense_category_id`) REFERENCES `expense_categories` (`id`) ON DELETE SET NULL
) ENGINE=InnoDB AUTO_INCREMENT=83 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `c_a_balances` (
  `id` int NOT NULL AUTO_INCREMENT,
  `account_id` int NOT NULL,
  `date` date NOT NULL,
  `total_expenses` decimal(15,2) DEFAULT NULL,
  `balance` decimal(15,2) DEFAULT NULL,
  `total_payments` decimal(15,2) DEFAULT NULL,
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_account_date_weekly` (`account_id`,`date`),
  KEY `account_id` (`account_id`),
  CONSTRAINT `c_a_balances_ibfk_account` FOREIGN KEY (`account_id`) REFERENCES `credit_accounts` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=1215214 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `c_a_balances_d` (
  `id` int NOT NULL AUTO_INCREMENT,
  `account_id` int NOT NULL,
  `date` date NOT NULL,
  `total_expenses` decimal(15,2) DEFAULT NULL,
  `balance` decimal(15,2) DEFAULT NULL,
  `total_payments` decimal(15,2) DEFAULT NULL,
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_account_date_daily` (`account_id`,`date`),
  KEY `account_id` (`account_id`),
  CONSTRAINT `c_a_balances_d_ibfk_account` FOREIGN KEY (`account_id`) REFERENCES `credit_accounts` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=8359473 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `c_a_balances_m` (
  `id` int NOT NULL AUTO_INCREMENT,
  `account_id` int NOT NULL,
  `date` date NOT NULL,
  `total_expenses` decimal(15,2) DEFAULT NULL,
  `balance` decimal(15,2) DEFAULT NULL,
  `total_payments` decimal(15,2) DEFAULT NULL,
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_account_date_monthly` (`account_id`,`date`),
  KEY `account_id` (`account_id`),
  CONSTRAINT `c_a_balances_m_ibfk_account` FOREIGN KEY (`account_id`) REFERENCES `credit_accounts` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=273973 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `c_expense_categories` (
  `id` int NOT NULL AUTO_INCREMENT,
  `account_id` int NOT NULL,
  `name` varchar(255) NOT NULL,
  `display_order` decimal(8,4) DEFAULT '0.0000',
  `group_id` int DEFAULT NULL,
  `is_recurring` tinyint(1) DEFAULT '0',
  `no_end_date` tinyint(1) DEFAULT '0',
  `hidden` tinyint(1) DEFAULT '0',
  `is_bundle` tinyint(1) NOT NULL DEFAULT '0',
  `bundle_id` int DEFAULT NULL,
  `is_interest` tinyint(1) NOT NULL DEFAULT '0',
  `is_auto_adjustment` tinyint(1) NOT NULL DEFAULT '0',
  `is_system` tinyint(1) NOT NULL DEFAULT '0',
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `idx_c_expense_categories_account_bundle` (`account_id`,`bundle_id`),
  KEY `account_id` (`account_id`),
  KEY `c_expense_categories_bundle_fk` (`bundle_id`),
  KEY `c_expense_categories_group_fk` (`group_id`),
  CONSTRAINT `c_expense_categories_group_fk` FOREIGN KEY (`group_id`) REFERENCES `c_expense_category_groups` (`id`) ON DELETE SET NULL,
  CONSTRAINT `c_expense_categories_bundle_fk` FOREIGN KEY (`bundle_id`) REFERENCES `bundles` (`id`) ON DELETE CASCADE,
  CONSTRAINT `c_expense_categories_ibfk_1` FOREIGN KEY (`account_id`) REFERENCES `credit_accounts` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=1658 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `c_expense_category_groups` (
  `id` int NOT NULL AUTO_INCREMENT,
  `account_id` int NOT NULL,
  `user_id` int DEFAULT NULL,
  `name` varchar(255) DEFAULT NULL,
  `display_order` decimal(8,4) DEFAULT '0.0000',
  `source_group_id` int DEFAULT NULL,
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `account_id` (`account_id`),
  KEY `user_id` (`user_id`),
  KEY `source_group_id` (`source_group_id`),
  CONSTRAINT `c_expense_category_groups_ibfk_1` FOREIGN KEY (`account_id`) REFERENCES `credit_accounts` (`id`) ON DELETE CASCADE,
  CONSTRAINT `c_expense_category_groups_ibfk_2` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE,
  CONSTRAINT `c_expense_category_groups_ibfk_3` FOREIGN KEY (`source_group_id`) REFERENCES `expense_category_groups` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=20 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `c_expense_entries` (
  `id` int NOT NULL AUTO_INCREMENT,
  `category_id` int DEFAULT NULL,
  `date` date DEFAULT NULL,
  `original_date` date DEFAULT NULL,
  `amount` decimal(15,2) DEFAULT NULL,
  `recurring_id` bigint DEFAULT NULL,
  `is_bucket` tinyint(1) DEFAULT '0',
  `original_amount` decimal(15,2) DEFAULT NULL,
  `processed` tinyint(1) DEFAULT '0',
  `auto_confirmed` tinyint(1) DEFAULT '0',
  `is_auto_adjustment` tinyint(1) NOT NULL DEFAULT '0',
  `pending` tinyint(1) DEFAULT '0',
  `bundle_item_id` int DEFAULT NULL,
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `category_id` (`category_id`),
  KEY `bundle_item_id` (`bundle_item_id`),
  KEY `idx_c_expense_entries_category_date` (`category_id`,`date`),
  KEY `idx_bucket_category_date` (`is_bucket`,`category_id`,`date`),
  KEY `idx_pending` (`pending`),
  KEY `idx_c_expense_entries_auto_confirmed` (`auto_confirmed`),
  CONSTRAINT `c_expense_entries_ibfk_1` FOREIGN KEY (`category_id`) REFERENCES `c_expense_categories` (`id`) ON DELETE CASCADE,
  CONSTRAINT `c_expense_entries_ibfk_bundle_item` FOREIGN KEY (`bundle_item_id`) REFERENCES `bundle_items` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=981768 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `c_payment_entries` (
  `id` int NOT NULL AUTO_INCREMENT,
  `account_id` int NOT NULL,
  `date` date DEFAULT NULL,
  `amount` decimal(15,2) DEFAULT NULL,
  `recurring_id` bigint DEFAULT NULL,
  `processed` tinyint(1) DEFAULT '0',
  `auto_confirmed` tinyint(1) DEFAULT '0',
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `is_auto_adjustment` tinyint(1) NOT NULL DEFAULT '0',
  PRIMARY KEY (`id`),
  KEY `account_id` (`account_id`),
  KEY `recurring_id` (`recurring_id`),
  KEY `idx_c_payment_entries_account_date` (`account_id`,`date`),
  KEY `idx_c_payment_entries_auto_confirmed` (`auto_confirmed`),
  CONSTRAINT `c_payment_entries_ibfk_account` FOREIGN KEY (`account_id`) REFERENCES `credit_accounts` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=857616 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `category_memory` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `merchant_id` varchar(255) DEFAULT NULL,
  `description` varchar(500) DEFAULT NULL,
  `category_id` int NOT NULL,
  `category_type` varchar(20) NOT NULL DEFAULT 'outgoing',
  `account_id` int DEFAULT NULL,
  `times_confirmed` int DEFAULT '1',
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_user_desc_type` (`user_id`,`description`(191),`category_type`),
  KEY `idx_merchant` (`user_id`,`merchant_id`),
  CONSTRAINT `category_memory_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=13 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `credit_accounts` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `name` varchar(255) NOT NULL,
  `mask` varchar(100) DEFAULT NULL,
  `linked_account_id` varchar(255) DEFAULT NULL,
  `interest_rate` decimal(5,2) DEFAULT NULL,
  `starting_balance` decimal(15,2) DEFAULT NULL,
  `is_card` tinyint(1) NOT NULL DEFAULT '0',
  `is_line` tinyint(1) NOT NULL DEFAULT '0',
  `is_linked` tinyint(1) NOT NULL DEFAULT '0',
  `display_order` int NOT NULL DEFAULT '0',
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `idx_credit_accounts_user_linked` (`user_id`,`linked_account_id`),
  KEY `user_id` (`user_id`),
  KEY `idx_credit_accounts_user_mask` (`user_id`,`mask`),
  KEY `idx_credit_accounts_is_linked` (`is_linked`),
  KEY `idx_credit_accounts_linked_account_id` (`linked_account_id`),
  CONSTRAINT `credit_accounts_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=334 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `device_tokens` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `device_token` varchar(255) NOT NULL,
  `platform` varchar(20) DEFAULT 'ios',
  `device_info` json DEFAULT NULL,
  `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_device_token` (`device_token`),
  KEY `idx_user_platform` (`user_id`,`platform`),
  CONSTRAINT `device_tokens_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `expense_categories` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int DEFAULT NULL,
  `name` varchar(255) DEFAULT NULL,
  `display_order` decimal(8,4) DEFAULT '0.0000',
  `group_id` int DEFAULT NULL,
  `is_recurring` tinyint(1) DEFAULT '0',
  `is_auto_adjustment` tinyint(1) NOT NULL DEFAULT '0',
  `no_end_date` tinyint(1) DEFAULT '0',
  `hidden` tinyint(1) DEFAULT '0',
  `is_bundle` tinyint(1) NOT NULL DEFAULT '0',
  `is_credit_account` tinyint(1) NOT NULL DEFAULT '0',
  `is_system` tinyint(1) NOT NULL DEFAULT '0',
  `credit_account_id` int DEFAULT NULL,
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `is_savings` tinyint(1) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `idx_expense_categories_user_savings` (`user_id`,`is_savings`),
  KEY `fk_expense_group` (`group_id`),
  KEY `expense_categories_ibfk_1` (`user_id`),
  KEY `idx_expense_categories_user_display` (`user_id`,`display_order`),
  KEY `idx_expense_categories_credit_account_id` (`credit_account_id`),
  CONSTRAINT `expense_categories_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_expense_group` FOREIGN KEY (`group_id`) REFERENCES `expense_category_groups` (`id`) ON DELETE SET NULL
) ENGINE=InnoDB AUTO_INCREMENT=1687 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `expense_category_groups` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int DEFAULT NULL,
  `name` varchar(255) DEFAULT NULL,
  `display_order` decimal(8,4) DEFAULT '0.0000',
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `expense_category_groups_ibfk_1` (`user_id`),
  CONSTRAINT `expense_category_groups_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=16 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `expense_entries` (
  `id` int NOT NULL AUTO_INCREMENT,
  `category_id` int DEFAULT NULL,
  `date` date DEFAULT NULL,
  `original_date` date DEFAULT NULL,
  `amount` decimal(15,2) DEFAULT NULL,
  `recurring_id` bigint DEFAULT NULL,
  `is_bucket` tinyint(1) DEFAULT '0',
  `original_amount` decimal(15,2) DEFAULT NULL,
  `processed` tinyint(1) DEFAULT '0',
  `auto_confirmed` tinyint(1) DEFAULT '0',
  `is_auto_adjustment` tinyint(1) NOT NULL DEFAULT '0',
  `pending` tinyint(1) DEFAULT '0',
  `bundle_item_id` int DEFAULT NULL,
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `expense_entries_ibfk_1` (`category_id`),
  KEY `bundle_item_id` (`bundle_item_id`),
  KEY `idx_expense_entries_category_date` (`category_id`,`date`),
  KEY `idx_expense_entries_date` (`date`),
  KEY `idx_bucket_category_date` (`is_bucket`,`category_id`,`date`),
  KEY `idx_pending` (`pending`),
  KEY `idx_expense_entries_auto_confirmed` (`auto_confirmed`),
  CONSTRAINT `expense_entries_ibfk_1` FOREIGN KEY (`category_id`) REFERENCES `expense_categories` (`id`) ON DELETE CASCADE,
  CONSTRAINT `expense_entries_ibfk_bundle_item` FOREIGN KEY (`bundle_item_id`) REFERENCES `bundle_items` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=210976419 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `income_categories` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int DEFAULT NULL,
  `name` varchar(255) DEFAULT NULL,
  `display_order` decimal(8,4) DEFAULT '0.0000',
  `group_id` int DEFAULT NULL,
  `is_recurring` tinyint(1) DEFAULT '0',
  `is_auto_adjustment` tinyint(1) NOT NULL DEFAULT '0',
  `no_end_date` tinyint(1) DEFAULT '0',
  `hidden` tinyint(1) DEFAULT '0',
  `is_system` tinyint(1) NOT NULL DEFAULT '0',
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `is_savings` tinyint(1) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `idx_income_categories_user_savings` (`user_id`,`is_savings`),
  KEY `fk_income_group` (`group_id`),
  KEY `income_categories_ibfk_1` (`user_id`),
  KEY `idx_income_categories_user_display` (`user_id`,`display_order`),
  CONSTRAINT `fk_income_group` FOREIGN KEY (`group_id`) REFERENCES `income_category_groups` (`id`) ON DELETE SET NULL,
  CONSTRAINT `income_categories_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=1615 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `income_category_groups` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int DEFAULT NULL,
  `name` varchar(255) DEFAULT NULL,
  `display_order` decimal(8,4) DEFAULT '0.0000',
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `income_category_groups_ibfk_1` (`user_id`),
  CONSTRAINT `income_category_groups_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=8 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `income_entries` (
  `id` int NOT NULL AUTO_INCREMENT,
  `category_id` int DEFAULT NULL,
  `date` date DEFAULT NULL,
  `original_date` date DEFAULT NULL,
  `amount` decimal(15,2) DEFAULT NULL,
  `recurring_id` bigint DEFAULT NULL,
  `is_bucket` tinyint(1) DEFAULT '0',
  `original_amount` decimal(15,2) DEFAULT NULL,
  `processed` tinyint(1) DEFAULT '0',
  `auto_confirmed` tinyint(1) DEFAULT '0',
  `is_auto_adjustment` tinyint(1) NOT NULL DEFAULT '0',
  `pending` tinyint(1) DEFAULT '0',
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `income_entries_ibfk_1` (`category_id`),
  KEY `idx_income_entries_category_date` (`category_id`,`date`),
  KEY `idx_income_entries_date` (`date`),
  KEY `idx_bucket_category_date` (`is_bucket`,`category_id`,`date`),
  KEY `idx_pending` (`pending`),
  KEY `idx_income_entries_auto_confirmed` (`auto_confirmed`),
  CONSTRAINT `income_entries_ibfk_1` FOREIGN KEY (`category_id`) REFERENCES `income_categories` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=4796596 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `instance_settings` (
  `id` tinyint NOT NULL DEFAULT '1',
  `smtp_server` varchar(255) DEFAULT NULL,
  `smtp_port` int DEFAULT NULL,
  `smtp_username` varchar(255) DEFAULT NULL,
  `smtp_password_encrypted` text COMMENT 'Fernet token, never a plaintext password',
  `from_email` varchar(255) DEFAULT NULL COMMENT 'Also the notification recipient',
  `use_tls` tinyint(1) NOT NULL DEFAULT '1',
  `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `verified_at` datetime DEFAULT NULL COMMENT 'When the configured address last confirmed a code',
  `verified_fingerprint` char(64) DEFAULT NULL COMMENT 'SHA-256 of the proven transport; deliberately excludes the password',
  `verification_secret` varchar(64) DEFAULT NULL COMMENT 'pyotp base32 secret for the pending code, never the code itself',
  `verification_sent_at` datetime DEFAULT NULL,
  `verification_attempts` int NOT NULL DEFAULT '0' COMMENT 'Failed attempts against the current secret; caps brute force',
  PRIMARY KEY (`id`),
  CONSTRAINT `instance_settings_single_row` CHECK ((`id` = 1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `linked_accounts` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `connection_id` int NOT NULL,
  `account_id` varchar(255) NOT NULL,
  `account_name` varchar(255) DEFAULT NULL,
  `alias` varchar(255) DEFAULT NULL,
  `account_type` varchar(50) DEFAULT NULL,
  `account_subtype` varchar(50) DEFAULT NULL,
  `mask` varchar(100) DEFAULT NULL,
  `current_balance` decimal(15,2) DEFAULT NULL,
  `available_balance` decimal(15,2) DEFAULT NULL,
  `currency` varchar(3) DEFAULT 'USD',
  `is_active` tinyint(1) DEFAULT '1',
  `sync_transactions` tinyint(1) DEFAULT '1',
  `metadata` json DEFAULT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `interest_rate` decimal(5,2) DEFAULT NULL COMMENT 'Annual interest rate percentage',
  `origination_principal` decimal(12,2) DEFAULT NULL COMMENT 'Original loan amount',
  `origination_date` date DEFAULT NULL COMMENT 'Date loan was originated',
  `maturity_date` date DEFAULT NULL COMMENT 'Date loan matures/final payment',
  `loan_term` int DEFAULT NULL COMMENT 'Loan term in months',
  `last_payment_date` date DEFAULT NULL COMMENT 'Date of last payment',
  `last_payment_amount` decimal(12,2) DEFAULT NULL COMMENT 'Amount of last payment',
  `next_payment_due_date` date DEFAULT NULL COMMENT 'Next payment due date',
  `minimum_payment_amount` decimal(12,2) DEFAULT NULL COMMENT 'Minimum payment amount',
  `next_payment_minimum_amount` decimal(12,2) DEFAULT NULL COMMENT 'Next minimum payment amount',
  `payment_frequency` varchar(50) DEFAULT NULL COMMENT 'Payment frequency (monthly, biweekly, etc)',
  `account_state` varchar(50) DEFAULT NULL COMMENT 'Account state (active, paid off, closed, etc)',
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_user_account` (`user_id`,`account_id`),
  KEY `connection_id` (`connection_id`),
  KEY `idx_account_id` (`account_id`),
  KEY `idx_account_type` (`account_type`),
  KEY `idx_account_type_subtype` (`account_type`,`account_subtype`),
  KEY `idx_interest_rate` (`interest_rate`),
  KEY `idx_maturity_date` (`maturity_date`),
  KEY `idx_next_payment_due` (`next_payment_due_date`),
  CONSTRAINT `linked_accounts_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE,
  CONSTRAINT `linked_accounts_ibfk_2` FOREIGN KEY (`connection_id`) REFERENCES `linked_connections` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=6633 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `linked_connections` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `connection_id` varchar(255) NOT NULL,
  `institution_name` varchar(255) DEFAULT NULL,
  `institution_id` varchar(255) DEFAULT NULL,
  `status` varchar(50) DEFAULT NULL,
  `last_synced_at` datetime DEFAULT NULL,
  `error_code` varchar(100) DEFAULT NULL,
  `metadata` json DEFAULT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_user_connection` (`user_id`,`connection_id`),
  KEY `idx_connection_id` (`connection_id`),
  KEY `idx_status` (`status`),
  CONSTRAINT `linked_connections_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=1709 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `linked_provider_profiles` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `provider` varchar(32) NOT NULL DEFAULT '',
  `provider_ref` varchar(255) NOT NULL,
  `metadata` json DEFAULT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_user_profile` (`user_id`),
  KEY `idx_provider_ref` (`provider_ref`),
  CONSTRAINT `linked_provider_profiles_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=505 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `linked_transactions` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `account_id` varchar(255) NOT NULL,
  `transaction_id` varchar(255) NOT NULL,
  `amount` decimal(15,2) DEFAULT NULL,
  `date` date NOT NULL,
  `description` text,
  `merchant_name` varchar(255) DEFAULT NULL,
  `category` varchar(100) DEFAULT NULL,
  `enrichment_labels` json DEFAULT NULL,
  `enrichment_merchant_id` varchar(255) DEFAULT NULL,
  `enrichment_logo` text,
  `enrichment_website` varchar(500) DEFAULT NULL,
  `enrichment_mcc` json DEFAULT NULL,
  `enrichment_location` text,
  `enrichment_location_city` varchar(100) DEFAULT NULL,
  `enrichment_location_state` varchar(50) DEFAULT NULL,
  `enrichment_location_country` varchar(50) DEFAULT NULL,
  `enrichment_recurrence` varchar(50) DEFAULT NULL,
  `enrichment_recurrence_group_id` varchar(255) DEFAULT NULL,
  `enrichment_periodicity` varchar(50) DEFAULT NULL,
  `enrichment_periodicity_days` decimal(10,2) DEFAULT NULL,
  `enrichment_avg_amount` decimal(15,2) DEFAULT NULL,
  `enrichment_first_payment_date` date DEFAULT NULL,
  `enrichment_last_payment_date` date DEFAULT NULL,
  `enrichment_person` varchar(255) DEFAULT NULL,
  `enrichment_transaction_type` varchar(50) DEFAULT NULL,
  `enriched_at` datetime DEFAULT NULL,
  `transaction_type` varchar(50) DEFAULT NULL,
  `pending` tinyint(1) DEFAULT '0',
  `imported_to_entry_id` int DEFAULT NULL,
  `imported_entry_type` enum('income','expense','c_expense','c_payment') DEFAULT NULL,
  `imported_at` datetime DEFAULT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `custom_category_suggestion` varchar(255) DEFAULT NULL COMMENT 'Category name from our custom Ntropy categories',
  `custom_category_id` int DEFAULT NULL COMMENT 'Matched category_id in user categories',
  `custom_category_type` enum('outgoing','incoming') DEFAULT NULL,
  `custom_category_confidence` enum('high','medium','low') DEFAULT NULL,
  `custom_suggestion_at` datetime DEFAULT NULL COMMENT 'When the custom suggestion was generated',
  `provider_created_date` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_transaction` (`user_id`,`transaction_id`),
  KEY `idx_transaction_id` (`transaction_id`),
  KEY `idx_date` (`date`),
  KEY `idx_pending` (`pending`),
  KEY `idx_imported` (`imported_to_entry_id`),
  KEY `idx_account_id` (`account_id`),
  KEY `idx_user_account` (`user_id`,`account_id`),
  KEY `idx_enrichment_merchant_id` (`enrichment_merchant_id`),
  KEY `idx_enrichment_recurrence` (`enrichment_recurrence`),
  KEY `idx_enrichment_recurrence_group` (`enrichment_recurrence_group_id`),
  KEY `idx_linked_transactions_imported` (`imported_to_entry_id`,`imported_entry_type`),
  KEY `idx_custom_suggestion` (`user_id`,`custom_suggestion_at`),
  CONSTRAINT `fk_transactions_account` FOREIGN KEY (`account_id`) REFERENCES `linked_accounts` (`account_id`) ON DELETE CASCADE,
  CONSTRAINT `linked_transactions_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=36951 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `notifications` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `date` datetime DEFAULT CURRENT_TIMESTAMP,
  `message` text NOT NULL,
  `is_read` tinyint(1) DEFAULT '0',
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `user_id` (`user_id`),
  KEY `idx_user_date` (`user_id`,`date`),
  CONSTRAINT `notifications_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=1119 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `password_resets` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `token` varchar(255) NOT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `expires_at` datetime NOT NULL,
  `used` tinyint(1) DEFAULT '0',
  PRIMARY KEY (`id`),
  UNIQUE KEY `token` (`token`),
  KEY `user_id` (`user_id`),
  KEY `idx_token_expiry` (`token`,`expires_at`,`used`),
  CONSTRAINT `password_resets_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=9 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `recurring_c_expense` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `category_id` int NOT NULL,
  `amount` decimal(15,2) DEFAULT NULL,
  `cadence_interval` int NOT NULL DEFAULT '1',
  `cadence_unit` enum('days','weeks','months','years') NOT NULL DEFAULT 'days',
  `start_date` date DEFAULT NULL,
  `end_date` date DEFAULT NULL,
  `weekdays` varchar(255) DEFAULT NULL,
  `monthly_days` varchar(255) DEFAULT NULL,
  `yearly_day` int DEFAULT NULL,
  `yearly_month` int DEFAULT NULL,
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `wage_bill` tinyint(1) NOT NULL DEFAULT '0',
  PRIMARY KEY (`id`),
  KEY `recurring_c_expense_ibfk_2` (`category_id`),
  KEY `recurring_c_expense_ibfk_1` (`user_id`),
  CONSTRAINT `recurring_c_expense_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE,
  CONSTRAINT `recurring_c_expense_ibfk_2` FOREIGN KEY (`category_id`) REFERENCES `c_expense_categories` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=75 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `recurring_c_expense_buckets` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `account_id` int NOT NULL,
  `category_id` int NOT NULL,
  `bucket_date` date NOT NULL,
  `amount` decimal(15,2) DEFAULT NULL,
  `original_amount` decimal(15,2) DEFAULT NULL,
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_category_date` (`category_id`,`bucket_date`),
  KEY `user_id` (`user_id`),
  KEY `account_id` (`account_id`),
  KEY `category_id` (`category_id`),
  CONSTRAINT `recurring_c_expense_buckets_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE,
  CONSTRAINT `recurring_c_expense_buckets_ibfk_2` FOREIGN KEY (`account_id`) REFERENCES `credit_accounts` (`id`) ON DELETE CASCADE,
  CONSTRAINT `recurring_c_expense_buckets_ibfk_3` FOREIGN KEY (`category_id`) REFERENCES `c_expense_categories` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=1903 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `recurring_expense` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `category_id` int NOT NULL,
  `amount` decimal(15,2) DEFAULT NULL,
  `cadence_interval` int NOT NULL DEFAULT '1',
  `cadence_unit` enum('days','weeks','months','years') NOT NULL DEFAULT 'days',
  `start_date` date DEFAULT NULL,
  `end_date` date DEFAULT NULL,
  `weekdays` varchar(255) DEFAULT NULL,
  `monthly_days` varchar(255) DEFAULT NULL,
  `yearly_day` int DEFAULT NULL,
  `yearly_month` int DEFAULT NULL,
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `wage_bill` tinyint(1) NOT NULL DEFAULT '0',
  PRIMARY KEY (`id`),
  KEY `recurring_expense_ibfk_2` (`category_id`),
  KEY `idx_recurring_expense_user_category` (`user_id`,`category_id`),
  CONSTRAINT `recurring_expense_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=905 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `recurring_expense_buckets` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `category_id` int NOT NULL,
  `bucket_date` date NOT NULL,
  `amount` decimal(15,2) DEFAULT NULL,
  `original_amount` decimal(15,2) DEFAULT NULL,
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_category_date` (`category_id`,`bucket_date`),
  KEY `user_id` (`user_id`),
  KEY `category_id` (`category_id`),
  CONSTRAINT `recurring_expense_buckets_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE,
  CONSTRAINT `recurring_expense_buckets_ibfk_2` FOREIGN KEY (`category_id`) REFERENCES `expense_categories` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=83251 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `recurring_income` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `category_id` int NOT NULL,
  `amount` decimal(15,2) DEFAULT NULL,
  `end_date` date DEFAULT NULL,
  `start_date` date DEFAULT NULL,
  `cadence_interval` int NOT NULL DEFAULT '1',
  `cadence_unit` enum('days','weeks','months','years') NOT NULL DEFAULT 'days',
  `weekdays` varchar(255) DEFAULT NULL,
  `monthly_day` int DEFAULT NULL,
  `yearly_day` int DEFAULT NULL,
  `yearly_month` int DEFAULT NULL,
  `monthly_days` varchar(255) DEFAULT NULL,
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `wage_bill` tinyint(1) NOT NULL DEFAULT '0',
  PRIMARY KEY (`id`),
  KEY `category_id` (`category_id`),
  KEY `idx_recurring_income_user_category` (`user_id`,`category_id`),
  CONSTRAINT `recurring_income_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=475 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `recurring_income_buckets` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `category_id` int NOT NULL,
  `bucket_date` date NOT NULL,
  `amount` decimal(15,2) DEFAULT NULL,
  `original_amount` decimal(15,2) DEFAULT NULL,
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_category_date` (`category_id`,`bucket_date`),
  KEY `user_id` (`user_id`),
  KEY `category_id` (`category_id`),
  CONSTRAINT `recurring_income_buckets_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE,
  CONSTRAINT `recurring_income_buckets_ibfk_2` FOREIGN KEY (`category_id`) REFERENCES `income_categories` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=9263 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `recurring_mismatches` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `recurring_table` enum('recurring_income','recurring_expense','recurring_c_expense') NOT NULL,
  `recurring_id` int NOT NULL,
  `category_id` int NOT NULL,
  `transaction_id` varchar(255) NOT NULL COMMENT 'quiltt_transactions.transaction_id that triggered detection',
  `dismissed` tinyint(1) NOT NULL DEFAULT '0',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_mismatch` (`user_id`,`recurring_table`,`recurring_id`),
  KEY `idx_user_dismissed` (`user_id`,`dismissed`),
  CONSTRAINT `recurring_mismatches_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `recurring_suggestions` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `suggestion_type` enum('recurring_income','recurring_expense','recurring_c_expense') NOT NULL,
  `category_id` int NOT NULL,
  `transaction_id` varchar(255) NOT NULL COMMENT 'quiltt_transactions.transaction_id that triggered suggestion',
  `detected_amount` decimal(12,2) NOT NULL,
  `detected_cadence_interval` int DEFAULT NULL,
  `detected_cadence_unit` varchar(20) DEFAULT NULL COMMENT 'days, weeks, months, years',
  `detected_weekday` varchar(20) DEFAULT NULL COMMENT 'e.g. thursday',
  `detected_monthly_day` int DEFAULT NULL COMMENT 'e.g. 15',
  `dismissed` tinyint(1) NOT NULL DEFAULT '0',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_user_type_cat` (`user_id`,`suggestion_type`,`category_id`),
  KEY `idx_user_dismissed` (`user_id`,`dismissed`),
  CONSTRAINT `recurring_suggestions_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `savings_adjustments` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `date` date NOT NULL,
  `amount` decimal(15,2) DEFAULT NULL,
  `description` varchar(255) DEFAULT NULL,
  `linked_account_id` varchar(255) DEFAULT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_user_date` (`user_id`,`date`),
  CONSTRAINT `savings_adjustments_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=1638 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `savings_entries` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `date` date DEFAULT NULL,
  `amount` decimal(15,2) DEFAULT NULL,
  `processed` tinyint(1) DEFAULT '0',
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_user_date` (`user_id`,`date`),
  CONSTRAINT `savings_entries_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=11137400 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `setup_state` (
  `user_id` int NOT NULL,
  `state` json DEFAULT NULL,
  `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`user_id`),
  CONSTRAINT `setup_state_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `starting_balance` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `amount` decimal(15,2) DEFAULT NULL,
  `date` date NOT NULL,
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `user_id` (`user_id`),
  CONSTRAINT `starting_balance_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=4 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `totals_remainders` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `date` date NOT NULL,
  `total_income` decimal(15,2) DEFAULT NULL,
  `total_expenses` decimal(15,2) DEFAULT NULL,
  `remainder` decimal(15,2) DEFAULT NULL,
  `last_week_remainder` decimal(15,2) DEFAULT NULL,
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `user_id` (`user_id`,`date`),
  KEY `idx_totals_remainders_user_date` (`user_id`,`date`),
  CONSTRAINT `totals_remainders_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=4575295 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `totals_remainders_d` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `date` date NOT NULL,
  `total_income` decimal(15,2) DEFAULT NULL,
  `total_expenses` decimal(15,2) DEFAULT NULL,
  `remainder` decimal(15,2) DEFAULT NULL,
  `last_day_remainder` decimal(15,2) DEFAULT NULL,
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_user_date` (`user_id`,`date`),
  CONSTRAINT `totals_remainders_d_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=16571165 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `totals_remainders_m` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `date` date NOT NULL,
  `total_income` decimal(15,2) DEFAULT NULL,
  `total_expenses` decimal(15,2) DEFAULT NULL,
  `remainder` decimal(15,2) DEFAULT NULL,
  `last_month_remainder` decimal(15,2) DEFAULT NULL,
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `user_id` (`user_id`,`date`),
  CONSTRAINT `totals_remainders_m_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=359993 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `users` (
  `id` int NOT NULL AUTO_INCREMENT,
  `username` varchar(100) DEFAULT NULL,
  `first_name` varchar(100) DEFAULT NULL,
  `last_name` varchar(100) DEFAULT NULL,
  `email` varchar(100) DEFAULT NULL,
  `password` varchar(255) NOT NULL,
  `profile_picture` varchar(255) DEFAULT NULL,
  `balance_threshold` decimal(15,2) DEFAULT NULL,
  `goofy_week_mode` tinyint(1) DEFAULT '0',
  `member_since` date DEFAULT NULL,
  `landing_page` varchar(50) DEFAULT 'dashboard_3m',
  `starting_savings` decimal(15,2) DEFAULT NULL,
  `currency_type` varchar(3) DEFAULT 'USD',
  `mfa_secret` varchar(32) DEFAULT NULL,
  `email_notifications` tinyint(1) DEFAULT '0',
  `bank_sync_enabled` tinyint(1) DEFAULT '0',
  `bank_auto_import` tinyint(1) DEFAULT '1',
  `setup_step` tinyint DEFAULT '0',
  `last_modified` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `completed_tutorials` text,
  `is_admin` tinyint(1) NOT NULL DEFAULT '0' COMMENT 'Exactly one administrator: the first account created',
  PRIMARY KEY (`id`),
  KEY `idx_users_username` (`username`)
) ENGINE=InnoDB AUTO_INCREMENT=335 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ENCRYPTION='Y';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;

