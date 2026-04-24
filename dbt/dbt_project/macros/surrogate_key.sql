{# ──────────────────────────────────────────────────────────
   generate_surrogate_key
   Wrapper dbt_utils.generate_surrogate_key dengan nama
   yang lebih pendek dan konsisten di seluruh project.
   Contoh: {{ surrogate_key(['customer_id', 'valid_from']) }}
────────────────────────────────────────────────────────── #}
{% macro surrogate_key(field_list) %}
    {{ dbt_utils.generate_surrogate_key(field_list) }}
{% endmacro %}