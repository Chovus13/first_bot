FROM python:3.12-slim

WORKDIR /app
RUN mkdir -p /app/logs && \
    mkdir -p /app/html && \
    touch /app/logs/bot.log && \
    touch /app/logs/error.log && \
    touch /app/logs/access.log
COPY . /app


RUN pip install --upgrade pip \
 && pip install -r requirements.txt
COPY ./web/dist /usr/share/nginx/html
# Kreiraj običnog korisnika
#USER nginx
RUN chown -R "$USER":www-data /usr/share/nginx/html && \
    chmod -R 0755 /usr/share/nginx/html

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8024"]
