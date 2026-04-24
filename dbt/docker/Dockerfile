FROM python:3.10-slim

RUN apt-get update && apt-get install -y git && \
    rm -rf /var/lib/apt/lists/*

COPY docker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY dbt_profiles/ /root/.dbt/

WORKDIR /app
COPY dbt_project/packages.yml /app/packages.yml
COPY dbt_project/dbt_project.yml /app/dbt_project.yml

ENV DBT_PROJECT_DIR=/app
ENV DBT_PROFILES_DIR=/root/.dbt
ENV DBT_PACKAGES_INSTALL_PATH=/dbt_packages

RUN dbt deps

COPY dbt_project/ /app

CMD ["dbt", "--version"]
# docker build
#     ↓
# /dbt_packages/          ← packages di-install di sini (di dalam image)
#     ├── dbt_utils/
#     ├── audit_helper/
#     ├── dbt_expectations/
#     └── dbt_project_evaluator/

# Mount volume /app       ← hanya overwrite project files
#     ├── models/
#     ├── macros/
#     └── dbt_project.yml ← packages-install-path: /dbt_packages

# dbt run                 ← baca packages dari /dbt_packages 