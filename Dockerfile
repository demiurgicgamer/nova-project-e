FROM node:20-alpine

WORKDIR /app

# espeak-ng: offline TTS → raw PCM (no internet needed, works in Docker)
# ffmpeg: audio format normalization
RUN apk add --no-cache ffmpeg espeak-ng

# Install dependencies first (better layer caching)
COPY package*.json ./
RUN npm ci --only=production

# Copy source
COPY . .

EXPOSE 3000

# Use nodemon for hot reload in development, node in production
CMD ["sh", "-c", "if [ \"$NODE_ENV\" = 'development' ]; then npx nodemon src/app.js; else node src/app.js; fi"]
