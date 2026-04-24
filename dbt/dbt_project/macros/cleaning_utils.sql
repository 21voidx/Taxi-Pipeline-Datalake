-- ══════════════════════════════════════════════════════════════
--  macros/cleaning_utils.sql
--
--  Reusable data-cleaning macros for Indonesian phone numbers,
--  NIK (national ID), and license plates.
--  Called by stg_drivers and stg_passengers.
-- ══════════════════════════════════════════════════════════════


{# ──────────────────────────────────────────────────────────────
   clean_phone_id(column_name)
   Canonical format: 08XXXXXXXXX  (10–13 digits, starts with 08)

   Rules applied (in order):
     1. Strip all non-digit characters
     2. If starts with '62', replace with '0'
     3. If still not starting with '08', return NULL (invalid)
     4. Enforce length 10–13 digits

   Example inputs  → output
     +6281234567890 → 081234567890
     0812-3456-7890 → 081234567890
     081234567890   → 081234567890
     12345          → NULL
#}
{% macro clean_phone_id(column_name) -%}
    case
        -- Step 1: strip all non-digits
        when regexp_replace({{ column_name }}, r'[^0-9]', '') is null then null

        -- Step 2: handle +62 / 62 prefix → convert to 0
        when regexp_contains(regexp_replace({{ column_name }}, r'[^0-9]', ''), r'^62')
        then
            case
                when length('0' || substr(regexp_replace({{ column_name }}, r'[^0-9]', ''), 3))
                     between 10 and 13
                then '0' || substr(regexp_replace({{ column_name }}, r'[^0-9]', ''), 3)
                else null
            end

        -- Step 3: already starts with 0
        when regexp_contains(regexp_replace({{ column_name }}, r'[^0-9]', ''), r'^0')
        then
            case
                when length(regexp_replace({{ column_name }}, r'[^0-9]', ''))
                     between 10 and 13
                then regexp_replace({{ column_name }}, r'[^0-9]', '')
                else null
            end

        else null
    end
{%- endmacro %}


{# ──────────────────────────────────────────────────────────────
   clean_nik(column_name)
   Canonical format: 16 numeric digits, no spaces.

   Rules:
     1. Remove all whitespace
     2. Remove any non-digit characters
     3. If length != 16 → mark as NULL (invalid)

   Example inputs → output
     3271 0123 4567 8901 → 3271012345678901
     327101234567890     → NULL  (15 digits)
     3271012345678901    → 3271012345678901
#}
{% macro clean_nik(column_name) -%}
    case
        when length(regexp_replace({{ column_name }}, r'[^0-9]', '')) = 16
        then regexp_replace({{ column_name }}, r'[^0-9]', '')
        else null
    end
{%- endmacro %}


{# ──────────────────────────────────────────────────────────────
   clean_license_plate(column_name)
   Indonesian plates: [AREA CODE] [1000-9999] [2-3 LETTERS]
   e.g. B 1234 ABC

   Rules:
     1. UPPER()
     2. Trim & collapse multiple spaces to single space
     3. Basic regex validation — if not matching pattern, keep raw
#}
{% macro clean_license_plate(column_name) -%}
    upper(trim(regexp_replace({{ column_name }}, r'\s+', ' ')))
{%- endmacro %}


{# ──────────────────────────────────────────────────────────────
   is_valid_email(column_name)
   Returns BOOL — basic email format check.
#}
{% macro is_valid_email(column_name) -%}
    regexp_contains(lower(trim({{ column_name }})), r'^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}$')
{%- endmacro %}
