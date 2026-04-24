-- ══════════════════════════════════════════════════════════════
--  tests/generic/assert_phone_canonical.sql
--
--  Generic test: asserts that a phone column contains ONLY
--  canonical Indonesian phone numbers (08XXXXXXXXX format)
--  or NULL. Fails if any non-null value is non-canonical.
--
--  Usage in _models.yml:
--    columns:
--      - name: phone_number_clean
--        tests:
--          - assert_phone_canonical
-- ══════════════════════════════════════════════════════════════

{% test assert_phone_canonical(model, column_name) %}

select
    {{ column_name }}
from {{ model }}
where
    {{ column_name }} is not null
    and not regexp_contains({{ column_name }}, r'^08[0-9]{8,11}$')

{% endtest %}
