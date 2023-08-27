# Use an official Python runtime as the base image
FROM python:3.9-slim

# Install necessary system packages
RUN apt-get update && \
    apt-get install -y git libssl-dev cmake libjpeg-dev build-essential && \
    apt-get clean

# Set the working directory in the container
WORKDIR /app

COPY requirements.txt .

# Create a virtual environment and install the required Python packages
RUN python -m venv venv && \
    . venv/bin/activate && \
    pip install --no-cache-dir -r requirements.txt

# Activate the virtual environment for the CMD
CMD . venv/bin/activate && python yadacoin/app.py --config=config/config.json