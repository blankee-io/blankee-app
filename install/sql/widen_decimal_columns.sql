-- Migration: Widen DECIMAL amount columns from (10,2)/(12,2) to (15,2)
-- Max value: 9,999,999,999,999.99 (~$10 trillion)
-- Storage: 7 bytes per value (was 5 bytes for DECIMAL(10,2))
-- Date: 2026-04-18

-- Income entries
ALTER TABLE income_entries MODIFY COLUMN amount DECIMAL(15,2) DEFAULT NULL;
ALTER TABLE income_entries MODIFY COLUMN original_amount DECIMAL(15,2) DEFAULT NULL;

-- Expense entries
ALTER TABLE expense_entries MODIFY COLUMN amount DECIMAL(15,2) DEFAULT NULL;
ALTER TABLE expense_entries MODIFY COLUMN original_amount DECIMAL(15,2) DEFAULT NULL;

-- Credit expense entries
ALTER TABLE c_expense_entries MODIFY COLUMN amount DECIMAL(15,2) DEFAULT NULL;
ALTER TABLE c_expense_entries MODIFY COLUMN original_amount DECIMAL(15,2) DEFAULT NULL;

-- Credit payment entries
ALTER TABLE c_payment_entries MODIFY COLUMN amount DECIMAL(15,2) DEFAULT NULL;

-- Credit accounts
ALTER TABLE credit_accounts MODIFY COLUMN starting_balance DECIMAL(15,2) DEFAULT NULL;

-- Recurring income
ALTER TABLE recurring_income MODIFY COLUMN amount DECIMAL(15,2) DEFAULT NULL;

-- Recurring expense
ALTER TABLE recurring_expense MODIFY COLUMN amount DECIMAL(15,2) DEFAULT NULL;

-- Recurring credit expense
ALTER TABLE recurring_c_expense MODIFY COLUMN amount DECIMAL(15,2) DEFAULT NULL;

-- Recurring income buckets
ALTER TABLE recurring_income_buckets MODIFY COLUMN amount DECIMAL(15,2) DEFAULT NULL;
ALTER TABLE recurring_income_buckets MODIFY COLUMN original_amount DECIMAL(15,2) DEFAULT NULL;

-- Recurring expense buckets
ALTER TABLE recurring_expense_buckets MODIFY COLUMN amount DECIMAL(15,2) DEFAULT NULL;
ALTER TABLE recurring_expense_buckets MODIFY COLUMN original_amount DECIMAL(15,2) DEFAULT NULL;

-- Recurring credit expense buckets
ALTER TABLE recurring_c_expense_buckets MODIFY COLUMN amount DECIMAL(15,2) DEFAULT NULL;
ALTER TABLE recurring_c_expense_buckets MODIFY COLUMN original_amount DECIMAL(15,2) DEFAULT NULL;

-- Starting balance
ALTER TABLE starting_balance MODIFY COLUMN amount DECIMAL(15,2) DEFAULT NULL;

-- Savings
ALTER TABLE savings_entries MODIFY COLUMN amount DECIMAL(15,2) DEFAULT NULL;
ALTER TABLE savings_adjustments MODIFY COLUMN amount DECIMAL(15,2) DEFAULT NULL;

-- Totals/remainders (weekly)
ALTER TABLE totals_remainders MODIFY COLUMN total_income DECIMAL(15,2) DEFAULT NULL;
ALTER TABLE totals_remainders MODIFY COLUMN total_expenses DECIMAL(15,2) DEFAULT NULL;
ALTER TABLE totals_remainders MODIFY COLUMN remainder DECIMAL(15,2) DEFAULT NULL;
ALTER TABLE totals_remainders MODIFY COLUMN last_week_remainder DECIMAL(15,2) DEFAULT NULL;

-- Totals/remainders (daily)
ALTER TABLE totals_remainders_d MODIFY COLUMN total_income DECIMAL(15,2) DEFAULT NULL;
ALTER TABLE totals_remainders_d MODIFY COLUMN total_expenses DECIMAL(15,2) DEFAULT NULL;
ALTER TABLE totals_remainders_d MODIFY COLUMN remainder DECIMAL(15,2) DEFAULT NULL;
ALTER TABLE totals_remainders_d MODIFY COLUMN last_day_remainder DECIMAL(15,2) DEFAULT NULL;

-- Totals/remainders (monthly)
ALTER TABLE totals_remainders_m MODIFY COLUMN total_income DECIMAL(15,2) DEFAULT NULL;
ALTER TABLE totals_remainders_m MODIFY COLUMN total_expenses DECIMAL(15,2) DEFAULT NULL;
ALTER TABLE totals_remainders_m MODIFY COLUMN remainder DECIMAL(15,2) DEFAULT NULL;
ALTER TABLE totals_remainders_m MODIFY COLUMN last_month_remainder DECIMAL(15,2) DEFAULT NULL;

-- Credit account balances (weekly)
ALTER TABLE c_a_balances MODIFY COLUMN total_expenses DECIMAL(15,2) DEFAULT NULL;
ALTER TABLE c_a_balances MODIFY COLUMN balance DECIMAL(15,2) DEFAULT NULL;
ALTER TABLE c_a_balances MODIFY COLUMN total_payments DECIMAL(15,2) DEFAULT NULL;

-- Credit account balances (daily)
ALTER TABLE c_a_balances_d MODIFY COLUMN total_expenses DECIMAL(15,2) DEFAULT NULL;
ALTER TABLE c_a_balances_d MODIFY COLUMN balance DECIMAL(15,2) DEFAULT NULL;
ALTER TABLE c_a_balances_d MODIFY COLUMN total_payments DECIMAL(15,2) DEFAULT NULL;

-- Credit account balances (monthly)
ALTER TABLE c_a_balances_m MODIFY COLUMN total_expenses DECIMAL(15,2) DEFAULT NULL;
ALTER TABLE c_a_balances_m MODIFY COLUMN balance DECIMAL(15,2) DEFAULT NULL;
ALTER TABLE c_a_balances_m MODIFY COLUMN total_payments DECIMAL(15,2) DEFAULT NULL;

-- Bud items
ALTER TABLE bud_items MODIFY COLUMN value DECIMAL(15,2) DEFAULT NULL;

-- Quiltt
ALTER TABLE quiltt_accounts MODIFY COLUMN current_balance DECIMAL(15,2) DEFAULT NULL;
ALTER TABLE quiltt_accounts MODIFY COLUMN available_balance DECIMAL(15,2) DEFAULT NULL;
ALTER TABLE quiltt_transactions MODIFY COLUMN amount DECIMAL(15,2) DEFAULT NULL;
ALTER TABLE quiltt_transactions MODIFY COLUMN ntropy_avg_amount DECIMAL(15,2) DEFAULT NULL;

-- Users
ALTER TABLE users MODIFY COLUMN balance_threshold DECIMAL(15,2) DEFAULT NULL;
ALTER TABLE users MODIFY COLUMN starting_savings DECIMAL(15,2) DEFAULT NULL;
