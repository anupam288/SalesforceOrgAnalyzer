# Validation Rule — Component Details


---

## `User.NoUsernameChangesAllowed` {#user-nousernamechangesallowed}

**Category:** Validation Rule  
**Business Process:** Supports data integrity by ensuring consistency of usernames. This prevents unauthorized or accidental changes.  
**Trigger / When:** Fires on update only. No record type restrictions; affects all User records.

**Purpose:**
Prevents changes to the Username field for all users. Affects any user attempting to change their username.

**Objects Read:** `User`  
**Objects Written:** —

**UI Context:** Field 'Username' on User layout  


**Hidden Logic:**
> ⚡ Field comparison between current Username and PRIORVALUE(Username)
> ⚡ No cross-object checks or thresholds, but specific to Username field value change

**Risk Flags:**
> ⚠️ Fires on all record types including internal/system ones — may block automation

**Dependencies:** `Username field on User object`


---

## `package` _(confidence: 0%)_ {#package}

**Category:** Validation Rule  
**Business Process:** The specific business process or data quality concern is unknown due to the lack of available validation formula and error message.  
**Trigger / When:** The trigger condition is indeterminate as the validation formula is not available. Typically, validation rules fire on both insert and update, but there could be restrictions not visible here.

**Purpose:**
The validation rule is intended to enforce a certain condition before allowing a record to be saved. However, the exact purpose is unclear as the validation formula and error message are not provided.

**Objects Read:** `Unknown`  
**Objects Written:** —

**UI Context:** Field 'Page' on Unknown layout  


**Hidden Logic:**
> ⚡ Any non-obvious conditions in the formula — thresholds, cross-object checks, date logic
> ⚡ Be specific about field names and values from the formula

**Risk Flags:**
> ⚠️ Fires on all record types including internal/system ones — may block automation
> ⚠️ Complex formula with no comments — hard to maintain
> ⚠️ Cross-object formula reference — may cause SOQL limits at scale

**Dependencies:** `fields and objects referenced in the formula`
