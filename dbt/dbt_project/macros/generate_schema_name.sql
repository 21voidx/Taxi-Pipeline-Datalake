-- ══════════════════════════════════════════════════════════════
--  macros/generate_schema_name.sql
--
--  Override dbt's default schema-naming logic.
--
--  Rules:
--    prod  target → use custom_schema_name exactly (no prefix)
--    dev   target → prefix with developer's name from target.schema
--    ci    target → prefix with "ci_"
--    all others   → dbt default behaviour (target_schema + custom)
--
--  Example outcomes:
--    prod  + custom="dev_silver"  → "dev_silver"
--    dev   + custom="dev_silver"  → "alice_dev_silver"   (if target.schema=alice)
--    ci    + custom="dev_silver"  → "ci_dev_silver"
-- ══════════════════════════════════════════════════════════════

{% macro generate_schema_name(custom_schema_name, node) -%}

    {%- set default_schema = target.schema -%}

    {%- if target.name == 'prod' -%}

        {#- Production: use the schema exactly as defined in dbt_project.yml -#}
        {{ custom_schema_name | trim if custom_schema_name else default_schema }}

    {%- elif target.name == 'ci' -%}

        {#- CI pipelines: prefix with ci_ to isolate test runs -#}
        {%- if custom_schema_name -%}
            ci_{{ custom_schema_name | trim }}
        {%- else -%}
            ci_{{ default_schema }}
        {%- endif -%}

    {%- elif target.name == 'dev' -%}

        {#- Local dev: prefix with developers target.schema name -#}
        {%- if custom_schema_name -%}
            {{ default_schema }}_{{ custom_schema_name | trim }}
        {%- else -%}
            {{ default_schema }}
        {%- endif -%}

    {%- else -%}

        {#- Fallback: dbt default — target_schema + custom (underscore-joined) -#}
        {%- if custom_schema_name -%}
            {{ default_schema }}_{{ custom_schema_name | trim }}
        {%- else -%}
            {{ default_schema }}
        {%- endif -%}

    {%- endif -%}

{%- endmacro %}
