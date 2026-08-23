FROM python:3.11-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the source code directly into the container image
COPY src/ ./src/

# Run the bot from its baked-in location
CMD ["python", "src/main.py"]
