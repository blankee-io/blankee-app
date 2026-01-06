# Category Recommendations - Implementation Checklist

**Feature Overview**: During profile setup, provide users with a set of generic starter categories that they can customize before their profile is created. These categories include both regular and recurring types with sensible defaults.

**Current Date Started**: January 2, 2026
**Phase 1 Completed**: January 3, 2026
**Phase 2 Updated**: January 4, 2026 - Changed from Ntropy-based to static generic categories

**IMPORTANT ARCHITECTURE DECISION (January 4, 2026)**:
> Ntropy enrichment data is NOT immediately available after bank connection. Per Quiltt docs:
> "You can access the latest enriched Ntropy data for a given Connection after the `connection.synced.successful` Webhook event has been fired"
>
> **Solution**: Use static generic categories during profile setup. Ntropy data will be used later for:
> - Transaction auto-categorization (post-setup)
> - Smart category suggestions based on spending patterns
> - Recurring transaction detection after webhook confirms data is ready

---

## Phase 1: UI Development ✅ COMPLETE (User Approved)

### 1.1 Create Modal Structure ✅ DONE
- [x] Add "Category Recommendations" modal to setup_profile.html
- [x] Position in flow: After account selection, before name modal
- [x] Loading screen: "Analyzing your transactions..." (now shows briefly before static categories)
- [x] Skip button text: "I want to create my own categories"
- [x] Page refresh detection: Returns to category recommendations if user hasn't entered name yet
- [x] Modal fits in viewport (accounts for 50px nav-bar, scrollable content)

### 1.2 Recommendation Display Structure ✅ DONE
- [x] **Table layout** (matching recurring_i.html and recurring_e.html)
- [x] Table columns: Category Name | Amount | Cadence | Actions (edit/delete buttons)
- [x] Shows "—" for non-recurring categories in Amount and Cadence columns
- [x] Separate tables for Income and Expense sections
- [x] Scrollable content area (not full window scroll)
- [x] Edit button (pencil icon) for each category
- [x] Delete button (X icon) for each category
- [x] Section headers styled like recurring pages (teal for income, orange for expenses)
- [x] "Add Category" buttons (functional in Phase 2)

### 1.3 Cadence Editor UI ✅ DONE
- [x] Cadence format: "Every X day(s)", "Every X week(s)", "Every X month(s) on days Y"
- [x] Matches backend `get_cadence_description()` exactly
- [x] Supports: cadence_interval, cadence_unit, monthly_days, weekdays
- [x] Edit function allows changing cadence via prompts

### 1.4 Static Starter Categories ✅ DONE (Updated Jan 4, 2026)

**Income:**
| Category | Recurring | Amount | Cadence |
|----------|-----------|--------|---------|
| Wages | Yes | $1,500 | Every 2 weeks on Friday |
| Variable | No | — | — |

**Expenses:**
| Category | Recurring | Amount | Cadence |
|----------|-----------|--------|---------|
| Housing | Yes | $1,200 | Monthly on the 1st |
| Utilities | Yes | $150 | Monthly on the 1st |
| Phone | Yes | $50 | Monthly on the 8th |
| Internet | Yes | $100 | Monthly on the 15th |
| Gas | Yes | $60 | Weekly on Friday |
| Groceries | Yes | $100 | Weekly on Saturday |
| Fun | Yes | $50 | Weekly on Friday |
| Subscriptions | Yes | $50 | Monthly on the 5th |

### 1.5 Description Text ✅ DONE
- [x] Display text: "We've prepared common budget categories with typical amounts to help you get started. Feel free to adjust, add, or remove any categories to match your needs."

---

## Phase 2: Backend - Static Categories ✅ MOSTLY COMPLETE

### 2.1 Backend Endpoint (app.py) ✅ DONE (Simplified Jan 4, 2026)
- [x] `/quiltt/analyze-transactions-for-categories` endpoint returns static categories
- [x] No Ntropy analysis during setup (data not available yet)
- [x] Returns JSON with category structure matching frontend expectations
- [x] Always returns `fallback: false` (static categories ARE the intended behavior)

### 2.2 Database Migration for Future Ntropy Storage ✅ DONE (Jan 4, 2026)
- [x] Created migration: `add_ntropy_fields_to_transactions.sql`
- [x] Added columns to `quiltt_transactions` table:
  - `ntropy_labels` (JSON)
  - `ntropy_merchant_id` (VARCHAR)
  - `ntropy_logo` (TEXT)
  - `ntropy_website` (VARCHAR)
  - `ntropy_mcc` (JSON)
  - `ntropy_location` (TEXT)
  - `ntropy_location_city`, `ntropy_location_state`, `ntropy_location_country`
  - `ntropy_recurrence` (VARCHAR)
  - `ntropy_recurrence_group_id` (VARCHAR)
  - `ntropy_periodicity` (VARCHAR)
  - `ntropy_periodicity_days` (DECIMAL)
  - `ntropy_avg_amount` (DECIMAL)
  - `ntropy_first_payment_date`, `ntropy_latest_payment_date` (DATE)
  - `ntropy_person` (VARCHAR)
  - `ntropy_transaction_type` (VARCHAR)
  - `ntropy_enriched_at` (DATETIME)
- [x] Deployed to dev server 192.0.2.44
- [x] Deployed to dev server 192.0.2.45
- [ ] Deploy to AWS production (needs RDS password)

