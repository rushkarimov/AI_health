FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY requirements.txt .

# torch ставим ОТДЕЛЬНО и только CPU-сборкой. Обычный пакет тянет
# libtorch_cuda.so и прочие библиотеки NVIDIA на несколько гигабайт — на
# сервере без видеокарты это мёртвый груз, из-за которого образ раздувался
# до 8.8 ГБ, а сборка падала с "no space left on device" на 40-гигабайтном
# диске. Ставим первым, чтобы sentence-transformers увидел готовый torch
# и не подтянул CUDA-вариант следом.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY migrations ./migrations

CMD ["python", "-m", "app.bot"]
