-- ══════════════════════════════════════════════════════════════
--  macros/bq_json_utils.sql
--
--  BigQuery-specific JSON extraction macros.
--  BigQuery stores JSON as STRING (not native JSONB like Postgres),
--  so we use JSON_VALUE / JSON_QUERY for safe extraction.
--
--  Macros:
--    extract_json_bq(col, key)          → scalar string value
--    extract_json_array_bq(col, key)    → JSON array as STRING
--    extract_json_safe_int(col, key)    → SAFE_CAST to INT64
--    extract_json_safe_float(col, key)  → SAFE_CAST to FLOAT64
--    extract_json_safe_bool(col, key)   → SAFE_CAST to BOOL
--    extract_nested_json_bq(col, path)  → nested path like $.a.b
-- ══════════════════════════════════════════════════════════════


{# ──────────────────────────────────────────────────────────────
   extract_json_bq(column_name, key)
   Returns: STRING (scalar leaf value)
   Example: {{ extract_json_bq('event_payload', 'surge') }} → '1.5'
#}
{% macro extract_json_bq(column_name, key) -%}
    JSON_VALUE({{ column_name }}, '$.{{ key }}')
{%- endmacro %}


{# ──────────────────────────────────────────────────────────────
   extract_json_array_bq(column_name, key)
   Returns: STRING (JSON array representation)
   Example: {{ extract_json_array_bq('payload', 'tags') }} → '["a","b"]'
#}
{% macro extract_json_array_bq(column_name, key) -%}
    JSON_QUERY({{ column_name }}, '$.{{ key }}')
{%- endmacro %}


{# ──────────────────────────────────────────────────────────────
   extract_json_safe_int(column_name, key)
   Returns: INT64 (NULL on parse failure)
#}
{% macro extract_json_safe_int(column_name, key) -%}
    SAFE_CAST(JSON_VALUE({{ column_name }}, '$.{{ key }}') AS INT64)
{%- endmacro %}


{# ──────────────────────────────────────────────────────────────
   extract_json_safe_float(column_name, key)
   Returns: FLOAT64 (NULL on parse failure)
#}
{% macro extract_json_safe_float(column_name, key) -%}
    SAFE_CAST(JSON_VALUE({{ column_name }}, '$.{{ key }}') AS FLOAT64)
{%- endmacro %}


{# ──────────────────────────────────────────────────────────────
   extract_json_safe_bool(column_name, key)
   Returns: BOOL (NULL on parse failure)
#}
{% macro extract_json_safe_bool(column_name, key) -%}
    SAFE_CAST(JSON_VALUE({{ column_name }}, '$.{{ key }}') AS BOOL)
{%- endmacro %}


{# ──────────────────────────────────────────────────────────────
   extract_nested_json_bq(column_name, json_path)
   For deeply nested paths — pass full JSONPath string.
   Example: {{ extract_nested_json_bq('payload', '$.metadata.device') }}
#}
{% macro extract_nested_json_bq(column_name, json_path) -%}
    JSON_VALUE({{ column_name }}, '{{ json_path }}')
{%- endmacro %}


{# ──────────────────────────────────────────────────────────────
   parse_gateway_response(column_name)
   Convenience macro: extract all standard fields from the
   gateway_raw_response JSON column produced by the generator.
   Usage: SELECT {{ parse_gateway_response('gateway_raw_response') }}
#}
{% macro parse_gateway_response(column_name) -%}
    JSON_VALUE({{ column_name }}, '$.gateway')                  AS gateway_name,
    JSON_VALUE({{ column_name }}, '$.reference')                AS gateway_reference,
    JSON_VALUE({{ column_name }}, '$.status')                   AS gateway_status,
    SAFE_CAST(JSON_VALUE({{ column_name }}, '$.amount') AS FLOAT64)
                                                                AS gateway_amount,
    JSON_VALUE({{ column_name }}, '$.timestamp')                AS gateway_timestamp,
    JSON_VALUE({{ column_name }}, '$.metadata.ip')              AS client_ip,
    JSON_VALUE({{ column_name }}, '$.metadata.device')          AS client_device
{%- endmacro %}
