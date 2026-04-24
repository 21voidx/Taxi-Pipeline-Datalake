# Dockerfile
FROM apache/airflow:3.0.6-python3.12

# Copy requirements untuk airflow (jika ada)
COPY requirements.txt /tmp/requirements.txt

# Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

# 1. Install requirements untuk Airflow ke system (menggunakan pip bawaan atau uv)
RUN uv pip install --no-cache-dir -r /tmp/requirements.txt

# # 2. Buat Virtual Environment KHUSUS untuk dbt menggunakan uv
# # Kita meletakkan venv dbt ini di folder /opt/airflow/dbt_venv
# RUN ~/.local/bin/uv venv /opt/airflow/dbt_venv

# # 3. Install dbt-core dan dbt-bigquery ke DALAM virtual environment tersebut
# RUN ~/.local/bin/uv pip install \
#     --python /opt/airflow/dbt_venv \
#     --no-cache-dir \
#     dbt-core==1.11.7 \
#     dbt-bigquery==1.11.1