### 2.3 Category Creation Endpoint ✅ DONE (Jan 5, 2026)
- [x] Created `/quiltt/create-recommended-categories` POST endpoint
- [x] Input: Array of category objects with:
  - name
  - is_income (boolean)
  - is_recurring (boolean)
  - amount (if recurring)
  - cadence_unit (if recurring)
  - cadence_interval (if recurring)
  - weekdays (if weekly recurring)
  - monthly_days (if monthly recurring)
  - start_date, end_date, no_end_date, occurrences_count
- [x] Create categories in Redis first (following Redis-first architecture)
- [x] For recurring categories:
  - [x] Create in income_categories or expense_categories (is_recurring=1)
  - [x] Create recurring_income or recurring_expense record
  - [x] Set start_date to today
  - [x] Set no_end_date=1 for ongoing recurring
  - [x] Generate initial bucket entries using existing `generate_income_entries` / `generate_expense_entries`

### 2.4 Frontend Integration ✅ DONE (Jan 5, 2026)
- [x] Submit button stores categories in `window.pendingCategoriesToCreate`
- [x] Submit button proceeds to name modal (via `fetchBankBalancesForSelectedAccounts`)
- [x] After threshold form submission, `createPendingCategoriesAndFinish()` is called
- [x] Categories are created via `/quiltt/create-recommended-categories` endpoint
- [x] Errors are logged but don't block profile completion
- [x] Flow continues to `saveDailyTotalsAndRemainders()` then `loadDashboard()`

---

## Phase 3: Ntropy Webhook Integration (Future Enhancement)

### 3.1 Webhook Handler for Ntropy Data
- [ ] Listen for `connection.synced.successful` webhook event
- [ ] After webhook received, fetch transactions with Ntropy enrichment
- [ ] Store Ntropy data in `quiltt_transactions` table using new columns
- [ ] Use Ntropy data for:
  - Transaction auto-categorization
  - Spending pattern analysis
  - Recurring transaction detection

### 3.2 Transaction Sync with Ntropy Data
- [ ] Update `sync_quiltt_transactions()` to check for Ntropy enrichment
- [ ] Store all Ntropy fields when available
- [ ] Track `ntropy_enriched_at` timestamp
- [ ] Handle missing Ntropy data gracefully (nullable columns)

---

## Phase 4: Testing & Polish

### 4.1 Testing Scenarios
- [ ] Test static categories display correctly
- [ ] Test category editing (name, amount, cadence)
- [ ] Test category deletion
- [ ] Test skip button flow
- [ ] Test category creation backend
- [ ] Test Redis/MySQL data integrity after creation

### 4.2 UX Improvements
- [ ] Smooth transitions between modals
- [ ] Loading indicators during category creation
- [ ] Success/error toast notifications
- [x] "Add Category" button functionality

---

## Current Status

**Phase**: Phase 2 - Backend Category Creation ✅ COMPLETE
**Last Updated**: January 5, 2026
**Next Step**: Testing (Phase 4)

**Progress**: 
- ✅ Phase 1 - UI Development - Complete (User Approved Jan 5, 2026)
- ✅ 1.6 Edit/Add Category Modal - Complete (matches recurring_i.html exactly)
- ✅ 2.1 Backend returns static categories - Complete
- ✅ 2.2 Ntropy database columns added - Complete (pending AWS deploy)
- ✅ 2.3 Category Creation Endpoint - Complete
- ✅ 2.4 Frontend Integration - Complete

### Phase 1.6: Edit/Add Category Modal ✅ DONE (Jan 5, 2026)
- [x] Full edit modal matching recurring_i.html structure
- [x] All cadence options (days, weeks, months, years)
- [x] Weekly day checkboxes
- [x] Monthly day select with "Last Day" option  
- [x] Yearly day/month pickers
- [x] Start date, end date, no-end-date checkbox
- [x] Occurrences-based end date option
- [x] Category name + recurring checkbox horizontal layout
- [x] Responsive CSS for screens under 400px
- [x] Table row heights (thead 20px, tbody 40px, tfoot 20px)
- [x] Scrollable modal content (entire modal scrolls)
- [x] Skip button text changed to "Create Categories Later"

---

## Architecture Notes

### Why Static Categories Instead of Ntropy Analysis?

1. **Timing Issue**: Ntropy enrichment happens asynchronously after bank connection
2. **Webhook Required**: Data only guaranteed after `connection.synced.successful` event
3. **User Experience**: Can't make users wait for enrichment during setup
4. **Solution**: Static categories for setup, Ntropy for ongoing features

### Future Ntropy Uses

- **Auto-categorization**: When user imports a transaction, suggest category based on Ntropy labels
- **Spending insights**: Analyze patterns using Ntropy's recurrence detection
- **Smart suggestions**: "You spend $X at merchant Y every month - add as recurring?"

### Static Categories Rationale

The chosen categories cover the most common budget items:
- **Income**: Most people have primary wages + variable income (gig work, bonuses)
- **Expenses**: Housing, utilities, phone, internet = essential fixed costs
- **Flexible**: Gas, groceries, fun = variable but predictable spending
- **Subscriptions**: Catch-all for streaming, gym, etc.

Users can customize all values during setup to match their actual situation.

---

## Files Modified

- `templates/setup_profile.html` - Updated static categories to match spec
- `app.py` - Simplified endpoint to return static categories
- `migrations/add_ntropy_fields_to_transactions.sql` - New Ntropy columns
- `migrations/redis_keys.sql` - (Future) Document Ntropy-related Redis keys

## Related Documentation

- [Quiltt Ntropy Integration](https://www.quiltt.dev/integrations/enrichment/ntropy)
- [Ntropy Consumer Labels](https://legacy.docs.ntropy.com/docs/enrichment/consumer-labels/)
- [Quiltt Remote Data](https://www.quiltt.dev/api/remote-data)
