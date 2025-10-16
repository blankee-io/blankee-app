-- High Priority Indexes (Execute these first)
CREATE INDEX idx_income_entries_category_date ON income_entries(category_id, date);
CREATE INDEX idx_expense_entries_category_date ON expense_entries(category_id, date);
CREATE INDEX idx_income_entries_date ON income_entries(date);
CREATE INDEX idx_expense_entries_date ON expense_entries(date);
CREATE INDEX idx_totals_remainders_user_date ON totals_remainders(user_id, date);
CREATE INDEX idx_c_expense_entries_category_date ON c_expense_entries(category_id, date);

-- Medium Priority Indexes
CREATE INDEX idx_income_categories_user_display ON income_categories(user_id, display_order);
CREATE INDEX idx_expense_categories_user_display ON expense_categories(user_id, display_order);
CREATE INDEX idx_c_payment_entries_account_date ON c_payment_entries(account_id, date);

-- Lower Priority But Still Beneficial
CREATE INDEX idx_recurring_income_user_category ON recurring_income(user_id, category_id);
CREATE INDEX idx_recurring_expense_user_category ON recurring_expense(user_id, category_id);
CREATE INDEX idx_users_username ON users(username);