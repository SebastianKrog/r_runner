FROM rocker/verse:4.5.2

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

RUN R -q -e "install.packages('pak', repos='https://cloud.r-project.org')"

RUN R -q -e "pak::pak(c(\
  'shiny',\
  'easystats',\
  'tidymodels',\
  'survival',\
  'lme4', 'lmerTest', 'nlme', 'mgcv', 'glmmTMB',\
  'lavaan', 'mice', 'survey',\
  'fixest', 'MatchIt',\
  'forecast', 'fable', 'tsibble',\
  'broom', 'modelsummary', 'gt',\
  'emmeans'\
))"

RUN mkdir -p /app/system \
    && R -q -e "writeLines(sort(unique(installed.packages()[, 'Package'])), '/app/system/r-packages.txt')"

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 8000

CMD ["/opt/venv/bin/uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
